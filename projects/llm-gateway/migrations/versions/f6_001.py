"""Allow durable cancellation outcomes without changing existing data."""
from alembic import op
from sqlalchemy import inspect

revision = 'f6_001'
down_revision = 'f5o1_001'
branch_labels = None
depends_on = None


def replace_check(table, marker, name, expression):
    # The initial migration used unnamed constraints; discover their actual
    # database names instead of assuming PostgreSQL's generated suffixes.
    constraints = inspect(op.get_bind()).get_check_constraints(table)
    constraint = next(item for item in constraints if marker in item['sqltext'])
    op.drop_constraint(constraint['name'], table, type_='check')
    op.create_check_constraint(name, table, expression)


def upgrade():
    replace_check('tb_llm_attempt', "'running'", 'ck_tb_llm_attempt_status',
                  "status IN ('running','completed','failed','canceled')")
    replace_check('tb_llm_stream_event', "'response.delta'", 'ck_tb_llm_stream_event_type',
                  "event_type IN ('response.delta','response.completed','response.failed','response.canceling','response.canceled')")


def downgrade():
    # Existing cancellation data makes the old schema incompatible: PostgreSQL
    # rejects this downgrade rather than silently deleting or rewriting data.
    replace_check('tb_llm_attempt', "'running'", 'ck_tb_llm_attempt_status',
                  "status IN ('running','completed','failed')")
    replace_check('tb_llm_stream_event', "'response.delta'", 'ck_tb_llm_stream_event_type',
                  "event_type IN ('response.delta','response.completed','response.failed')")
