"""Persistent Agent identity, per-attempt holds and immutable budget operations."""
from alembic import op

revision = 'f9_001'
down_revision = 'f8_001'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE TABLE tb_agent (agent_id text PRIMARY KEY, name text NOT NULL)")
    op.execute("""CREATE TABLE tb_agent_credential (
        public_id text PRIMARY KEY, agent_id text NOT NULL REFERENCES tb_agent,
        digest text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), revoked_at timestamptz)""")
    op.execute("ALTER TABLE tb_llm_request ADD COLUMN agent_id text REFERENCES tb_agent")
    op.execute("CREATE INDEX ix_llm_request_agent_page ON tb_llm_request(agent_id,created_at,id)")
    op.execute("""CREATE TABLE tb_billing_attempt (
        request_pk bigint NOT NULL, attempt_no integer NOT NULL, agent_id text NOT NULL REFERENCES tb_agent,
        PRIMARY KEY(request_pk,attempt_no), FOREIGN KEY(request_pk,attempt_no) REFERENCES tb_llm_attempt,
        state text NOT NULL CHECK (state IN ('reserved','pending','settled','released','platform')),
        reserved_amount numeric NOT NULL CHECK (reserved_amount >= 0), agent_charge numeric CHECK (agent_charge >= 0),
        supplier_cost numeric CHECK (supplier_cost >= 0), quote jsonb, cached_input_tokens bigint, billing_observed_at timestamptz,
        reason text, created_at timestamptz NOT NULL DEFAULT now())""")
    op.execute("CREATE INDEX ix_tb_billing_attempt_agent_id ON tb_billing_attempt(agent_id)")
    op.execute("""CREATE TABLE tb_billing_operation (
        operation_id text PRIMARY KEY, agent_id text REFERENCES tb_agent,
        kind text NOT NULL CHECK (kind IN ('deposit','settle','zero','platform','supplier','statement')),
        amount numeric CHECK (amount >= 0), request_pk bigint, attempt_no integer,
        FOREIGN KEY(request_pk,attempt_no) REFERENCES tb_billing_attempt,
        reason text NOT NULL, details jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now())""")
    op.execute("CREATE INDEX ix_tb_billing_operation_agent_id ON tb_billing_operation(agent_id)")


def downgrade():
    # Refuse destructive downgrade once financial records or identities exist.
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT FROM tb_agent) OR EXISTS (SELECT FROM tb_billing_operation)
                  THEN RAISE EXCEPTION 'F9 billing data exists'; END IF; END $$""")
    op.execute("DROP TABLE tb_billing_operation, tb_billing_attempt")
    op.execute("ALTER TABLE tb_llm_request DROP COLUMN agent_id")
    op.execute("DROP TABLE tb_agent_credential, tb_agent")
