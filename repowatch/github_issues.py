"""Read-only GitHub Issue intake and issue-bound repository analysis."""

import asyncio
import logging
import time

from .diagnosis import redact
from .github_accounts import GitHubError
from .store import Conflict, Missing, uid

LOG = logging.getLogger(__name__)
SYNC_INTERVAL_SECONDS = 24 * 60 * 60
SYNC_MAX_PAGES = 5


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS github_issues (
            id TEXT PRIMARY KEY,
            repo_id TEXT NOT NULL REFERENCES repositories(id),
            number INTEGER NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            state TEXT NOT NULL,
            author TEXT,
            url TEXT,
            updated_at TEXT,
            fetched_at REAL NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            UNIQUE(repo_id, number)
        );
    """)


class GitHubIssues:
    """Sync open Issues for registered GitHub repositories."""

    def __init__(self, store, github):
        self.store = store
        self.github = github

    @staticmethod
    def _repository(db, repo_id):
        row = db.execute("SELECT * FROM repositories WHERE id=?", (repo_id,)).fetchone()
        if not row:
            raise Missing("Repository not found.")
        if row["kind"] != "github":
            raise Conflict("github_repository_required")
        return row

    @staticmethod
    def _active_account(db):
        row = db.execute(
            "SELECT a.id,a.version FROM github_accounts a "
            "JOIN github_configuration c ON c.active_account_id=a.id "
            "WHERE a.state='verified'"
        ).fetchone()
        if not row:
            raise Conflict("github_active_account_required")
        return row

    def sync(self, repo_id):
        with self.store.connection() as db:
            repository = self._repository(db, repo_id)
            account = self._active_account(db)
        token, _ = self.github.validated_identity(account["id"], account["version"])
        result = []
        for page in range(1, SYNC_MAX_PAGES + 1):
            page_result, _ = self.github.client.request_json(
                "GET",
                f"/repos/{repository['name']}/issues"
                f"?state=open&per_page=100&page={page}",
                token,
            )
            if not isinstance(page_result, list):
                raise GitHubError("github_invalid_response", 502)
            result.extend(page_result)
            if len(page_result) < 100:
                break
        now = time.time()
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._repository(db, repo_id)
            active_account = self._active_account(db)
            if (
                active_account["id"] != account["id"]
                or active_account["version"] != account["version"]
            ):
                raise Conflict("github_active_account_changed")
            issue_numbers = []
            for item in result:
                if not isinstance(item, dict) or item.get("pull_request") is not None:
                    continue
                number, title, state = item.get("number"), item.get("title"), item.get("state")
                if type(number) is not int or not isinstance(title, str) or state != "open":
                    continue
                issue_numbers.append(number)
                user = item.get("user") if isinstance(item.get("user"), dict) else {}
                author = user.get("login") if isinstance(user.get("login"), str) else None
                url = item.get("html_url") if isinstance(item.get("html_url"), str) else None
                updated_at = (
                    item.get("updated_at")
                    if isinstance(item.get("updated_at"), str) else None
                )
                values = (
                    redact(title)[:300],
                    redact(item.get("body") or "")[:10000],
                    state,
                    author,
                    url,
                    updated_at,
                    now,
                )
                existing = db.execute(
                    "SELECT id FROM github_issues WHERE repo_id=? AND number=?",
                    (repo_id, number),
                ).fetchone()
                if existing:
                    db.execute(
                        "UPDATE github_issues SET title=?,body=?,state=?,author=?,url=?,"
                        "updated_at=?,fetched_at=?,version=version+1 WHERE id=?",
                        (*values, existing["id"]),
                    )
                else:
                    db.execute(
                        "INSERT INTO github_issues(id,repo_id,number,title,body,state,author,"
                        "url,updated_at,fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (uid("issue"), repo_id, number, *values),
                    )
            if issue_numbers:
                placeholders = ",".join("?" for _ in issue_numbers)
                db.execute(
                    "UPDATE github_issues SET state='closed',version=version+1 "
                    f"WHERE repo_id=? AND state='open' AND number NOT IN ({placeholders})",
                    (repo_id, *issue_numbers),
                )
            else:
                db.execute(
                    "UPDATE github_issues SET state='closed',version=version+1 "
                    "WHERE repo_id=? AND state='open'",
                    (repo_id,),
                )
        return self.list(repo_id)

    def list(self, repo_id):
        with self.store.connection() as db:
            self._repository(db, repo_id)
            rows = db.execute(
                "SELECT * FROM github_issues WHERE repo_id=? ORDER BY number", (repo_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def get(self, issue_id):
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM github_issues WHERE id=?", (issue_id,)).fetchone()
        if not row:
            raise Missing("Issue not found.")
        return dict(row)

    def sync_all(self):
        with self.store.connection() as db:
            repo_ids = [
                row["id"] for row in db.execute("SELECT id FROM repositories WHERE kind='github'")
            ]
        for repo_id in repo_ids:
            try:
                self.sync(repo_id)
            except (Conflict, Missing, GitHubError) as exc:
                LOG.warning("GitHub Issue sync failed for %s: %s", repo_id, exc)

    async def run(self):
        while True:
            try:
                self.sync_all()
            except Exception:
                from .system_faults import record_worker_failure

                record_worker_failure(self.store, "github_issue_worker")
                LOG.exception("GitHub Issue scheduler failed")
            await asyncio.sleep(SYNC_INTERVAL_SECONDS)
