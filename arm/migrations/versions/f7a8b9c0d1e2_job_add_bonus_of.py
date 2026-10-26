"""Add bonus_of_job_id and bonus_of_film_id to the job table

Revision ID: f7a8b9c0d1e2
Revises: e5f6a7b8c9d0
Create Date: 2026-10-26

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f7a8b9c0d1e2'
down_revision = 'e5f6a7b8c9d0'
branch_labels = None
depends_on = None


def upgrade():
    """
    Mark a disc as the bonus disc of an earlier job.

    bonus_of_job_id  - the job whose film the disc belongs to; every title it rips is filed as that
                       film's extras. NULL for an ordinary disc
    bonus_of_film_id - one film of that job's double feature (film table) when the disc belongs to it
                       rather than to the job's own title
    """
    with op.batch_alter_table('job', schema=None) as batch_op:
        batch_op.add_column(sa.Column('bonus_of_job_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('bonus_of_film_id', sa.Integer(), nullable=True))


def downgrade():
    with op.batch_alter_table('job', schema=None) as batch_op:
        batch_op.drop_column('bonus_of_film_id')
        batch_op.drop_column('bonus_of_job_id')
