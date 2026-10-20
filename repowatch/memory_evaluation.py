"""Paired memory/no-memory repository analysis for bounded evaluation."""

import asyncio
import hashlib
import json
import logging
import time

from .diagnosis import redact_values
from .memory import Memory, compact_experience
from .repository_analysis import AnalysisFailure, validate_report
from .store import Conflict, Missing, uid

LOG = logging.getLogger(__name__)
REFERENCE_LIMIT = 6


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS memory_evaluations (
            id TEXT PRIMARY KEY,
            analysis_id TEXT NOT NULL UNIQUE REFERENCES repository_analyses(id),
            repo_id TEXT NOT NULL REFERENCES repositories(id),
            revision TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN
                ('queued','running','completed','failed')),
            baseline TEXT, memory TEXT, summary TEXT, error TEXT,
            version INTEGER NOT NULL DEFAULT 1,
            created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS memory_evaluation_scope
            ON memory_evaluations(repo_id, revision, status);
    """)


def _references(memory, repo_id, revision, context):
    """Build the same bounded memory packet used by repository analysis."""
    with memory.store.connection() as db:
        rows = db.execute(
            "SELECT id,version,title,category,content,evidence,updated FROM memories "
            "WHERE repo_id=? AND revision=? AND status='active' "
            "ORDER BY updated DESC LIMIT 24",
            (repo_id, revision),
        ).fetchall()
        read_stats = Memory.read_stats(db, [row["id"] for row in rows])
    ranked = Memory.rank(rows, context, read_stats)[:REFERENCE_LIMIT]
    references = []
    for row in ranked:
        reference = {
            key: row[key] for key in
            ("id", "version", "title", "category", "content")
        }
        experience = compact_experience(row)
        if experience:
            reference["experience"] = experience
        references.append(redact_values(reference))
    return references


def _findings(report):
    items = report.get("findings", []) if isinstance(report, dict) else []
    result = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        paths = sorted({
            str(evidence.get("path", ""))
            for evidence in item.get("evidence", []) if isinstance(evidence, dict)
            and evidence.get("path")
        })
        fingerprint = hashlib.sha256(json.dumps({
            "title": title, "paths": paths,
        }, sort_keys=True).encode()).hexdigest()
        result.append({"title": title[:160], "paths": paths, "fingerprint": fingerprint})
    return result


def _summary(baseline, memory):
    baseline_findings, memory_findings = _findings(baseline["report"]), _findings(
        memory["report"],
    )
    baseline_paths = {path for item in baseline_findings for path in item["paths"]}
    memory_paths = {path for item in memory_findings for path in item["paths"]}
    baseline_hashes = {item["fingerprint"] for item in baseline_findings}
    memory_hashes = {item["fingerprint"] for item in memory_findings}
    baseline_titles = {item["title"] for item in baseline_findings}
    memory_titles = {item["title"] for item in memory_findings}
    return {
        "version": "paired_ab_v1",
        "finding_counts": {
            "baseline": len(baseline_findings), "memory": len(memory_findings),
        },
        "finding_overlap": sorted(baseline_hashes & memory_hashes),
        "finding_only_baseline": sorted(baseline_hashes - memory_hashes),
        "finding_only_memory": sorted(memory_hashes - baseline_hashes),
        "titles": {
            "baseline": sorted(baseline_titles), "memory": sorted(memory_titles),
        },
        "paths": {
            "overlap": sorted(baseline_paths & memory_paths),
            "only_baseline": sorted(baseline_paths - memory_paths),
            "only_memory": sorted(memory_paths - baseline_paths),
        },
        "model_calls": {
            "baseline": baseline["model_calls"], "memory": memory["model_calls"],
        },
        "elapsed_seconds": {
            "baseline": baseline["elapsed_seconds"], "memory": memory["elapsed_seconds"],
        },
        "memory_entry_count": len(memory["memories"]),
        "memory_ids": [item["id"] for item in memory["memories"]],
    }


class MemoryEvaluations:
    def __init__(self, analyses, engine, memory=None):
        self.analyses, self.engine = analyses, engine
        self.store = analyses.store
        self.memory = memory or Memory(self.store)
        self.closing = asyncio.Event()

    @staticmethod
    def _row(row):
        result = {
            key: row[key] for key in (
                "id", "analysis_id", "repo_id", "revision", "status", "error",
                "version", "created", "updated",
            )
        }
        result["baseline"] = json.loads(row["baseline"]) if row["baseline"] else None
        result["memory"] = json.loads(row["memory"]) if row["memory"] else None
        result["summary"] = json.loads(row["summary"]) if row["summary"] else None
        return result

    def enqueue(self, analysis_id):
        with self.store.connection() as db:
            analysis = db.execute(
                "SELECT * FROM repository_analyses WHERE id=?", (analysis_id,)
            ).fetchone()
            if not analysis:
                raise Missing("Analysis not found.")
            if analysis["status"] != "completed":
                raise Conflict("analysis_not_completed")
            if not self.analyses.current(analysis):
                raise Conflict("analysis_stale")
            context = json.loads(analysis["context"] or "{}")
            if not isinstance(context, dict) or not context.get("files"):
                raise Conflict("analysis_source_snapshot_required")
            if not db.execute(
                "SELECT 1 FROM memories WHERE repo_id=? AND revision=? AND status='active'",
                (analysis["repo_id"], analysis["revision"]),
            ).fetchone():
                raise Conflict("memory_evaluation_memory_required")
            existing = db.execute(
                "SELECT * FROM memory_evaluations WHERE analysis_id=?", (analysis_id,)
            ).fetchone()
            if existing:
                return self._row(existing)
            now = time.time()
            db.execute(
                "INSERT INTO memory_evaluations(id,analysis_id,repo_id,revision,status,"
                "created,updated) VALUES(?,?,?,?,?,?,?)",
                (uid("meval"), analysis_id, analysis["repo_id"], analysis["revision"],
                 "queued", now, now),
            )
            row = db.execute(
                "SELECT * FROM memory_evaluations WHERE analysis_id=?", (analysis_id,)
            ).fetchone()
        return self._row(row)

    def get(self, evaluation_id):
        with self.store.connection() as db:
            row = db.execute(
                "SELECT * FROM memory_evaluations WHERE id=?", (evaluation_id,)
            ).fetchone()
        if not row:
            raise Missing("Memory evaluation not found.")
        return self._row(row)

    def get_by_analysis(self, analysis_id):
        with self.store.connection() as db:
            row = db.execute(
                "SELECT * FROM memory_evaluations WHERE analysis_id=?", (analysis_id,)
            ).fetchone()
        if not row:
            raise Missing("Memory evaluation not found.")
        return self._row(row)

    def _claim(self):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM memory_evaluations WHERE status='queued' "
                "ORDER BY created LIMIT 1"
            ).fetchone()
            if not row:
                return None
            changed = db.execute(
                "UPDATE memory_evaluations SET status='running',version=version+1,"
                "updated=? WHERE id=? AND status='queued'",
                (time.time(), row["id"]),
            ).rowcount
            if not changed:
                return None
            row = dict(row)
            row["version"] = row["version"] + 1
            return dict(row)

    async def _variant(self, context, memories):
        calls = 0

        def event(kind, agent=""):
            nonlocal calls
            if kind == "model_call":
                calls += 1

        variant_context = {
            **context, "memories": memories,
        }
        started = time.monotonic()
        report = redact_values(await self.engine.analyze(variant_context, event))
        validate_report(report, variant_context)
        return {
            "report": report, "memories": memories, "model_calls": calls,
            "elapsed_seconds": round(time.monotonic() - started),
        }

    async def _execute(self, row):
        with self.store.connection() as db:
            analysis = db.execute(
                "SELECT * FROM repository_analyses WHERE id=?", (row["analysis_id"],)
            ).fetchone()
        context = json.loads(analysis["context"] or "{}")
        if not context.get("files"):
            raise Conflict("analysis_source_snapshot_required")
        memories = _references(
            self.memory, row["repo_id"], row["revision"], context,
        )
        baseline = await self._variant(context, [])
        memory = await self._variant(context, memories)
        summary = _summary(baseline, memory)
        with self.store.connection() as db:
            db.execute(
                "UPDATE memory_evaluations SET status='completed',baseline=?,memory=?,"
                "summary=?,error=NULL,version=version+1,updated=? WHERE id=? AND version=?",
                (json.dumps(baseline, ensure_ascii=False),
                 json.dumps(memory, ensure_ascii=False),
                 json.dumps(summary, ensure_ascii=False), time.time(),
                 row["id"], row["version"]),
            )

    async def tick(self):
        row = self._claim()
        if not row:
            return False
        try:
            await self._execute(row)
        except Exception as exc:
            code = str(exc.code) if isinstance(exc, Conflict) else (
                str(exc) if isinstance(exc, AnalysisFailure)
                else "memory_evaluation_failed"
            )
            with self.store.connection() as db:
                db.execute(
                    "UPDATE memory_evaluations SET status='failed',error=?,"
                    "version=version+1,updated=? WHERE id=? AND version=?",
                    (code[:80], time.time(), row["id"], row["version"]),
                )
            LOG.warning("Memory evaluation %s failed: %s", row["id"], code)
        return True

    async def run(self):
        while not self.closing.is_set():
            try:
                await self.tick()
            except Exception:
                LOG.exception("Memory evaluation worker failed")
            await asyncio.sleep(2)
