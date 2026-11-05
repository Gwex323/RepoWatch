from alembic import op

revision = "0013_runtime_progress"
down_revision = "0012_budget_adjustments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE runs ADD COLUMN active_child_run_id TEXT REFERENCES runs(id)")
    op.execute("DROP TRIGGER run_snapshot_immutable")
    op.execute("""CREATE TRIGGER run_snapshot_immutable BEFORE UPDATE ON runs
        WHEN NEW.task_id != OLD.task_id OR NEW.generation != OLD.generation
        OR NEW.parent_run_id IS NOT OLD.parent_run_id
        OR NEW.budget_owner_task_id != OLD.budget_owner_task_id
        OR NEW.snapshot_json != OLD.snapshot_json OR NEW.input_revision != OLD.input_revision
        OR NEW.feedback_revision != OLD.feedback_revision
        OR NEW.admission_revision != OLD.admission_revision
        OR NEW.graph_thread_id IS NOT OLD.graph_thread_id
        BEGIN SELECT RAISE(ABORT,'run snapshot is immutable'); END""")
    op.execute("""CREATE TABLE runtime_stages (
        id TEXT PRIMARY KEY REFERENCES stage_intents(id),
        run_id TEXT NOT NULL REFERENCES runs(id),
        objective_key TEXT NOT NULL,
        policy_json TEXT NOT NULL,
        steps INTEGER NOT NULL DEFAULT 0 CHECK(steps>=0),
        started_active_ms INTEGER NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('running','completed','waiting','failed'))
    )""")
    op.execute("""CREATE TRIGGER runtime_stage_policy_immutable BEFORE UPDATE ON runtime_stages
        WHEN NEW.id != OLD.id OR NEW.run_id != OLD.run_id OR NEW.objective_key != OLD.objective_key
        OR NEW.policy_json != OLD.policy_json OR NEW.started_active_ms != OLD.started_active_ms
        OR NEW.steps < OLD.steps
        BEGIN SELECT RAISE(ABORT,'runtime stage policy is immutable'); END""")
    op.execute("""CREATE TABLE progress_windows (
        owner_task_id TEXT NOT NULL REFERENCES budget_ledgers(owner_task_id),
        objective_key TEXT NOT NULL,
        window INTEGER NOT NULL DEFAULT 0,
        attempts INTEGER NOT NULL DEFAULT 0,
        consecutive_empty INTEGER NOT NULL DEFAULT 0,
        steps INTEGER NOT NULL DEFAULT 0,
        progress_revision INTEGER NOT NULL DEFAULT 0,
        blocked_code TEXT,
        retry_authorization_id TEXT REFERENCES retry_authorizations(id),
        retry_satisfied INTEGER NOT NULL DEFAULT 0 CHECK(retry_satisfied IN (0,1)),
        started_active_ms INTEGER NOT NULL,
        PRIMARY KEY(owner_task_id,objective_key)
    )""")
    op.execute("""CREATE TABLE progress_consumptions (
        progress_id TEXT PRIMARY KEY REFERENCES budget_progress(id),
        owner_task_id TEXT NOT NULL REFERENCES budget_ledgers(owner_task_id),
        objective_key TEXT NOT NULL,
        attempt_id TEXT NOT NULL REFERENCES progress_attempts(id)
    )""")
    for definition in (
        "window INTEGER NOT NULL DEFAULT 0",
        "recorded_at REAL NOT NULL DEFAULT 0",
        "outcome TEXT NOT NULL DEFAULT 'completed'",
        "ordinal INTEGER NOT NULL DEFAULT 0",
    ):
        op.execute(f"ALTER TABLE progress_attempts ADD COLUMN {definition}")
    op.execute("""CREATE TRIGGER progress_attempt_immutable BEFORE UPDATE ON progress_attempts
        BEGIN SELECT RAISE(ABORT,'progress attempt is immutable'); END""")


def downgrade() -> None:
    raise RuntimeError("运行及无进展历史不自动降级")
