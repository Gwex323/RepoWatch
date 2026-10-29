"""Separate, exact-target authorizations and GitHub-confirmed merge facts."""

import json
import time

from .maintenance_target import REPOSITORY, MaintenanceTarget
from .release_state import Journal
from .store import Conflict


class ReleaseAuthority:
    def __init__(self, store, github):
        self.store, self.github = store, github
        self.target = MaintenanceTarget(github)

    def authorize(self, kind, target, *, resume_original=False):
        if kind not in {"merge", "deploy"} or not target:
            raise Conflict("release_authorization_invalid")
        with self.store.connection() as db:
            account = db.execute(
                "SELECT a.id,a.version FROM github_accounts a JOIN github_configuration c "
                "ON c.active_account_id=a.id WHERE a.state='verified'").fetchone()
        if not account:
            raise Conflict("release_account_required")
        binding = {"account_id": account["id"], "account_version": account["version"]}
        _, _, repository = self.target.authority(**binding)
        binding["repository_id"] = repository["id"]
        with self.store.connection() as db:
            db.execute("INSERT INTO release_authorizations VALUES(?,?,?,?,?,0) "
                       "ON CONFLICT(kind,target) DO UPDATE SET binding=excluded.binding,"
                       "resume_original=excluded.resume_original,created=excluded.created,revoked=0",
                       (kind, target, json.dumps(binding), int(resume_original), time.time()))
        return binding

    def require(self, kind, target, binding=None):
        with self.store.connection() as db:
            row = db.execute(
                "SELECT * FROM release_authorizations WHERE kind=? AND target=? AND revoked=0",
                (kind, target)).fetchone()
        if not row:
            raise Conflict("release_" + kind + "_authorization_required")
        saved = json.loads(row["binding"])
        if binding is not None and saved != binding:
            raise Conflict("release_account_changed")
        self.target.authority(**saved)
        return dict(row)

    def merged(self, pr, commit, binding):
        request, _, _, metadata = self.target.session(binding, lambda: None)
        item = request("GET", f"/repos/{REPOSITORY}/pulls/{pr}")
        if (not item.get("merged_at") or item.get("merged") is not True
                or item.get("merge_commit_sha") != commit
                or item.get("base", {}).get("ref") != metadata["default_branch"]
                or item.get("base", {}).get("repo", {}).get("id") != binding["repository_id"]):
            raise Conflict("release_merged_commit_required")
        comparison = request("GET", f"/repos/{REPOSITORY}/compare/"
                             f"{commit}...{metadata['default_branch']}")
        if comparison.get("status") not in {"ahead", "identical"}:
            raise Conflict("release_default_branch_ancestry_required")
        return {"pr": pr, "commit": commit, "merged_at": item["merged_at"]}

    def merge(self, pr, head, journal: Journal):
        authorization = self.require("merge", head)
        binding = json.loads(authorization["binding"])
        request, token, _, metadata = self.target.session(
            binding, lambda: self.require("merge", head, binding))
        existing = request("GET", f"/repos/{REPOSITORY}/pulls/{pr}")
        if existing.get("head", {}).get("sha") != head:
            raise Conflict("release_pr_head_changed")
        if existing.get("merged_at"):
            return self.merged(pr, existing["merge_commit_sha"], binding)
        # Ask GitHub's rule-aware view, not only the basic REST 'mergeable' bit.
        query = """query($number:Int!){
          repository(owner:"Gwex323",name:"RepoWatch"){
            pullRequest(number:$number){
              headRefOid baseRefName isDraft mergeable mergeStateStatus reviewDecision
              commits(last:1){nodes{commit{oid statusCheckRollup{state}}}}
            }
          }
        }"""
        response, _ = self.github.client.request_json(
            "POST", "/graphql", token, {"query": query, "variables": {"number": pr}})
        value = ((response.get("data") or {}).get("repository") or {}).get("pullRequest") or {}
        commits = value.get("commits", {}).get("nodes", [])
        check = commits[0].get("commit", {}) if len(commits) == 1 else {}
        if (response.get("errors") or value.get("headRefOid") != head
                or value.get("baseRefName") != metadata["default_branch"]
                or value.get("isDraft") is not False
                or value.get("mergeable") != "MERGEABLE"
                or value.get("mergeStateStatus") != "CLEAN"
                or value.get("reviewDecision") != "APPROVED"
                or check.get("oid") != head
                or (check.get("statusCheckRollup") or {}).get("state") != "SUCCESS"):
            raise Conflict("release_protected_merge_not_ready")
        # Save intent before PUT. A lost response is reconciled via GET, never a new push.
        pending = "merge:" + head
        with journal.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS merges(id TEXT PRIMARY KEY,pr INTEGER)")
            if db.execute("SELECT 1 FROM merges WHERE id=?", (pending,)).fetchone():
                raise Conflict("release_merge_receipt_unknown")
            db.execute("INSERT INTO merges VALUES(?,?)", (pending, pr))
        request("PUT", f"/repos/{REPOSITORY}/pulls/{pr}/merge",
                {"sha": head, "merge_method": "squash"})
        actual = request("GET", f"/repos/{REPOSITORY}/pulls/{pr}")
        if not actual.get("merged_at"):
            raise Conflict("release_merge_receipt_unknown")
        receipt = self.merged(pr, actual["merge_commit_sha"], binding)
        journal.event(head, "merged", commit=receipt["commit"], pr=pr)
        return receipt
