"""Conservative same-repository Issue matching; remote text never grants authority."""

import re
from difflib import SequenceMatcher
from urllib.parse import urlencode

from .github_accounts import GitHubError
from .store import Conflict


def _title(value):
    text = re.sub(r"\[repowatch\]|^修复[:：]\s*", "", str(value), flags=re.I).casefold()
    return "".join(re.findall(r"[a-z0-9_\u4e00-\u9fff]+", text))[:300]


def _grams(text):
    return {text[index:index + 2] for index in range(len(text) - 1)}


def similar_issue(request, repository, finding):
    """Scan open and closed Issues, requiring symptom overlap plus a shared source path.

    A title match alone is accepted only when it is effectively identical. Ambiguous
    topic-only matches are left separate. Exhausted/failed scans never create an Issue.
    """
    title = _title(finding["title"])
    paths = {entry["path"].casefold() for entry in finding.get("evidence", [])}
    grams = _grams(title)
    matches = []
    for page in range(1, 21):
        query = urlencode({"state": "all", "per_page": 100, "page": page,
                           "sort": "created", "direction": "desc"})
        items = request("GET", f"/repos/{repository}/issues?{query}")
        if not isinstance(items, list):
            raise GitHubError("github_invalid_response", 502)
        for item in items:
            if not isinstance(item, dict) or "pull_request" in item:
                continue
            if (item.get("state") not in ("open", "closed")
                    or type(item.get("number")) is not int or item["number"] < 1
                    or not isinstance(item.get("title"), str)):
                raise GitHubError("github_invalid_response", 502)
            other = _title(item.get("title", ""))
            other_grams = _grams(other)
            overlap = len(grams & other_grams) / max(1, min(len(grams), len(other_grams)))
            ratio = SequenceMatcher(None, title, other, autojunk=False).ratio()
            text = str(item.get("body") or "").casefold()
            shared_paths = sorted(path for path in paths if re.search(
                r"(?<![\w./-])" + re.escape(path) + r"(?![\w./-])", text,
            ))
            exact = len(title) >= 10 and title == other
            if exact or (shared_paths and ratio >= 0.45 and overlap >= 0.55
                         and len(grams & other_grams) >= 8):
                matches.append((exact, ratio + overlap, item["state"] == "open",
                                item, shared_paths))
        if len(items) < 100:
            break
    else:
        raise Conflict("repair_remote_scan_limit")
    if not matches:
        return None
    matches.sort(key=lambda match: (match[0], match[1], match[2], -match[3]["number"]),
                 reverse=True)
    exact, score, _, item, shared_paths = matches[0]
    return {"item": item, "reason": "identical_title" if exact else "source_and_symptom",
            "paths": shared_paths, "score": round(score, 4)}


def issue_comment(request, repository, number, marker, account_id, body):
    """Return lookup/write callbacks for the publisher's persisted POST intent."""
    path = f"/repos/{repository}/issues/{number}/comments"

    def validate(item):
        expected = f"https://github.com/{repository}/issues/{number}#issuecomment-"
        if (not isinstance(item, dict) or type(item.get("id")) is not int
                or item["id"] < 1 or item.get("user", {}).get("id") != account_id
                or item.get("body") != body
                or item.get("html_url") != expected + str(item["id"])):
            raise Conflict("repair_remote_conflict")
        return {"id": item["id"], "url": item["html_url"]}

    def lookup():
        found = []
        for page in range(1, 21):
            items = request("GET", path + "?" + urlencode({"per_page": 100, "page": page}))
            if not isinstance(items, list):
                raise GitHubError("github_invalid_response", 502)
            for item in items:
                if isinstance(item, dict) and marker in str(item.get("body") or ""):
                    found.append(validate(item))
            if len(items) < 100:
                break
        else:
            raise Conflict("repair_remote_scan_limit")
        if len(found) > 1:
            raise Conflict("repair_remote_conflict")
        return found[0] if found else None

    return lookup, lambda: validate(request("POST", path, {"body": body}))
