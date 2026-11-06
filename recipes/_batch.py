"""Run one operation over many VMs, ``jobs`` at a time.

The engine behind ``deploy-vm --range`` and ``update-vm --range``. Native rather
than a wrapper around GNU parallel, so a batch can be checked as a whole before
anything starts (names, IPs, datastore space for all of it, unique MACs) -- N
independent processes each see the same free space and each "fit".

Each worker thread opens its own ESXi session (pyVmomi sessions are not shared
across threads here) and reuses it for every VM it takes. A failure is recorded
against its VM and the rest carry on. Cancelling (``cancel.set()``, which the
CLIs do on Ctrl-C) stops new VMs from starting and asks in-flight vSphere tasks
to cancel; VMs never started are reported as skipped.
"""

import contextvars
import logging
import queue
import re
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

import vmkit

log = logging.getLogger("recipes.batch")

T = TypeVar("T")

#: Placeholder a batch template uses for each number of its range.
PLACEHOLDER = "{n}"
#: Upper bound on one batch, so a typo like 1-10000 fails instead of cloning.
MAX_BATCH = 256
DEFAULT_JOBS = 3

#: The VM a batch worker is currently on; log filters read it to tag lines.
current_item: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "batch_current_item", default=None
)

_PART = re.compile(r"^(\d+)(?:-(\d+))?$")


def parse_range(spec: str) -> list[str]:
    """``"91-96"`` -> ``["91", ..., "96"]``; also ``"91,92,95"`` and ``"1-3,7"``.

    A leading zero sets the width: ``"01-10"`` -> ``"01" .. "10"``. Raises
    ``ValueError`` for an empty, malformed, reversed or duplicated range, or one
    longer than ``MAX_BATCH``.
    """
    out: list[str] = []
    for part in (p.strip() for p in spec.split(",")):
        m = _PART.match(part)
        if not m:
            raise ValueError(
                f"invalid range part {part!r} in {spec!r}: expected N or N-M, comma-separated"
            )
        start_text, end_text = m.group(1), m.group(2) or m.group(1)
        start, end = int(start_text), int(end_text)
        if end < start:
            raise ValueError(f"range {part!r} runs backwards")
        width = len(start_text) if start_text.startswith("0") and len(start_text) > 1 else 0
        if end - start + 1 + len(out) > MAX_BATCH:
            raise ValueError(f"range {spec!r} is longer than {MAX_BATCH} VMs")
        out.extend(str(n).zfill(width) for n in range(start, end + 1))
    seen: set[str] = set()
    dupes = sorted({n for n in out if n in seen or seen.add(n)}, key=out.index)
    if dupes:
        raise ValueError(f"range {spec!r} repeats {', '.join(dupes)}")
    return out


def expand(template: str, n: str) -> str:
    """Substitute ``n`` for every ``{n}`` in ``template``."""
    return template.replace(PLACEHOLDER, n)


@dataclass(frozen=True)
class Outcome(Generic[T]):
    """What happened to one VM of a batch."""

    name: str
    item: T
    ok: bool
    result: Any = None
    error: BaseException | None = None
    #: Never started: the batch was cancelled first.
    skipped: bool = False
    #: Extra detail, e.g. what failure cleanup did.
    note: str = ""


@dataclass(frozen=True)
class BatchResult(Generic[T]):
    """Every VM's outcome, in the order the batch listed them."""

    outcomes: tuple[Outcome[T], ...]
    cancelled: bool = False

    @property
    def succeeded(self) -> list[Outcome[T]]:
        return [o for o in self.outcomes if o.ok]

    @property
    def failed(self) -> list[Outcome[T]]:
        return [o for o in self.outcomes if not o.ok and not o.skipped]

    @property
    def skipped(self) -> list[Outcome[T]]:
        return [o for o in self.outcomes if o.skipped]

    @property
    def ok(self) -> bool:
        return len(self.succeeded) == len(self.outcomes)


def run_batch(
    items: Sequence[T],
    *,
    name_of: Callable[[T], str],
    work: Callable[[vmkit.Connection, T, Callable[[], bool]], Any],
    connect: Callable[[], vmkit.Connection],
    jobs: int = DEFAULT_JOBS,
    on_failure: Callable[[vmkit.Connection | None, T, BaseException], str] | None = None,
    cancel: threading.Event | None = None,
    on_outcome: Callable[[Outcome[T]], None] | None = None,
) -> BatchResult[T]:
    """Run ``work(conn, item, should_cancel)`` for every item, ``jobs`` at a time.

    ``connect`` opens one session per worker, reused across that worker's items
    and closed when it finishes; if it fails, the item it was opened for fails
    and the next item retries it. ``on_failure(conn, item, error)`` runs after a
    failed item (e.g. to clean up) and returns a note for its outcome.
    ``on_outcome`` is called as each item finishes, from the worker's thread.

    Blocks until every worker is done. A ``KeyboardInterrupt`` while waiting
    sets ``cancel`` and keeps waiting for in-flight items to wind down; a second
    one propagates.
    """
    if jobs < 1:
        raise ValueError("jobs must be at least 1")
    cancel = cancel or threading.Event()
    results: list[Outcome[T] | None] = [None] * len(items)
    pending: queue.Queue[tuple[int, T]] = queue.Queue()
    for index, item in enumerate(items):
        pending.put((index, item))

    def record(index: int, outcome: Outcome[T]) -> None:
        results[index] = outcome
        if on_outcome is not None:
            try:
                on_outcome(outcome)
            except Exception:  # reporting must not take a worker down
                log.exception("on_outcome callback failed for %s", outcome.name)

    def worker() -> None:
        conn: vmkit.Connection | None = None
        try:
            while True:
                try:
                    index, item = pending.get_nowait()
                except queue.Empty:
                    return
                name = name_of(item)
                if cancel.is_set():
                    record(index, Outcome(name, item, ok=False, skipped=True,
                                          note="batch cancelled before it started"))
                    continue
                token = current_item.set(name)
                try:
                    if conn is None:
                        conn = connect()
                    result = work(conn, item, cancel.is_set)
                except Exception as exc:
                    note = ""
                    if on_failure is not None:
                        try:
                            note = on_failure(conn, item, exc)
                        except Exception as cleanup_exc:
                            note = f"cleanup after the failure also failed: {cleanup_exc}"
                    log.error("%s", exc)
                    record(index, Outcome(name, item, ok=False, error=exc, note=note))
                else:
                    record(index, Outcome(name, item, ok=True, result=result))
                finally:
                    current_item.reset(token)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    log.debug("Closing a batch session failed", exc_info=True)

    threads = [
        threading.Thread(target=worker, name=f"batch-{i + 1}", daemon=True)
        for i in range(min(jobs, len(items)))
    ]
    for t in threads:
        t.start()

    interrupted = False
    while any(t.is_alive() for t in threads):
        try:
            for t in threads:
                t.join(timeout=0.2)
        except KeyboardInterrupt:
            if interrupted:
                raise
            interrupted = True
            cancel.set()
            log.warning("Cancelling: no new VMs will start; in-flight ones are being "
                        "stopped. Press Ctrl-C again to abort immediately.")

    return BatchResult(
        outcomes=tuple(o for o in results if o is not None),
        cancelled=cancel.is_set(),
    )
