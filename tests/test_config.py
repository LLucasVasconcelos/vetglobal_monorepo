"""`.env.example` and `Settings` have to describe the same set of knobs.

A field added to one and forgotten in the other is silent: the application keeps
booting on defaults while the file that documents it lies. This test is the only
thing that catches it.
"""

import re
from pathlib import Path

from app.core.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"
ASSIGNMENT = re.compile(r"^([A-Z][A-Z0-9_]*)=", re.MULTILINE)

# Read by docker-compose.yml, never by the application. The single .env feeds
# both sides, so `.env.example` legitimately holds more than `Settings` does.
COMPOSE_ONLY = {"DB_BIND_HOST"}


def documented_keys() -> set[str]:
    return set(ASSIGNMENT.findall(ENV_EXAMPLE.read_text()))


def test_env_example_matches_settings():
    fields = {name.upper() for name in Settings.model_fields}
    documented = documented_keys() - COMPOSE_ONLY

    assert fields - documented == set(), "setting missing from .env.example"
    assert documented - fields == set(), ".env.example documents an unread variable"


def test_secrets_have_no_default():
    """A guessable signing key makes every token forgeable, and D26 rests on
    the token being the only source of `tenant_id`."""
    for name in ("db_password", "jwt_secret", "internal_token"):
        assert Settings.model_fields[name].is_required(), f"{name} must not have a default"


def test_database_url_percent_encodes_the_password():
    settings = Settings(
        db_user="vet",
        db_password="p@ss/word",
        db_host="db.example",
        db_port=5432,
        db_name="vetglobal",
        jwt_secret="x",
        internal_token="y",
    )

    assert settings.database_url == (
        "postgresql+asyncpg://vet:p%40ss%2Fword@db.example:5432/vetglobal"
    )
