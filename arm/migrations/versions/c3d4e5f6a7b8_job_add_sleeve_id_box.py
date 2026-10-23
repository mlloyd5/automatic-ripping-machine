"""Add sleeve_id and box columns to job table

Revision ID: c3d4e5f6a7b8
Revises: d4e5f6a7b8c9
Create Date: 2026-10-21

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c3d4e5f6a7b8'
down_revision = 'd4e5f6a7b8c9'
branch_labels = None
depends_on = None


def upgrade():
    """
    Add the sleeve label columns to the job table.

    sleeve_id     - code printed on the disc's sleeve label (T6-0001, T6-0002, ...), assigned once
    label_printed - when the label last came out of the printer; NULL means it is still to print
    box           - name of the box the sleeve was filed in, free text
    """
    with op.batch_alter_table('job', schema=None) as batch_op:
        batch_op.add_column(sa.Column('sleeve_id', sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('label_printed', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('box', sa.String(length=64), nullable=True))
        batch_op.create_index('ix_job_sleeve_id', ['sleeve_id'], unique=False)


def downgrade():
    with op.batch_alter_table('job', schema=None) as batch_op:
        batch_op.drop_index('ix_job_sleeve_id')
        batch_op.drop_column('box')
        batch_op.drop_column('label_printed')
        batch_op.drop_column('sleeve_id')
