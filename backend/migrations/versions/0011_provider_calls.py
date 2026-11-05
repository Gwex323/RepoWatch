from alembic import op

revision = "0011_provider_calls"
down_revision = "0010_role_capabilities"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""ALTER TABLE runs ADD COLUMN budget_clock_state TEXT NOT NULL DEFAULT 'active'
        CHECK(budget_clock_state IN ('active','backoff'))""")
    op.execute("""CREATE TABLE provider_invocations (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(id),
        call_key TEXT NOT NULL,
        request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
        state TEXT NOT NULL CHECK(state IN ('pending','waiting','succeeded','failed','unknown')),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
        artifact_id TEXT REFERENCES artifacts(id),
        error_code TEXT,
        UNIQUE(run_id,call_key),
        CHECK(state!='succeeded' OR artifact_id IS NOT NULL)
    )""")
    op.execute("""CREATE TRIGGER invocation_identity_immutable BEFORE UPDATE ON provider_invocations
        WHEN NEW.id != OLD.id OR NEW.run_id != OLD.run_id OR NEW.call_key != OLD.call_key
        OR NEW.request_sha256 != OLD.request_sha256
        OR OLD.state IN ('succeeded','failed','unknown')
        BEGIN SELECT RAISE(ABORT,'model invocation is immutable'); END""")


def downgrade() -> None:
    raise RuntimeError("模型调用意图和用量历史不自动降级")
