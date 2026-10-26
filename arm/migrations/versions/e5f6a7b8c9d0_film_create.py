"""Add the film table for discs that carry several films

Revision ID: e5f6a7b8c9d0
Revises: c3d4e5f6a7b8
Create Date: 2026-10-26

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e5f6a7b8c9d0'
down_revision = 'c3d4e5f6a7b8'
branch_labels = None
depends_on = None


def upgrade():
    """
    One row per film on a double-feature disc, in disc order. Position 1 is the job's own title;
    the others are filed in their own folders. track_number records which ripped title was filed
    as the film, runtime (seconds) is what the metadata provider reported and is what the titles
    are matched on.
    """
    op.create_table('film',
                    sa.Column('film_id', sa.Integer(), nullable=False),
                    sa.Column('job_id', sa.Integer(), nullable=True),
                    sa.Column('position', sa.Integer(), nullable=True),
                    sa.Column('title', sa.String(length=256), nullable=True),
                    sa.Column('year', sa.String(length=4), nullable=True),
                    sa.Column('imdb_id', sa.String(length=15), nullable=True),
                    sa.Column('video_type', sa.String(length=20), nullable=True),
                    sa.Column('poster_url', sa.String(length=256), nullable=True),
                    sa.Column('runtime', sa.Integer(), nullable=True),
                    sa.Column('track_number', sa.String(length=4), nullable=True),
                    sa.ForeignKeyConstraint(['job_id'], ['job.job_id'], ),
                    sa.PrimaryKeyConstraint('film_id')
                    )
    op.create_index('ix_film_job_id', 'film', ['job_id'], unique=False)


def downgrade():
    op.drop_index('ix_film_job_id', table_name='film')
    op.drop_table('film')
