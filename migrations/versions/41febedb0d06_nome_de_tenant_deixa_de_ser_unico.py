"""nome de tenant deixa de ser unico

Nome de clinica e' rotulo, nao identidade: duas clinicas reais podem se chamar
igual, e a unicidade recusaria um cadastro legitimo. O 409 que ela produzia
ainda dizia quais nomes ja existem.

Revision ID: 41febedb0d06
Revises: 2ac41aa7f0d2
Create Date: 2026-08-21 18:41:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "41febedb0d06"
down_revision: str | Sequence[str] | None = "2ac41aa7f0d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("uq_tenants_name"), "tenants", type_="unique")


def downgrade() -> None:
    op.create_unique_constraint(op.f("uq_tenants_name"), "tenants", ["name"])
