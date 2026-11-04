"""Initial request, attempt and durable stream event schema.

Frozen initial schema: migrations must not follow future application metadata.
"""
from alembic import op
from sqlalchemy import ForeignKeyConstraint

revision = 'f5o1_001'
down_revision = None
branch_labels = None
depends_on = None

from sqlalchemy import (MetaData, Table, Column, BigInteger, Integer, Text, DateTime,
                        Double, Identity, ForeignKey, CheckConstraint, Index, Uuid)

metadata = MetaData()
request = Table('tb_llm_request', metadata,
    Column('id', BigInteger, Identity(always=True), primary_key=True),
    Column('request_id', Uuid, nullable=False, unique=True),
    Column('operation', Text, nullable=False), Column('status', Text, nullable=False),
    *[Column(name, Text) for name in ('requested_model', 'prompt_name', 'prompt_version',
      'prompt_fingerprint', 'schema_fingerprint', 'input_snapshot', 'counter_version',
      'structured_output_mode', 'error_code', 'error_message', 'timing_snapshot')],
    Column('input_token_count', BigInteger), Column('max_output_tokens', BigInteger),
    Column('timeout_seconds', Integer),
    Column('created_at', DateTime(timezone=True), nullable=False),
    *[Column(name, DateTime(timezone=True)) for name in ('started_at', 'deadline_at', 'ended_at')],
    Column('last_event_seq', BigInteger),
    CheckConstraint("operation IN ('completion','streaming')"),
    CheckConstraint("status IN ('pending','running','canceling','completed','failed','canceled')"),
    CheckConstraint('input_token_count >= 0'), CheckConstraint('max_output_tokens > 0'),
    CheckConstraint('timeout_seconds > 0'), CheckConstraint('last_event_seq >= 0'),
    CheckConstraint("(operation='streaming' AND last_event_seq IS NOT NULL) OR (operation='completion' AND last_event_seq IS NULL)"),
)
Index('ix_tb_llm_request_status', request.c.status)
attempt = Table('tb_llm_attempt', metadata,
    Column('request_pk', BigInteger, ForeignKey(request.c.id), primary_key=True),
    Column('attempt_no', Integer, primary_key=True),
    *[Column(name, Text, nullable=False) for name in ('model','provider','adapter','api_protocol','configured_model','status')],
    *[Column(name, Text) for name in ('actual_model','finish_reason','upstream_status','upstream_finish_reason',
                                    'error_code','provider_error_code','cleanup_error_code')],
    *[Column(name, BigInteger) for name in ('input_tokens','output_tokens','total_tokens')],
    Column('started_at', DateTime(timezone=True), nullable=False),
    Column('ended_at', DateTime(timezone=True)), Column('latency_ms', Double),
    CheckConstraint('attempt_no > 0'), CheckConstraint("status IN ('running','completed','failed')"),
    CheckConstraint('input_tokens >= 0 AND output_tokens >= 0 AND total_tokens >= 0'),
    CheckConstraint('(input_tokens IS NULL AND output_tokens IS NULL AND total_tokens IS NULL) OR '
                    '(input_tokens IS NOT NULL AND output_tokens IS NOT NULL AND total_tokens IS NOT NULL AND total_tokens=input_tokens+output_tokens)'),
    CheckConstraint('latency_ms >= 0'),
)
event = Table('tb_llm_stream_event', metadata,
    Column('request_pk', BigInteger, ForeignKey(request.c.id), primary_key=True),
    Column('seq', BigInteger, primary_key=True), Column('event_type', Text, nullable=False),
    Column('payload', Text, nullable=False), Column('created_at', DateTime(timezone=True), nullable=False),
    CheckConstraint('seq > 0'),
    CheckConstraint("event_type IN ('response.delta','response.completed','response.failed')"),
)


def upgrade():
    for table in metadata.sorted_tables:
        constraints = [CheckConstraint(str(item.sqltext)) for item in table.constraints
                       if isinstance(item, CheckConstraint)]
        if table.name != 'tb_llm_request':
            constraints.append(ForeignKeyConstraint(['request_pk'], ['tb_llm_request.id']))
        op.create_table(table.name, *[column._copy() for column in table.columns], *constraints)
    op.create_index('ix_tb_llm_request_status', 'tb_llm_request', ['status'])


def downgrade():
    for table in reversed(metadata.sorted_tables):
        op.drop_table(table.name)
