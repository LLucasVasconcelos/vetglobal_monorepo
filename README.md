# VetGlobal Backend

Asynchronous clinical document summarization API.

A document is uploaded for a pet, a summarization job is queued, a worker
processes it, and the frontend follows progress through long polling —
without holding any per-request state in process memory.

> **Status:** early scaffolding. Endpoints are being implemented stage by stage;
> only `GET /health` exists so far.

---

## Requirements

You need exactly two things installed:

| tool | why |
|---|---|
| **Docker** + **Docker Compose** | runs PostgreSQL 16 |
| **uv** | manages Python and the dependencies |

You do **not** need a system Python. `uv` downloads and pins the required
interpreter (3.12, see `.python-version`) on its own, isolated from anything
already installed on your machine.

---

## Setup

### 1. Install uv

**macOS / Linux**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Windows (PowerShell)**

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**Alternatives:** `brew install uv` · `pipx install uv` · `pacman -S uv`

The installer puts `uv` in `~/.local/bin`. If the shell cannot find it, add that
directory to your `PATH` and reopen the terminal:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc   # bash
fish_add_path ~/.local/bin                                 # fish
```

Check it:

```bash
uv --version
```

### 2. Install Python and the dependencies

```bash
uv sync
```

This reads `.python-version` and `pyproject.toml`, downloads CPython 3.12 if it
is missing, creates `.venv/`, and installs everything from `uv.lock` — the exact
versions this project was built against.

Confirm the interpreter:

```bash
uv run python --version     # Python 3.12.x
```

### 3. Create your `.env`

```bash
cp .env.example .env
```

Then open `.env` and fill in the three blank values. Generate each secret with:

```bash
uv run python -c "import secrets; print(secrets.token_urlsafe(32))"
```

| variable | what it is |
|---|---|
| `DB_PASSWORD` | password for the Postgres container |
| `JWT_SECRET` | signs the authentication tokens |
| `INTERNAL_TOKEN` | shared secret required by the `/internal/*` worker endpoints |

A **single `.env` feeds both sides**: Docker Compose substitutes the variables
when starting Postgres, and the application reads the same names through
`pydantic-settings`. There are no default values in `docker-compose.yml` on
purpose — a missing variable fails loudly instead of silently starting the
database with credentials the application does not have.

`.env` is git-ignored. Only `.env.example` is committed.

### 4. Start PostgreSQL

```bash
docker compose up -d
```

Wait until it reports healthy:

```bash
docker compose ps
```

The port is published on one explicit address, never on `0.0.0.0`. It defaults
to `127.0.0.1`, which keeps the database unreachable from the network; set
`DB_BIND_HOST` in `.env` to expose it to another machine on purpose — and then
`DB_HOST` has to match, or the application gets `ConnectionRefusedError`.

### 5. Apply the migrations

```bash
uv run alembic upgrade head
```

### 6. Seed the demo tenants

```bash
uv run python -m scripts.seed
```

There is no sign-up endpoint (deliberately out of scope), so logins come
from here. It creates two clinics with one user each:

| tenant | email | password |
|---|---|---|
| Clinica Aurora | `vet@aurora.test` | `vetglobal` |
| Clinica Boreal | `vet@boreal.test` | `vetglobal` |

Two tenants is not decoration: it is what makes cross-tenant isolation testable —
Aurora asking for a Boreal document has to get a `404`, not a `403`.

The script is idempotent; running it twice changes nothing.

---

## Running

```bash
uv run uvicorn app.main:app --reload
```

- API: <http://127.0.0.1:8000>
- Interactive docs: <http://127.0.0.1:8000/docs>
- Health check: <http://127.0.0.1:8000/health>

## Tests

```bash
uv run pytest -q
```

## Linting

```bash
uv run ruff check .          # report
uv run ruff check . --fix    # fix what is auto-fixable
uv run ruff format .         # format
```

---

## Troubleshooting

**`port is already allocated` on `docker compose up`**
Something else already listens on 5432 — often another project's Postgres.
Either stop it, or set a different `DB_PORT` in `.env` (for example `5433`).

**`ConnectionRefusedError` on `alembic upgrade head`**
First check `docker compose ps` and wait for `healthy`.

If the container reports `healthy` but the `PORTS` column is **empty**, the
container was created while the port was still taken: Compose kept the container
but never published the mapping, and a later `docker compose up -d` only *starts*
it without reapplying it. The database works internally and is unreachable from
outside. Recreate it:

```bash
docker compose up -d --force-recreate
```

`PORTS` should then read `127.0.0.1:5432->5432/tcp`.

**`uv: command not found`**
`~/.local/bin` is not on your `PATH` — see step 1.

**Changed `DB_USER` / `DB_PASSWORD` / `DB_NAME` and cannot connect**
Those only take effect when the volume is first created. Recreate it:

```bash
docker compose down -v && docker compose up -d
```

---

## Project layout

```
app/
  main.py       FastAPI application
  core/         settings, error handling, security
  db/           engine, session, LISTEN/NOTIFY listener
  models/       SQLAlchemy models
  schemas/      Pydantic request/response models
  services/     business logic — routes never touch the database directly
  api/          route handlers
migrations/     Alembic migrations
tests/
```
