"""Human-confirmed, versioned WGS84 station locations.

Revision ID: 0003_station_locations
Revises: 0002_agent_tasks
"""

from alembic import op
import sqlalchemy as sa

revision = '0003_station_locations'
down_revision = '0002_agent_tasks'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('station_locations',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('station_id', sa.Uuid(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('latitude', sa.Float(), nullable=True),
        sa.Column('longitude', sa.Float(), nullable=True),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('address', sa.Text(), nullable=False),
        sa.Column('source', sa.String(500), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('created_by', sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_station_locations')),
        sa.ForeignKeyConstraint(['station_id'], ['operational_stations.id'], name=op.f('fk_station_locations_station_id_operational_stations')),
        sa.ForeignKeyConstraint(['created_by'], ['user_accounts.id'], name=op.f('fk_station_locations_created_by_user_accounts')),
        sa.UniqueConstraint('station_id', 'version', name='uq_station_location_version'),
        sa.CheckConstraint('version > 0', name=op.f('ck_station_locations_station_location_version')),
        sa.CheckConstraint('latitude BETWEEN -90 AND 90 AND longitude BETWEEN -180 AND 180', name=op.f('ck_station_locations_station_location_bounds')),
        sa.CheckConstraint('NOT active OR (latitude IS NOT NULL AND longitude IS NOT NULL)', name=op.f('ck_station_locations_station_location_coordinates')),
    )
    op.create_index('ix_station_locations_station_id', 'station_locations', ['station_id'])


def downgrade():
    op.drop_table('station_locations')
