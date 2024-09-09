"""Normalize the bearing so always positive.

Revision ID: 43142bd50852
Revises: c0541aabafa8
Create Date: 2023-01-06 18:48:55.197296

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "43142bd50852"
down_revision = "c0541aabafa8"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("UPDATE link SET bearing = 360 + bearing WHERE bearing < 0")


def downgrade():
    pass
