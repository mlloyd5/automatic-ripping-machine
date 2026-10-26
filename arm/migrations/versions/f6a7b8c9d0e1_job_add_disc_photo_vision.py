"""Add disc_photo_vision column to job table

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-10-26

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f6a7b8c9d0e1'
down_revision = 'e5f6a7b8c9d0'
branch_labels = None
depends_on = None


def upgrade():
    """
    Add the vision model column to the job table.

    disc_photo_vision - JSON (title, subtitle, year, studio, ...) a vision model read off the
                        disc photo, offered as a title search when nothing online matched it
    """
    with op.batch_alter_table('job', schema=None) as batch_op:
        batch_op.add_column(sa.Column('disc_photo_vision', sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table('job', schema=None) as batch_op:
        batch_op.drop_column('disc_photo_vision')
