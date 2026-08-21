"""Importing this package registers every table on `Base.metadata`.

`migrations/env.py` imports it so that autogenerate sees the full schema.
"""

from app.models.document import Document
from app.models.job import Job, JobStatus
from app.models.pet import Pet
from app.models.tenant import Tenant, User

__all__ = ["Document", "Job", "JobStatus", "Pet", "Tenant", "User"]
