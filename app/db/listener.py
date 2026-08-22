"""The Postgres side of the long poll: one dedicated `LISTEN` connection per
instance, and the fan-out to the polls waiting inside this process (D13).

Both halves of the protocol live in this module -- what `complete` emits and
what the listener receives -- because a channel name spelled in two files is a
channel name that will eventually be spelled two ways.

**The connection is deliberately outside the SQLAlchemy pool.** A pooled
connection is checked out for the length of a statement; `LISTEN` has to hold
one open for the lifetime of the process. Taking that one from the pool would
mean one fewer connection for every request that has actual work to do, and a
pool that recycles connections would silently stop listening.

**Nothing here is load-bearing for correctness.** A notification is not
persistent: it reaches whoever is listening at that instant and is otherwise
gone. That is why every waiter also re-queries on a timer
(`POLL_RECHECK_SECONDS`), and why this whole module may fail, reconnect, or
never connect at all without a poll ever giving a wrong answer. The fan-out in
memory makes the answer fast; the database is what makes it true -- which is
also why holding these waiters in process memory does not break invariant 1.
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import Iterator

import asyncpg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings

CHANNEL = "document_events"

# Long enough not to hammer a database that is down, short enough that a poll
# opened during a restart still gets its wake-up rather than sitting out the
# whole 25 seconds on rechecks.
RECONNECT_DELAY_SECONDS = 2.0

logger = logging.getLogger(__name__)


async def notify_job_finished(
    db: AsyncSession, *, document_id: int, job_id: int, status: str
) -> None:
    """Announce a finished job to whoever is polling for it.

    Emitted on the same connection and inside the same transaction as the
    `UPDATE` that closed the job, on purpose: Postgres holds notifications until
    `COMMIT`, so a listener is never woken for a row it cannot read yet, and a
    transaction that rolls back announces nothing. Emitting after the commit
    instead would open both windows -- a wake-up racing its own data, and a
    committed job whose announcement was lost with the next line of code.
    """
    await db.execute(
        text("SELECT pg_notify(:channel, :payload)"),
        {
            "channel": CHANNEL,
            "payload": json.dumps(
                {"document_id": document_id, "job_id": job_id, "status": status}
            ),
        },
    )


class DocumentEvents:
    """Waiters on this instance, keyed by document, woken by `NOTIFY`."""

    def __init__(self) -> None:
        self._waiters: dict[int, set[asyncio.Event]] = {}
        self._task: asyncio.Task[None] | None = None
        self._listening = asyncio.Event()

    # --- the poll's side ---------------------------------------------------

    @contextlib.contextmanager
    def subscribe(self, document_id: int) -> Iterator[asyncio.Event]:
        """Register interest in a document *before* reading its state.

        The context manager is what guarantees the other half: a waiter left
        behind by a client that hung up would keep this dictionary growing for
        the life of the process.
        """
        event = asyncio.Event()
        self._waiters.setdefault(document_id, set()).add(event)
        try:
            yield event
        finally:
            waiters = self._waiters.get(document_id)
            if waiters is not None:
                waiters.discard(event)
                if not waiters:
                    del self._waiters[document_id]

    async def wait_until_listening(self) -> None:
        """Resolves once this instance is actually subscribed to the channel.

        For tests and for a worker that wants to know the fast path is up. No
        request waits on this: a poll that starts before the connection is ready
        is answered by the recheck, a little later.
        """
        await self._listening.wait()

    # --- the connection's side ---------------------------------------------

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="document-events-listener")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        """Reconnect forever. The listener going down degrades latency, not
        correctness, so it must never be the reason the process dies."""
        while True:
            try:
                await self._listen_until_closed()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("listener on %s failed, retrying", CHANNEL, exc_info=True)
            await asyncio.sleep(RECONNECT_DELAY_SECONDS)

    async def _listen_until_closed(self) -> None:
        connection = await asyncpg.connect(
            user=settings.db_user,
            password=settings.db_password,
            host=settings.db_host,
            port=settings.db_port,
            database=settings.db_name,
        )
        closed = asyncio.Event()
        try:
            connection.add_termination_listener(lambda _connection: closed.set())
            await connection.add_listener(CHANNEL, self._on_notify)
            logger.info("listening on %s", CHANNEL)
            self._listening.set()
            await closed.wait()
        finally:
            self._listening.clear()
            # `terminate()` and not `close()`: close is a round trip, and this
            # runs both when the connection is already gone and from inside a
            # cancellation, where there is no awaiting left to do.
            connection.terminate()

    def _on_notify(self, _connection: object, _pid: int, _channel: str, payload: str) -> None:
        """Wake every poll watching this document.

        Only `document_id` is read. The `job_id` and `status` in the payload are
        for a human tailing the channel: the waiter re-reads the row anyway,
        because a notification can be delivered twice, out of order, or be about
        a job the client is not waiting for -- and because the database, not
        this message, is the answer.
        """
        try:
            document_id = int(json.loads(payload)["document_id"])
        except (TypeError, ValueError, KeyError):
            logger.warning("ignoring malformed payload on %s", CHANNEL)
            return

        for event in self._waiters.get(document_id, ()):
            event.set()


# One per process, mirroring the one connection it owns.
document_events = DocumentEvents()
