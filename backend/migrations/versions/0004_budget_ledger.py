"""累计预算、预留／实际用量和有限续期的持久化。"""

from alembic import op

revision = "0004_budget_ledger"
down_revision = "0003_task_records"
branch_labels = None
depends_on = None

DIMENSIONS = ("active_ms", "model_calls", "tokens", "repair_rounds", "recovery_rounds")


def upgrade() -> None:
    for definition in (
        "initial_limits_json TEXT NOT NULL DEFAULT '{}'",
        "authorization_revision INTEGER NOT NULL DEFAULT 1",
        "approved_progress_json TEXT NOT NULL DEFAULT '[]'",
    ):
        op.execute(f"ALTER TABLE budget_ledgers ADD COLUMN {definition}")
    for definition in (
        "configuration_json TEXT NOT NULL DEFAULT '{}'",
        "acceptance_revision INTEGER NOT NULL DEFAULT 1 CHECK(acceptance_revision > 0)",
        "budget_wait_reason TEXT",
    ):
        op.execute(f"ALTER TABLE tasks ADD COLUMN {definition}")
    op.execute("ALTER TABLE runs ADD COLUMN budget_metered_ms INTEGER")
    op.execute("""CREATE TABLE budget_step_plans (
        owner_task_id TEXT NOT NULL REFERENCES budget_ledgers(owner_task_id),
        operation_key TEXT NOT NULL,
        purpose TEXT NOT NULL,
        upper_json TEXT NOT NULL,
        PRIMARY KEY(owner_task_id,operation_key)
    )""")
    op.execute("""CREATE TRIGGER budget_step_plan_immutable BEFORE UPDATE ON budget_step_plans
        BEGIN SELECT RAISE(ABORT,'step upper bound is immutable'); END""")
    op.execute("""CREATE TABLE budget_reservations (
        id TEXT PRIMARY KEY,
        owner_task_id TEXT NOT NULL REFERENCES budget_ledgers(owner_task_id),
        run_id TEXT NOT NULL REFERENCES runs(id),
        generation INTEGER NOT NULL,
        operation_key TEXT NOT NULL,
        purpose TEXT NOT NULL CHECK(purpose IN ('model','tool','publish','counter')),
        upper_json TEXT NOT NULL,
        actual_json TEXT NOT NULL DEFAULT '{}',
        outstanding_json TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN
            ('reserved','dispatched','unknown','settled','cancelled')),
        created_at REAL NOT NULL,
        metered_active_ms INTEGER NOT NULL DEFAULT 0 CHECK(metered_active_ms >= 0),
        finished_at REAL,
        UNIQUE(owner_task_id,operation_key)
    )""")
    op.execute("""CREATE TABLE budget_progress (
        id TEXT PRIMARY KEY,
        owner_task_id TEXT NOT NULL REFERENCES budget_ledgers(owner_task_id),
        run_id TEXT NOT NULL REFERENCES runs(id),
        objective_key TEXT NOT NULL,
        fact_key TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN
            ('coverage','refutation','validated_evidence','verified_subproblem')),
        artifact_id TEXT NOT NULL REFERENCES artifacts(id),
        UNIQUE(owner_task_id,objective_key,kind,fact_key)
    )""")
    op.execute("""CREATE UNIQUE INDEX one_dispatched_budget_step ON budget_reservations(run_id)
        WHERE state='dispatched'""")
    op.execute("""CREATE TABLE budget_renewals (
        id TEXT PRIMARY KEY,
        owner_task_id TEXT NOT NULL REFERENCES budget_ledgers(owner_task_id),
        task_id TEXT NOT NULL REFERENCES tasks(id),
        run_id TEXT NOT NULL REFERENCES runs(id),
        revision INTEGER NOT NULL DEFAULT 1,
        basis_sha256 TEXT NOT NULL,
        basis_json TEXT NOT NULL,
        report_json TEXT NOT NULL,
        request_sha256 TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN
            ('pending','approved','rejected','scope_confirmation','cancelled','stale')),
        decision_json TEXT,
        result_json TEXT,
        UNIQUE(owner_task_id,basis_sha256),
        UNIQUE(run_id)
    )""")
    op.execute("""CREATE UNIQUE INDEX one_pending_renewal ON budget_renewals(owner_task_id)
        WHERE state='pending'""")
    op.execute("""CREATE TABLE budget_renewal_revisions (
        renewal_id TEXT NOT NULL REFERENCES budget_renewals(id),
        revision INTEGER NOT NULL,
        basis_json TEXT NOT NULL,
        report_json TEXT NOT NULL,
        state TEXT NOT NULL,
        PRIMARY KEY(renewal_id,revision)
    )""")
    op.execute("""CREATE TRIGGER task_budget_owner_immutable BEFORE UPDATE ON tasks
        WHEN OLD.budget_owner_task_id IS NOT NULL
        AND NEW.budget_owner_task_id IS NOT OLD.budget_owner_task_id
        BEGIN SELECT RAISE(ABORT,'budget owner is immutable'); END""")
    comparisons = " OR ".join(
        f"COALESCE(json_extract(NEW.{column},'$.{key}'),0)"
        f" < COALESCE(json_extract(OLD.{column},'$.{key}'),0)"
        for column in ("limits_json", "used_json")
        for key in DIMENSIONS
    )
    op.execute(f"""CREATE TRIGGER budget_totals_monotonic BEFORE UPDATE ON budget_ledgers
        WHEN NEW.owner_task_id != OLD.owner_task_id OR {comparisons}
        BEGIN SELECT RAISE(ABORT,'budget totals cannot decrease or transfer'); END""")
    op.execute("""CREATE TRIGGER budget_reservation_identity BEFORE UPDATE ON budget_reservations
        WHEN NEW.owner_task_id != OLD.owner_task_id OR NEW.run_id != OLD.run_id
        OR NEW.generation != OLD.generation OR NEW.operation_key != OLD.operation_key
        OR NEW.purpose != OLD.purpose OR NEW.upper_json != OLD.upper_json
        BEGIN SELECT RAISE(ABORT,'reservation identity and upper bound are immutable'); END""")


def downgrade() -> None:
    raise RuntimeError("预算历史不自动降级；升级前须停写并备份")
