"""Add continuation_of_job_id and continues to the job table

Revision ID: a8b9c0d1e2f3
Revises: f7a8b9c0d1e2
Create Date: 2026-10-28

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a8b9c0d1e2f3'
down_revision = 'f7a8b9c0d1e2'
branch_labels = None
depends_on = None


def upgrade():
    """
    Join a film split over two sides or discs into one file.

    continuation_of_job_id - the job whose film this disc continues (side B of a flipper, disc 2): its
                             main title is appended to that film's main feature. NULL for an ordinary disc
    continues              - the job's film goes on on another side or disc still to be ripped; the
                             completed shipper holds the job's folder while it is set
    """
    with op.batch_alter_table('job', schema=None) as batch_op:
        batch_op.add_column(sa.Column('continuation_of_job_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('continues', sa.Boolean(), nullable=True))


def downgrade():
    with op.batch_alter_table('job', schema=None) as batch_op:
        batch_op.drop_column('continues')
        batch_op.drop_column('continuation_of_job_id')
