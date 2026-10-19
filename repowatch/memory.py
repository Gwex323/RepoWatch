"""Repository-scoped reference data, never an authorization source."""

import json
import time
from itertools import zip_longest

from . import fixture
from .store import Conflict, Missing, reproduction_evidence, uid

KNOWLEDGE_CATEGORIES = ("architecture", "setup", "testing")
CATEGORIES = (*KNOWLEDGE_CATEGORIES, "bug", "fix", "investigation")
ROLE_CATEGORIES = {
    "repository_analyst": ("architecture", "setup", "testing", "bug"),
    "problem_investigator": ("bug", "investigation", "testing", "architecture"),
    "reproduction_assistant": ("setup", "testing", "bug", "investigation"),
    "repair_agent": ("fix", "bug", "architecture", "testing"),
    "independent_reviewer": ("testing", "bug", "fix", "investigation"),
    "pr_author": ("bug", "fix", "testing"),
}
CONTEXT_LIMIT = 6000
ENTRY_LIMIT = 6


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            repo_id TEXT NOT NULL REFERENCES repositories(id),
            category TEXT NOT NULL CHECK(category IN
                ('architecture','setup','testing','bug','fix','investigation')),
            title TEXT NOT NULL, content TEXT NOT NULL, revision TEXT NOT NULL,
            source TEXT NOT NULL CHECK(source IN ('owner','workflow')),
            source_run_id TEXT REFERENCES runs(id),
            source_key TEXT NOT NULL,
            evidence TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived')),
            version INTEGER NOT NULL DEFAULT 1,
            created REAL NOT NULL, updated REAL NOT NULL,
            UNIQUE(repo_id, source_key)
        );
        CREATE INDEX IF NOT EXISTS memory_scope
            ON memories(repo_id, revision, status, category, updated DESC);
        CREATE TABLE IF NOT EXISTS memory_history (
            memory_id TEXT NOT NULL REFERENCES memories(id),
            version INTEGER NOT NULL, action TEXT NOT NULL,
            record TEXT NOT NULL, created REAL NOT NULL,
            PRIMARY KEY(memory_id, version)
        );
        CREATE TABLE IF NOT EXISTS memory_reads (
            run_id TEXT NOT NULL REFERENCES runs(id), agent TEXT NOT NULL,
            memory_id TEXT NOT NULL, version INTEGER NOT NULL, created REAL NOT NULL,
            PRIMARY KEY(run_id, agent, memory_id, version),
            FOREIGN KEY(memory_id, version) REFERENCES memory_history(memory_id, version)
        );
    """)
    db.execute("BEGIN IMMEDIATE")
    if not db.execute("SELECT 1 FROM settings WHERE key='memory_backfill_v1'").fetchone():
        # Only import the known fixture's current-revision, authoritative evidence.
        runs = db.execute(
            "SELECT runs.id,runs.status FROM runs JOIN repositories ON "
            "repositories.id=runs.repo_id WHERE repositories.kind='fixture' "
            "AND runs.revision=? AND runs.confirmation IS NOT NULL",
            (fixture.REVISION,),
        ).fetchall()
        for run in runs:
            capture_reproduction(db, run["id"])
            if run["status"] == "demo_completed":
                capture_fix(db, run["id"])
        db.execute("INSERT INTO settings VALUES ('memory_backfill_v1','done')")


def decoded(row):
    record = dict(row)
    record["evidence"] = json.loads(record["evidence"])
    record.pop("source_key", None)
    return record


def audit(db, memory_id, action):
    row = db.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
    db.execute(
        "INSERT INTO memory_history VALUES (?,?,?,?,?)",
        (memory_id, row["version"], action, json.dumps(decoded(row)), time.time()),
    )


def insert(db, *, repo_id, category, title, content, revision, source,
           source_key, source_run_id=None, evidence=None):
    memory_id, now = uid("mem"), time.time()
    cursor = db.execute(
        "INSERT OR IGNORE INTO memories "
        "(id,repo_id,category,title,content,revision,source,source_run_id,source_key,"
        "evidence,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (memory_id, repo_id, category, title, content, revision, source, source_run_id,
         source_key, json.dumps(evidence or {}), now, now),
    )
    if cursor.rowcount:
        audit(db, memory_id, "created")
    return db.execute(
        "SELECT * FROM memories WHERE repo_id=? AND source_key=?", (repo_id, source_key)
    ).fetchone()


def capture_reproduction(db, run_id):
    run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if run["source_kind"] == "existing_issue":
        try:
            evidence = reproduction_evidence(run)
        except Conflict:
            return
        insert(
            db, repo_id=run["repo_id"], revision=run["revision"], source="workflow",
            source_run_id=run_id, source_key=f"run:{run_id}:reproduction", category="bug",
            title="Existing Issue sample automatically reproduced",
            content="The workflow reproduced the trusted pagination Issue sample. "
                    "This is automatic fixture evidence, not owner confirmation.",
            evidence={**evidence, "scope": "trusted pagination fixture"},
        )
        return
    confirmation = json.loads(run["confirmation"]) if run["confirmation"] else None
    if not confirmation or confirmation["revision"] != run["revision"]:
        return
    observation = confirmation.get("observation")
    confirmed = confirmation["decision"] == "confirmed"
    if confirmed and (
        not observation or not observation["reproduced"]
        or observation["revision"] != run["revision"]
    ):
        return
    summary = (
        "The owner reproduced the controlled pagination discrepancy at this revision."
        if confirmed else
        "The owner did not confirm this finding. This does not prove the bug is absent."
    )
    if observation:
        summary += " Observation: " + json.dumps({
            key: observation[key] for key in ("input", "expected", "actual", "reproduced")
        })
    insert(
        db, repo_id=run["repo_id"], revision=run["revision"], source="workflow",
        source_run_id=run_id, source_key=f"run:{run_id}:reproduction",
        category="bug" if confirmed else "investigation",
        title="Page-window reproduction " + ("confirmed" if confirmed else "not confirmed"),
        content=summary,
        evidence={"decision": confirmation["decision"], "observation": observation,
                  "scope": "trusted pagination fixture", "actor": confirmation["actor"]},
    )


def capture_fix(db, run_id):
    run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    try:
        evidence = reproduction_evidence(run)
    except Conflict:
        return
    artifacts = {
        row["kind"]: json.loads(row["content"])
        for row in db.execute("SELECT * FROM artifacts WHERE run_id=?", (run_id,))
    }
    verification = artifacts.get("verification", {})
    if (
        not artifacts.get("patch_draft") or verification.get("failed") != 0
        or verification.get("passed", 0) < 1
    ):
        return
    insert(
        db, repo_id=run["repo_id"], revision=run["revision"], source="workflow",
        source_run_id=run_id, source_key=f"run:{run_id}:fix", category="fix",
        title="Controlled pagination patch passed fixture checks",
        content=(
            "Remove the fallback that resets an out-of-range start to the preceding page. "
            "The predefined patch passed local fixture assertions only; "
            "it is not a published or merged repository fix."
        ),
        evidence={"verification": verification, "artifacts": ["patch_draft", "verification"],
                  "reproduction_actor": evidence["actor"], "source_kind": run["source_kind"],
                  "published": False, "merged": False},
    )


def capture_repair_fix(db, repair):
    """Persist a merged real-repository repair as bounded reference data."""
    remote = json.loads(repair["remote"]) if repair["remote"] else None
    patch = json.loads(repair["patch"]) if repair["patch"] else None
    review = json.loads(repair["review"]) if repair["review"] else None
    verification = json.loads(repair["verification"]) if repair["verification"] else None
    finding = db.execute(
        "SELECT data FROM analysis_findings WHERE id=?", (repair["finding_id"],),
    ).fetchone()
    if (repair["status"] != "pr_created" or not isinstance(remote, dict)
            or not isinstance(remote.get("pr"), dict) or not isinstance(patch, dict)
            or not isinstance(review, dict) or review.get("approved") is not True
            or not isinstance(verification, dict) or not finding):
        return
    data = json.loads(finding["data"])
    pr_url = str(remote["pr"].get("url", ""))[:500]
    paths = [str(edit.get("path", ""))[:240] for edit in patch.get("edits", [])[:10]
             if isinstance(edit, dict)]
    regression = [str(item)[:240] for item in patch.get("regression_paths", [])[:5]]
    content = (
        f"问题：{str(data.get('title', ''))[:160]}。{str(data.get('explanation', ''))[:400]}\n"
        f"修改：{str(patch.get('summary', ''))[:600]}\n"
        f"涉及文件：{'、'.join(paths)[:600]}\n"
        f"回归测试：{'、'.join(regression)[:300]}\n"
        "验证：原版本测试、新增回归测试与完整测试在隔离容器中通过。\n"
        f"审查：{str(review.get('summary', ''))[:400]}\n"
        "状态：修复 PR 已由 owner 核验合并。本条为参考经验，不代表当前版本仍适用。"
    )
    insert(
        db, repo_id=repair["repo_id"], revision=repair["revision"], source="workflow",
        source_key=f"repair:{repair['id']}", category="fix",
        title=f"修复经验：{str(data.get('title', ''))[:120]}",
        content=content,
        evidence={
            "repair_id": repair["id"], "finding_id": repair["finding_id"],
            "analysis_id": repair["analysis_id"], "pr": pr_url,
            "merged": True, "published": True,
            "paths": paths, "regression_paths": regression,
            "verification": {
                str(key)[:80]: value.get("exit_code") if isinstance(value, dict) else None
                for key, value in list(verification.items())[:8] if isinstance(key, str)
            },
            "review_risks": [str(item)[:240] for item in review.get("risks", [])[:3]],
        },
    )


class Memory:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def require_repo(db, repo_id):
        if not db.execute("SELECT 1 FROM repositories WHERE id=?", (repo_id,)).fetchone():
            raise Missing("Repository not found.")

    @staticmethod
    def entry(db, repo_id, memory_id):
        row = db.execute(
            "SELECT * FROM memories WHERE repo_id=? AND id=?", (repo_id, memory_id)
        ).fetchone()
        if not row:
            raise Missing("Memory not found in this repository.")
        return row

    def list(self, repo_id, *, revision="", query="", category="", status="active",
             limit=50, offset=0):
        clauses, params = ["repo_id=?", "status=?"], [repo_id, status]
        for field, value in (("revision", revision), ("category", category)):
            if value:
                clauses.append(f"{field}=?")
                params.append(value)
        if query.strip():
            clauses.append("instr(lower(title || ' ' || content), lower(?))>0")
            params.append(query.strip())
        where = " AND ".join(clauses)
        with self.store.connection() as db:
            self.require_repo(db, repo_id)
            total = db.execute(
                f"SELECT count(*) FROM memories WHERE {where}", params
            ).fetchone()[0]
            rows = db.execute(
                f"SELECT * FROM memories WHERE {where} ORDER BY updated DESC,id LIMIT ? OFFSET ?",
                (*params, max(1, min(limit, 100)), max(offset, 0)),
            ).fetchall()
        return {"items": [decoded(row) for row in rows], "total": total}

    @staticmethod
    def validate_note(category, title, content, revision):
        if category not in KNOWLEDGE_CATEGORIES:
            raise Conflict("Owner notes must be architecture, setup or testing knowledge.")
        for value, limit in ((title, 160), (content, 4000), (revision, 128)):
            if not value.strip() or len(value) > limit:
                raise Conflict("Memory fields must be nonempty and within their length limits.")

    def create(self, repo_id, *, category, title, content, revision):
        self.validate_note(category, title, content, revision)
        with self.store.connection() as db:
            self.require_repo(db, repo_id)
            row = insert(
                db, repo_id=repo_id, category=category, title=title.strip(),
                content=content.strip(), revision=revision.strip(), source="owner",
                source_key=uid("note"),
            )
            return decoded(row)

    def update(self, repo_id, memory_id, *, version, category, title, content, revision):
        self.validate_note(category, title, content, revision)
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.entry(db, repo_id, memory_id)
            if row["version"] != version:
                raise Conflict("Memory changed. Refresh before editing.")
            if row["source"] != "owner" or row["status"] != "active":
                raise Conflict("Only active owner notes can be edited.")
            db.execute(
                "UPDATE memories SET category=?,title=?,content=?,revision=?,version=version+1,"
                "updated=? WHERE id=?",
                (category, title.strip(), content.strip(), revision.strip(),
                 time.time(), memory_id),
            )
            audit(db, memory_id, "edited")
            return decoded(self.entry(db, repo_id, memory_id))

    def archive(self, repo_id, memory_id, *, version):
        return self._set_status(repo_id, memory_id, version=version, status="archived")

    def restore(self, repo_id, memory_id, *, version):
        return self._set_status(repo_id, memory_id, version=version, status="active")

    def _set_status(self, repo_id, memory_id, *, version, status):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.entry(db, repo_id, memory_id)
            if row["version"] != version:
                raise Conflict("Memory changed. Refresh before archiving.")
            if row["status"] != status:
                db.execute(
                    "UPDATE memories SET status=?,version=version+1,updated=? WHERE id=?",
                    (status, time.time(), memory_id),
                )
                audit(db, memory_id, "archived" if status == "archived" else "restored")
            return decoded(self.entry(db, repo_id, memory_id))

    def history(self, repo_id, memory_id):
        with self.store.connection() as db:
            self.entry(db, repo_id, memory_id)
            return [
                {**dict(row), "record": json.loads(row["record"])}
                for row in db.execute(
                    "SELECT * FROM memory_history WHERE memory_id=? ORDER BY version DESC",
                    (memory_id,),
                )
            ]

    def context(self, run_id, role):
        categories = ROLE_CATEGORIES[role]
        with self.store.connection() as db:
            # Selection and its audit record use one snapshot, including concurrent archives.
            db.execute("BEGIN IMMEDIATE")
            run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not run:
                raise Missing("Run not found.")
            selected, size = [], 2
            candidates = [
                db.execute(
                    "SELECT * FROM memories WHERE repo_id=? AND revision=? AND status='active' "
                    "AND category=? ORDER BY updated DESC,id LIMIT ?",
                    (run["repo_id"], run["revision"], category, ENTRY_LIMIT),
                ).fetchall()
                for category in categories
            ]
            # Round-robin prevents daily bug records from crowding out setup/test knowledge.
            for group in zip_longest(*candidates):
                for row in group:
                    if row is None:
                        continue
                    item = {
                        "id": row["id"], "version": row["version"], "category": row["category"],
                        "title": row["title"], "content": row["content"], "source": row["source"],
                        "source_run_id": row["source_run_id"], "revision": row["revision"],
                    }
                    encoded = json.dumps(item, ensure_ascii=False)
                    if len(selected) >= ENTRY_LIMIT or size + len(encoded) + 2 > CONTEXT_LIMIT:
                        continue
                    selected.append(item)
                    size += len(encoded) + 2
                    db.execute(
                        "INSERT OR IGNORE INTO memory_reads VALUES (?,?,?,?,?)",
                        (run_id, role, row["id"], row["version"], time.time()),
                    )
        return json.dumps(selected, ensure_ascii=False)

    def reads(self, run_id):
        self.store.run(run_id)
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT r.*, h.record FROM memory_reads r JOIN memory_history h "
                "ON h.memory_id=r.memory_id AND h.version=r.version "
                "WHERE r.run_id=? ORDER BY r.created,r.memory_id",
                (run_id,),
            ).fetchall()
            return [{**dict(row), "record": json.loads(row["record"])} for row in rows]
