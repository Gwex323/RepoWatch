from alembic import op

revision = "0010_role_capabilities"
down_revision = "0009_providers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE role_bindings (
        binding_key TEXT PRIMARY KEY,
        revision INTEGER NOT NULL CHECK(revision>0),
        definition_json TEXT NOT NULL
    )""")
    op.execute("""CREATE TABLE role_binding_versions (
        binding_key TEXT NOT NULL REFERENCES role_bindings(binding_key),
        revision INTEGER NOT NULL CHECK(revision>0),
        definition_json TEXT NOT NULL,
        operation_id TEXT NOT NULL REFERENCES user_operations(id),
        PRIMARY KEY(binding_key,revision)
    )""")
    op.execute("""CREATE TABLE capability_checks (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        diagnostic_task_id TEXT NOT NULL UNIQUE REFERENCES diagnostic_authorizations(task_id),
        artifact_id TEXT NOT NULL REFERENCES artifacts(id),
        scope_sha256 TEXT NOT NULL CHECK(length(scope_sha256)=64),
        scope_json TEXT NOT NULL,
        passed INTEGER NOT NULL CHECK(passed IN (0,1)),
        checked_at REAL NOT NULL,
        expires_at REAL NOT NULL CHECK(expires_at>checked_at)
    )""")
    op.execute("CREATE INDEX capability_scope ON capability_checks(scope_sha256,sequence)")
    for table in ("capability_checks", "role_binding_versions"):
        for action in ("UPDATE", "DELETE"):
            op.execute(f"""CREATE TRIGGER {table}_{action.lower()}_forbidden
                BEFORE {action} ON {table}
                BEGIN SELECT RAISE(ABORT,'role authorization history is immutable'); END""")


def downgrade() -> None:
    raise RuntimeError("角色配置与能力记录不自动降级")
