"""Resolve repair provenance from trusted analysis and GitHub intake records."""

import json

from .store import Conflict


def analysis_origin(db, analysis):
    """An Issue snapshot is authority only when it matches this repository's intake."""
    try:
        context = json.loads(analysis["context"] or "{}")
    except (TypeError, ValueError):
        raise Conflict("repair_issue_reference_invalid") from None
    if not isinstance(context, dict):
        raise Conflict("repair_issue_reference_invalid")
    if "issue" not in context:
        return {"source_kind": "discovered", "source_issue": None}
    issue = context["issue"]
    if (not isinstance(issue, dict) or not isinstance(issue.get("id"), str)
            or type(issue.get("number")) is not int or issue["number"] < 1):
        raise Conflict("repair_issue_reference_invalid")
    cached = db.execute("SELECT * FROM github_issues WHERE id=?", (issue["id"],)).fetchone()
    repo = db.execute(
        "SELECT * FROM repositories WHERE id=?", (analysis["repo_id"],),
    ).fetchone()
    if (not cached or not repo or repo["kind"] != "github"
            or cached["repo_id"] != analysis["repo_id"]
            or cached["number"] != issue["number"]
            or issue.get("repo_id", analysis["repo_id"]) != analysis["repo_id"]
            or repo["url"] != "https://github.com/" + repo["name"]):
        raise Conflict("repair_issue_reference_invalid")
    url = f"https://github.com/{repo['name']}/issues/{issue['number']}"
    for value in (issue.get("url"), cached["url"]):
        if not isinstance(value, str) or value.casefold() != url.casefold():
            raise Conflict("repair_issue_reference_invalid")
    return {"source_kind": "existing_issue", "source_issue": {
        "id": issue["id"], "repo_id": analysis["repo_id"],
        "number": issue["number"], "url": url,
    }}


def repair_origin(db, task):
    """Recover legacy tasks, but never replace a conflicting frozen binding."""
    analysis = db.execute(
        "SELECT * FROM repository_analyses WHERE id=?", (task["analysis_id"],),
    ).fetchone()
    if not analysis or any(analysis[key] != task[key] for key in (
        "repo_id", "env_id", "revision",
    )):
        raise Conflict("repair_issue_reference_invalid")
    origin = analysis_origin(db, analysis)
    try:
        context = json.loads(task["context"])
    except (TypeError, ValueError):
        raise Conflict("repair_issue_reference_invalid") from None
    if not isinstance(context, dict):
        raise Conflict("repair_issue_reference_invalid")
    if any(key in context for key in origin) and any(
        key not in context or context[key] != value for key, value in origin.items()
    ):
        raise Conflict("repair_issue_reference_invalid")
    return origin
