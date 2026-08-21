"""schema inicial: tenants, users, pets, documents, jobs

Revision ID: 2ac41aa7f0d2
Revises:
Create Date: 2026-08-21 11:54:06.868188

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2ac41aa7f0d2"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

job_status = sa.Enum("ENQUEUED", "PROCESSING", "DONE", "FAILED", name="job_status")


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenants")),
        sa.UniqueConstraint("name", name=op.f("uq_tenants_name")),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_users_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )
    op.create_index(op.f("ix_users_tenant_id"), "users", ["tenant_id"], unique=False)
    op.create_table(
        "pets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("owner_name", sa.String(length=120), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_pets_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pets")),
    )
    op.create_index(op.f("ix_pets_tenant_id"), "pets", ["tenant_id"], unique=False)
    op.create_table(
        "documents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("pet_id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=120), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        # The file itself lives here, not on local disk (D14).
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["pet_id"], ["pets.id"], name=op.f("fk_documents_pet_id_pets")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_documents_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
    )
    op.create_index("ix_documents_tenant_id_id", "documents", ["tenant_id", "id"], unique=False)
    # Dedupe key (D24), enforced by the database so two concurrent uploads of
    # the same content cannot both win.
    op.create_index("uq_documents_pet_id_sha256", "documents", ["pet_id", "sha256"], unique=True)
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("status", job_status, server_default="ENQUEUED", nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "enqueued_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["document_id"], ["documents.id"], name=op.f("fk_jobs_document_id_documents")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_jobs_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
    )
    # Poll cursor (D23).
    op.create_index("ix_jobs_document_id_id", "jobs", ["document_id", "id"], unique=False)
    # Claim hot path (D21).
    op.create_index("ix_jobs_status_id", "jobs", ["status", "id"], unique=False)
    op.create_index(op.f("ix_jobs_tenant_id"), "jobs", ["tenant_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_jobs_tenant_id"), table_name="jobs")
    op.drop_index("ix_jobs_status_id", table_name="jobs")
    op.drop_index("ix_jobs_document_id_id", table_name="jobs")
    op.drop_table("jobs")
    # create_table creates the enum type implicitly; drop_table does not remove
    # it, so a downgrade + upgrade would fail with "type already exists".
    job_status.drop(op.get_bind(), checkfirst=False)
    op.drop_index("uq_documents_pet_id_sha256", table_name="documents")
    op.drop_index("ix_documents_tenant_id_id", table_name="documents")
    op.drop_table("documents")
    op.drop_index(op.f("ix_users_tenant_id"), table_name="users")
    op.drop_table("users")
    op.drop_index(op.f("ix_pets_tenant_id"), table_name="pets")
    op.drop_table("pets")
    op.drop_table("tenants")
