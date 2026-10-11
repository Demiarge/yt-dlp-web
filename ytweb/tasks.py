"""Fire-and-forget tasks. Kept referenced so they aren't GC'd, cancelled together on shutdown."""
from __future__ import annotations

import asyncio

_tasks: set[asyncio.Task] = set()


def spawn(coro) -> asyncio.Task:
    task = asyncio.ensure_future(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


async def cancel_all() -> None:
    pending = list(_tasks)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
