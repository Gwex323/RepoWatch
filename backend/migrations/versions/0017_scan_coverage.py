from alembic import op

revision = "0017_scan_coverage"
down_revision = "0016_workspaces"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE scan_batches (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(id),
        repository_id TEXT NOT NULL REFERENCES repositories(id),
        version_json TEXT NOT NULL,
        manifest_json TEXT NOT NULL,
        checks_json TEXT NOT NULL,
        environment_json TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'incomplete',
        created_at REAL NOT NULL,
        UNIQUE(task_id,version_json,environment_json)
    )""")
    op.execute("""CREATE TABLE scan_coverage (
        batch_id TEXT NOT NULL REFERENCES scan_batches(id),
        unit_id TEXT NOT NULL,
        input_artifact_id TEXT NOT NULL REFERENCES artifacts(id),
        result_artifact_id TEXT NOT NULL REFERENCES artifacts(id),
        run_id TEXT NOT NULL REFERENCES runs(id),
        PRIMARY KEY(batch_id,unit_id)
    )""")
    op.execute("""CREATE TABLE scan_baselines (
        batch_id TEXT NOT NULL REFERENCES scan_batches(id),
        check_id TEXT NOT NULL,
        outcome TEXT NOT NULL
            CHECK(outcome IN ('passed','existing_failed','not_applicable','unable')),
        evidence_json TEXT NOT NULL,
        PRIMARY KEY(batch_id,check_id)
    )""")
    op.execute("""CREATE TABLE scan_candidates (
        id TEXT PRIMARY KEY,
        batch_id TEXT NOT NULL REFERENCES scan_batches(id),
        unit_id TEXT NOT NULL,
        evidence_artifact_id TEXT NOT NULL REFERENCES artifacts(id),
        finding_json TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'local',
        revision INTEGER NOT NULL DEFAULT 1,
        request_task_id TEXT REFERENCES tasks(id)
    )""")
    op.execute("""CREATE TABLE scan_attempts (
        id TEXT PRIMARY KEY,
        batch_id TEXT NOT NULL REFERENCES scan_batches(id),
        run_id TEXT NOT NULL REFERENCES runs(id),
        unit_id TEXT NOT NULL,
        input_artifact_id TEXT NOT NULL REFERENCES artifacts(id),
        outcome TEXT NOT NULL,
        created_at REAL NOT NULL
    )""")


def downgrade():
    raise RuntimeError("扫描证据不自动降级")
