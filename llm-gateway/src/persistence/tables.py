from sqlalchemy import (MetaData, Table, Column, BigInteger, Integer, Text, DateTime,
                        Double, Identity, ForeignKey, CheckConstraint, Index, Uuid, Boolean)

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
    CheckConstraint("operation IN ('completion','streaming','provider_probe')"),
    CheckConstraint("status IN ('pending','running','canceling','completed','failed','canceled')"),
    CheckConstraint('input_token_count >= 0'), CheckConstraint('max_output_tokens > 0'),
    CheckConstraint('timeout_seconds > 0'), CheckConstraint('last_event_seq >= 0'),
    CheckConstraint("(operation='streaming' AND last_event_seq IS NOT NULL) OR (operation IN ('completion','provider_probe') AND last_event_seq IS NULL)"),
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
    Column('ended_at', DateTime(timezone=True)), Column('latency_ms', Double), Column('provider_duration_ms', Double),
    *[Column(name, Text) for name in ('input_snapshot', 'prompt_fingerprint', 'counter_version', 'reason')],
    Column('input_token_count', BigInteger), Column('max_output_tokens', BigInteger),
    Column('provider_entered', Boolean),
    CheckConstraint('input_token_count >= 0', name='ck_tb_llm_attempt_input_count'),
    CheckConstraint('max_output_tokens > 0', name='ck_tb_llm_attempt_output_budget'),
    CheckConstraint('attempt_no > 0'), CheckConstraint("status IN ('running','completed','failed','canceled')"),
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
    CheckConstraint("event_type IN ('response.delta','response.completed','response.failed','response.canceling','response.canceled')"),
)

# F9. Historical requests keep a NULL owner and are never backfilled into a budget.
from sqlalchemy import ForeignKeyConstraint, Numeric, func
from sqlalchemy.dialects.postgresql import JSONB

agent = Table('tb_agent', metadata,
    Column('agent_id', Text, primary_key=True), Column('name', Text, nullable=False))
agent_credential = Table('tb_agent_credential', metadata,
    Column('public_id', Text, primary_key=True),
    Column('agent_id', Text, ForeignKey('tb_agent.agent_id'), nullable=False),
    Column('digest', Text, nullable=False),
    Column('created_at', DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column('revoked_at', DateTime(timezone=True)))
request.append_column(Column('agent_id', Text, ForeignKey('tb_agent.agent_id')))
Index('ix_llm_request_agent_page', request.c.agent_id, request.c.created_at, request.c.id)
billing_attempt = Table('tb_billing_attempt', metadata,
    Column('request_pk', BigInteger, primary_key=True), Column('attempt_no', Integer, primary_key=True),
    ForeignKeyConstraint(['request_pk', 'attempt_no'], ['tb_llm_attempt.request_pk', 'tb_llm_attempt.attempt_no']),
    Column('agent_id', Text, ForeignKey('tb_agent.agent_id'), nullable=False, index=True),
    Column('state', Text, nullable=False), Column('reserved_amount', Numeric, nullable=False),
    Column('agent_charge', Numeric), Column('supplier_cost', Numeric), Column('quote', JSONB),
    Column('cached_input_tokens', BigInteger), Column('billing_observed_at', DateTime(timezone=True)), Column('reason', Text),
    Column('created_at', DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("state in ('reserved','pending','settled','released','platform')"),
    CheckConstraint('reserved_amount >= 0 and agent_charge >= 0 and supplier_cost >= 0'))
billing_operation = Table('tb_billing_operation', metadata,
    Column('operation_id',Text,primary_key=True), Column('kind',Text,nullable=False),
    Column('agent_id',Text,ForeignKey('tb_agent.agent_id'),index=True),
    Column('amount',Numeric), Column('request_pk',BigInteger), Column('attempt_no',Integer),
    ForeignKeyConstraint(['request_pk','attempt_no'], ['tb_billing_attempt.request_pk','tb_billing_attempt.attempt_no']),
    Column('reason',Text,nullable=False), Column('details',JSONB,nullable=False),
    Column('created_at',DateTime(timezone=True),nullable=False,server_default=func.now()),
    CheckConstraint("kind in ('deposit','settle','zero','platform','supplier','statement')"),
    CheckConstraint('amount >= 0'))
