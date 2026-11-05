from alembic import op

revision = "0001_core_draft"
down_revision = None
branch_labels = None
depends_on = None

STATEMENTS = (
    """CREATE TABLE repositories (
        id TEXT PRIMARY KEY,
        github_id TEXT NOT NULL UNIQUE,
        target_revision INTEGER NOT NULL DEFAULT 1 CHECK(target_revision > 0),
        import_epoch INTEGER NOT NULL DEFAULT 1 CHECK(import_epoch > 0),
        admission_revision INTEGER NOT NULL DEFAULT 1 CHECK(admission_revision > 0),
        admission_mode TEXT NOT NULL CHECK(admission_mode IN ('manual','labels','all_bugs')),
        paused INTEGER NOT NULL DEFAULT 0 CHECK(paused IN (0,1))
    )""",
    """CREATE TABLE tasks (
        id TEXT PRIMARY KEY,
        repository_id TEXT NOT NULL REFERENCES repositories(id),
        source_key TEXT NOT NULL,
        issue_id TEXT,
        state TEXT NOT NULL CHECK(state IN
            ('queued','running','waiting','blocked','ended','cancelled')),
        mode TEXT NOT NULL,
        input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision > 0),
        feedback_revision INTEGER NOT NULL DEFAULT 1 CHECK(feedback_revision > 0),
        user_excluded INTEGER NOT NULL DEFAULT 0 CHECK(user_excluded IN (0,1)),
        UNIQUE(repository_id, source_key),
        UNIQUE(repository_id, issue_id),
        UNIQUE(id, repository_id, issue_id)
    )""",
    """CREATE TABLE budget_ledgers (
        owner_task_id TEXT PRIMARY KEY REFERENCES tasks(id),
        revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
        limits_json TEXT NOT NULL,
        used_json TEXT NOT NULL,
        reserved_json TEXT NOT NULL
    )""",
    """CREATE TABLE runs (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(id),
        budget_owner_task_id TEXT NOT NULL REFERENCES budget_ledgers(owner_task_id),
        generation INTEGER NOT NULL CHECK(generation > 0),
        parent_run_id TEXT REFERENCES runs(id),
        snapshot_json TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('running','suspended','ended','abandoned')),
        wait_reason TEXT,
        CHECK(state != 'suspended' OR wait_reason IS NOT NULL)
    )""",
    """CREATE TABLE global_control (
        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
        paused INTEGER NOT NULL CHECK(paused IN (0,1)),
        revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0)
    )""",
    "INSERT INTO global_control(singleton, paused) VALUES(1,1)",
    """CREATE TABLE execution_lease (
        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
        generation INTEGER NOT NULL DEFAULT 0 CHECK(generation >= 0),
        holder_run_id TEXT REFERENCES runs(id),
        expires_at REAL,
        heartbeat_at REAL,
        CHECK((holder_run_id IS NULL AND expires_at IS NULL AND heartbeat_at IS NULL)
            OR (holder_run_id IS NOT NULL AND expires_at IS NOT NULL AND heartbeat_at IS NOT NULL))
    )""",
    "INSERT INTO execution_lease(singleton) VALUES(1)",
    """CREATE TABLE triage_decisions (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(id),
        input_revision INTEGER NOT NULL,
        admission_revision INTEGER NOT NULL,
        result TEXT NOT NULL CHECK(result IN ('auto_repair','needs_information','needs_approval',
            'excluded','external_repair','blocked')),
        evidence_json TEXT NOT NULL,
        UNIQUE(task_id,input_revision,admission_revision)
    )""",
    """CREATE TABLE pending_signals (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(id),
        source_key TEXT NOT NULL,
        revision TEXT NOT NULL,
        consumed_by_run_id TEXT REFERENCES runs(id),
        UNIQUE(task_id,source_key,revision)
    )""",
    """CREATE TABLE artifacts (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(id),
        sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
        relative_path TEXT NOT NULL UNIQUE,
        available INTEGER NOT NULL DEFAULT 1 CHECK(available IN (0,1)),
        context_json TEXT NOT NULL
    )""",
    """CREATE TABLE publish_operations (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(id),
        operation_key TEXT NOT NULL UNIQUE,
        state TEXT NOT NULL CHECK(state IN ('intent','unknown','confirmed','rejected')),
        expected_json TEXT NOT NULL,
        receipt_json TEXT
    )""",
    """CREATE TABLE managed_pull_requests (
        task_id TEXT PRIMARY KEY,
        issue_repository_id TEXT NOT NULL,
        issue_id TEXT NOT NULL,
        pr_repository_id TEXT NOT NULL,
        pr_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('open','closed','merged')),
        UNIQUE(issue_repository_id,issue_id),
        UNIQUE(pr_repository_id,pr_id),
        FOREIGN KEY(task_id,issue_repository_id,issue_id)
            REFERENCES tasks(id,repository_id,issue_id)
    )""",
    """CREATE TABLE external_pr_observations (
        repository_id TEXT NOT NULL REFERENCES repositories(id),
        pr_id TEXT NOT NULL,
        issue_id TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        PRIMARY KEY(repository_id,pr_id,issue_id)
    )""",
    """CREATE TABLE progress_attempts (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(id),
        run_id TEXT NOT NULL REFERENCES runs(id),
        objective_key TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        evidence_json TEXT NOT NULL
    )""",
    """CREATE TABLE scan_dependencies (
        waiting_task_id TEXT NOT NULL REFERENCES tasks(id),
        scan_task_id TEXT NOT NULL REFERENCES tasks(id),
        PRIMARY KEY(waiting_task_id,scan_task_id),
        CHECK(waiting_task_id != scan_task_id)
    )""",
    """CREATE TABLE business_events (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT REFERENCES tasks(id),
        kind TEXT NOT NULL,
        payload_json TEXT NOT NULL
    )""",
    """CREATE TABLE user_operations (
        id TEXT PRIMARY KEY,
        request_sha256 TEXT NOT NULL CHECK(length(request_sha256) = 64),
        expected_revision INTEGER NOT NULL CHECK(expected_revision > 0),
        result_json TEXT NOT NULL
    )""",
)


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("P0 迁移草案仅支持新库验证；降级须在后续维护流程中实现并验证")
