"""任务验收规范及其输入与批准版本。"""

from alembic import op

revision = "0019_task_acceptance"
down_revision = "0018_scan_policy"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE task_acceptance_specs (
        task_id TEXT NOT NULL REFERENCES tasks(id),
        revision INTEGER NOT NULL CHECK(revision > 0),
        kind TEXT NOT NULL CHECK(kind IN ('bug','feature','refactor')),
        content_json TEXT NOT NULL,
        input_revision INTEGER NOT NULL CHECK(input_revision > 0),
        feedback_revision INTEGER NOT NULL CHECK(feedback_revision > 0),
        admission_revision INTEGER NOT NULL CHECK(admission_revision > 0),
        target_revision INTEGER NOT NULL CHECK(target_revision > 0),
        approved_revision INTEGER,
        approved_by TEXT,
        approved_feedback_revision INTEGER,
        PRIMARY KEY(task_id,revision),
        CHECK((approved_revision IS NULL AND approved_by IS NULL)
            OR (approved_revision = revision AND length(approved_by) > 0))
    )""")
    op.execute("""CREATE TRIGGER task_acceptance_content_immutable
        BEFORE UPDATE ON task_acceptance_specs
        WHEN NEW.task_id IS NOT OLD.task_id OR NEW.revision IS NOT OLD.revision
        OR NEW.kind IS NOT OLD.kind OR NEW.content_json IS NOT OLD.content_json
        OR NEW.input_revision IS NOT OLD.input_revision
        OR NEW.feedback_revision IS NOT OLD.feedback_revision
        OR NEW.admission_revision IS NOT OLD.admission_revision
        OR NEW.target_revision IS NOT OLD.target_revision
        OR (OLD.approved_revision IS NOT NULL AND
            (NEW.approved_revision IS NOT OLD.approved_revision
            OR NEW.approved_by IS NOT OLD.approved_by
            OR NEW.approved_feedback_revision IS NOT OLD.approved_feedback_revision))
        BEGIN SELECT RAISE(ABORT,'acceptance history is immutable'); END""")
    op.execute("""CREATE TRIGGER task_acceptance_no_delete BEFORE DELETE ON task_acceptance_specs
        BEGIN SELECT RAISE(ABORT,'acceptance history is permanent'); END""")


def downgrade():
    raise RuntimeError("任务验收规范历史不自动降级")
