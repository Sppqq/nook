"""Bound output and stop a subprocess launched in its own session."""
import asyncio
from contextlib import suppress
import os
import signal


async def read_tail(stream, limit=4096):
    tail = b''
    while chunk := await stream.read(65536):
        tail = (tail + chunk)[-limit:]
    return tail


async def stop_process_group(proc, *, grace=2):
    streams = [stream for stream in (proc.stdout, proc.stderr) if stream is not None]
    if proc.returncode is not None and all(stream.at_eof() for stream in streams):
        return
    drains = [asyncio.create_task(read_tail(stream)) for stream in streams]
    finished = asyncio.gather(proc.wait(), *drains, return_exceptions=True)
    try:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            with suppress(ProcessLookupError):
                os.killpg(proc.pid, sig)
            try:
                await asyncio.wait_for(asyncio.shield(finished), grace)
                return
            except asyncio.TimeoutError:
                continue
    finally:
        if not finished.done():
            finished.cancel()
            with suppress(asyncio.CancelledError):
                await finished
