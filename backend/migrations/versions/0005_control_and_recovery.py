"""暂停、实际命令身份、有限重试授权及恢复核对记录。"""

from alembic import op

revision = "0005_control_and_recovery"
down_revision = "0004_budget_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE retry_authorizations (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(id),
        reason_id TEXT NOT NULL REFERENCES block_reasons(id),
        plan_json TEXT NOT NULL,
        limits_json TEXT NOT NULL,
        configuration_sha256 TEXT NOT NULL,
        consumed_by_run_id TEXT REFERENCES runs(id)
    )""")
    op.execute("""CREATE TABLE reconciliation_reports (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(id),
        basis_sha256 TEXT NOT NULL,
        facts_json TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('unsafe','review_required','safe_wait')),
        created_at REAL NOT NULL
    )""")
    op.execute("""CREATE TRIGGER command_identity_immutable BEFORE UPDATE ON command_states
        WHEN NEW.run_id != OLD.run_id OR NEW.generation != OLD.generation
        OR (OLD.state != 'intent' AND NEW.identity_json != OLD.identity_json)
        OR (OLD.state IN ('stopped','isolated') AND NEW.state != OLD.state)
        BEGIN SELECT RAISE(ABORT,'command identity or terminal proof is immutable'); END""")


def downgrade() -> None:
    raise RuntimeError("恢复及命令审计不自动降级")
