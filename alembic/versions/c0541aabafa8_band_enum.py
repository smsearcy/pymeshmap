"""Fix for RF band typo (and now using enum).

Revision ID: c0541aabafa8
Revises: 0ab33c522eef
Create Date: 2022-07-26 16:25:53.305063

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "c0541aabafa8"
down_revision = "0ab33c522eef"
branch_labels = None
depends_on = None


def upgrade():
    # Fix typo in the 3 GHz band
    op.execute("UPDATE node SET band = '3GHz' WHERE band = '3GHZ'")


def downgrade():
    pass
