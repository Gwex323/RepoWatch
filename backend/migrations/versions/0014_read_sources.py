from alembic import op

revision = "0014_read_sources"
down_revision = "0013_runtime_progress"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for column in ("github_owner TEXT", "github_name TEXT", "default_branch TEXT"):
        op.execute(f"ALTER TABLE repositories ADD COLUMN {column}")
    op.execute("""CREATE TABLE github_read_operations (
        id TEXT PRIMARY KEY,
        operation_id TEXT NOT NULL UNIQUE REFERENCES user_operations(id),
        repository_id TEXT NOT NULL REFERENCES repositories(id),
        page INTEGER NOT NULL CHECK(page>0),
        state TEXT NOT NULL CHECK(state IN ('pending','completed','failed')),
        owner_process_json TEXT NOT NULL,
        result_json TEXT,
        error_code TEXT
    )""")
    op.execute("""CREATE UNIQUE INDEX one_pending_repository_read
        ON github_read_operations(repository_id) WHERE state='pending'""")
    op.execute("""CREATE TABLE github_observations (
        id TEXT PRIMARY KEY,
        repository_id TEXT NOT NULL REFERENCES repositories(id),
        kind TEXT NOT NULL CHECK(kind IN ('issue','pull_request')),
        object_id TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),
        payload_json TEXT NOT NULL,
        remote_version TEXT,
        UNIQUE(id,repository_id),
        UNIQUE(repository_id,kind,object_id,payload_sha256)
    )""")
    op.execute("""CREATE TABLE github_object_heads (
        repository_id TEXT NOT NULL REFERENCES repositories(id),
        kind TEXT NOT NULL,
        object_id TEXT NOT NULL,
        observation_id TEXT NOT NULL REFERENCES github_observations(id),
        PRIMARY KEY(repository_id,kind,object_id)
    )""")
    op.execute("""CREATE TABLE github_read_cache (
        repository_id TEXT NOT NULL REFERENCES repositories(id),
        page INTEGER NOT NULL,
        etag TEXT,
        observation_ids_json TEXT NOT NULL,
        has_next INTEGER NOT NULL CHECK(has_next IN (0,1)),
        last_success_at REAL NOT NULL,
        PRIMARY KEY(repository_id,page)
    )""")
    op.execute("""CREATE TABLE memory_source_facts (
        observation_id TEXT PRIMARY KEY REFERENCES github_observations(id),
        repository_id TEXT NOT NULL REFERENCES repositories(id),
        signal_id TEXT REFERENCES pending_signals(id),
        visibility TEXT NOT NULL CHECK(visibility='repository'),
        FOREIGN KEY(observation_id,repository_id) REFERENCES github_observations(id,repository_id)
    )""")
    for table in ("github_observations", "memory_source_facts"):
        for action in ("UPDATE", "DELETE"):
            op.execute(f"""CREATE TRIGGER {table}_{action.lower()}_immutable
                BEFORE {action} ON {table}
                BEGIN SELECT RAISE(ABORT,'source facts are immutable'); END""")


def downgrade() -> None:
    raise RuntimeError("只读来源快照与同步记录不自动降级")
