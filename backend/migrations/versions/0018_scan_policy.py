from alembic import op

revision = "0018_scan_policy"
down_revision = "0017_scan_coverage"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE repositories ADD COLUMN scan_policy_json TEXT NOT NULL DEFAULT '{}'")
    op.execute(
        "ALTER TABLE repositories ADD COLUMN scan_ready_batch_id TEXT REFERENCES scan_batches(id)"
    )
    op.execute("""CREATE TABLE scan_splits (
        batch_id TEXT NOT NULL REFERENCES scan_batches(id),
        parent_id TEXT NOT NULL,
        children_json TEXT NOT NULL,
        created_at REAL NOT NULL,
        PRIMARY KEY(batch_id,parent_id)
    )""")


def downgrade():
    raise RuntimeError("扫描策略历史不自动降级")
