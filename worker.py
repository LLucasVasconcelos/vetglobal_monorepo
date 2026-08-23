"""The worker: a separate process that turns `ENQUEUED` jobs into finished ones.

It is a **client** of the API, not a part of it. It imports nothing from `app/`,
holds no database credentials, and reaches the queue only through the two
`/internal/*` routes any other client could call. That is the whole point: the
service boundary D11 described and D28 made real is only real if crossing it
costs an HTTP request.

Three consequences worth naming, because they are the argument for the boundary:

- **It needs no `JWT_SECRET` and no `DB_PASSWORD`.** Importing the API's
  `Settings` would have demanded both -- they are required, with no defaults, on
  purpose -- so a process that only speaks HTTP would refuse to start without
  secrets it never uses. A worker that cannot read the database also cannot
  bypass the tenant isolation of D26.
- **Killing it is safe.** Nothing lives in this process. The job it was holding
  goes back to the queue when its lease expires, and another worker takes it
  with `attempt: 2` (D21). There is no shutdown handshake to get wrong.
- **It is not privileged.** Everything it does, you can do with `curl` -- which
  is what the assignment asked for when it said the completion endpoint
  simulates a worker callback.

Run it:

    uv run python worker.py                                   # one worker
    API_BASE_URL=http://127.0.0.1:8000 uv run python worker.py

Two of them, in two terminals, against a queue with two jobs is the `SKIP
LOCKED` demonstration: they claim different jobs at the same instant instead of
one waiting behind the other. Kill one mid-job and the lease hands its work to
the other.
"""

import asyncio
import contextlib
import logging
import re
import signal

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("worker")


class WorkerSettings(BaseSettings):
    """Everything this process is allowed to know.

    `API_BASE_URL` is deliberately absent from `.env.example`: that file
    documents the knobs the *API* reads, and `tests/test_config.py` fails if it
    ever documents one the API does not. The API has no business knowing its own
    address; the worker does, and it is set here or in the environment.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    internal_token: str
    api_base_url: str = "http://127.0.0.1:8000"
    # How long to wait before asking again when the queue is empty. Short enough
    # to feel immediate, long enough not to be a busy loop against Postgres.
    idle_sleep_seconds: float = 1.0


# --- the part that would be replaced by something real ----------------------

SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
SUMMARY_SENTENCES = 3
SUMMARY_MAX_CHARS = 600


class NothingToSummarize(Exception):
    """The document held no prose. A real extractor would fail the same way."""


def summarize(text: str) -> str:
    """A placeholder, and named like one.

    The assignment asks for a worker that *simulates* summarization: the
    exercise is the job lifecycle, not the quality of the summary. So this is
    deliberately dumb -- the opening sentences, trimmed on a word boundary --
    and deliberately **deterministic**, which matters twice over. A test can
    assert on the output, and a job retried after a lease expired produces the
    same answer as the attempt that died rather than a different one.

    The day a real model goes behind this, it goes behind *this function* and
    nothing else in the loop changes. What changes is everything around it: an
    API key, a per-job cost, a latency that would make the 60 second lease of
    D21 too short, and a clinical record leaving the machine. Those are
    decisions, not details, which is exactly why they are not made here.
    """
    sentences = [part.strip() for part in SENTENCE_END.split(text.strip()) if part.strip()]
    if not sentences:
        raise NothingToSummarize("the document holds no readable text")

    summary = " ".join(sentences[:SUMMARY_SENTENCES])
    if len(summary) <= SUMMARY_MAX_CHARS:
        return summary

    # Cut on a word boundary: a summary that stops mid-word reads as corruption.
    return summary[:SUMMARY_MAX_CHARS].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"


# --- the loop ---------------------------------------------------------------


async def run_once(client: httpx.AsyncClient) -> bool:
    """Claim one job, do the work, report the verdict. False when idle.

    Every failure mode here ends with the job *closed* rather than the process
    dead. A worker that crashes on a bad document leaves that document stuck in
    `PROCESSING` until its lease expires, three times over, and only then does
    the client learn anything -- so the exception becomes a `FAILED` verdict
    with a reason instead.
    """
    claim = await client.post("/internal/jobs/claim")
    if claim.status_code == 204:
        return False
    claim.raise_for_status()

    job = claim.json()
    try:
        verdict = {"status": "DONE", "summary": summarize(job["content"])}
    except Exception as exc:
        logger.warning("job %s could not be summarized: %s", job["job_id"], exc)
        verdict = {
            "status": "FAILED",
            "error_code": "EXTRACTION_FAILED",
            "message": f"Could not summarize {job['filename']}.",
        }

    # The fencing token. Without it, a worker that stalled past its lease would
    # silently overwrite the result of the worker that replaced it (D18).
    verdict["attempt"] = job["attempt"]

    result = await client.post(f"/internal/jobs/{job['job_id']}/complete", json=verdict)
    if result.status_code == 409:
        # This claim no longer owns the job: the lease expired and somebody else
        # redid the work. Losing a race is not this process's emergency -- the
        # job is in hand, and the next claim is the right next move.
        logger.warning(
            "job %s was taken from us: %s", job["job_id"], result.json().get("error_code")
        )
        return True

    result.raise_for_status()
    logger.info("job %s -> %s", job["job_id"], verdict["status"])
    return True


async def main() -> None:
    settings = WorkerSettings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Checked between jobs, never during one: a worker that dropped a job
        # halfway on shutdown would leave it to time out, when finishing it
        # takes milliseconds.
        loop.add_signal_handler(sig, stopping.set)

    async with httpx.AsyncClient(
        base_url=settings.api_base_url,
        headers={"X-Internal-Token": settings.internal_token},
        timeout=30.0,
    ) as client:
        logger.info("worker up, asking %s for work", settings.api_base_url)
        while not stopping.is_set():
            try:
                busy = await run_once(client)
            except httpx.HTTPError as exc:
                # The API restarting is not a reason to exit; the queue is still
                # there and so is the job.
                logger.warning("API unreachable (%s), retrying", exc)
                busy = False

            if not busy:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stopping.wait(), timeout=settings.idle_sleep_seconds)

    logger.info("worker down")


if __name__ == "__main__":
    asyncio.run(main())
