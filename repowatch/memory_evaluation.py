"""Paired memory/no-memory repository analysis for bounded evaluation."""

import asyncio
import hashlib
import json
import logging
import time

from .diagnosis import redact_values
from .memory import Memory, compact_experience
from .repository_analysis import (
    ANALYSIS_TIMEOUT_SECONDS,
    AnalysisFailure,
    failure_code,
    validate_report,
)
from .store import Conflict, Missing, uid

LOG = logging.getLogger(__name__)
REFERENCE_LIMIT = 6
RECOVERY_GRACE_SECONDS = ANALYSIS_TIMEOUT_SECONDS + 60
MODEL_IDENTITY_KEYS = ("model", "base_url", "api", "version", "reasoning_effort", "role_models")


def model_identity(engine):
    status = engine.status()
    return tuple(status.get(key) for key in MODEL_IDENTITY_KEYS)


def _model_consistent(baseline, memory):
    first, second = baseline["report"], memory["report"]
    for key in ("model", "model_version", "reasoning_effort", "role_models"):
        if first.get(key) != second.get(key):
            return False
    return True


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
            baseline_error TEXT, memory_error TEXT,
            version INTEGER NOT NULL DEFAULT 1,
            created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS memory_evaluation_scope
            ON memory_evaluations(repo_id, revision, status);
    """)
    columns = {row[1] for row in db.execute("PRAGMA table_info(memory_evaluations)")}
    for column in ("baseline_error", "memory_error"):
        if column not in columns:
            db.execute(f"ALTER TABLE memory_evaluations ADD COLUMN {column} TEXT")


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
    baseline_findings = (
        _findings(baseline["report"]) if baseline and baseline.get("report") else None
    )
    memory_findings = _findings(memory["report"]) if memory and memory.get("report") else None
    complete = baseline_findings is not None and memory_findings is not None
    if not complete:
        return {
            "version": "paired_ab_v2",
            "variants": {
                "baseline": baseline["status"] if baseline else "not_run",
                "memory": memory["status"] if memory else "not_run",
            },
            "errors": {
                "baseline": baseline.get("error_code") if baseline else None,
                "memory": memory.get("error_code") if memory else None,
            },
            "finding_counts": {
                "baseline": len(baseline_findings) if baseline_findings is not None else None,
                "memory": len(memory_findings) if memory_findings is not None else None,
            },
            "model_calls": {
                "baseline": baseline["model_calls"] if baseline else None,
                "memory": memory["model_calls"] if memory else None,
            },
            "elapsed_seconds": {
                "baseline": baseline["elapsed_seconds"] if baseline else None,
                "memory": memory["elapsed_seconds"] if memory else None,
            },
            "memory_entry_count": len(memory["memories"]) if memory else None,
            "memory_ids": [item["id"] for item in memory["memories"]] if memory else [],
            "model_consistent": None,
            "conclusion": "insufficient_evidence",
        }
    baseline_paths = {path for item in baseline_findings for path in item["paths"]}
    memory_paths = {path for item in memory_findings for path in item["paths"]}
    baseline_hashes = {item["fingerprint"] for item in baseline_findings}
    memory_hashes = {item["fingerprint"] for item in memory_findings}
    baseline_titles = {item["title"] for item in baseline_findings}
    memory_titles = {item["title"] for item in memory_findings}
    consistent = _model_consistent(baseline, memory)
    return {
        "version": "paired_ab_v2",
        "variants": {"baseline": "completed", "memory": "completed"},
        "errors": {"baseline": None, "memory": None},
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
        "model_consistent": consistent,
        "conclusion": "requires_human_review" if consistent else "insufficient_evidence",
    }


async def run_variant(engine, context, memories):
    """Run one bounded analysis without granting any repair or publication authority."""
    calls = 0

    def event(kind, agent=""):
        nonlocal calls
        if kind == "model_call":
            calls += 1

    variant_context = {**context, "memories": memories}
    started = time.monotonic()
    try:
        report = redact_values(await engine.analyze(variant_context, event))
        validate_report(report, variant_context)
    except Exception as exc:
        code = failure_code(exc)
        if code == "analysis_failed":
            code = "memory_evaluation_failed"
        return {
            "status": "failed", "error_code": code, "memories": memories,
            "model_calls": calls, "elapsed_seconds": round(time.monotonic() - started),
        }
    return {
        "status": "completed", "report": report, "memories": memories,
        "model_calls": calls, "elapsed_seconds": round(time.monotonic() - started),
    }


class MemoryEvaluations:
    def __init__(self, analyses, engine, memory=None):
        self.analyses, self.engine = analyses, engine
        self.store = analyses.store
        self.memory = memory or Memory(self.store)
        self.closing = asyncio.Event()
        self._recover_interrupted()

    def _recover_interrupted(self):
        stale_before = time.time() - RECOVERY_GRACE_SECONDS
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT id,baseline,memory FROM memory_evaluations "
                "WHERE status='running' AND updated<?", (stale_before,)
            ).fetchall()
            for row in rows:
                baseline = json.loads(row["baseline"]) if row["baseline"] else None
                memory = json.loads(row["memory"]) if row["memory"] else None
                db.execute(
                    "UPDATE memory_evaluations SET status='failed',"
                    "error='memory_evaluation_interrupted',summary=?,version=version+1,"
                    "updated=? WHERE id=? AND status='running' AND updated<?",
                    (json.dumps(_summary(baseline, memory), ensure_ascii=False),
                     time.time(), row["id"], stale_before),
                )

    @staticmethod
    def _row(row):
        result = {
            key: row[key] for key in (
                "id", "analysis_id", "repo_id", "revision", "status", "error",
                "baseline_error", "memory_error",
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
        return await run_variant(self.engine, context, memories)

    def _save_variant(self, row, name, outcome, version):
        error = outcome.get("error_code")
        with self.store.connection() as db:
            changed = db.execute(
                f"UPDATE memory_evaluations SET {name}=?,{name}_error=?,"
                "version=version+1,updated=? WHERE id=? AND status='running' AND version=?",
                (json.dumps(outcome, ensure_ascii=False), error, time.time(),
                 row["id"], version),
            ).rowcount
        if not changed:
            raise Conflict("memory_evaluation_changed")
        return version + 1

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
        identity = model_identity(self.engine)
        version = row["version"]
        baseline = await self._variant(context, [])
        version = self._save_variant(row, "baseline", baseline, version)
        if model_identity(self.engine) == identity:
            memory = await self._variant(context, memories)
        else:
            memory = {
                "status": "failed", "error_code": "analysis_model_changed",
                "memories": memories, "model_calls": 0, "elapsed_seconds": 0,
            }
        version = self._save_variant(row, "memory", memory, version)
        summary = _summary(baseline, memory)
        status = "completed" if all(
            outcome["status"] == "completed" for outcome in (baseline, memory)
        ) and summary["model_consistent"] is True else "failed"
        error = next((
            outcome["error_code"] for outcome in (baseline, memory)
            if outcome["status"] == "failed"
        ), None) or ("analysis_model_changed" if not summary["model_consistent"] else None)
        with self.store.connection() as db:
            changed = db.execute(
                "UPDATE memory_evaluations SET status=?,summary=?,error=?,"
                "version=version+1,updated=? WHERE id=? AND status='running' AND version=?",
                (status, json.dumps(summary, ensure_ascii=False), error, time.time(),
                 row["id"], version),
            ).rowcount
        if not changed:
            raise Conflict("memory_evaluation_changed")

    async def tick(self):
        self._recover_interrupted()
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
                saved = db.execute(
                    "SELECT baseline,memory FROM memory_evaluations WHERE id=?", (row["id"],)
                ).fetchone()
                baseline = json.loads(saved["baseline"]) if saved["baseline"] else None
                memory = json.loads(saved["memory"]) if saved["memory"] else None
                db.execute(
                    "UPDATE memory_evaluations SET status='failed',error=?,summary=?,"
                    "version=version+1,updated=? WHERE id=? AND status='running'",
                    (code[:80], json.dumps(_summary(baseline, memory), ensure_ascii=False),
                     time.time(), row["id"]),
                )
            LOG.warning("Memory evaluation %s failed: %s", row["id"], code)
        return True

    async def run(self):
        while not self.closing.is_set():
            try:
                await self.tick()
            except Exception:
                from .system_faults import record_worker_failure

                record_worker_failure(self.store, "memory_evaluation_worker")
                LOG.exception("Memory evaluation worker failed")
            await asyncio.sleep(2)
