from alembic import op

revision = "0006_recovery_holds"
down_revision = "0005_control_and_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE recovery_holds (
        run_id TEXT PRIMARY KEY REFERENCES runs(id),
        owner_task_id TEXT NOT NULL REFERENCES budget_ledgers(owner_task_id),
        amounts_json TEXT NOT NULL,
        evidence_json TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('held','reconciled'))
    )""")


def downgrade() -> None:
    raise RuntimeError("未知用量占用不自动降级")
