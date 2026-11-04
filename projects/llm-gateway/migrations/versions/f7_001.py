"""Record each invocation's exact input, budget, reason and local entry evidence."""
from alembic import op
from sqlalchemy import Column, Text, BigInteger, Boolean

revision = 'f7_001'
down_revision = 'f6_001'
branch_labels = None
depends_on = None


def upgrade():
    for name in ('input_snapshot', 'prompt_fingerprint', 'counter_version', 'reason'):
        op.add_column('tb_llm_attempt', Column(name, Text))
    op.add_column('tb_llm_attempt', Column('input_token_count', BigInteger))
    op.add_column('tb_llm_attempt', Column('max_output_tokens', BigInteger))
    # Old rows retain unknown evidence, not a fabricated "never called" claim.
    op.add_column('tb_llm_attempt', Column('provider_entered', Boolean))
    op.create_check_constraint('ck_tb_llm_attempt_input_count', 'tb_llm_attempt', 'input_token_count >= 0')
    op.create_check_constraint('ck_tb_llm_attempt_output_budget', 'tb_llm_attempt', 'max_output_tokens > 0')


def downgrade():
    op.drop_constraint('ck_tb_llm_attempt_output_budget', 'tb_llm_attempt', type_='check')
    op.drop_constraint('ck_tb_llm_attempt_input_count', 'tb_llm_attempt', type_='check')
    for name in ('provider_entered', 'max_output_tokens', 'input_token_count', 'reason',
                 'counter_version', 'prompt_fingerprint', 'input_snapshot'):
        op.drop_column('tb_llm_attempt', name)
