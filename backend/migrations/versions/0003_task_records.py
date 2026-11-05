"""主任务、运行输入快照、信号领取与人工操作修订。"""

from alembic import op

revision = "0003_task_records"
down_revision = "0002_business_store"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for definition in (
        "revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0)",
        "target_sha TEXT",
        "scan_ready_sha TEXT",
    ):
        op.execute(f"ALTER TABLE repositories ADD COLUMN {definition}")
    for definition in (
        "revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0)",
        "wait_reason TEXT",
        "outcome TEXT",
        "selected INTEGER NOT NULL DEFAULT 0 CHECK(selected IN (0,1))",
        "input_json TEXT NOT NULL DEFAULT '{}'",
        "cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1))",
        "stop_requested INTEGER NOT NULL DEFAULT 0 CHECK(stop_requested IN (0,1))",
        "queued_at REAL NOT NULL DEFAULT 0",
        "ready_since REAL",
        "ready_seconds REAL NOT NULL DEFAULT 0 CHECK(ready_seconds >= 0)",
        "manual_priority INTEGER",
        "severity INTEGER NOT NULL DEFAULT 0 CHECK(severity BETWEEN 0 AND 4)",
        "budget_owner_task_id TEXT REFERENCES tasks(id)",
        "pending_transition_json TEXT",
    ):
        op.execute(f"ALTER TABLE tasks ADD COLUMN {definition}")
    op.execute("UPDATE tasks SET budget_owner_task_id=id")
    op.execute("ALTER TABLE budget_ledgers ADD COLUMN revoked INTEGER NOT NULL DEFAULT 0")
    op.execute("""CREATE TABLE task_sources (
        repository_id TEXT NOT NULL REFERENCES repositories(id),
        source_key TEXT NOT NULL,
        task_id TEXT NOT NULL REFERENCES tasks(id),
        PRIMARY KEY(repository_id,source_key)
    )""")
    op.execute("INSERT INTO task_sources SELECT repository_id,source_key,id FROM tasks")
    for definition in (
        "input_revision INTEGER NOT NULL DEFAULT 1",
        "feedback_revision INTEGER NOT NULL DEFAULT 1",
        "admission_revision INTEGER NOT NULL DEFAULT 1",
        "graph_thread_id TEXT",
        "started_at REAL",
        "ended_at REAL",
    ):
        op.execute(f"ALTER TABLE runs ADD COLUMN {definition}")
    op.execute("ALTER TABLE pending_signals ADD COLUMN payload_json TEXT NOT NULL DEFAULT '{}'")
    op.execute("ALTER TABLE pending_signals ADD COLUMN kind TEXT NOT NULL DEFAULT 'input'")
    op.execute("""CREATE TABLE run_signals (
        run_id TEXT NOT NULL REFERENCES runs(id),
        signal_id TEXT NOT NULL REFERENCES pending_signals(id),
        PRIMARY KEY(run_id,signal_id)
    )""")
    op.execute("""CREATE TABLE task_input_versions (
        task_id TEXT NOT NULL REFERENCES tasks(id),
        input_revision INTEGER NOT NULL,
        feedback_revision INTEGER NOT NULL,
        input_json TEXT NOT NULL,
        PRIMARY KEY(task_id,input_revision,feedback_revision)
    )""")
    op.execute("""INSERT INTO task_input_versions
        SELECT id,input_revision,feedback_revision,input_json FROM tasks""")
    op.execute("""CREATE TABLE triage_decisions_v2 (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(id),
        run_id TEXT REFERENCES runs(id),
        input_revision INTEGER NOT NULL,
        feedback_revision INTEGER NOT NULL,
        admission_revision INTEGER NOT NULL,
        result TEXT NOT NULL CHECK(result IN ('auto_repair','needs_information','needs_approval',
            'excluded','external_repair','blocked')),
        reason_code TEXT NOT NULL,
        evidence_json TEXT NOT NULL,
        UNIQUE(task_id,input_revision,feedback_revision,admission_revision)
    )""")
    op.execute("""INSERT INTO triage_decisions_v2
        (id,task_id,input_revision,feedback_revision,admission_revision,result,reason_code,evidence_json)
        SELECT id,task_id,input_revision,1,admission_revision,result,'p0_draft',evidence_json
        FROM triage_decisions""")
    op.execute("DROP TABLE triage_decisions")
    op.execute("ALTER TABLE triage_decisions_v2 RENAME TO triage_decisions")
    op.execute("""CREATE TABLE block_reasons (
        id TEXT PRIMARY KEY,
        task_id TEXT REFERENCES tasks(id),
        code TEXT NOT NULL,
        details_json TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1,
        resolved INTEGER NOT NULL DEFAULT 0 CHECK(resolved IN (0,1))
    )""")
    op.execute("""CREATE TABLE command_states (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(id),
        generation INTEGER NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('intent','running','unknown','stopped','isolated')),
        identity_json TEXT NOT NULL,
        evidence_json TEXT
    )""")
    op.execute("""CREATE TRIGGER managed_pr_no_delete BEFORE DELETE ON managed_pull_requests
        BEGIN SELECT RAISE(ABORT,'managed PR binding is permanent'); END""")
    op.execute("""CREATE TRIGGER managed_pr_identity BEFORE UPDATE ON managed_pull_requests
        WHEN NEW.task_id != OLD.task_id OR NEW.issue_repository_id != OLD.issue_repository_id
        OR NEW.issue_id != OLD.issue_id OR NEW.pr_repository_id != OLD.pr_repository_id
        OR NEW.pr_id != OLD.pr_id
        BEGIN SELECT RAISE(ABORT,'managed PR identity is immutable'); END""")
    op.execute("""CREATE TRIGGER run_snapshot_immutable BEFORE UPDATE ON runs
        WHEN NEW.task_id != OLD.task_id OR NEW.generation != OLD.generation
        OR NEW.budget_owner_task_id != OLD.budget_owner_task_id
        OR NEW.snapshot_json != OLD.snapshot_json OR NEW.input_revision != OLD.input_revision
        OR NEW.feedback_revision != OLD.feedback_revision
        OR NEW.admission_revision != OLD.admission_revision
        OR NEW.graph_thread_id IS NOT OLD.graph_thread_id
        BEGIN SELECT RAISE(ABORT,'run snapshot is immutable'); END""")


def downgrade() -> None:
    raise RuntimeError("不自动降级业务数据；升级前须停写并备份")
