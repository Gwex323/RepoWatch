"""将草案接续为正式业务存储，记录业务／checkpoint 交接意图。"""

from alembic import op

revision = "0002_business_store"
down_revision = "0001_core_draft"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE stage_intents (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(id),
        stage_key TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        checkpoint_id TEXT,
        state TEXT NOT NULL CHECK(state IN ('intent','result_saved','checkpoint_saved')),
        context_json TEXT NOT NULL,
        result_json TEXT,
        UNIQUE(run_id,stage_key),
        CHECK(state = 'intent' OR result_json IS NOT NULL),
        CHECK(state != 'checkpoint_saved' OR checkpoint_id IS NOT NULL)
    )""")


def downgrade() -> None:
    raise RuntimeError("不自动降级业务数据；升级前须停写并备份")
