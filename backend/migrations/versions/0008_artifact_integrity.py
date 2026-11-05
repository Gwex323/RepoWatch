from alembic import op

revision = "0008_artifact_integrity"
down_revision = "0007_diagnostics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE artifact_intents (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(id),
        operation_key TEXT NOT NULL,
        sha256 TEXT NOT NULL CHECK(length(sha256)=64),
        relative_path TEXT NOT NULL UNIQUE,
        context_json TEXT NOT NULL,
        byte_length INTEGER NOT NULL CHECK(byte_length>=0),
        state TEXT NOT NULL CHECK(state IN ('pending','registered','unavailable')),
        UNIQUE(run_id,operation_key)
    )""")
    op.execute("""CREATE TRIGGER artifact_identity_immutable BEFORE UPDATE ON artifacts
        WHEN NEW.id != OLD.id OR NEW.run_id != OLD.run_id OR NEW.sha256 != OLD.sha256
        OR NEW.relative_path != OLD.relative_path OR NEW.context_json != OLD.context_json
        OR NEW.available > OLD.available
        BEGIN SELECT RAISE(ABORT,'artifact is immutable'); END""")
    op.execute("""CREATE TRIGGER artifact_no_delete BEFORE DELETE ON artifacts
        BEGIN SELECT RAISE(ABORT,'artifact record is permanent'); END""")
    op.execute("""CREATE INDEX event_task_cursor ON business_events(task_id,sequence)""")


def downgrade() -> None:
    raise RuntimeError("不可变证据历史不自动降级")
