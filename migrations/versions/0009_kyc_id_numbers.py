"""KYC: ID number and CAC number for the automated check (Youverify), and what it returned

Revision ID: 3c9e4a7b21d0
Revises: 1fdd6b6f0c75
Create Date: 2026-11-23 12:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "3c9e4a7b21d0"
down_revision = "1fdd6b6f0c75"
branch_labels = None
depends_on = None

COLUMNS = (
    ("id_type", 20),
    ("id_number", 40),
    ("id_number_last4", 4),
    ("registration_number", 30),
    ("verified_person_name", 200),
    ("verified_business_name", 300),
)


def upgrade() -> None:
    for name, length in COLUMNS:
        op.add_column("kyc_submissions", sa.Column(name, sa.String(length=length), nullable=True))


def downgrade() -> None:
    for name, _ in reversed(COLUMNS):
        op.drop_column("kyc_submissions", name)
