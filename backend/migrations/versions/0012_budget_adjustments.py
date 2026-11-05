from alembic import op

revision = "0012_budget_adjustments"
down_revision = "0011_provider_calls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE budget_adjustments (
        operation_id TEXT PRIMARY KEY REFERENCES user_operations(id),
        owner_task_id TEXT NOT NULL REFERENCES budget_ledgers(owner_task_id),
        basis_sha256 TEXT NOT NULL CHECK(length(basis_sha256)=64),
        increase_json TEXT NOT NULL,
        reason TEXT NOT NULL,
        previous_revision INTEGER NOT NULL,
        resulting_revision INTEGER NOT NULL,
        created_at REAL NOT NULL
    )""")
    for action in ("UPDATE", "DELETE"):
        op.execute(f"""CREATE TRIGGER adjustment_{action.lower()}_forbidden
            BEFORE {action} ON budget_adjustments
            BEGIN SELECT RAISE(ABORT,'budget adjustment is immutable'); END""")


def downgrade() -> None:
    raise RuntimeError("人工预算授权历史不自动降级")
