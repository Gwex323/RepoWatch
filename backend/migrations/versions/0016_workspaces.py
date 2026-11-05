from alembic import op

revision = "0016_workspaces"
down_revision = "0015_repository_imports"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE workspaces (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(id),
        repository_id TEXT NOT NULL REFERENCES repositories(id),
        snapshot_json TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('ready','removed','unavailable')),
        created_at REAL NOT NULL
    )""")
    op.execute("""CREATE TABLE runner_results (
        command_id TEXT PRIMARY KEY REFERENCES command_states(id),
        run_id TEXT NOT NULL REFERENCES runs(id),
        workspace_id TEXT NOT NULL REFERENCES workspaces(id),
        result_json TEXT NOT NULL,
        artifact_id TEXT NOT NULL REFERENCES artifacts(id),
        created_at REAL NOT NULL
    )""")


def downgrade():
    raise RuntimeError("工作区与检查证据不自动降级")
