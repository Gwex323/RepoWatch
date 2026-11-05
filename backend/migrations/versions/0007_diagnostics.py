"""独立于 GitHub 仓库的显式诊断及有限授权。"""

import sqlalchemy as sa
from alembic import op

revision = "0007_diagnostics"
down_revision = "0006_recovery_holds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite batch reflection omits unnamed CHECKs; retain every existing contract.
    checks = (
        "state IN ('queued','running','waiting','blocked','ended','cancelled')",
        "input_revision > 0",
        "feedback_revision > 0",
        "revision > 0",
        "user_excluded IN (0,1)",
        "selected IN (0,1)",
        "cancel_requested IN (0,1)",
        "stop_requested IN (0,1)",
        "ready_seconds >= 0",
        "severity BETWEEN 0 AND 4",
        "acceptance_revision > 0",
        "repository_id IS NOT NULL OR (mode = 'diagnose' AND issue_id IS NULL)",
    )
    with op.batch_alter_table(
        "tasks", table_args=tuple(sa.CheckConstraint(check) for check in checks)
    ) as batch:
        batch.alter_column("repository_id", existing_type=sa.Text(), nullable=True)
    op.execute("""CREATE UNIQUE INDEX global_task_source ON tasks(source_key)
        WHERE repository_id IS NULL""")
    op.execute("""CREATE TRIGGER task_budget_owner_immutable BEFORE UPDATE ON tasks
        WHEN OLD.budget_owner_task_id IS NOT NULL
        AND NEW.budget_owner_task_id IS NOT OLD.budget_owner_task_id
        BEGIN SELECT RAISE(ABORT,'budget owner is immutable'); END""")
    op.execute("""CREATE TABLE diagnostic_authorizations (
        task_id TEXT PRIMARY KEY REFERENCES tasks(id),
        operation_id TEXT NOT NULL UNIQUE REFERENCES user_operations(id),
        policy_revision INTEGER NOT NULL CHECK(policy_revision > 0),
        kind TEXT NOT NULL CHECK(kind IN
            ('storage_probe','provider_connection','provider_capability')),
        parameters_json TEXT NOT NULL,
        deadline REAL NOT NULL,
        run_id TEXT UNIQUE REFERENCES runs(id),
        state TEXT NOT NULL CHECK(state IN ('queued','running','completed','failed')),
        result_json TEXT
    )""")
    op.execute("""CREATE TRIGGER diagnostic_scope_immutable
        BEFORE UPDATE ON diagnostic_authorizations
        WHEN NEW.task_id != OLD.task_id OR NEW.operation_id != OLD.operation_id
        OR NEW.policy_revision != OLD.policy_revision OR NEW.kind != OLD.kind
        OR NEW.parameters_json != OLD.parameters_json OR NEW.deadline != OLD.deadline
        OR (OLD.run_id IS NOT NULL AND NEW.run_id IS NOT OLD.run_id)
        OR (OLD.state IN ('completed','failed') AND
            (NEW.state != OLD.state OR NEW.result_json IS NOT OLD.result_json))
        BEGIN SELECT RAISE(ABORT,'diagnostic scope is immutable'); END""")


def downgrade() -> None:
    raise RuntimeError("诊断授权和运行历史不自动降级")
