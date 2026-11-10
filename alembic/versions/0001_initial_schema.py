"""Initial application schema, graph change triggers and checkpoint schema."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0001'
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE SCHEMA IF NOT EXISTS agent_checkpoints")
    op.create_table('business_graph_outbox',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('source_table', sa.String(length=100), nullable=False),
    sa.Column('record_id', sa.String(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_business_graph_outbox'))
    )
    op.create_table('business_graph_projection',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('generation', sa.String(length=64), nullable=False),
    sa.Column('synced_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('counts', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_business_graph_projection'))
    )
    op.create_table('inspection_records',
    sa.Column('organization_unit_id', sa.Uuid(), nullable=False),
    sa.Column('station_name', sa.String(length=200), nullable=False),
    sa.Column('pipeline_name', sa.String(length=200), nullable=True),
    sa.Column('equipment_name', sa.String(length=200), nullable=True),
    sa.Column('inspected_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_inspection_records'))
    )
    op.create_index('ix_inspection_org_time', 'inspection_records', ['organization_unit_id', 'inspected_at'], unique=False)
    op.create_table('model_aliases',
    sa.Column('capability', sa.String(length=32), server_default='text', nullable=False),
    sa.Column('code', sa.String(length=100), nullable=False),
    sa.Column('provider', sa.String(length=40), nullable=False),
    sa.Column('model_name', sa.String(length=200), nullable=False),
    sa.Column('model_snapshot', sa.String(length=200), nullable=True),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('config', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_model_aliases')),
    sa.UniqueConstraint('code', name=op.f('uq_model_aliases_code'))
    )
    op.create_table('operation_reports',
    sa.Column('report_type', sa.String(length=40), nullable=False),
    sa.Column('business_date', sa.Date(), nullable=False),
    sa.Column('organization_unit_id', sa.Uuid(), nullable=False),
    sa.Column('timezone', sa.String(length=80), nullable=False),
    sa.Column('scope_filter', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('scope_digest', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('current_version_id', sa.Uuid(), nullable=True),
    sa.Column('published_version_id', sa.Uuid(), nullable=True),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_operation_reports')),
    sa.UniqueConstraint('report_type', 'business_date', 'organization_unit_id', 'scope_digest', name='uq_operation_report_scope')
    )
    op.create_index('ix_operation_reports_status', 'operation_reports', ['organization_unit_id', 'status', 'business_date'], unique=False)
    op.create_table('organization_units',
    sa.Column('parent_id', sa.Uuid(), nullable=True),
    sa.Column('code', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('unit_type', sa.String(length=32), nullable=False),
    sa.Column('path', sa.String(length=1000), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['parent_id'], ['organization_units.id'], name=op.f('fk_organization_units_parent_id_organization_units'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_organization_units')),
    sa.UniqueConstraint('code', name=op.f('uq_organization_units_code'))
    )
    op.create_index(op.f('ix_organization_units_path'), 'organization_units', ['path'], unique=False)
    op.create_table('permission_definitions',
    sa.Column('code', sa.String(length=120), nullable=False),
    sa.Column('module', sa.String(length=60), nullable=False),
    sa.Column('name', sa.String(length=160), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('risk_level', sa.String(length=20), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_permission_definitions')),
    sa.UniqueConstraint('code', name=op.f('uq_permission_definitions_code'))
    )
    op.create_index(op.f('ix_permission_definitions_module'), 'permission_definitions', ['module'], unique=False)
    op.create_table('production_events',
    sa.Column('organization_unit_id', sa.Uuid(), nullable=False),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('event_type', sa.String(length=80), nullable=False),
    sa.Column('severity', sa.String(length=20), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('business_status', sa.String(length=32), nullable=False),
    sa.Column('current_version_id', sa.Uuid(), nullable=True),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('confirmed_by', sa.Uuid(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_production_events'))
    )
    op.create_index(op.f('ix_production_events_event_type'), 'production_events', ['event_type'], unique=False)
    op.create_index('ix_production_events_org_time', 'production_events', ['organization_unit_id', 'occurred_at'], unique=False)
    op.create_index('ix_production_events_status', 'production_events', ['business_status', 'updated_at'], unique=False)
    op.create_table('role_definitions',
    sa.Column('code', sa.String(length=80), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('permissions', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('system_role', sa.Boolean(), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_role_definitions')),
    sa.UniqueConstraint('code', name=op.f('uq_role_definitions_code'))
    )
    op.create_table('abnormality_assessments',
    sa.Column('event_id', sa.Uuid(), nullable=False),
    sa.Column('organization_unit_id', sa.Uuid(), nullable=False),
    sa.Column('category', sa.String(length=80), nullable=False),
    sa.Column('risk_level', sa.String(length=20), nullable=False),
    sa.Column('rationale', sa.Text(), nullable=False),
    sa.Column('recommended_action', sa.Text(), nullable=False),
    sa.Column('review_status', sa.String(length=32), nullable=False),
    sa.Column('reviewed_by', sa.Uuid(), nullable=True),
    sa.Column('review_reason', sa.Text(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['event_id'], ['production_events.id'], name=op.f('fk_abnormality_assessments_event_id_production_events'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_abnormality_assessments'))
    )
    op.create_index('ix_assessment_event_status', 'abnormality_assessments', ['event_id', 'review_status'], unique=False)
    op.create_table('event_source_links',
    sa.Column('event_id', sa.Uuid(), nullable=False),
    sa.Column('source_type', sa.String(length=60), nullable=False),
    sa.Column('source_id', sa.Uuid(), nullable=False),
    sa.Column('source_version_id', sa.Uuid(), nullable=True),
    sa.Column('evidence_text', sa.Text(), nullable=True),
    sa.Column('evidence_locator', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['event_id'], ['production_events.id'], name=op.f('fk_event_source_links_event_id_production_events'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_event_source_links')),
    sa.UniqueConstraint('event_id', 'source_type', 'source_id', name='uq_event_source_link')
    )
    op.create_index(op.f('ix_event_source_links_event_id'), 'event_source_links', ['event_id'], unique=False)
    op.create_table('inspection_images',
    sa.Column('inspection_id', sa.Uuid(), nullable=False),
    sa.Column('object_key', sa.String(length=1024), nullable=False),
    sa.Column('filename', sa.String(length=255), nullable=False),
    sa.Column('mime_type', sa.String(length=120), nullable=False),
    sa.Column('size_bytes', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('deleted', sa.Boolean(), nullable=False),
    sa.Column('deleted_by', sa.Uuid(), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('protected_reason', sa.String(length=300), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['inspection_id'], ['inspection_records.id'], name=op.f('fk_inspection_images_inspection_id_inspection_records'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_inspection_images')),
    sa.UniqueConstraint('object_key', name=op.f('uq_inspection_images_object_key'))
    )
    op.create_index('ix_inspection_images_record_status', 'inspection_images', ['inspection_id', 'status'], unique=False)
    op.create_table('production_event_versions',
    sa.Column('event_id', sa.Uuid(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('structured_data', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=True),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['event_id'], ['production_events.id'], name=op.f('fk_production_event_versions_event_id_production_events'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_production_event_versions')),
    sa.UniqueConstraint('event_id', 'version', name='uq_production_event_version')
    )
    op.create_index(op.f('ix_production_event_versions_event_id'), 'production_event_versions', ['event_id'], unique=False)
    op.create_table('report_versions',
    sa.Column('report_id', sa.Uuid(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('content', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('review_status', sa.String(length=32), nullable=False),
    sa.Column('reviewed_by', sa.Uuid(), nullable=True),
    sa.Column('review_reason', sa.Text(), nullable=True),
    sa.Column('immutable', sa.Boolean(), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['report_id'], ['operation_reports.id'], name=op.f('fk_report_versions_report_id_operation_reports'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_report_versions')),
    sa.UniqueConstraint('report_id', 'version', name='uq_report_version')
    )
    op.create_index(op.f('ix_report_versions_report_id'), 'report_versions', ['report_id'], unique=False)
    op.create_table('role_permissions',
    sa.Column('role_id', sa.Uuid(), nullable=False),
    sa.Column('permission_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['permission_id'], ['permission_definitions.id'], name=op.f('fk_role_permissions_permission_id_permission_definitions'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['role_id'], ['role_definitions.id'], name=op.f('fk_role_permissions_role_id_role_definitions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_role_permissions')),
    sa.UniqueConstraint('role_id', 'permission_id', name='uq_role_permissions_pair')
    )
    op.create_index(op.f('ix_role_permissions_permission_id'), 'role_permissions', ['permission_id'], unique=False)
    op.create_index(op.f('ix_role_permissions_role_id'), 'role_permissions', ['role_id'], unique=False)
    op.create_table('upload_sessions',
    sa.Column('resource_type', sa.String(length=40), nullable=False),
    sa.Column('resource_id', sa.Uuid(), nullable=False),
    sa.Column('organization_unit_id', sa.Uuid(), nullable=False),
    sa.Column('object_key', sa.String(length=1024), nullable=False),
    sa.Column('filename', sa.String(length=255), nullable=False),
    sa.Column('mime_type', sa.String(length=120), nullable=False),
    sa.Column('size_bytes', sa.Integer(), nullable=False),
    sa.Column('client_sha256', sa.String(length=64), nullable=True),
    sa.Column('server_sha256', sa.String(length=64), nullable=True),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('pending', 'uploaded', 'verified', 'expired', 'failed')", name=op.f('ck_upload_sessions_upload_status_valid')),
    sa.CheckConstraint('size_bytes >= 0', name=op.f('ck_upload_sessions_upload_size_nonnegative')),
    sa.ForeignKeyConstraint(['organization_unit_id'], ['organization_units.id'], name=op.f('fk_upload_sessions_organization_unit_id_organization_units'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_upload_sessions')),
    sa.UniqueConstraint('object_key', name=op.f('uq_upload_sessions_object_key')),
    sa.UniqueConstraint('resource_type', 'resource_id', name='uq_upload_sessions_resource')
    )
    op.create_index(op.f('ix_upload_sessions_organization_unit_id'), 'upload_sessions', ['organization_unit_id'], unique=False)
    op.create_index(op.f('ix_upload_sessions_status'), 'upload_sessions', ['status'], unique=False)
    op.create_table('user_accounts',
    sa.Column('external_issuer', sa.String(length=300), nullable=False),
    sa.Column('external_subject', sa.String(length=200), nullable=False),
    sa.Column('username', sa.String(length=100), nullable=False),
    sa.Column('display_name', sa.String(length=200), nullable=False),
    sa.Column('organization_unit_id', sa.Uuid(), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('authz_version', sa.Integer(), nullable=False),
    sa.Column('attributes', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['organization_unit_id'], ['organization_units.id'], name=op.f('fk_user_accounts_organization_unit_id_organization_units'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_user_accounts')),
    sa.UniqueConstraint('external_issuer', 'external_subject', name='uq_user_accounts_external_identity'),
    sa.UniqueConstraint('username', name=op.f('uq_user_accounts_username'))
    )
    op.create_index('ix_user_accounts_organization_active', 'user_accounts', ['organization_unit_id', 'active'], unique=False)
    op.create_index(op.f('ix_user_accounts_organization_unit_id'), 'user_accounts', ['organization_unit_id'], unique=False)
    op.create_table('workflow_runs',
    sa.Column('workflow_type', sa.String(length=60), nullable=False),
    sa.Column('resource_type', sa.String(length=60), nullable=False),
    sa.Column('resource_id', sa.Uuid(), nullable=False),
    sa.Column('organization_unit_id', sa.Uuid(), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('current_node', sa.String(length=100), nullable=True),
    sa.Column('state_snapshot', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('thread_id', sa.String(length=200), nullable=False),
    sa.Column('schema_version', sa.Integer(), nullable=False),
    sa.Column('lock_version', sa.Integer(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('lock_version >= 1', name=op.f('ck_workflow_runs_workflow_lock_version_positive')),
    sa.CheckConstraint('schema_version >= 1', name=op.f('ck_workflow_runs_workflow_schema_version_positive')),
    sa.ForeignKeyConstraint(['organization_unit_id'], ['organization_units.id'], name=op.f('fk_workflow_runs_organization_unit_id_organization_units'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_workflow_runs')),
    sa.UniqueConstraint('thread_id', name=op.f('uq_workflow_runs_thread_id'))
    )
    op.create_index(op.f('ix_workflow_runs_organization_unit_id'), 'workflow_runs', ['organization_unit_id'], unique=False)
    op.create_index('ix_workflow_runs_status_updated', 'workflow_runs', ['status', 'updated_at'], unique=False)
    op.create_table('agent_offline_evaluations',
    sa.Column('requested_by', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('split', sa.String(length=20), nullable=False),
    sa.Column('dataset', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('results', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['requested_by'], ['user_accounts.id'], name=op.f('fk_agent_offline_evaluations_requested_by_user_accounts')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_agent_offline_evaluations'))
    )
    op.create_table('async_jobs',
    sa.Column('job_type', sa.String(length=60), nullable=False),
    sa.Column('resource_type', sa.String(length=60), nullable=False),
    sa.Column('resource_id', sa.Uuid(), nullable=False),
    sa.Column('organization_unit_id', sa.Uuid(), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('progress', sa.Integer(), nullable=False),
    sa.Column('message', sa.String(length=500), nullable=True),
    sa.Column('error_code', sa.String(length=100), nullable=True),
    sa.Column('error_detail', sa.Text(), nullable=True),
    sa.Column('cancel_requested', sa.Boolean(), nullable=False),
    sa.Column('attempt', sa.Integer(), nullable=False),
    sa.Column('parent_job_id', sa.Uuid(), nullable=True),
    sa.Column('config_snapshot', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('requested_by', sa.Uuid(), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('worker_id', sa.String(length=200), nullable=True),
    sa.Column('execution_token', sa.Uuid(), nullable=True),
    sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('lock_version', sa.Integer(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')", name=op.f('ck_async_jobs_async_job_status_valid')),
    sa.CheckConstraint('attempt >= 1', name=op.f('ck_async_jobs_async_job_attempt_positive')),
    sa.CheckConstraint('lock_version >= 1', name=op.f('ck_async_jobs_async_job_lock_version_positive')),
    sa.CheckConstraint('progress >= 0 AND progress <= 100', name=op.f('ck_async_jobs_async_job_progress_valid')),
    sa.ForeignKeyConstraint(['organization_unit_id'], ['organization_units.id'], name=op.f('fk_async_jobs_organization_unit_id_organization_units'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['parent_job_id'], ['async_jobs.id'], name=op.f('fk_async_jobs_parent_job_id_async_jobs'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['requested_by'], ['user_accounts.id'], name=op.f('fk_async_jobs_requested_by_user_accounts'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_async_jobs'))
    )
    op.create_index(op.f('ix_async_jobs_organization_unit_id'), 'async_jobs', ['organization_unit_id'], unique=False)
    op.create_index('ix_async_jobs_resource', 'async_jobs', ['resource_type', 'resource_id'], unique=False)
    op.create_index('ix_async_jobs_status_updated', 'async_jobs', ['status', 'updated_at'], unique=False)
    op.create_index('uq_async_jobs_active_resource', 'async_jobs', ['job_type', 'resource_type', 'resource_id'], unique=True, postgresql_where=sa.text("status IN ('queued', 'running')"), sqlite_where=sa.text("status IN ('queued', 'running')"))
    op.create_table('audio_records',
    sa.Column('organization_unit_id', sa.Uuid(), nullable=False),
    sa.Column('shift_date', sa.Date(), nullable=False),
    sa.Column('shift_code', sa.String(length=40), nullable=False),
    sa.Column('filename', sa.String(length=255), nullable=False),
    sa.Column('object_key', sa.String(length=1024), nullable=False),
    sa.Column('mime_type', sa.String(length=120), nullable=False),
    sa.Column('size_bytes', sa.Integer(), nullable=False),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.Column('language', sa.String(length=20), nullable=True),
    sa.Column('upload_status', sa.String(length=32), nullable=False),
    sa.Column('business_status', sa.String(length=32), nullable=False),
    sa.Column('current_transcript_version_id', sa.Uuid(), nullable=True),
    sa.Column('current_summary_version_id', sa.Uuid(), nullable=True),
    sa.Column('confirmed_by', sa.Uuid(), nullable=True),
    sa.Column('deleted', sa.Boolean(), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("business_status IN ('draft', 'transcribed', 'summary_ready', 'confirmed')", name=op.f('ck_audio_records_audio_business_status_valid')),
    sa.CheckConstraint("upload_status IN ('pending', 'verified', 'failed')", name=op.f('ck_audio_records_audio_upload_status_valid')),
    sa.CheckConstraint('duration_ms IS NULL OR duration_ms > 0', name=op.f('ck_audio_records_audio_duration_positive')),
    sa.CheckConstraint('size_bytes > 0', name=op.f('ck_audio_records_audio_record_size_positive')),
    sa.ForeignKeyConstraint(['confirmed_by'], ['user_accounts.id'], name='fk_audio_confirmed_by', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['created_by'], ['user_accounts.id'], name='fk_audio_created_by', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['current_summary_version_id'], ['handover_summary_versions.id'], name='fk_audio_current_summary', ondelete='RESTRICT', use_alter=True),
    sa.ForeignKeyConstraint(['current_transcript_version_id'], ['audio_transcript_versions.id'], name='fk_audio_current_transcript', ondelete='RESTRICT', use_alter=True),
    sa.ForeignKeyConstraint(['organization_unit_id'], ['organization_units.id'], name='fk_audio_org', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audio_records')),
    sa.UniqueConstraint('object_key', name=op.f('uq_audio_records_object_key'))
    )
    op.create_index('ix_audio_records_org_shift', 'audio_records', ['organization_unit_id', 'shift_date'], unique=False)
    op.create_table('audit_logs',
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('actor_id', sa.Uuid(), nullable=False),
    sa.Column('organization_unit_id', sa.Uuid(), nullable=False),
    sa.Column('action', sa.String(length=120), nullable=False),
    sa.Column('resource_type', sa.String(length=60), nullable=False),
    sa.Column('resource_id', sa.Uuid(), nullable=False),
    sa.Column('before_data', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('after_data', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('request_id', sa.String(length=100), nullable=True),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('previous_hash', sa.String(length=64), nullable=True),
    sa.Column('entry_hash', sa.String(length=64), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['actor_id'], ['user_accounts.id'], name=op.f('fk_audit_logs_actor_id_user_accounts'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['organization_unit_id'], ['organization_units.id'], name=op.f('fk_audit_logs_organization_unit_id_organization_units'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audit_logs')),
    sa.UniqueConstraint('entry_hash', name=op.f('uq_audit_logs_entry_hash'))
    )
    op.create_index(op.f('ix_audit_logs_actor_id'), 'audit_logs', ['actor_id'], unique=False)
    op.create_index(op.f('ix_audit_logs_organization_unit_id'), 'audit_logs', ['organization_unit_id'], unique=False)
    op.create_index('ix_audit_logs_resource', 'audit_logs', ['resource_type', 'resource_id', 'occurred_at'], unique=False)
    op.create_table('business_config_versions',
    sa.Column('config_code', sa.String(length=100), nullable=False),
    sa.Column('config_type', sa.String(length=40), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('payload', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('version >= 1', name=op.f('ck_business_config_versions_business_config_version_positive')),
    sa.ForeignKeyConstraint(['created_by'], ['user_accounts.id'], name=op.f('fk_business_config_versions_created_by_user_accounts'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_business_config_versions')),
    sa.UniqueConstraint('config_code', 'version', name='uq_business_config_version')
    )
    op.create_index(op.f('ix_business_config_versions_config_code'), 'business_config_versions', ['config_code'], unique=False)
    op.create_index('uq_business_config_active', 'business_config_versions', ['config_code'], unique=True, postgresql_where=sa.text('active'), sqlite_where=sa.text('active = 1'))
    op.create_table('case_observations',
    sa.Column('event_id', sa.Uuid(), nullable=False),
    sa.Column('event_version_id', sa.Uuid(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('confirmed_by', sa.Uuid(), nullable=True),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('pending','confirmed','retracted')", name=op.f('ck_case_observations_observation_status')),
    sa.ForeignKeyConstraint(['confirmed_by'], ['user_accounts.id'], name=op.f('fk_case_observations_confirmed_by_user_accounts')),
    sa.ForeignKeyConstraint(['created_by'], ['user_accounts.id'], name=op.f('fk_case_observations_created_by_user_accounts')),
    sa.ForeignKeyConstraint(['event_id'], ['production_events.id'], name=op.f('fk_case_observations_event_id_production_events')),
    sa.ForeignKeyConstraint(['event_version_id'], ['production_event_versions.id'], name=op.f('fk_case_observations_event_version_id_production_event_versions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_case_observations'))
    )
    op.create_index(op.f('ix_case_observations_event_id'), 'case_observations', ['event_id'], unique=False)
    op.create_table('evaluation_improvements',
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('assignee_id', sa.Uuid(), nullable=True),
    sa.Column('reviewer_id', sa.Uuid(), nullable=True),
    sa.Column('due_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('problem', sa.Text(), nullable=False),
    sa.Column('measures', sa.Text(), nullable=False),
    sa.Column('baseline_id', sa.Uuid(), nullable=False),
    sa.Column('baseline', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('targets', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('history', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind IN ('rag','agent')", name=op.f('ck_evaluation_improvements_improvement_kind')),
    sa.CheckConstraint("status IN ('confirmed','implemented','retested','closed')", name=op.f('ck_evaluation_improvements_improvement_status')),
    sa.ForeignKeyConstraint(['assignee_id'], ['user_accounts.id'], name=op.f('fk_evaluation_improvements_assignee_id_user_accounts')),
    sa.ForeignKeyConstraint(['created_by'], ['user_accounts.id'], name=op.f('fk_evaluation_improvements_created_by_user_accounts')),
    sa.ForeignKeyConstraint(['reviewer_id'], ['user_accounts.id'], name=op.f('fk_evaluation_improvements_reviewer_id_user_accounts')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_evaluation_improvements'))
    )
    op.create_index(op.f('ix_evaluation_improvements_assignee_id'), 'evaluation_improvements', ['assignee_id'], unique=False)
    op.create_index(op.f('ix_evaluation_improvements_baseline_id'), 'evaluation_improvements', ['baseline_id'], unique=False)
    op.create_index(op.f('ix_evaluation_improvements_created_by'), 'evaluation_improvements', ['created_by'], unique=False)
    op.create_index(op.f('ix_evaluation_improvements_due_at'), 'evaluation_improvements', ['due_at'], unique=False)
    op.create_index(op.f('ix_evaluation_improvements_reviewer_id'), 'evaluation_improvements', ['reviewer_id'], unique=False)
    op.create_table('failure_modes',
    sa.Column('organization_unit_id', sa.Uuid(), nullable=False),
    sa.Column('code', sa.String(length=100), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['created_by'], ['user_accounts.id'], name=op.f('fk_failure_modes_created_by_user_accounts')),
    sa.ForeignKeyConstraint(['organization_unit_id'], ['organization_units.id'], name=op.f('fk_failure_modes_organization_unit_id_organization_units')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_failure_modes')),
    sa.UniqueConstraint('organization_unit_id', 'code', name=op.f('uq_failure_modes_organization_unit_id'))
    )
    op.create_index(op.f('ix_failure_modes_organization_unit_id'), 'failure_modes', ['organization_unit_id'], unique=False)
    op.create_table('hazards',
    sa.Column('organization_unit_id', sa.Uuid(), nullable=False),
    sa.Column('hazard_no', sa.String(length=60), nullable=False),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('location', sa.String(length=300), nullable=False),
    sa.Column('category', sa.String(length=80), nullable=False),
    sa.Column('source_type', sa.String(length=40), nullable=False),
    sa.Column('source_id', sa.Uuid(), nullable=True),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('risk_level', sa.String(length=20), nullable=True),
    sa.Column('likelihood', sa.Integer(), nullable=True),
    sa.Column('consequence', sa.Integer(), nullable=True),
    sa.Column('risk_score', sa.Integer(), nullable=True),
    sa.Column('control_measures', sa.Text(), nullable=True),
    sa.Column('assignee_id', sa.Uuid(), nullable=True),
    sa.Column('due_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('closed_by', sa.Uuid(), nullable=True),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("risk_level IS NULL OR risk_level IN ('low','medium','high','major')", name=op.f('ck_hazards_hazard_risk_level')),
    sa.CheckConstraint("status IN ('pending_assessment','pending_rectification','rectifying','pending_acceptance','closed','cancelled')", name=op.f('ck_hazards_hazard_status')),
    sa.CheckConstraint('consequence IS NULL OR consequence BETWEEN 1 AND 5', name=op.f('ck_hazards_hazard_consequence_range')),
    sa.CheckConstraint('likelihood IS NULL OR likelihood BETWEEN 1 AND 5', name=op.f('ck_hazards_hazard_likelihood_range')),
    sa.CheckConstraint('version >= 1', name=op.f('ck_hazards_hazard_version_positive')),
    sa.ForeignKeyConstraint(['assignee_id'], ['user_accounts.id'], name=op.f('fk_hazards_assignee_id_user_accounts')),
    sa.ForeignKeyConstraint(['closed_by'], ['user_accounts.id'], name=op.f('fk_hazards_closed_by_user_accounts')),
    sa.ForeignKeyConstraint(['created_by'], ['user_accounts.id'], name=op.f('fk_hazards_created_by_user_accounts')),
    sa.ForeignKeyConstraint(['organization_unit_id'], ['organization_units.id'], name=op.f('fk_hazards_organization_unit_id_organization_units')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_hazards')),
    sa.UniqueConstraint('hazard_no', name=op.f('uq_hazards_hazard_no'))
    )
    op.create_index('ix_hazards_org_status_due', 'hazards', ['organization_unit_id', 'status', 'due_at'], unique=False)
    op.create_index('ix_hazards_source', 'hazards', ['source_type', 'source_id'], unique=False)
    op.create_table('idempotency_records',
    sa.Column('actor_id', sa.Uuid(), nullable=False),
    sa.Column('operation', sa.String(length=120), nullable=False),
    sa.Column('idempotency_key', sa.String(length=200), nullable=False),
    sa.Column('request_digest', sa.String(length=64), nullable=False),
    sa.Column('response_status', sa.Integer(), nullable=True),
    sa.Column('response_body', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('response_status IS NULL OR (response_status >= 100 AND response_status <= 599)', name=op.f('ck_idempotency_records_idempotency_response_status_valid')),
    sa.ForeignKeyConstraint(['actor_id'], ['user_accounts.id'], name=op.f('fk_idempotency_records_actor_id_user_accounts'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_idempotency_records')),
    sa.UniqueConstraint('actor_id', 'operation', 'idempotency_key', name='uq_idempotency_key')
    )
    op.create_table('inspection_findings',
    sa.Column('inspection_id', sa.Uuid(), nullable=False),
    sa.Column('image_id', sa.Uuid(), nullable=False),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('category', sa.String(length=80), nullable=False),
    sa.Column('severity', sa.String(length=20), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('evidence', sa.Text(), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('review_status', sa.String(length=32), nullable=False),
    sa.Column('reviewed_by', sa.Uuid(), nullable=True),
    sa.Column('review_reason', sa.Text(), nullable=True),
    sa.Column('workflow_event_id', sa.Uuid(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['image_id'], ['inspection_images.id'], name=op.f('fk_inspection_findings_image_id_inspection_images'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['inspection_id'], ['inspection_records.id'], name=op.f('fk_inspection_findings_inspection_id_inspection_records'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['workflow_event_id'], ['production_events.id'], name=op.f('fk_inspection_findings_workflow_event_id_production_events'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_inspection_findings')),
    sa.UniqueConstraint('workflow_event_id', name=op.f('uq_inspection_findings_workflow_event_id'))
    )
    op.create_index('ix_findings_inspection_status', 'inspection_findings', ['inspection_id', 'review_status'], unique=False)
    op.create_table('knowledge_bases',
    sa.Column('organization_unit_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('reader_ids', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('acl_version', sa.Integer(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['created_by'], ['user_accounts.id'], name=op.f('fk_knowledge_bases_created_by_user_accounts')),
    sa.ForeignKeyConstraint(['organization_unit_id'], ['organization_units.id'], name=op.f('fk_knowledge_bases_organization_unit_id_organization_units')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_knowledge_bases'))
    )
    op.create_index(op.f('ix_knowledge_bases_organization_unit_id'), 'knowledge_bases', ['organization_unit_id'], unique=False)
    op.create_table('knowledge_conversations',
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['created_by'], ['user_accounts.id'], name=op.f('fk_knowledge_conversations_created_by_user_accounts')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_knowledge_conversations'))
    )
    op.create_index(op.f('ix_knowledge_conversations_created_by'), 'knowledge_conversations', ['created_by'], unique=False)
    op.create_table('manual_operation_records',
    sa.Column('organization_unit_id', sa.Uuid(), nullable=False),
    sa.Column('occurred_at', sa.String(length=40), nullable=False),
    sa.Column('record_type', sa.String(length=60), nullable=False),
    sa.Column('current_version_id', sa.Uuid(), nullable=True),
    sa.Column('business_status', sa.String(length=32), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("business_status = 'confirmed'", name=op.f('ck_manual_operation_records_manual_operation_status_valid')),
    sa.ForeignKeyConstraint(['created_by'], ['user_accounts.id'], name='fk_manual_operation_creator', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['current_version_id'], ['manual_operation_record_versions.id'], name='fk_manual_operation_current_version', ondelete='RESTRICT', use_alter=True),
    sa.ForeignKeyConstraint(['organization_unit_id'], ['organization_units.id'], name='fk_manual_operation_org', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_manual_operation_records'))
    )
    op.create_index('ix_manual_operation_org_time', 'manual_operation_records', ['organization_unit_id', 'occurred_at'], unique=False)
    op.create_table('meetings',
    sa.Column('organization_unit_id', sa.Uuid(), nullable=False),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('location', sa.String(length=300), nullable=True),
    sa.Column('filename', sa.String(length=255), nullable=False),
    sa.Column('object_key', sa.String(length=1024), nullable=False),
    sa.Column('mime_type', sa.String(length=120), nullable=False),
    sa.Column('size_bytes', sa.Integer(), nullable=False),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.Column('upload_status', sa.String(length=32), nullable=False),
    sa.Column('recording_deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('recording_delete_reason', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('current_transcript_version_id', sa.Uuid(), nullable=True),
    sa.Column('reader_ids', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('acl_version', sa.Integer(), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('published_by', sa.Uuid(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('draft','transcribed','published','withdrawn')", name=op.f('ck_meetings_meeting_status')),
    sa.CheckConstraint("upload_status IN ('pending','verified','failed','deleting','deleted')", name=op.f('ck_meetings_meeting_upload_status')),
    sa.CheckConstraint('duration_ms IS NULL OR duration_ms > 0', name=op.f('ck_meetings_meeting_duration_positive')),
    sa.CheckConstraint('size_bytes > 0', name=op.f('ck_meetings_meeting_size_positive')),
    sa.ForeignKeyConstraint(['created_by'], ['user_accounts.id'], name=op.f('fk_meetings_created_by_user_accounts')),
    sa.ForeignKeyConstraint(['current_transcript_version_id'], ['meeting_transcript_versions.id'], name='fk_meeting_current_transcript', use_alter=True),
    sa.ForeignKeyConstraint(['organization_unit_id'], ['organization_units.id'], name=op.f('fk_meetings_organization_unit_id_organization_units')),
    sa.ForeignKeyConstraint(['published_by'], ['user_accounts.id'], name=op.f('fk_meetings_published_by_user_accounts')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_meetings')),
    sa.UniqueConstraint('object_key', name=op.f('uq_meetings_object_key'))
    )
    op.create_index('ix_meetings_org_started', 'meetings', ['organization_unit_id', 'started_at'], unique=False)
    op.create_index(op.f('ix_meetings_organization_unit_id'), 'meetings', ['organization_unit_id'], unique=False)
    op.create_table('operational_stations',
    sa.Column('organization_unit_id', sa.Uuid(), nullable=False),
    sa.Column('code', sa.String(length=100), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['created_by'], ['user_accounts.id'], name=op.f('fk_operational_stations_created_by_user_accounts')),
    sa.ForeignKeyConstraint(['organization_unit_id'], ['organization_units.id'], name=op.f('fk_operational_stations_organization_unit_id_organization_units')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_operational_stations')),
    sa.UniqueConstraint('organization_unit_id', 'code', name=op.f('uq_operational_stations_organization_unit_id'))
    )
    op.create_index(op.f('ix_operational_stations_organization_unit_id'), 'operational_stations', ['organization_unit_id'], unique=False)
    op.create_table('prompt_template_versions',
    sa.Column('template_code', sa.String(length=100), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('system_prompt', sa.Text(), nullable=False),
    sa.Column('user_template', sa.Text(), nullable=False),
    sa.Column('output_schema', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('version >= 1', name=op.f('ck_prompt_template_versions_prompt_version_positive')),
    sa.ForeignKeyConstraint(['created_by'], ['user_accounts.id'], name=op.f('fk_prompt_template_versions_created_by_user_accounts'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_prompt_template_versions')),
    sa.UniqueConstraint('template_code', 'version', name='uq_prompt_template_version')
    )
    op.create_index(op.f('ix_prompt_template_versions_template_code'), 'prompt_template_versions', ['template_code'], unique=False)
    op.create_index('uq_prompt_template_active', 'prompt_template_versions', ['template_code'], unique=True, postgresql_where=sa.text('active'), sqlite_where=sa.text('active = 1'))
    op.create_table('report_exports',
    sa.Column('report_id', sa.Uuid(), nullable=False),
    sa.Column('report_version_id', sa.Uuid(), nullable=False),
    sa.Column('export_format', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('object_key', sa.String(length=1024), nullable=True),
    sa.Column('error_code', sa.String(length=100), nullable=True),
    sa.Column('requested_by', sa.Uuid(), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['report_id'], ['operation_reports.id'], name=op.f('fk_report_exports_report_id_operation_reports'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['report_version_id'], ['report_versions.id'], name=op.f('fk_report_exports_report_version_id_report_versions'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_report_exports'))
    )
    op.create_index('ix_report_exports_report_status', 'report_exports', ['report_id', 'status'], unique=False)
    op.create_table('report_sources',
    sa.Column('report_version_id', sa.Uuid(), nullable=False),
    sa.Column('source_type', sa.String(length=60), nullable=False),
    sa.Column('source_id', sa.Uuid(), nullable=False),
    sa.Column('source_version_id', sa.Uuid(), nullable=True),
    sa.Column('snapshot', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['report_version_id'], ['report_versions.id'], name=op.f('fk_report_sources_report_version_id_report_versions'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_report_sources')),
    sa.UniqueConstraint('report_version_id', 'source_type', 'source_id', name='uq_report_source')
    )
    op.create_index(op.f('ix_report_sources_report_version_id'), 'report_sources', ['report_version_id'], unique=False)
    op.create_table('role_assignments',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('role_id', sa.Uuid(), nullable=False),
    sa.Column('scope_type', sa.String(length=32), nullable=False),
    sa.Column('data_scope', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('scope_digest', sa.String(length=64), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('effective_from', sa.DateTime(timezone=True), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('assigned_by', sa.Uuid(), nullable=True),
    sa.Column('grant_reason', sa.Text(), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_by', sa.Uuid(), nullable=True),
    sa.Column('revoke_reason', sa.Text(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['assigned_by'], ['user_accounts.id'], name=op.f('fk_role_assignments_assigned_by_user_accounts'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['revoked_by'], ['user_accounts.id'], name=op.f('fk_role_assignments_revoked_by_user_accounts'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['role_id'], ['role_definitions.id'], name=op.f('fk_role_assignments_role_id_role_definitions'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['user_id'], ['user_accounts.id'], name=op.f('fk_role_assignments_user_id_user_accounts'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_role_assignments'))
    )
    op.create_index('ix_role_assignments_user_active', 'role_assignments', ['user_id', 'active'], unique=False)
    op.create_index('ix_role_assignments_validity', 'role_assignments', ['active', 'effective_from', 'expires_at'], unique=False)
    op.create_table('work_orders',
    sa.Column('organization_unit_id', sa.Uuid(), nullable=False),
    sa.Column('event_id', sa.Uuid(), nullable=True),
    sa.Column('source_workflow_run_id', sa.Uuid(), nullable=False),
    sa.Column('order_no', sa.String(length=60), nullable=False),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('risk_level', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('assignee_id', sa.Uuid(), nullable=True),
    sa.Column('due_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['assignee_id'], ['user_accounts.id'], name=op.f('fk_work_orders_assignee_id_user_accounts')),
    sa.ForeignKeyConstraint(['event_id'], ['production_events.id'], name=op.f('fk_work_orders_event_id_production_events'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['source_workflow_run_id'], ['workflow_runs.id'], name=op.f('fk_work_orders_source_workflow_run_id_workflow_runs'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_work_orders')),
    sa.UniqueConstraint('order_no', name=op.f('uq_work_orders_order_no')),
    sa.UniqueConstraint('source_workflow_run_id', name='uq_work_order_workflow')
    )
    op.create_index('ix_work_orders_org_status', 'work_orders', ['organization_unit_id', 'status'], unique=False)
    op.create_table('agent_runs',
    sa.Column('event_id', sa.Uuid(), nullable=False),
    sa.Column('event_version_id', sa.Uuid(), nullable=False),
    sa.Column('job_id', sa.Uuid(), nullable=True),
    sa.Column('organization_unit_id', sa.Uuid(), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('current_stage', sa.String(length=40), nullable=False),
    sa.Column('config_snapshot', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('result', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('error_detail', sa.Text(), nullable=True),
    sa.Column('cancel_requested', sa.Boolean(), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('trace_id', sa.Uuid(), nullable=False),
    sa.Column('input_tokens', sa.Integer(), nullable=False),
    sa.Column('output_tokens', sa.Integer(), nullable=False),
    sa.Column('cost_microusd', sa.Integer(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['event_id'], ['production_events.id'], name=op.f('fk_agent_runs_event_id_production_events'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['event_version_id'], ['production_event_versions.id'], name=op.f('fk_agent_runs_event_version_id_production_event_versions'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['job_id'], ['async_jobs.id'], name=op.f('fk_agent_runs_job_id_async_jobs'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_agent_runs')),
    sa.UniqueConstraint('job_id', name=op.f('uq_agent_runs_job_id'))
    )
    op.create_index('ix_agent_runs_event_status', 'agent_runs', ['event_id', 'status'], unique=False)
    op.create_index('ix_agent_runs_org_created', 'agent_runs', ['organization_unit_id', 'created_at'], unique=False)
    op.create_table('ai_call_logs',
    sa.Column('run_id', sa.Uuid(), nullable=True),
    sa.Column('capability', sa.String(length=32), nullable=True),
    sa.Column('job_id', sa.Uuid(), nullable=False),
    sa.Column('provider', sa.String(length=40), nullable=False),
    sa.Column('model_alias', sa.String(length=100), nullable=False),
    sa.Column('model_name', sa.String(length=200), nullable=False),
    sa.Column('provider_request_id', sa.String(length=200), nullable=True),
    sa.Column('duration_ms', sa.Integer(), nullable=False),
    sa.Column('input_units', sa.Integer(), nullable=True),
    sa.Column('output_units', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('error_code', sa.String(length=100), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('succeeded', 'failed', 'timeout', 'cancelled')", name=op.f('ck_ai_call_logs_ai_call_status_valid')),
    sa.CheckConstraint('duration_ms >= 0', name=op.f('ck_ai_call_logs_ai_call_duration_nonnegative')),
    sa.CheckConstraint('input_units IS NULL OR input_units >= 0', name=op.f('ck_ai_call_logs_ai_call_input_nonnegative')),
    sa.CheckConstraint('output_units IS NULL OR output_units >= 0', name=op.f('ck_ai_call_logs_ai_call_output_nonnegative')),
    sa.ForeignKeyConstraint(['job_id'], ['async_jobs.id'], name=op.f('fk_ai_call_logs_job_id_async_jobs'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_ai_call_logs'))
    )
    op.create_index('ix_ai_call_logs_job', 'ai_call_logs', ['job_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_ai_call_logs_run_id'), 'ai_call_logs', ['run_id'], unique=False)
    op.create_table('audio_transcript_versions',
    sa.Column('audio_record_id', sa.Uuid(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('full_text', sa.Text(), nullable=False),
    sa.Column('language', sa.String(length=20), nullable=True),
    sa.Column('provider_request_id', sa.String(length=200), nullable=True),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("source IN ('ai', 'manual')", name=op.f('ck_audio_transcript_versions_audio_transcript_source_valid')),
    sa.CheckConstraint('version >= 1', name=op.f('ck_audio_transcript_versions_audio_transcript_version_positive')),
    sa.ForeignKeyConstraint(['audio_record_id'], ['audio_records.id'], name=op.f('fk_audio_transcript_versions_audio_record_id_audio_records'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['created_by'], ['user_accounts.id'], name='fk_audio_transcript_creator', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audio_transcript_versions')),
    sa.UniqueConstraint('audio_record_id', 'version', name='uq_audio_transcript_version')
    )
    op.create_index(op.f('ix_audio_transcript_versions_audio_record_id'), 'audio_transcript_versions', ['audio_record_id'], unique=False)
    op.create_table('experience_memories',
    sa.Column('organization_unit_id', sa.Uuid(), nullable=False),
    sa.Column('primary_order_id', sa.Uuid(), nullable=False),
    sa.Column('job_id', sa.Uuid(), nullable=True),
    sa.Column('parent_id', sa.Uuid(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('content', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('search_text', sa.Text(), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('applicability_key', sa.String(length=64), nullable=False),
    sa.Column('source_orders', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('dependency_ids', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('supersedes_ids', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('checks', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('reviewed_by', sa.Uuid(), nullable=True),
    sa.Column('review_reason', sa.Text(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('pending','approved','rejected','withdrawn','superseded')", name=op.f('ck_experience_memories_experience_status')),
    sa.ForeignKeyConstraint(['created_by'], ['user_accounts.id'], name=op.f('fk_experience_memories_created_by_user_accounts')),
    sa.ForeignKeyConstraint(['job_id'], ['async_jobs.id'], name=op.f('fk_experience_memories_job_id_async_jobs')),
    sa.ForeignKeyConstraint(['organization_unit_id'], ['organization_units.id'], name=op.f('fk_experience_memories_organization_unit_id_organization_units')),
    sa.ForeignKeyConstraint(['parent_id'], ['experience_memories.id'], name=op.f('fk_experience_memories_parent_id_experience_memories')),
    sa.ForeignKeyConstraint(['primary_order_id'], ['work_orders.id'], name=op.f('fk_experience_memories_primary_order_id_work_orders')),
    sa.ForeignKeyConstraint(['reviewed_by'], ['user_accounts.id'], name=op.f('fk_experience_memories_reviewed_by_user_accounts')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_experience_memories')),
    sa.UniqueConstraint('job_id', name=op.f('uq_experience_memories_job_id'))
    )
    op.create_index(op.f('ix_experience_memories_applicability_key'), 'experience_memories', ['applicability_key'], unique=False)
    op.create_index(op.f('ix_experience_memories_content_hash'), 'experience_memories', ['content_hash'], unique=False)
    op.create_index(op.f('ix_experience_memories_expires_at'), 'experience_memories', ['expires_at'], unique=False)
    op.create_index(op.f('ix_experience_memories_organization_unit_id'), 'experience_memories', ['organization_unit_id'], unique=False)
    op.create_index(op.f('ix_experience_memories_primary_order_id'), 'experience_memories', ['primary_order_id'], unique=False)
    op.create_index(op.f('ix_experience_memories_status'), 'experience_memories', ['status'], unique=False)
    op.create_table('hazard_assessments',
    sa.Column('hazard_id', sa.Uuid(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('likelihood', sa.Integer(), nullable=False),
    sa.Column('consequence', sa.Integer(), nullable=False),
    sa.Column('risk_score', sa.Integer(), nullable=False),
    sa.Column('risk_level', sa.String(length=20), nullable=False),
    sa.Column('rationale', sa.Text(), nullable=False),
    sa.Column('assessed_by', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('consequence BETWEEN 1 AND 5', name=op.f('ck_hazard_assessments_assessment_consequence_range')),
    sa.CheckConstraint('likelihood BETWEEN 1 AND 5', name=op.f('ck_hazard_assessments_assessment_likelihood_range')),
    sa.ForeignKeyConstraint(['assessed_by'], ['user_accounts.id'], name=op.f('fk_hazard_assessments_assessed_by_user_accounts')),
    sa.ForeignKeyConstraint(['hazard_id'], ['hazards.id'], name=op.f('fk_hazard_assessments_hazard_id_hazards'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_hazard_assessments')),
    sa.UniqueConstraint('hazard_id', 'version', name='uq_hazard_assessment_version')
    )
    op.create_index(op.f('ix_hazard_assessments_hazard_id'), 'hazard_assessments', ['hazard_id'], unique=False)
    op.create_table('hazard_attachments',
    sa.Column('hazard_id', sa.Uuid(), nullable=False),
    sa.Column('object_key', sa.String(length=1024), nullable=False),
    sa.Column('filename', sa.String(length=255), nullable=False),
    sa.Column('mime_type', sa.String(length=120), nullable=False),
    sa.Column('size_bytes', sa.Integer(), nullable=False),
    sa.Column('uploaded_by', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['hazard_id'], ['hazards.id'], name=op.f('fk_hazard_attachments_hazard_id_hazards')),
    sa.ForeignKeyConstraint(['uploaded_by'], ['user_accounts.id'], name=op.f('fk_hazard_attachments_uploaded_by_user_accounts')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_hazard_attachments')),
    sa.UniqueConstraint('object_key', name=op.f('uq_hazard_attachments_object_key'))
    )
    op.create_index(op.f('ix_hazard_attachments_hazard_id'), 'hazard_attachments', ['hazard_id'], unique=False)
    op.create_table('hazard_rectifications',
    sa.Column('hazard_version', sa.Integer(), nullable=True),
    sa.Column('measurements', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('attachment_ids', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('hazard_id', sa.Uuid(), nullable=False),
    sa.Column('action', sa.Text(), nullable=False),
    sa.Column('result', sa.Text(), nullable=False),
    sa.Column('submitted_by', sa.Uuid(), nullable=False),
    sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['hazard_id'], ['hazards.id'], name=op.f('fk_hazard_rectifications_hazard_id_hazards'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['submitted_by'], ['user_accounts.id'], name=op.f('fk_hazard_rectifications_submitted_by_user_accounts')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_hazard_rectifications'))
    )
    op.create_index('ix_hazard_rectification_time', 'hazard_rectifications', ['hazard_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_hazard_rectifications_hazard_id'), 'hazard_rectifications', ['hazard_id'], unique=False)
    op.create_table('hazard_transitions',
    sa.Column('hazard_id', sa.Uuid(), nullable=False),
    sa.Column('from_status', sa.String(length=32), nullable=False),
    sa.Column('to_status', sa.String(length=32), nullable=False),
    sa.Column('actor_id', sa.Uuid(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('version_after', sa.Integer(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['actor_id'], ['user_accounts.id'], name=op.f('fk_hazard_transitions_actor_id_user_accounts')),
    sa.ForeignKeyConstraint(['hazard_id'], ['hazards.id'], name=op.f('fk_hazard_transitions_hazard_id_hazards'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_hazard_transitions'))
    )
    op.create_index('ix_hazard_transition_time', 'hazard_transitions', ['hazard_id', 'occurred_at'], unique=False)
    op.create_table('hazard_work_order_links',
    sa.Column('hazard_id', sa.Uuid(), nullable=False),
    sa.Column('work_order_id', sa.Uuid(), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['created_by'], ['user_accounts.id'], name=op.f('fk_hazard_work_order_links_created_by_user_accounts')),
    sa.ForeignKeyConstraint(['hazard_id'], ['hazards.id'], name=op.f('fk_hazard_work_order_links_hazard_id_hazards'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['work_order_id'], ['work_orders.id'], name=op.f('fk_hazard_work_order_links_work_order_id_work_orders'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_hazard_work_order_links')),
    sa.UniqueConstraint('hazard_id', 'work_order_id', name='uq_hazard_work_order_link')
    )
    op.create_index(op.f('ix_hazard_work_order_links_hazard_id'), 'hazard_work_order_links', ['hazard_id'], unique=False)
    op.create_index(op.f('ix_hazard_work_order_links_work_order_id'), 'hazard_work_order_links', ['work_order_id'], unique=False)
    op.create_table('inspection_finding_links',
    sa.Column('finding_id', sa.Uuid(), nullable=False),
    sa.Column('target_type', sa.String(length=40), nullable=False),
    sa.Column('target_id', sa.Uuid(), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('revoked_by', sa.Uuid(), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoke_reason', sa.Text(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['finding_id'], ['inspection_findings.id'], name=op.f('fk_inspection_finding_links_finding_id_inspection_findings'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_inspection_finding_links')),
    sa.UniqueConstraint('finding_id', 'target_type', 'target_id', 'active', name='uq_finding_link')
    )
    op.create_table('job_events',
    sa.Column('job_id', sa.Uuid(), nullable=False),
    sa.Column('event_type', sa.String(length=40), nullable=False),
    sa.Column('payload', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['job_id'], ['async_jobs.id'], name=op.f('fk_job_events_job_id_async_jobs'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_job_events'))
    )
    op.create_index('ix_job_events_job_created', 'job_events', ['job_id', 'created_at'], unique=False)
    op.create_table('knowledge_documents',
    sa.Column('base_id', sa.Uuid(), nullable=False),
    sa.Column('code', sa.String(length=120), nullable=False),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('kind', sa.String(length=40), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('reader_ids', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('acl_version', sa.Integer(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['base_id'], ['knowledge_bases.id'], name=op.f('fk_knowledge_documents_base_id_knowledge_bases')),
    sa.ForeignKeyConstraint(['created_by'], ['user_accounts.id'], name=op.f('fk_knowledge_documents_created_by_user_accounts')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_knowledge_documents')),
    sa.UniqueConstraint('base_id', 'code', name='uq_knowledge_document_code')
    )
    op.create_index(op.f('ix_knowledge_documents_base_id'), 'knowledge_documents', ['base_id'], unique=False)
    op.create_table('knowledge_evaluations',
    sa.Column('requested_by', sa.Uuid(), nullable=False),
    sa.Column('job_id', sa.Uuid(), nullable=True),
    sa.Column('dataset', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('options', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('results', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['job_id'], ['async_jobs.id'], name=op.f('fk_knowledge_evaluations_job_id_async_jobs')),
    sa.ForeignKeyConstraint(['requested_by'], ['user_accounts.id'], name=op.f('fk_knowledge_evaluations_requested_by_user_accounts')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_knowledge_evaluations'))
    )
    op.create_table('knowledge_runs',
    sa.Column('conversation_id', sa.Uuid(), nullable=True),
    sa.Column('requested_by', sa.Uuid(), nullable=False),
    sa.Column('job_id', sa.Uuid(), nullable=True),
    sa.Column('question', sa.Text(), nullable=False),
    sa.Column('options', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('answer', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('trace', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('next_sequence', sa.Integer(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['knowledge_conversations.id'], name=op.f('fk_knowledge_runs_conversation_id_knowledge_conversations')),
    sa.ForeignKeyConstraint(['job_id'], ['async_jobs.id'], name=op.f('fk_knowledge_runs_job_id_async_jobs')),
    sa.ForeignKeyConstraint(['requested_by'], ['user_accounts.id'], name=op.f('fk_knowledge_runs_requested_by_user_accounts')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_knowledge_runs'))
    )
    op.create_index(op.f('ix_knowledge_runs_requested_by'), 'knowledge_runs', ['requested_by'], unique=False)
    op.create_table('manual_operation_record_versions',
    sa.Column('record_id', sa.Uuid(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('structured_data', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('version >= 1', name=op.f('ck_manual_operation_record_versions_version_positive')),
    sa.ForeignKeyConstraint(['created_by'], ['user_accounts.id'], name='fk_manual_operation_version_creator', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['record_id'], ['manual_operation_records.id'], name=op.f('fk_manual_operation_record_versions_record_id_manual_operation_records'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_manual_operation_record_versions')),
    sa.UniqueConstraint('record_id', 'version', name='uq_manual_operation_record_version')
    )
    op.create_table('meeting_participants',
    sa.Column('meeting_id', sa.Uuid(), nullable=False),
    sa.Column('participant_key', sa.String(length=120), nullable=False),
    sa.Column('display_name', sa.String(length=200), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=True),
    sa.Column('role', sa.String(length=100), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], name=op.f('fk_meeting_participants_meeting_id_meetings')),
    sa.ForeignKeyConstraint(['user_id'], ['user_accounts.id'], name=op.f('fk_meeting_participants_user_id_user_accounts')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_meeting_participants')),
    sa.UniqueConstraint('meeting_id', 'participant_key', name='uq_meeting_participant_key')
    )
    op.create_index(op.f('ix_meeting_participants_meeting_id'), 'meeting_participants', ['meeting_id'], unique=False)
    op.create_table('meeting_transcript_versions',
    sa.Column('meeting_id', sa.Uuid(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('full_text', sa.Text(), nullable=False),
    sa.Column('language', sa.String(length=20), nullable=True),
    sa.Column('provider_request_id', sa.String(length=200), nullable=True),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("source IN ('ai','manual')", name=op.f('ck_meeting_transcript_versions_meeting_transcript_source')),
    sa.CheckConstraint('version >= 1', name=op.f('ck_meeting_transcript_versions_meeting_transcript_version_pos')),
    sa.ForeignKeyConstraint(['created_by'], ['user_accounts.id'], name=op.f('fk_meeting_transcript_versions_created_by_user_accounts')),
    sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], name=op.f('fk_meeting_transcript_versions_meeting_id_meetings')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_meeting_transcript_versions')),
    sa.UniqueConstraint('meeting_id', 'version', name='uq_meeting_transcript_version')
    )
    op.create_index(op.f('ix_meeting_transcript_versions_meeting_id'), 'meeting_transcript_versions', ['meeting_id'], unique=False)
    op.create_table('operational_equipment',
    sa.Column('station_id', sa.Uuid(), nullable=False),
    sa.Column('organization_unit_id', sa.Uuid(), nullable=False),
    sa.Column('code', sa.String(length=100), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('equipment_type', sa.String(length=100), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['created_by'], ['user_accounts.id'], name=op.f('fk_operational_equipment_created_by_user_accounts')),
    sa.ForeignKeyConstraint(['organization_unit_id'], ['organization_units.id'], name=op.f('fk_operational_equipment_organization_unit_id_organization_units')),
    sa.ForeignKeyConstraint(['station_id'], ['operational_stations.id'], name=op.f('fk_operational_equipment_station_id_operational_stations')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_operational_equipment')),
    sa.UniqueConstraint('station_id', 'code', name=op.f('uq_operational_equipment_station_id'))
    )
    op.create_index(op.f('ix_operational_equipment_organization_unit_id'), 'operational_equipment', ['organization_unit_id'], unique=False)
    op.create_index(op.f('ix_operational_equipment_station_id'), 'operational_equipment', ['station_id'], unique=False)
    op.create_table('task_outbox',
    sa.Column('job_id', sa.Uuid(), nullable=False),
    sa.Column('operation_key', sa.String(length=200), nullable=False),
    sa.Column('task_name', sa.String(length=200), nullable=False),
    sa.Column('queue', sa.String(length=60), nullable=False),
    sa.Column('payload', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('publish_attempts', sa.Integer(), nullable=False),
    sa.Column('available_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('pending', 'publishing', 'published', 'failed')", name=op.f('ck_task_outbox_task_outbox_status_valid')),
    sa.CheckConstraint('publish_attempts >= 0', name=op.f('ck_task_outbox_task_outbox_attempts_nonnegative')),
    sa.ForeignKeyConstraint(['job_id'], ['async_jobs.id'], name=op.f('fk_task_outbox_job_id_async_jobs'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_task_outbox')),
    sa.UniqueConstraint('operation_key', name='uq_task_outbox_operation')
    )
    op.create_index('ix_task_outbox_publish', 'task_outbox', ['status', 'available_at'], unique=False)
    op.create_table('work_order_attachments',
    sa.Column('work_order_id', sa.Uuid(), nullable=False),
    sa.Column('object_key', sa.String(length=1024), nullable=False),
    sa.Column('filename', sa.String(length=255), nullable=False),
    sa.Column('mime_type', sa.String(length=120), nullable=False),
    sa.Column('size_bytes', sa.Integer(), nullable=False),
    sa.Column('uploaded_by', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['work_order_id'], ['work_orders.id'], name=op.f('fk_work_order_attachments_work_order_id_work_orders'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_work_order_attachments')),
    sa.UniqueConstraint('object_key', name=op.f('uq_work_order_attachments_object_key'))
    )
    op.create_index(op.f('ix_work_order_attachments_work_order_id'), 'work_order_attachments', ['work_order_id'], unique=False)
    op.create_table('work_order_reminders',
    sa.Column('work_order_id', sa.Uuid(), nullable=False),
    sa.Column('reminder_type', sa.String(length=40), nullable=False),
    sa.Column('scheduled_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('recipient_id', sa.Uuid(), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('acknowledged_by', sa.Uuid(), nullable=True),
    sa.Column('acknowledged_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('acknowledge_reason', sa.Text(), nullable=True),
    sa.Column('payload', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['acknowledged_by'], ['user_accounts.id'], name=op.f('fk_work_order_reminders_acknowledged_by_user_accounts')),
    sa.ForeignKeyConstraint(['recipient_id'], ['user_accounts.id'], name=op.f('fk_work_order_reminders_recipient_id_user_accounts')),
    sa.ForeignKeyConstraint(['work_order_id'], ['work_orders.id'], name=op.f('fk_work_order_reminders_work_order_id_work_orders'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_work_order_reminders')),
    sa.UniqueConstraint('work_order_id', 'reminder_type', 'scheduled_at', name='uq_work_order_reminder')
    )
    op.create_index(op.f('ix_work_order_reminders_recipient_id'), 'work_order_reminders', ['recipient_id'], unique=False)
    op.create_index(op.f('ix_work_order_reminders_work_order_id'), 'work_order_reminders', ['work_order_id'], unique=False)
    op.create_table('work_order_resolutions',
    sa.Column('work_order_id', sa.Uuid(), nullable=False),
    sa.Column('order_version', sa.Integer(), nullable=False),
    sa.Column('action', sa.Text(), nullable=False),
    sa.Column('result', sa.Text(), nullable=False),
    sa.Column('measurements', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('attachment_ids', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('submitted_by', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['submitted_by'], ['user_accounts.id'], name=op.f('fk_work_order_resolutions_submitted_by_user_accounts')),
    sa.ForeignKeyConstraint(['work_order_id'], ['work_orders.id'], name=op.f('fk_work_order_resolutions_work_order_id_work_orders')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_work_order_resolutions')),
    sa.UniqueConstraint('work_order_id', 'order_version', name='uq_order_resolution_version')
    )
    op.create_index(op.f('ix_work_order_resolutions_work_order_id'), 'work_order_resolutions', ['work_order_id'], unique=False)
    op.create_table('work_order_transitions',
    sa.Column('work_order_id', sa.Uuid(), nullable=False),
    sa.Column('from_status', sa.String(length=32), nullable=False),
    sa.Column('to_status', sa.String(length=32), nullable=False),
    sa.Column('actor_id', sa.Uuid(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('version_after', sa.Integer(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['work_order_id'], ['work_orders.id'], name=op.f('fk_work_order_transitions_work_order_id_work_orders'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_work_order_transitions'))
    )
    op.create_index('ix_work_order_transition_time', 'work_order_transitions', ['work_order_id', 'occurred_at'], unique=False)
    op.create_table('agent_proposals',
    sa.Column('run_id', sa.Uuid(), nullable=False),
    sa.Column('category', sa.String(length=80), nullable=False),
    sa.Column('risk_level', sa.String(length=20), nullable=False),
    sa.Column('rationale', sa.Text(), nullable=False),
    sa.Column('recommended_action', sa.Text(), nullable=False),
    sa.Column('evidence_ids', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('unknowns', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('conflicts', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('review_status', sa.String(length=32), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['agent_runs.id'], name=op.f('fk_agent_proposals_run_id_agent_runs'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_agent_proposals')),
    sa.UniqueConstraint('run_id', name='uq_agent_proposal_run')
    )
    op.create_table('agent_quality_evaluations',
    sa.Column('run_id', sa.Uuid(), nullable=False),
    sa.Column('overall_score', sa.Integer(), nullable=False),
    sa.Column('evidence_coverage', sa.Integer(), nullable=False),
    sa.Column('citation_validity', sa.Integer(), nullable=False),
    sa.Column('completeness', sa.Integer(), nullable=False),
    sa.Column('conflict_score', sa.Integer(), nullable=False),
    sa.Column('critic_score', sa.Integer(), nullable=False),
    sa.Column('model_judge_score', sa.Integer(), nullable=True),
    sa.Column('details', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['agent_runs.id'], name=op.f('fk_agent_quality_evaluations_run_id_agent_runs'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_agent_quality_evaluations')),
    sa.UniqueConstraint('run_id', name='uq_agent_quality_run')
    )
    op.create_table('agent_tasks',
    sa.Column('run_id', sa.Uuid(), nullable=False),
    sa.Column('role', sa.String(length=40), nullable=False),
    sa.Column('goal', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('depends_on', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('input_snapshot', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('output', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('error_detail', sa.Text(), nullable=True),
    sa.Column('step_count', sa.Integer(), nullable=False),
    sa.Column('input_tokens', sa.Integer(), nullable=False),
    sa.Column('output_tokens', sa.Integer(), nullable=False),
    sa.Column('cost_microusd', sa.Integer(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['agent_runs.id'], name=op.f('fk_agent_tasks_run_id_agent_runs'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_agent_tasks')),
    sa.UniqueConstraint('run_id', 'role', name='uq_agent_task_run_role')
    )
    op.create_index(op.f('ix_agent_tasks_run_id'), 'agent_tasks', ['run_id'], unique=False)
    op.create_table('audio_transcript_segments',
    sa.Column('transcript_version_id', sa.Uuid(), nullable=False),
    sa.Column('segment_index', sa.Integer(), nullable=False),
    sa.Column('start_ms', sa.Integer(), nullable=False),
    sa.Column('end_ms', sa.Integer(), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('speaker_label', sa.String(length=80), nullable=True),
    sa.Column('confidence', sa.Float(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('confidence IS NULL OR (confidence >= 0 AND confidence <= 1)', name=op.f('ck_audio_transcript_segments_segment_confidence_valid')),
    sa.CheckConstraint('segment_index >= 0', name=op.f('ck_audio_transcript_segments_segment_index_nonnegative')),
    sa.CheckConstraint('start_ms >= 0 AND end_ms > start_ms', name=op.f('ck_audio_transcript_segments_segment_range_valid')),
    sa.ForeignKeyConstraint(['transcript_version_id'], ['audio_transcript_versions.id'], name=op.f('fk_audio_transcript_segments_transcript_version_id_audio_transcript_versions'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audio_transcript_segments')),
    sa.UniqueConstraint('transcript_version_id', 'segment_index', name='uq_transcript_segment')
    )
    op.create_index('ix_transcript_segments_time', 'audio_transcript_segments', ['transcript_version_id', 'start_ms', 'end_ms'], unique=False)
    op.create_table('case_memories',
    sa.Column('event_id', sa.Uuid(), nullable=False),
    sa.Column('event_version_id', sa.Uuid(), nullable=False),
    sa.Column('run_id', sa.Uuid(), nullable=False),
    sa.Column('observation_revision', sa.String(length=64), nullable=False),
    sa.Column('summary', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['event_id'], ['production_events.id'], name=op.f('fk_case_memories_event_id_production_events')),
    sa.ForeignKeyConstraint(['event_version_id'], ['production_event_versions.id'], name=op.f('fk_case_memories_event_version_id_production_event_versions')),
    sa.ForeignKeyConstraint(['run_id'], ['agent_runs.id'], name=op.f('fk_case_memories_run_id_agent_runs')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_case_memories')),
    sa.UniqueConstraint('run_id', name='uq_case_memory_run')
    )
    op.create_index(op.f('ix_case_memories_event_id'), 'case_memories', ['event_id'], unique=False)
    op.create_table('equipment_aliases',
    sa.Column('station_id', sa.Uuid(), nullable=False),
    sa.Column('equipment_id', sa.Uuid(), nullable=False),
    sa.Column('alias', sa.String(length=200), nullable=False),
    sa.Column('normalized', sa.String(length=200), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('confirmed_by', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['confirmed_by'], ['user_accounts.id'], name=op.f('fk_equipment_aliases_confirmed_by_user_accounts')),
    sa.ForeignKeyConstraint(['equipment_id'], ['operational_equipment.id'], name=op.f('fk_equipment_aliases_equipment_id_operational_equipment')),
    sa.ForeignKeyConstraint(['station_id'], ['operational_stations.id'], name=op.f('fk_equipment_aliases_station_id_operational_stations')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_equipment_aliases')),
    sa.UniqueConstraint('station_id', 'normalized', name=op.f('uq_equipment_aliases_station_id'))
    )
    op.create_index(op.f('ix_equipment_aliases_equipment_id'), 'equipment_aliases', ['equipment_id'], unique=False)
    op.create_table('equipment_associations',
    sa.Column('equipment_id', sa.Uuid(), nullable=False),
    sa.Column('failure_mode_id', sa.Uuid(), nullable=False),
    sa.Column('record_kind', sa.String(length=30), nullable=False),
    sa.Column('record_id', sa.Uuid(), nullable=False),
    sa.Column('source_revision', sa.String(length=64), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('confirmed_by', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("record_kind IN ('event','hazard','work_order')", name=op.f('ck_equipment_associations_equipment_association_kind')),
    sa.CheckConstraint('version > 0', name=op.f('ck_equipment_associations_equipment_association_version')),
    sa.ForeignKeyConstraint(['confirmed_by'], ['user_accounts.id'], name=op.f('fk_equipment_associations_confirmed_by_user_accounts')),
    sa.ForeignKeyConstraint(['equipment_id'], ['operational_equipment.id'], name=op.f('fk_equipment_associations_equipment_id_operational_equipment')),
    sa.ForeignKeyConstraint(['failure_mode_id'], ['failure_modes.id'], name=op.f('fk_equipment_associations_failure_mode_id_failure_modes')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_equipment_associations')),
    sa.UniqueConstraint('equipment_id', 'record_kind', 'record_id', 'failure_mode_id', name=op.f('uq_equipment_associations_equipment_id'))
    )
    op.create_index(op.f('ix_equipment_associations_equipment_id'), 'equipment_associations', ['equipment_id'], unique=False)
    op.create_index(op.f('ix_equipment_associations_record_id'), 'equipment_associations', ['record_id'], unique=False)
    op.create_table('handover_summary_versions',
    sa.Column('audio_record_id', sa.Uuid(), nullable=False),
    sa.Column('transcript_version_id', sa.Uuid(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('content', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('source_segment_ids', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("source IN ('ai', 'manual')", name=op.f('ck_handover_summary_versions_handover_summary_source_valid')),
    sa.CheckConstraint('version >= 1', name=op.f('ck_handover_summary_versions_handover_summary_version_positive')),
    sa.ForeignKeyConstraint(['audio_record_id'], ['audio_records.id'], name=op.f('fk_handover_summary_versions_audio_record_id_audio_records'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['created_by'], ['user_accounts.id'], name='fk_handover_summary_creator', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['transcript_version_id'], ['audio_transcript_versions.id'], name=op.f('fk_handover_summary_versions_transcript_version_id_audio_transcript_versions'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_handover_summary_versions')),
    sa.UniqueConstraint('audio_record_id', 'version', name='uq_handover_summary_version')
    )
    op.create_index(op.f('ix_handover_summary_versions_audio_record_id'), 'handover_summary_versions', ['audio_record_id'], unique=False)
    op.create_table('hazard_acceptances',
    sa.Column('rectification_id', sa.Uuid(), nullable=True),
    sa.Column('measurements', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('hazard_id', sa.Uuid(), nullable=False),
    sa.Column('decision', sa.String(length=20), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('accepted_by', sa.Uuid(), nullable=False),
    sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['accepted_by'], ['user_accounts.id'], name=op.f('fk_hazard_acceptances_accepted_by_user_accounts')),
    sa.ForeignKeyConstraint(['hazard_id'], ['hazards.id'], name=op.f('fk_hazard_acceptances_hazard_id_hazards'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['rectification_id'], ['hazard_rectifications.id'], name=op.f('fk_hazard_acceptances_rectification_id_hazard_rectifications')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_hazard_acceptances')),
    sa.UniqueConstraint('rectification_id', name=op.f('uq_hazard_acceptances_rectification_id'))
    )
    op.create_index('ix_hazard_acceptance_time', 'hazard_acceptances', ['hazard_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_hazard_acceptances_hazard_id'), 'hazard_acceptances', ['hazard_id'], unique=False)
    op.create_table('knowledge_feedback',
    sa.Column('run_id', sa.Uuid(), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('category', sa.String(length=50), nullable=False),
    sa.Column('comment', sa.Text(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['created_by'], ['user_accounts.id'], name=op.f('fk_knowledge_feedback_created_by_user_accounts')),
    sa.ForeignKeyConstraint(['run_id'], ['knowledge_runs.id'], name=op.f('fk_knowledge_feedback_run_id_knowledge_runs')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_knowledge_feedback'))
    )
    op.create_index(op.f('ix_knowledge_feedback_run_id'), 'knowledge_feedback', ['run_id'], unique=False)
    op.create_table('knowledge_run_events',
    sa.Column('run_id', sa.Uuid(), nullable=False),
    sa.Column('sequence', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=50), nullable=False),
    sa.Column('payload', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['knowledge_runs.id'], name=op.f('fk_knowledge_run_events_run_id_knowledge_runs')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_knowledge_run_events')),
    sa.UniqueConstraint('run_id', 'sequence', name='uq_knowledge_run_sequence')
    )
    op.create_index(op.f('ix_knowledge_run_events_run_id'), 'knowledge_run_events', ['run_id'], unique=False)
    op.create_table('knowledge_versions',
    sa.Column('document_id', sa.Uuid(), nullable=False),
    sa.Column('number', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('filename', sa.String(length=255), nullable=False),
    sa.Column('mime_type', sa.String(length=150), nullable=False),
    sa.Column('size_bytes', sa.Integer(), nullable=False),
    sa.Column('object_key', sa.String(length=1024), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('effective_from', sa.DateTime(timezone=True), nullable=False),
    sa.Column('effective_to', sa.DateTime(timezone=True), nullable=True),
    sa.Column('equipment_models', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('authority', sa.String(length=120), nullable=False),
    sa.Column('generation', sa.String(length=64), nullable=False),
    sa.Column('snapshot', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('artifact', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('index_state', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('reviewed_by', sa.Uuid(), nullable=True),
    sa.Column('review_reason', sa.Text(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('uploading','uploaded','processing','failed','review', 'approved','rejected','published','withdrawn')", name=op.f('ck_knowledge_versions_knowledge_version_status')),
    sa.CheckConstraint('effective_to IS NULL OR effective_to > effective_from', name=op.f('ck_knowledge_versions_knowledge_effective_range')),
    sa.CheckConstraint('number > 0', name=op.f('ck_knowledge_versions_knowledge_version_positive')),
    sa.ForeignKeyConstraint(['created_by'], ['user_accounts.id'], name=op.f('fk_knowledge_versions_created_by_user_accounts')),
    sa.ForeignKeyConstraint(['document_id'], ['knowledge_documents.id'], name=op.f('fk_knowledge_versions_document_id_knowledge_documents')),
    sa.ForeignKeyConstraint(['reviewed_by'], ['user_accounts.id'], name=op.f('fk_knowledge_versions_reviewed_by_user_accounts')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_knowledge_versions')),
    sa.UniqueConstraint('document_id', 'number', name='uq_knowledge_version_number'),
    sa.UniqueConstraint('object_key', name=op.f('uq_knowledge_versions_object_key'))
    )
    op.create_index(op.f('ix_knowledge_versions_document_id'), 'knowledge_versions', ['document_id'], unique=False)
    op.create_table('meeting_minutes_versions',
    sa.Column('meeting_id', sa.Uuid(), nullable=False),
    sa.Column('transcript_version_id', sa.Uuid(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('source', sa.String(length=20), nullable=False),
    sa.Column('content', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('review_status', sa.String(length=20), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('confirmed_by', sa.Uuid(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['confirmed_by'], ['user_accounts.id'], name=op.f('fk_meeting_minutes_versions_confirmed_by_user_accounts')),
    sa.ForeignKeyConstraint(['created_by'], ['user_accounts.id'], name=op.f('fk_meeting_minutes_versions_created_by_user_accounts')),
    sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], name=op.f('fk_meeting_minutes_versions_meeting_id_meetings'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['transcript_version_id'], ['meeting_transcript_versions.id'], name='fk_meeting_minutes_transcript'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_meeting_minutes_versions')),
    sa.UniqueConstraint('meeting_id', 'version', name='uq_meeting_minutes_version')
    )
    op.create_index(op.f('ix_meeting_minutes_versions_meeting_id'), 'meeting_minutes_versions', ['meeting_id'], unique=False)
    op.create_table('meeting_transcript_segments',
    sa.Column('transcript_version_id', sa.Uuid(), nullable=False),
    sa.Column('segment_index', sa.Integer(), nullable=False),
    sa.Column('start_ms', sa.Integer(), nullable=False),
    sa.Column('end_ms', sa.Integer(), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('speaker_label', sa.String(length=120), nullable=True),
    sa.Column('confidence', sa.Float(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('segment_index >= 0', name=op.f('ck_meeting_transcript_segments_meeting_segment_index_nonneg')),
    sa.CheckConstraint('start_ms >= 0 AND end_ms > start_ms', name=op.f('ck_meeting_transcript_segments_meeting_segment_range')),
    sa.ForeignKeyConstraint(['transcript_version_id'], ['meeting_transcript_versions.id'], name=op.f('fk_meeting_transcript_segments_transcript_version_id_meeting_transcript_versions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_meeting_transcript_segments')),
    sa.UniqueConstraint('transcript_version_id', 'segment_index', name='uq_meeting_segment_index')
    )
    op.create_index(op.f('ix_meeting_transcript_segments_transcript_version_id'), 'meeting_transcript_segments', ['transcript_version_id'], unique=False)
    op.create_table('work_order_acceptances',
    sa.Column('resolution_id', sa.Uuid(), nullable=False),
    sa.Column('work_order_id', sa.Uuid(), nullable=False),
    sa.Column('approved', sa.Boolean(), nullable=False),
    sa.Column('measurements', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('reviewed_by', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['resolution_id'], ['work_order_resolutions.id'], name=op.f('fk_work_order_acceptances_resolution_id_work_order_resolutions')),
    sa.ForeignKeyConstraint(['reviewed_by'], ['user_accounts.id'], name=op.f('fk_work_order_acceptances_reviewed_by_user_accounts')),
    sa.ForeignKeyConstraint(['work_order_id'], ['work_orders.id'], name=op.f('fk_work_order_acceptances_work_order_id_work_orders')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_work_order_acceptances')),
    sa.UniqueConstraint('resolution_id', name=op.f('uq_work_order_acceptances_resolution_id'))
    )
    op.create_index(op.f('ix_work_order_acceptances_work_order_id'), 'work_order_acceptances', ['work_order_id'], unique=False)
    op.create_table('agent_evidence',
    sa.Column('run_id', sa.Uuid(), nullable=False),
    sa.Column('task_id', sa.Uuid(), nullable=False),
    sa.Column('source_server', sa.String(length=80), nullable=False),
    sa.Column('source_resource', sa.String(length=200), nullable=False),
    sa.Column('source_version', sa.String(length=200), nullable=True),
    sa.Column('content', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('access_scope', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['agent_runs.id'], name=op.f('fk_agent_evidence_run_id_agent_runs'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['task_id'], ['agent_tasks.id'], name=op.f('fk_agent_evidence_task_id_agent_tasks'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_agent_evidence'))
    )
    op.create_index(op.f('ix_agent_evidence_run_id'), 'agent_evidence', ['run_id'], unique=False)
    op.create_index('ix_agent_evidence_run_source', 'agent_evidence', ['run_id', 'source_server'], unique=False)
    op.create_table('agent_steps',
    sa.Column('task_id', sa.Uuid(), nullable=False),
    sa.Column('step_index', sa.Integer(), nullable=False),
    sa.Column('action', sa.String(length=80), nullable=False),
    sa.Column('tool_name', sa.String(length=120), nullable=True),
    sa.Column('tool_arguments', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('observation', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('trace_id', sa.Uuid(), nullable=False),
    sa.Column('span_id', sa.Uuid(), nullable=False),
    sa.Column('parent_span_id', sa.Uuid(), nullable=True),
    sa.Column('duration_ms', sa.Integer(), nullable=False),
    sa.Column('input_tokens', sa.Integer(), nullable=True),
    sa.Column('output_tokens', sa.Integer(), nullable=True),
    sa.Column('cost_microusd', sa.Integer(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['task_id'], ['agent_tasks.id'], name=op.f('fk_agent_steps_task_id_agent_tasks'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_agent_steps')),
    sa.UniqueConstraint('task_id', 'step_index', name='uq_agent_step_index')
    )
    op.create_index(op.f('ix_agent_steps_task_id'), 'agent_steps', ['task_id'], unique=False)
    op.create_table('graph_impact_reviews',
    sa.Column('source_version_id', sa.Uuid(), nullable=False),
    sa.Column('replacement_version_id', sa.Uuid(), nullable=True),
    sa.Column('change_type', sa.String(length=30), nullable=False),
    sa.Column('change_signature', sa.String(length=64), nullable=False),
    sa.Column('target_key', sa.String(length=100), nullable=False),
    sa.Column('target_revision', sa.String(length=64), nullable=False),
    sa.Column('paths', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('history', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('reviewed_by', sa.Uuid(), nullable=True),
    sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("change_type IN ('withdrawal','expiry','new_revision')", name=op.f('ck_graph_impact_reviews_graph_review_change_type')),
    sa.CheckConstraint("status IN ('pending','needs_action','closed','superseded')", name=op.f('ck_graph_impact_reviews_graph_review_status')),
    sa.ForeignKeyConstraint(['replacement_version_id'], ['knowledge_versions.id'], name=op.f('fk_graph_impact_reviews_replacement_version_id_knowledge_versions')),
    sa.ForeignKeyConstraint(['reviewed_by'], ['user_accounts.id'], name=op.f('fk_graph_impact_reviews_reviewed_by_user_accounts')),
    sa.ForeignKeyConstraint(['source_version_id'], ['knowledge_versions.id'], name=op.f('fk_graph_impact_reviews_source_version_id_knowledge_versions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_graph_impact_reviews')),
    sa.UniqueConstraint('source_version_id', 'target_key', 'change_signature', name=op.f('uq_graph_impact_reviews_source_version_id'))
    )
    op.create_index(op.f('ix_graph_impact_reviews_source_version_id'), 'graph_impact_reviews', ['source_version_id'], unique=False)
    op.create_index(op.f('ix_graph_impact_reviews_target_key'), 'graph_impact_reviews', ['target_key'], unique=False)
    op.create_table('knowledge_chunks',
    sa.Column('version_id', sa.Uuid(), nullable=False),
    sa.Column('ordinal', sa.Integer(), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('parent_text', sa.Text(), nullable=False),
    sa.Column('locator', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['version_id'], ['knowledge_versions.id'], name=op.f('fk_knowledge_chunks_version_id_knowledge_versions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_knowledge_chunks')),
    sa.UniqueConstraint('version_id', 'ordinal', name='uq_knowledge_chunk_order')
    )
    op.create_index(op.f('ix_knowledge_chunks_version_id'), 'knowledge_chunks', ['version_id'], unique=False)
    op.create_table('meeting_knowledge_publications',
    sa.Column('meeting_id', sa.Uuid(), nullable=False),
    sa.Column('transcript_version_id', sa.Uuid(), nullable=False),
    sa.Column('knowledge_base_id', sa.Uuid(), nullable=False),
    sa.Column('knowledge_document_id', sa.Uuid(), nullable=False),
    sa.Column('knowledge_version_id', sa.Uuid(), nullable=False),
    sa.Column('index_job_id', sa.Uuid(), nullable=True),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['created_by'], ['user_accounts.id'], name=op.f('fk_meeting_knowledge_publications_created_by_user_accounts')),
    sa.ForeignKeyConstraint(['index_job_id'], ['async_jobs.id'], name=op.f('fk_meeting_knowledge_publications_index_job_id_async_jobs')),
    sa.ForeignKeyConstraint(['knowledge_base_id'], ['knowledge_bases.id'], name=op.f('fk_meeting_knowledge_publications_knowledge_base_id_knowledge_bases')),
    sa.ForeignKeyConstraint(['knowledge_document_id'], ['knowledge_documents.id'], name=op.f('fk_meeting_knowledge_publications_knowledge_document_id_knowledge_documents')),
    sa.ForeignKeyConstraint(['knowledge_version_id'], ['knowledge_versions.id'], name=op.f('fk_meeting_knowledge_publications_knowledge_version_id_knowledge_versions')),
    sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], name=op.f('fk_meeting_knowledge_publications_meeting_id_meetings')),
    sa.ForeignKeyConstraint(['transcript_version_id'], ['meeting_transcript_versions.id'], name=op.f('fk_meeting_knowledge_publications_transcript_version_id_meeting_transcript_versions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_meeting_knowledge_publications')),
    sa.UniqueConstraint('meeting_id', 'transcript_version_id', name='uq_meeting_knowledge_version')
    )
    op.create_index(op.f('ix_meeting_knowledge_publications_meeting_id'), 'meeting_knowledge_publications', ['meeting_id'], unique=False)
    op.create_table('knowledge_facts',
    sa.Column('version_id', sa.Uuid(), nullable=False),
    sa.Column('evidence_id', sa.Uuid(), nullable=False),
    sa.Column('source', sa.String(length=300), nullable=False),
    sa.Column('relation', sa.String(length=100), nullable=False),
    sa.Column('target', sa.String(length=300), nullable=False),
    sa.Column('evidence_quote', sa.Text(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['evidence_id'], ['knowledge_chunks.id'], name=op.f('fk_knowledge_facts_evidence_id_knowledge_chunks')),
    sa.ForeignKeyConstraint(['version_id'], ['knowledge_versions.id'], name=op.f('fk_knowledge_facts_version_id_knowledge_versions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_knowledge_facts'))
    )
    op.create_index(op.f('ix_knowledge_facts_version_id'), 'knowledge_facts', ['version_id'], unique=False)


    op.create_foreign_key('fk_audio_current_summary', 'audio_records', 'handover_summary_versions', ['current_summary_version_id'], ['id'], ondelete='RESTRICT')
    op.create_foreign_key('fk_audio_current_transcript', 'audio_records', 'audio_transcript_versions', ['current_transcript_version_id'], ['id'], ondelete='RESTRICT')
    op.create_foreign_key('fk_manual_operation_current_version', 'manual_operation_records', 'manual_operation_record_versions', ['current_version_id'], ['id'], ondelete='RESTRICT')
    op.create_foreign_key('fk_meeting_current_transcript', 'meetings', 'meeting_transcript_versions', ['current_transcript_version_id'], ['id'], ondelete=None)
    op.execute("CREATE OR REPLACE FUNCTION capture_business_graph_change() RETURNS trigger\n    LANGUAGE plpgsql AS $$ BEGIN\n      INSERT INTO business_graph_outbox(source_table,record_id)\n      VALUES (TG_TABLE_NAME, COALESCE(NEW.id,OLD.id)::text);\n      IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;\n    END $$")
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "operational_stations"')
    op.execute('CREATE TRIGGER business_graph_change AFTER INSERT OR UPDATE OR DELETE ON "operational_stations" FOR EACH ROW EXECUTE FUNCTION capture_business_graph_change()')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "operational_equipment"')
    op.execute('CREATE TRIGGER business_graph_change AFTER INSERT OR UPDATE OR DELETE ON "operational_equipment" FOR EACH ROW EXECUTE FUNCTION capture_business_graph_change()')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "equipment_aliases"')
    op.execute('CREATE TRIGGER business_graph_change AFTER INSERT OR UPDATE OR DELETE ON "equipment_aliases" FOR EACH ROW EXECUTE FUNCTION capture_business_graph_change()')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "failure_modes"')
    op.execute('CREATE TRIGGER business_graph_change AFTER INSERT OR UPDATE OR DELETE ON "failure_modes" FOR EACH ROW EXECUTE FUNCTION capture_business_graph_change()')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "equipment_associations"')
    op.execute('CREATE TRIGGER business_graph_change AFTER INSERT OR UPDATE OR DELETE ON "equipment_associations" FOR EACH ROW EXECUTE FUNCTION capture_business_graph_change()')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "production_events"')
    op.execute('CREATE TRIGGER business_graph_change AFTER INSERT OR UPDATE OR DELETE ON "production_events" FOR EACH ROW EXECUTE FUNCTION capture_business_graph_change()')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "event_source_links"')
    op.execute('CREATE TRIGGER business_graph_change AFTER INSERT OR UPDATE OR DELETE ON "event_source_links" FOR EACH ROW EXECUTE FUNCTION capture_business_graph_change()')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "agent_runs"')
    op.execute('CREATE TRIGGER business_graph_change AFTER INSERT OR UPDATE OR DELETE ON "agent_runs" FOR EACH ROW EXECUTE FUNCTION capture_business_graph_change()')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "agent_evidence"')
    op.execute('CREATE TRIGGER business_graph_change AFTER INSERT OR UPDATE OR DELETE ON "agent_evidence" FOR EACH ROW EXECUTE FUNCTION capture_business_graph_change()')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "workflow_runs"')
    op.execute('CREATE TRIGGER business_graph_change AFTER INSERT OR UPDATE OR DELETE ON "workflow_runs" FOR EACH ROW EXECUTE FUNCTION capture_business_graph_change()')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "hazards"')
    op.execute('CREATE TRIGGER business_graph_change AFTER INSERT OR UPDATE OR DELETE ON "hazards" FOR EACH ROW EXECUTE FUNCTION capture_business_graph_change()')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "hazard_work_order_links"')
    op.execute('CREATE TRIGGER business_graph_change AFTER INSERT OR UPDATE OR DELETE ON "hazard_work_order_links" FOR EACH ROW EXECUTE FUNCTION capture_business_graph_change()')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "hazard_rectifications"')
    op.execute('CREATE TRIGGER business_graph_change AFTER INSERT OR UPDATE OR DELETE ON "hazard_rectifications" FOR EACH ROW EXECUTE FUNCTION capture_business_graph_change()')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "hazard_acceptances"')
    op.execute('CREATE TRIGGER business_graph_change AFTER INSERT OR UPDATE OR DELETE ON "hazard_acceptances" FOR EACH ROW EXECUTE FUNCTION capture_business_graph_change()')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "work_orders"')
    op.execute('CREATE TRIGGER business_graph_change AFTER INSERT OR UPDATE OR DELETE ON "work_orders" FOR EACH ROW EXECUTE FUNCTION capture_business_graph_change()')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "work_order_resolutions"')
    op.execute('CREATE TRIGGER business_graph_change AFTER INSERT OR UPDATE OR DELETE ON "work_order_resolutions" FOR EACH ROW EXECUTE FUNCTION capture_business_graph_change()')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "work_order_acceptances"')
    op.execute('CREATE TRIGGER business_graph_change AFTER INSERT OR UPDATE OR DELETE ON "work_order_acceptances" FOR EACH ROW EXECUTE FUNCTION capture_business_graph_change()')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "experience_memories"')
    op.execute('CREATE TRIGGER business_graph_change AFTER INSERT OR UPDATE OR DELETE ON "experience_memories" FOR EACH ROW EXECUTE FUNCTION capture_business_graph_change()')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "knowledge_versions"')
    op.execute('CREATE TRIGGER business_graph_change AFTER INSERT OR UPDATE OR DELETE ON "knowledge_versions" FOR EACH ROW EXECUTE FUNCTION capture_business_graph_change()')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "knowledge_documents"')
    op.execute('CREATE TRIGGER business_graph_change AFTER INSERT OR UPDATE OR DELETE ON "knowledge_documents" FOR EACH ROW EXECUTE FUNCTION capture_business_graph_change()')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "knowledge_bases"')
    op.execute('CREATE TRIGGER business_graph_change AFTER INSERT OR UPDATE OR DELETE ON "knowledge_bases" FOR EACH ROW EXECUTE FUNCTION capture_business_graph_change()')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "knowledge_runs"')
    op.execute('CREATE TRIGGER business_graph_change AFTER INSERT OR UPDATE OR DELETE ON "knowledge_runs" FOR EACH ROW EXECUTE FUNCTION capture_business_graph_change()')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "meetings"')
    op.execute('CREATE TRIGGER business_graph_change AFTER INSERT OR UPDATE OR DELETE ON "meetings" FOR EACH ROW EXECUTE FUNCTION capture_business_graph_change()')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "meeting_knowledge_publications"')
    op.execute('CREATE TRIGGER business_graph_change AFTER INSERT OR UPDATE OR DELETE ON "meeting_knowledge_publications" FOR EACH ROW EXECUTE FUNCTION capture_business_graph_change()')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "audio_records"')
    op.execute('CREATE TRIGGER business_graph_change AFTER INSERT OR UPDATE OR DELETE ON "audio_records" FOR EACH ROW EXECUTE FUNCTION capture_business_graph_change()')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "manual_operation_records"')
    op.execute('CREATE TRIGGER business_graph_change AFTER INSERT OR UPDATE OR DELETE ON "manual_operation_records" FOR EACH ROW EXECUTE FUNCTION capture_business_graph_change()')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "inspection_records"')
    op.execute('CREATE TRIGGER business_graph_change AFTER INSERT OR UPDATE OR DELETE ON "inspection_records" FOR EACH ROW EXECUTE FUNCTION capture_business_graph_change()')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "inspection_findings"')
    op.execute('CREATE TRIGGER business_graph_change AFTER INSERT OR UPDATE OR DELETE ON "inspection_findings" FOR EACH ROW EXECUTE FUNCTION capture_business_graph_change()')
    op.execute("INSERT INTO business_graph_outbox(source_table,record_id) VALUES ('initial_snapshot','initial')")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('fk_audio_current_summary', 'audio_records', type_="foreignkey")
    op.drop_constraint('fk_audio_current_transcript', 'audio_records', type_="foreignkey")
    op.drop_constraint('fk_manual_operation_current_version', 'manual_operation_records', type_="foreignkey")
    op.drop_constraint('fk_meeting_current_transcript', 'meetings', type_="foreignkey")
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "operational_stations"')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "operational_equipment"')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "equipment_aliases"')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "failure_modes"')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "equipment_associations"')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "production_events"')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "event_source_links"')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "agent_runs"')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "agent_evidence"')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "workflow_runs"')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "hazards"')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "hazard_work_order_links"')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "hazard_rectifications"')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "hazard_acceptances"')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "work_orders"')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "work_order_resolutions"')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "work_order_acceptances"')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "experience_memories"')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "knowledge_versions"')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "knowledge_documents"')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "knowledge_bases"')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "knowledge_runs"')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "meetings"')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "meeting_knowledge_publications"')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "audio_records"')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "manual_operation_records"')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "inspection_records"')
    op.execute('DROP TRIGGER IF EXISTS business_graph_change ON "inspection_findings"')
    op.execute("DROP FUNCTION IF EXISTS capture_business_graph_change()")
    op.drop_index(op.f('ix_knowledge_facts_version_id'), table_name='knowledge_facts')
    op.drop_table('knowledge_facts')
    op.drop_index(op.f('ix_meeting_knowledge_publications_meeting_id'), table_name='meeting_knowledge_publications')
    op.drop_table('meeting_knowledge_publications')
    op.drop_index(op.f('ix_knowledge_chunks_version_id'), table_name='knowledge_chunks')
    op.drop_table('knowledge_chunks')
    op.drop_index(op.f('ix_graph_impact_reviews_target_key'), table_name='graph_impact_reviews')
    op.drop_index(op.f('ix_graph_impact_reviews_source_version_id'), table_name='graph_impact_reviews')
    op.drop_table('graph_impact_reviews')
    op.drop_index(op.f('ix_agent_steps_task_id'), table_name='agent_steps')
    op.drop_table('agent_steps')
    op.drop_index('ix_agent_evidence_run_source', table_name='agent_evidence')
    op.drop_index(op.f('ix_agent_evidence_run_id'), table_name='agent_evidence')
    op.drop_table('agent_evidence')
    op.drop_index(op.f('ix_work_order_acceptances_work_order_id'), table_name='work_order_acceptances')
    op.drop_table('work_order_acceptances')
    op.drop_index(op.f('ix_meeting_transcript_segments_transcript_version_id'), table_name='meeting_transcript_segments')
    op.drop_table('meeting_transcript_segments')
    op.drop_index(op.f('ix_meeting_minutes_versions_meeting_id'), table_name='meeting_minutes_versions')
    op.drop_table('meeting_minutes_versions')
    op.drop_index(op.f('ix_knowledge_versions_document_id'), table_name='knowledge_versions')
    op.drop_table('knowledge_versions')
    op.drop_index(op.f('ix_knowledge_run_events_run_id'), table_name='knowledge_run_events')
    op.drop_table('knowledge_run_events')
    op.drop_index(op.f('ix_knowledge_feedback_run_id'), table_name='knowledge_feedback')
    op.drop_table('knowledge_feedback')
    op.drop_index(op.f('ix_hazard_acceptances_hazard_id'), table_name='hazard_acceptances')
    op.drop_index('ix_hazard_acceptance_time', table_name='hazard_acceptances')
    op.drop_table('hazard_acceptances')
    op.drop_index(op.f('ix_handover_summary_versions_audio_record_id'), table_name='handover_summary_versions')
    op.drop_table('handover_summary_versions')
    op.drop_index(op.f('ix_equipment_associations_record_id'), table_name='equipment_associations')
    op.drop_index(op.f('ix_equipment_associations_equipment_id'), table_name='equipment_associations')
    op.drop_table('equipment_associations')
    op.drop_index(op.f('ix_equipment_aliases_equipment_id'), table_name='equipment_aliases')
    op.drop_table('equipment_aliases')
    op.drop_index(op.f('ix_case_memories_event_id'), table_name='case_memories')
    op.drop_table('case_memories')
    op.drop_index('ix_transcript_segments_time', table_name='audio_transcript_segments')
    op.drop_table('audio_transcript_segments')
    op.drop_index(op.f('ix_agent_tasks_run_id'), table_name='agent_tasks')
    op.drop_table('agent_tasks')
    op.drop_table('agent_quality_evaluations')
    op.drop_table('agent_proposals')
    op.drop_index('ix_work_order_transition_time', table_name='work_order_transitions')
    op.drop_table('work_order_transitions')
    op.drop_index(op.f('ix_work_order_resolutions_work_order_id'), table_name='work_order_resolutions')
    op.drop_table('work_order_resolutions')
    op.drop_index(op.f('ix_work_order_reminders_work_order_id'), table_name='work_order_reminders')
    op.drop_index(op.f('ix_work_order_reminders_recipient_id'), table_name='work_order_reminders')
    op.drop_table('work_order_reminders')
    op.drop_index(op.f('ix_work_order_attachments_work_order_id'), table_name='work_order_attachments')
    op.drop_table('work_order_attachments')
    op.drop_index('ix_task_outbox_publish', table_name='task_outbox')
    op.drop_table('task_outbox')
    op.drop_index(op.f('ix_operational_equipment_station_id'), table_name='operational_equipment')
    op.drop_index(op.f('ix_operational_equipment_organization_unit_id'), table_name='operational_equipment')
    op.drop_table('operational_equipment')
    op.drop_index(op.f('ix_meeting_transcript_versions_meeting_id'), table_name='meeting_transcript_versions')
    op.drop_table('meeting_transcript_versions')
    op.drop_index(op.f('ix_meeting_participants_meeting_id'), table_name='meeting_participants')
    op.drop_table('meeting_participants')
    op.drop_table('manual_operation_record_versions')
    op.drop_index(op.f('ix_knowledge_runs_requested_by'), table_name='knowledge_runs')
    op.drop_table('knowledge_runs')
    op.drop_table('knowledge_evaluations')
    op.drop_index(op.f('ix_knowledge_documents_base_id'), table_name='knowledge_documents')
    op.drop_table('knowledge_documents')
    op.drop_index('ix_job_events_job_created', table_name='job_events')
    op.drop_table('job_events')
    op.drop_table('inspection_finding_links')
    op.drop_index(op.f('ix_hazard_work_order_links_work_order_id'), table_name='hazard_work_order_links')
    op.drop_index(op.f('ix_hazard_work_order_links_hazard_id'), table_name='hazard_work_order_links')
    op.drop_table('hazard_work_order_links')
    op.drop_index('ix_hazard_transition_time', table_name='hazard_transitions')
    op.drop_table('hazard_transitions')
    op.drop_index(op.f('ix_hazard_rectifications_hazard_id'), table_name='hazard_rectifications')
    op.drop_index('ix_hazard_rectification_time', table_name='hazard_rectifications')
    op.drop_table('hazard_rectifications')
    op.drop_index(op.f('ix_hazard_attachments_hazard_id'), table_name='hazard_attachments')
    op.drop_table('hazard_attachments')
    op.drop_index(op.f('ix_hazard_assessments_hazard_id'), table_name='hazard_assessments')
    op.drop_table('hazard_assessments')
    op.drop_index(op.f('ix_experience_memories_status'), table_name='experience_memories')
    op.drop_index(op.f('ix_experience_memories_primary_order_id'), table_name='experience_memories')
    op.drop_index(op.f('ix_experience_memories_organization_unit_id'), table_name='experience_memories')
    op.drop_index(op.f('ix_experience_memories_expires_at'), table_name='experience_memories')
    op.drop_index(op.f('ix_experience_memories_content_hash'), table_name='experience_memories')
    op.drop_index(op.f('ix_experience_memories_applicability_key'), table_name='experience_memories')
    op.drop_table('experience_memories')
    op.drop_index(op.f('ix_audio_transcript_versions_audio_record_id'), table_name='audio_transcript_versions')
    op.drop_table('audio_transcript_versions')
    op.drop_index(op.f('ix_ai_call_logs_run_id'), table_name='ai_call_logs')
    op.drop_index('ix_ai_call_logs_job', table_name='ai_call_logs')
    op.drop_table('ai_call_logs')
    op.drop_index('ix_agent_runs_org_created', table_name='agent_runs')
    op.drop_index('ix_agent_runs_event_status', table_name='agent_runs')
    op.drop_table('agent_runs')
    op.drop_index('ix_work_orders_org_status', table_name='work_orders')
    op.drop_table('work_orders')
    op.drop_index('ix_role_assignments_validity', table_name='role_assignments')
    op.drop_index('ix_role_assignments_user_active', table_name='role_assignments')
    op.drop_table('role_assignments')
    op.drop_index(op.f('ix_report_sources_report_version_id'), table_name='report_sources')
    op.drop_table('report_sources')
    op.drop_index('ix_report_exports_report_status', table_name='report_exports')
    op.drop_table('report_exports')
    op.drop_index('uq_prompt_template_active', table_name='prompt_template_versions', postgresql_where=sa.text('active'), sqlite_where=sa.text('active = 1'))
    op.drop_index(op.f('ix_prompt_template_versions_template_code'), table_name='prompt_template_versions')
    op.drop_table('prompt_template_versions')
    op.drop_index(op.f('ix_operational_stations_organization_unit_id'), table_name='operational_stations')
    op.drop_table('operational_stations')
    op.drop_index(op.f('ix_meetings_organization_unit_id'), table_name='meetings')
    op.drop_index('ix_meetings_org_started', table_name='meetings')
    op.drop_table('meetings')
    op.drop_index('ix_manual_operation_org_time', table_name='manual_operation_records')
    op.drop_table('manual_operation_records')
    op.drop_index(op.f('ix_knowledge_conversations_created_by'), table_name='knowledge_conversations')
    op.drop_table('knowledge_conversations')
    op.drop_index(op.f('ix_knowledge_bases_organization_unit_id'), table_name='knowledge_bases')
    op.drop_table('knowledge_bases')
    op.drop_index('ix_findings_inspection_status', table_name='inspection_findings')
    op.drop_table('inspection_findings')
    op.drop_table('idempotency_records')
    op.drop_index('ix_hazards_source', table_name='hazards')
    op.drop_index('ix_hazards_org_status_due', table_name='hazards')
    op.drop_table('hazards')
    op.drop_index(op.f('ix_failure_modes_organization_unit_id'), table_name='failure_modes')
    op.drop_table('failure_modes')
    op.drop_index(op.f('ix_evaluation_improvements_reviewer_id'), table_name='evaluation_improvements')
    op.drop_index(op.f('ix_evaluation_improvements_due_at'), table_name='evaluation_improvements')
    op.drop_index(op.f('ix_evaluation_improvements_created_by'), table_name='evaluation_improvements')
    op.drop_index(op.f('ix_evaluation_improvements_baseline_id'), table_name='evaluation_improvements')
    op.drop_index(op.f('ix_evaluation_improvements_assignee_id'), table_name='evaluation_improvements')
    op.drop_table('evaluation_improvements')
    op.drop_index(op.f('ix_case_observations_event_id'), table_name='case_observations')
    op.drop_table('case_observations')
    op.drop_index('uq_business_config_active', table_name='business_config_versions', postgresql_where=sa.text('active'), sqlite_where=sa.text('active = 1'))
    op.drop_index(op.f('ix_business_config_versions_config_code'), table_name='business_config_versions')
    op.drop_table('business_config_versions')
    op.drop_index('ix_audit_logs_resource', table_name='audit_logs')
    op.drop_index(op.f('ix_audit_logs_organization_unit_id'), table_name='audit_logs')
    op.drop_index(op.f('ix_audit_logs_actor_id'), table_name='audit_logs')
    op.drop_table('audit_logs')
    op.drop_index('ix_audio_records_org_shift', table_name='audio_records')
    op.drop_table('audio_records')
    op.drop_index('uq_async_jobs_active_resource', table_name='async_jobs', postgresql_where=sa.text("status IN ('queued', 'running')"), sqlite_where=sa.text("status IN ('queued', 'running')"))
    op.drop_index('ix_async_jobs_status_updated', table_name='async_jobs')
    op.drop_index('ix_async_jobs_resource', table_name='async_jobs')
    op.drop_index(op.f('ix_async_jobs_organization_unit_id'), table_name='async_jobs')
    op.drop_table('async_jobs')
    op.drop_table('agent_offline_evaluations')
    op.drop_index('ix_workflow_runs_status_updated', table_name='workflow_runs')
    op.drop_index(op.f('ix_workflow_runs_organization_unit_id'), table_name='workflow_runs')
    op.drop_table('workflow_runs')
    op.drop_index(op.f('ix_user_accounts_organization_unit_id'), table_name='user_accounts')
    op.drop_index('ix_user_accounts_organization_active', table_name='user_accounts')
    op.drop_table('user_accounts')
    op.drop_index(op.f('ix_upload_sessions_status'), table_name='upload_sessions')
    op.drop_index(op.f('ix_upload_sessions_organization_unit_id'), table_name='upload_sessions')
    op.drop_table('upload_sessions')
    op.drop_index(op.f('ix_role_permissions_role_id'), table_name='role_permissions')
    op.drop_index(op.f('ix_role_permissions_permission_id'), table_name='role_permissions')
    op.drop_table('role_permissions')
    op.drop_index(op.f('ix_report_versions_report_id'), table_name='report_versions')
    op.drop_table('report_versions')
    op.drop_index(op.f('ix_production_event_versions_event_id'), table_name='production_event_versions')
    op.drop_table('production_event_versions')
    op.drop_index('ix_inspection_images_record_status', table_name='inspection_images')
    op.drop_table('inspection_images')
    op.drop_index(op.f('ix_event_source_links_event_id'), table_name='event_source_links')
    op.drop_table('event_source_links')
    op.drop_index('ix_assessment_event_status', table_name='abnormality_assessments')
    op.drop_table('abnormality_assessments')
    op.drop_table('role_definitions')
    op.drop_index('ix_production_events_status', table_name='production_events')
    op.drop_index('ix_production_events_org_time', table_name='production_events')
    op.drop_index(op.f('ix_production_events_event_type'), table_name='production_events')
    op.drop_table('production_events')
    op.drop_index(op.f('ix_permission_definitions_module'), table_name='permission_definitions')
    op.drop_table('permission_definitions')
    op.drop_index(op.f('ix_organization_units_path'), table_name='organization_units')
    op.drop_table('organization_units')
    op.drop_index('ix_operation_reports_status', table_name='operation_reports')
    op.drop_table('operation_reports')
    op.drop_table('model_aliases')
    op.drop_index('ix_inspection_org_time', table_name='inspection_records')
    op.drop_table('inspection_records')
    op.drop_table('business_graph_projection')
    op.drop_table('business_graph_outbox')
    op.execute("DROP SCHEMA IF EXISTS agent_checkpoints CASCADE")
