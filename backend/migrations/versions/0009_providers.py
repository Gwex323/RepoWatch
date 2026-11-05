from alembic import op

revision = "0009_providers"
down_revision = "0008_artifact_integrity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE provider_batches (
        id TEXT PRIMARY KEY,
        operation_id TEXT NOT NULL UNIQUE REFERENCES user_operations(id),
        state TEXT NOT NULL CHECK(state IN ('preparing','active','aborted')),
        owner_process_json TEXT NOT NULL,
        result_json TEXT
    )""")
    op.execute("""CREATE TABLE providers (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        name_key TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision > 0),
        active_revision_id TEXT REFERENCES provider_revisions(id),
        state TEXT NOT NULL CHECK(state IN ('staged','active','aborted'))
    )""")
    op.execute("""CREATE TABLE provider_revisions (
        id TEXT PRIMARY KEY,
        provider_id TEXT NOT NULL REFERENCES providers(id),
        revision INTEGER NOT NULL CHECK(revision > 0),
        base_revision INTEGER,
        batch_id TEXT NOT NULL REFERENCES provider_batches(id),
        configuration_json TEXT NOT NULL,
        credential_reference TEXT NOT NULL,
        credential_revision INTEGER NOT NULL CHECK(credential_revision > 0),
        state TEXT NOT NULL CHECK(state IN ('staged','active','aborted')),
        UNIQUE(provider_id,revision),
        UNIQUE(id,provider_id)
    )""")
    op.execute("""CREATE UNIQUE INDEX live_provider_name ON providers(name_key)
        WHERE state != 'aborted'""")
    op.execute("""CREATE TABLE provider_name_claims (
        name_key TEXT PRIMARY KEY,
        batch_id TEXT NOT NULL REFERENCES provider_batches(id)
    )""")
    op.execute("""CREATE TABLE credential_stages (
        reference TEXT PRIMARY KEY,
        batch_id TEXT NOT NULL REFERENCES provider_batches(id),
        state TEXT NOT NULL CHECK(state IN ('planned','stored','active','deleted'))
    )""")
    op.execute("""CREATE TRIGGER provider_revision_immutable BEFORE UPDATE ON provider_revisions
        WHEN NEW.id != OLD.id OR NEW.provider_id != OLD.provider_id OR NEW.revision != OLD.revision
        OR NEW.batch_id != OLD.batch_id OR NEW.configuration_json != OLD.configuration_json
        OR NEW.base_revision IS NOT OLD.base_revision
        OR NEW.credential_reference != OLD.credential_reference
        OR NEW.credential_revision != OLD.credential_revision
        OR (OLD.state != 'staged' AND NEW.state != OLD.state)
        BEGIN SELECT RAISE(ABORT,'provider revision is immutable'); END""")
    op.execute("""CREATE TRIGGER provider_active_binding BEFORE UPDATE ON providers
        WHEN NEW.active_revision_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM provider_revisions r WHERE r.id=NEW.active_revision_id
            AND r.provider_id=NEW.id AND r.state='active' AND r.revision=NEW.revision)
        BEGIN SELECT RAISE(ABORT,'provider active revision mismatch'); END""")


def downgrade() -> None:
    raise RuntimeError("供应商凭据修订与跨存储对账历史不自动降级")
