"""Update many existing VMs at once (``update-vm --range``).

Each VM goes through ``vmkit.update_workflow``: re-render and upload its VMX
(CPU, RAM, MAC, ISO), powering it off first if it is running -- a hard power
off, so shut the guests down cleanly beforehand when that matters.
"""

import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import vmkit

from recipes._batch import DEFAULT_JOBS, BatchResult, Outcome, run_batch
from recipes.errors import BatchPreflightError


def update_batch(
    connect: Callable[[], vmkit.Connection],
    names: Sequence[str],
    *,
    datastore: str,
    jobs: int = DEFAULT_JOBS,
    iso_paths: Mapping[str, str] | None = None,
    cancel: threading.Event | None = None,
    on_outcome: Callable[[Outcome[str]], None] | None = None,
    **update_options: Any,
) -> BatchResult[str]:
    """Run ``vmkit.update_workflow`` on every named VM, ``jobs`` at a time.

    Checks first that the names are distinct and every VM exists, raising
    ``BatchPreflightError`` listing all that are not. ``iso_paths`` gives a VM
    its own config ISO; ``update_options`` (cpus, mem_mb, remove_iso, power_on,
    ...) apply to every VM.
    """
    problems: list[str] = []
    for name in sorted({n for n in names if list(names).count(n) > 1}):
        problems.append(f"name {name} appears more than once in the batch")
    conn = connect()
    try:
        existing = vmkit.list_vm_names(conn.content)
    finally:
        conn.close()
    problems += [f"no VM named {n} on the host" for n in names if n not in existing]
    if problems:
        raise BatchPreflightError(problems)

    iso_paths = dict(iso_paths or {})

    def work(conn: vmkit.Connection, name: str, should_cancel: Callable[[], bool]):
        return vmkit.update_workflow(
            conn, name=name, datastore=datastore, iso_path=iso_paths.get(name),
            progress_context=name, should_cancel=should_cancel, **update_options,
        )

    return run_batch(
        names, name_of=lambda n: n, work=work, connect=connect, jobs=jobs,
        cancel=cancel, on_outcome=on_outcome,
    )
