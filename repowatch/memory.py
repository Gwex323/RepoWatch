"""Repository-scoped reference data, never an authorization source."""

import json
import re
import time

from .diagnosis import redact_values
from .store import Conflict, Missing, uid

KNOWLEDGE_CATEGORIES = ("architecture", "setup", "testing")
CATEGORIES = (*KNOWLEDGE_CATEGORIES, "bug", "fix", "investigation")
METRIC_VERSION = "structured_effect_v1"
AGING_DEFAULT_DAYS = 90


def _keywords(value):
    """Extract bounded literal tokens, including Chinese bigrams."""
    text = str(value or "").lower()
    tokens = set(re.findall(r"[a-z0-9_./-]{2,}", text))
    for sequence in re.findall(r"[\u4e00-\u9fff]+", text):
        tokens.update(sequence[index:index + 2] for index in range(len(sequence) - 1))
    return tokens


def _row_data(row):
    try:
        return dict(row)
    except TypeError:
        return vars(row)


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            repo_id TEXT NOT NULL REFERENCES repositories(id),
            category TEXT NOT NULL CHECK(category IN
                ('architecture','setup','testing','bug','fix','investigation')),
            title TEXT NOT NULL, content TEXT NOT NULL, revision TEXT NOT NULL,
            source TEXT NOT NULL CHECK(source IN ('owner','workflow')),
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
    """)


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
           source_key, evidence=None):
    memory_id, now = uid("mem"), time.time()
    cursor = db.execute(
        "INSERT OR IGNORE INTO memories "
        "(id,repo_id,category,title,content,revision,source,source_key,"
        "evidence,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (memory_id, repo_id, category, title, content, revision, source, source_key,
         json.dumps(evidence or {}), now, now),
    )
    if cursor.rowcount:
        audit(db, memory_id, "created")
    return db.execute(
        "SELECT * FROM memories WHERE repo_id=? AND source_key=?", (repo_id, source_key)
    ).fetchone()


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
    verification_codes = {
        str(key)[:80]: value.get("exit_code") if isinstance(value, dict) else None
        for key, value in list(verification.items())[:8] if isinstance(key, str)
    }
    experience = {
        "problem": {
            "title": str(data.get("title", ""))[:160],
            "summary": str(data.get("explanation", ""))[:400],
        },
        "change": {
            "summary": str(patch.get("summary", ""))[:600],
            "paths": paths,
            "regression_paths": regression,
            "regression_command": str(patch.get("regression_command", ""))[:300],
        },
        "verification": {"passed": True, "checks": verification_codes},
        "review": {
            "approved": True,
            "summary": str(review.get("summary", ""))[:400],
            "risks": [str(item)[:240] for item in review.get("risks", [])[:3]],
        },
        "resolution": {"published": True, "merged": True, "pr": pr_url},
    }
    experience = redact_values(experience)
    change = experience["change"]
    content = (
        f"问题：{experience['problem']['title']}。{experience['problem']['summary']}\n"
        f"修改：{change['summary']}\n"
        f"涉及文件：{'、'.join(change['paths'])[:600]}\n"
        f"回归测试：{'、'.join(change['regression_paths'])[:300]}\n"
        "验证：原版本测试、新增回归测试与完整测试在隔离容器中通过。\n"
        f"审查：{experience['review']['summary']}\n"
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
            "verification": verification_codes,
            "experience": experience,
            "review_risks": [str(item)[:240] for item in review.get("risks", [])[:3]],
        },
    )


def compact_experience(row):
    """Return bounded structured fields for model context, not raw evidence."""
    try:
        experience = json.loads(row["evidence"]).get("experience")
    except (ValueError, TypeError):
        return None
    if not isinstance(experience, dict):
        return None
    return redact_values({
        "problem": {
            "title": str(experience.get("problem", {}).get("title", ""))[:160],
            "summary": str(experience.get("problem", {}).get("summary", ""))[:400],
        },
        "change": {
            "summary": str(experience.get("change", {}).get("summary", ""))[:600],
            "paths": experience.get("change", {}).get("paths", [])[:5],
            "regression_paths": experience.get("change", {}).get("regression_paths", [])[:3],
        },
        "review": {"summary": str(experience.get("review", {}).get("summary", ""))[:400]},
        "resolution": {
            "merged": True,
            "pr": str(experience.get("resolution", {}).get("pr", ""))[:500],
        },
    })


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

    @staticmethod
    def read_stats(db, memory_ids):
        if not memory_ids:
            return {}
        marks = ",".join("?" for _ in memory_ids)
        rows = db.execute(
            "SELECT json_extract(CASE WHEN m.type='object' THEN m.value ELSE '{}' END,"
            "'$.id') AS memory_id,count(DISTINCT a.id) AS read_count,"
            "max(a.created) AS last_read FROM repository_analyses a,"
            "json_each(CASE WHEN json_valid(a.context) THEN a.context ELSE '{}' END,"
            "'$.memories') m WHERE memory_id IN (" + marks + ") GROUP BY memory_id",
            memory_ids,
        ).fetchall()
        return {row["memory_id"]: {
            "read_count": row["read_count"], "last_read": row["last_read"],
        } for row in rows}

    @staticmethod
    def rank(rows, context=None, read_stats=None):
        """Rank memories by bounded literal relevance, reads and recency."""
        context = context if isinstance(context, dict) else {}
        read_stats = read_stats or {}
        source_paths = set(context.get("files", {}).keys())
        source_names = {path.rsplit("/", 1)[-1] for path in source_paths}
        issue = context.get("issue") if isinstance(context.get("issue"), dict) else {}
        issue_text = " ".join(
            str(issue.get(key, "")) for key in ("title", "body", "number")
        )
        issue_tokens = _keywords(issue_text)

        def score(row):
            data = _row_data(row)
            value = 0.0
            experience = compact_experience(row)
            paths = set()
            if experience:
                change = experience.get("change", {})
                paths.update(change.get("paths", []))
                paths.update(change.get("regression_paths", []))
            if paths & source_paths:
                value += 4
            elif any(path.rsplit("/", 1)[-1] in source_names for path in paths):
                value += 2

            memory_text = " ".join((data["title"], data["content"]))
            if experience:
                problem = experience.get("problem", {})
                memory_text += " " + " ".join(
                    (problem.get("title", ""), problem.get("summary", ""),
                     experience.get("change", {}).get("summary", ""))
                )
            if issue_tokens & _keywords(memory_text):
                value += 3

            stats = read_stats.get(data["id"], {})
            read_count = stats.get("read_count", 0)
            last_read = stats.get("last_read")
            value += min(read_count, 10) * 0.1
            now = time.time()
            if last_read:
                age = (now - last_read) / 86400
                if age <= 30:
                    value += 1
                elif age <= 90:
                    value += 0.5
            updated_age = (now - data["updated"]) / 86400
            if updated_age <= 30:
                value += 0.5
            elif updated_age <= 90:
                value += 0.25
            return value

        return sorted(rows, key=lambda row: (
            score(row), _row_data(row)["updated"], _row_data(row)["id"],
        ),
                      reverse=True)

    def metrics(self, repo_id, *, revision=""):
        """Return bounded outcome counts for memory-assisted and baseline runs."""
        buckets = {
            key: {name: 0 for name in (
                "analyses", "completed_analyses", "failed_analyses", "memory_entries",
                "findings", "confirmed_findings", "repair_candidates", "resolved_findings",
                "merged_resolutions", "repairs", "published_repairs", "failed_repairs",
            )} for key in ("with_memory", "without_memory")
        }
        with self.store.connection() as db:
            self.require_repo(db, repo_id)
            clauses, params = ["repo_id=?"], [repo_id]
            if revision:
                clauses.append("revision=?")
                params.append(revision)
            analyses = db.execute(
                "SELECT * FROM repository_analyses WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created DESC LIMIT 500",
                params,
            ).fetchall()
            repairs = {
                row["finding_id"]: row for row in db.execute(
                    "SELECT * FROM repair_tasks WHERE repo_id=?", (repo_id,)
                )
            }
            for analysis in analyses:
                if analysis["status"] in ("queued", "waiting_model"):
                    continue
                try:
                    context = json.loads(analysis["context"] or "{}")
                except ValueError:
                    context = {}
                memories = context.get("memories") if isinstance(context, dict) else None
                if not isinstance(memories, list):
                    memories = None
                bucket = "with_memory" if memories else "without_memory"
                metrics = buckets[bucket]
                metrics["analyses"] += 1
                metrics["completed_analyses"] += int(analysis["status"] == "completed")
                metrics["failed_analyses"] += int(
                    analysis["status"] in ("failed", "interrupted")
                )
                if memories:
                    metrics["memory_entries"] += len(memories)
                findings = db.execute(
                    "SELECT * FROM analysis_findings WHERE analysis_id=?",
                    (analysis["id"],),
                ).fetchall()
                for finding in findings:
                    try:
                        feedback = json.loads(finding["feedback"] or "{}")
                    except ValueError:
                        feedback = {}
                    if not isinstance(feedback, dict):
                        feedback = {}
                    metrics["findings"] += 1
                    metrics["repair_candidates"] += int(finding["status"] == "repair_candidate")
                    metrics["confirmed_findings"] += int(
                        finding["status"] == "confirmed"
                        and feedback.get("actor") == "owner"
                        and feedback.get("decision") == "confirmed"
                        and feedback.get("revision") == analysis["revision"]
                    )
                    metrics["resolved_findings"] += int(finding["status"] == "resolved")
                    metrics["merged_resolutions"] += int(
                        feedback.get("resolution") == "merged_pr"
                    )
                    repair = repairs.get(finding["id"])
                    if repair and repair["revision"] == analysis["revision"]:
                        metrics["repairs"] += 1
                        metrics["published_repairs"] += int(repair["status"] == "pr_created")
                        metrics["failed_repairs"] += int(
                            repair["status"] in ("failed", "interrupted")
                        )
        return {
            "repo_id": repo_id, "revision": revision or None,
            "metric_version": METRIC_VERSION, **buckets,
        }

    def aging(self, repo_id, *, revision="", days=AGING_DEFAULT_DAYS, limit=50):
        """Return review candidates; this never archives or deletes memory."""
        with self.store.connection() as db:
            self.require_repo(db, repo_id)
            clauses, params = ["m.repo_id=?", "m.status='active'"], [repo_id]
            if revision:
                clauses.append("m.revision=?")
                params.append(revision)
            rows = db.execute(
                "SELECT m.* FROM memories m WHERE " + " AND ".join(clauses), params,
            ).fetchall()
            stats = self.read_stats(db, [row["id"] for row in rows])
        rows = [{**dict(row), "read_count": stats.get(row["id"], {}).get("read_count", 0),
                 "last_read": stats.get(row["id"], {}).get("last_read")} for row in rows]
        rows.sort(key=lambda row: (
            row["last_read"] is not None, row["last_read"] or row["updated"], row["updated"],
        ))
        rows = rows[:max(1, min(limit, 100))]
        now = time.time()
        items = []
        for row in rows:
            last_read = row["last_read"]
            days_since_updated = max(0, int((now - row["updated"]) / 86400))
            days_since_last_read = (
                max(0, int((now - last_read) / 86400)) if last_read else None
            )
            items.append({
                "id": row["id"], "title": row["title"], "category": row["category"],
                "revision": row["revision"], "source": row["source"],
                "status": row["status"], "version": row["version"],
                "updated": row["updated"], "last_read": last_read,
                "read_count": row["read_count"],
                "days_since_updated": days_since_updated,
                "days_since_last_read": days_since_last_read,
                "stale": (
                    days_since_updated > days
                    and (days_since_last_read is None or days_since_last_read > days)
                ),
            })
        return {"items": items, "total": len(items), "threshold_days": days}
