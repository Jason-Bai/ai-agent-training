"""Allow metadata-only provider probes and measured Provider durations."""
from alembic import op
from sqlalchemy import Column, Double, inspect

revision = 'f8_001'
down_revision = 'f7_001'
branch_labels = None
depends_on = None

TABLE = 'tb_llm_request'


def replace_operation_checks(allow_probe):
    # Both operation checks have PostgreSQL-generated names on an F7 database.
    checks = inspect(op.get_bind()).get_check_constraints(TABLE)
    for check in checks:
        if 'operation' in check['sqltext']:
            op.drop_constraint(check['name'], TABLE, type_='check')
    nonstream = "operation IN ('completion','provider_probe')" if allow_probe else "operation = 'completion'"
    op.create_check_constraint('ck_llm_request_operation', TABLE, f"operation='streaming' OR {nonstream}")
    op.create_check_constraint('ck_llm_request_event_seq', TABLE,
        f"(operation='streaming' AND last_event_seq IS NOT NULL) OR ({nonstream} AND last_event_seq IS NULL)")


def upgrade():
    replace_operation_checks(True)
    op.add_column('tb_llm_attempt', Column('provider_duration_ms', Double))


def downgrade():
    # PostgreSQL refuses the older constraint when incompatible probes exist.
    replace_operation_checks(False)
    op.drop_column('tb_llm_attempt', 'provider_duration_ms')
