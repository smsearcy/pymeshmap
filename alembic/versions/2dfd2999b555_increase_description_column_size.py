"""Increase description column size

Revision ID: 2dfd2999b555
Revises: 7737b0856caa
Create Date: 2021-01-05 08:49:37.135343

"""
import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "2dfd2999b555"
down_revision = "7737b0856caa"
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.alter_column(
        "node",
        "description",
        existing_type=sa.Unicode(length=200),
        type_=sa.Unicode(length=1024),
    )
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.alter_column(
        "node",
        "description",
        existing_type=sa.Unicode(length=1024),
        type_=sa.Unicode(length=200),
    )
    # ### end Alembic commands ###