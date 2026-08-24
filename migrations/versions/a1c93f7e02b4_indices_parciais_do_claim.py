"""índices parciais do claim (D54)

O `ix_jobs_status_id` serve a primeira metade de "reivindicável"
(`status = 'ENQUEUED'`) e não serve a segunda (`PROCESSING` com lease vencido):
num sistema ocupado metade da tabela está em `PROCESSING`, casar isso são cem mil
entradas de índice, e o planejador conclui — corretamente — que varrer a tabela
sai mais barato.

Medido com 200.001 jobs, dos quais 100.000 `PROCESSING` com lease vivo:

    sem estes índices   Seq Scan   3200 buffers   13,49 ms
    com estes índices   BitmapOr      7 buffers    0,097 ms

Parciais de propósito: um job `DONE` não entra em nenhum dos dois, então eles
crescem com a fila viva e não com a tabela.

Revision ID: a1c93f7e02b4
Revises: 41febedb0d06
"""

from alembic import op

revision = "a1c93f7e02b4"
down_revision = "41febedb0d06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_jobs_claimable",
        "jobs",
        ["id"],
        postgresql_where="status = 'ENQUEUED'",
    )
    op.create_index(
        "ix_jobs_expiring",
        "jobs",
        ["lease_expires_at", "id"],
        postgresql_where="status = 'PROCESSING'",
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_expiring", table_name="jobs")
    op.drop_index("ix_jobs_claimable", table_name="jobs")
