"""Durable human input and reflective prompt optimization.

Revision ID: 0002_agent_tasks
Revises: 0001_initial
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0002_agent_tasks'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint(op.f('ck_async_jobs_async_job_status_valid'), 'async_jobs', type_='check')
    op.create_check_constraint(op.f('ck_async_jobs_async_job_status_valid'), 'async_jobs',
        "status IN ('queued','running','waiting_input','succeeded','failed','cancelled')")
    op.drop_index('uq_async_jobs_active_resource', table_name='async_jobs')
    op.create_index('uq_async_jobs_active_resource', 'async_jobs',
        ['job_type','resource_type','resource_id'], unique=True,
        postgresql_where=sa.text("status IN ('queued','running','waiting_input')"))
    op.create_table('prompt_optimizations',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('created_by', sa.Uuid(), sa.ForeignKey('user_accounts.id'), nullable=False),
        sa.Column('job_id', sa.Uuid(), sa.ForeignKey('async_jobs.id'), nullable=False, unique=True),
        sa.Column('evaluation_id', sa.Uuid(), sa.ForeignKey('agent_offline_evaluations.id'), nullable=False),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('options', postgresql.JSONB(), nullable=False),
        sa.Column('results', postgresql.JSONB(), nullable=False),
        sa.Column('candidate_id', sa.Uuid(), sa.ForeignKey('prompt_template_versions.id')),
        sa.Column('published_improvement_id', sa.Uuid(), sa.ForeignKey('evaluation_improvements.id')),
    )
    op.create_index('ix_prompt_optimizations_created_by', 'prompt_optimizations', ['created_by'])


def downgrade():
    # Existing paused investigations must be resolved before removing this state.
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT count(*) FROM async_jobs WHERE status='waiting_input'")).scalar():
        raise RuntimeError('Resolve waiting-input investigations before downgrading')
    op.drop_table('prompt_optimizations')
    op.drop_index('uq_async_jobs_active_resource', table_name='async_jobs')
    op.create_index('uq_async_jobs_active_resource', 'async_jobs',
        ['job_type','resource_type','resource_id'], unique=True,
        postgresql_where=sa.text("status IN ('queued','running')"))
    op.drop_constraint(op.f('ck_async_jobs_async_job_status_valid'), 'async_jobs', type_='check')
    op.create_check_constraint(op.f('ck_async_jobs_async_job_status_valid'), 'async_jobs',
        "status IN ('queued','running','succeeded','failed','cancelled')")
