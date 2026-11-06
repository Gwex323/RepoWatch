"""修复核验、人工关系判断与受控发布的持久记录。"""

from alembic import op

revision = "0020_repair_workflow"
down_revision = "0019_task_acceptance"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE issue_contexts (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(id),
        run_id TEXT NOT NULL REFERENCES runs(id),
        digest TEXT NOT NULL,
        artifact_id TEXT NOT NULL REFERENCES artifacts(id),
        facts_json TEXT NOT NULL,
        created_at REAL NOT NULL,
        UNIQUE(task_id,digest)
    )""")
    op.execute("""CREATE TABLE relation_decisions (
        task_id TEXT NOT NULL REFERENCES tasks(id),
        context_digest TEXT NOT NULL,
        relation_key TEXT NOT NULL,
        decision TEXT NOT NULL CHECK(decision IN ('same','unrelated','occupied','available')),
        evidence_json TEXT NOT NULL,
        actor TEXT NOT NULL,
        PRIMARY KEY(task_id,context_digest,relation_key)
    )""")
    op.execute("""CREATE TABLE repair_sessions (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(id),
        run_id TEXT NOT NULL REFERENCES runs(id),
        workspace_id TEXT NOT NULL REFERENCES workspaces(id),
        base_sha TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('active','frozen','verified','blocked','retired')),
        revision INTEGER NOT NULL DEFAULT 1,
        snapshot_json TEXT NOT NULL,
        frozen_json TEXT,
        evidence_json TEXT,
        created_at REAL NOT NULL
    )""")
    op.execute("""CREATE TABLE publication_intents (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(id),
        repository_id TEXT NOT NULL REFERENCES repositories(id),
        issue_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('issue','pull_request','claim')),
        request_json TEXT NOT NULL,
        request_sha256 TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('intent','dispatched','unknown','confirmed','blocked')),
        result_json TEXT,
        created_at REAL NOT NULL,
        UNIQUE(task_id,kind)
    )""")
    op.execute("""CREATE TABLE publication_revisions (
        intent_id TEXT NOT NULL REFERENCES publication_intents(id),
        revision INTEGER NOT NULL,
        request_json TEXT NOT NULL,
        result_json TEXT NOT NULL,
        PRIMARY KEY(intent_id,revision)
    )""")
    op.execute("""CREATE TABLE candidate_issue_decisions (
        task_id TEXT NOT NULL REFERENCES tasks(id),
        context_digest TEXT NOT NULL,
        issue_number INTEGER,
        decision TEXT NOT NULL CHECK(decision IN ('same','none')),
        evidence_json TEXT NOT NULL,
        PRIMARY KEY(task_id,context_digest)
    )""")
    op.execute("""CREATE TABLE distillation_intents (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(id),
        session_id TEXT NOT NULL REFERENCES repair_sessions(id),
        source_sha256 TEXT NOT NULL,
        feedback_revision INTEGER NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending',
        UNIQUE(task_id,source_sha256,feedback_revision)
    )""")
    for table in ("issue_contexts", "relation_decisions", "distillation_intents"):
        for action in ("UPDATE", "DELETE"):
            op.execute(f"""CREATE TRIGGER {table}_{action.lower()}_immutable
                BEFORE {action} ON {table}
                BEGIN SELECT RAISE(ABORT,'workflow evidence is immutable'); END""")


def downgrade():
    raise RuntimeError("修复与发布证据不自动降级")
