from alembic import op

revision = "0015_repository_imports"
down_revision = "0014_read_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for definition in (
        "target_branch TEXT",
        "contribution_path TEXT CHECK(contribution_path IN ('direct','fork'))",
        "publication_json TEXT",
        "policy_json TEXT NOT NULL DEFAULT '{}'",
        "identity_json TEXT",
        "credential_reference TEXT",
        "scan_policy_revision INTEGER NOT NULL DEFAULT 1 CHECK(scan_policy_revision>0)",
        "scan_ready_json TEXT",
    ):
        op.execute(f"ALTER TABLE repositories ADD COLUMN {definition}")
    op.execute("""CREATE TABLE repository_revisions (
        repository_id TEXT NOT NULL REFERENCES repositories(id),
        revision INTEGER NOT NULL,
        configuration_json TEXT NOT NULL,
        created_at REAL NOT NULL,
        PRIMARY KEY(repository_id,revision)
    )""")
    op.execute("""CREATE TABLE repository_preflights (
        id TEXT PRIMARY KEY,
        operation_id TEXT NOT NULL UNIQUE REFERENCES user_operations(id),
        request_json TEXT NOT NULL,
        identity_json TEXT NOT NULL,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL
    )""")
    op.execute("""CREATE TABLE repository_imports (
        id TEXT PRIMARY KEY,
        repository_id TEXT NOT NULL REFERENCES repositories(id),
        import_epoch INTEGER NOT NULL,
        task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
        configuration_json TEXT NOT NULL,
        created_at REAL NOT NULL,
        UNIQUE(repository_id,import_epoch)
    )""")
    for table in ("repository_revisions", "repository_preflights", "repository_imports"):
        for action in ("UPDATE", "DELETE"):
            op.execute(f"""CREATE TRIGGER {table}_{action.lower()}_immutable
                BEFORE {action} ON {table}
                BEGIN SELECT RAISE(ABORT,'repository history is immutable'); END""")


def downgrade() -> None:
    raise RuntimeError("仓库配置与导入历史不自动降级")
