"""待提炼意图的来源不可覆盖，消费状态可独立推进。"""

from alembic import op

revision = "0021_distillation_state"
down_revision = "0020_repair_workflow"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("DROP TRIGGER distillation_intents_update_immutable")
    op.execute("""CREATE TRIGGER distillation_intents_update_immutable
        BEFORE UPDATE ON distillation_intents
        WHEN NEW.id IS NOT OLD.id OR NEW.task_id IS NOT OLD.task_id
        OR NEW.session_id IS NOT OLD.session_id
        OR NEW.source_sha256 IS NOT OLD.source_sha256
        OR NEW.feedback_revision IS NOT OLD.feedback_revision
        BEGIN SELECT RAISE(ABORT,'distillation source is immutable'); END""")


def downgrade():
    raise RuntimeError("提炼来源历史不自动降级")
