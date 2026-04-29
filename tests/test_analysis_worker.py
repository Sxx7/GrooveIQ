"""
GrooveIQ – Tests for the analysis worker pool's hung-worker SIGKILL path.

Regression for #30: when libavcodec spins indefinitely on a corrupted
bitstream (see #31), the per-task asyncio timeout used to abandon the
future but leave the worker subprocess hanging. After this fix, the
timeout path SIGKILLs the specific worker holding that ``request_id``
based on the heartbeat the worker emits when it pulls from the input
queue.
"""

from __future__ import annotations

import asyncio
import multiprocessing as mp
import os
import time

import pytest


# Module-level (and not a `test_` prefix) so the spawn-context subprocess
# can import this function by name.
def _hung_worker(input_queue, output_queue, worker_id):
    """Test worker target. Emits the 'started' heartbeat (matching the
    production protocol) so the pool can record which subprocess owns the
    in-flight request_id, then hangs forever — simulating a libavcodec
    C-code spin that doesn't respect Python signals."""
    my_pid = os.getpid()
    while True:
        item = input_queue.get()
        if item is None:
            return
        request_id, _file_path, _cached = item
        try:
            output_queue.put((request_id, "started", my_pid))
        except Exception:
            pass
        # Hang. SIGKILL is the only way out.
        while True:
            time.sleep(60)


@pytest.mark.asyncio
async def test_hung_worker_sigkilled_on_timeout(monkeypatch):
    """Without the #30 fix, this would either deadlock the pool entirely
    or take ANALYSIS_TIMEOUT (default 300s) and STILL leave the subprocess
    spinning. With the fix the timeout path looks up the worker holding
    that request_id and SIGKILLs it directly."""
    from app.core.config import settings
    from app.services.analysis_worker import AnalysisWorkerPool

    monkeypatch.setattr(settings, "ANALYSIS_TIMEOUT", 1)

    # Hand-construct the pool with a hung target so we don't depend on
    # essentia/onnx being installable in the test env (the production
    # _worker_main imports both at module top of its body).
    pool = AnalysisWorkerPool(num_workers=1)
    ctx = mp.get_context("spawn")
    pool._input_queue = ctx.Queue(maxsize=4)
    pool._output_queue = ctx.Queue()
    p = ctx.Process(
        target=_hung_worker,
        args=(pool._input_queue, pool._output_queue, 0),
        name="test-hung-0",
        daemon=True,
    )
    p.start()
    pool._workers.append(p)
    pool._running = True
    pool._collector_task = asyncio.create_task(pool._collect_results())

    try:
        initial_pid = p.pid
        assert initial_pid is not None, "worker subprocess failed to start"

        # Brief settle so the worker reaches its `input_queue.get()` block.
        await asyncio.sleep(0.2)

        t0 = time.monotonic()
        result = await pool.analyze("/tmp/grooveiq-test-fake.flac")
        elapsed = time.monotonic() - t0

        # (a) analyze() returned a timeout result.
        assert "Timed out after 1s" in result.get("analysis_error", ""), result
        # And it returned promptly — not 5 minutes later.
        assert elapsed < 3.0, f"analyze() took {elapsed:.1f}s (expected <3s)"

        # (b) the original subprocess is gone within 3s of the timeout.
        deadline = time.monotonic() + 3.0
        killed = False
        while time.monotonic() < deadline:
            try:
                os.kill(initial_pid, 0)
            except ProcessLookupError:
                killed = True
                break
            await asyncio.sleep(0.1)
        assert killed, f"Original pid={initial_pid} still alive 3s after timeout"

        # (c) the in-flight bookkeeping was cleared so we don't leak entries
        # for finished/killed requests.
        assert pool._in_flight == {}
        assert pool._pending == {}

    finally:
        pool._running = False
        if pool._collector_task:
            pool._collector_task.cancel()
            try:
                await pool._collector_task
            except asyncio.CancelledError:
                pass
        for w in pool._workers:
            if w.is_alive():
                w.kill()
            try:
                w.join(timeout=2)
            except Exception:
                pass
        pool._workers.clear()
        pool._pending.clear()
        pool._in_flight.clear()


def _quick_worker(input_queue, output_queue, worker_id):
    """Test worker that returns a synthetic result immediately. Used to
    verify the new 3-tuple protocol's happy path doesn't leak in-flight
    entries on successful completion."""
    my_pid = os.getpid()
    while True:
        item = input_queue.get()
        if item is None:
            return
        request_id, file_path, _cached = item
        output_queue.put((request_id, "started", my_pid))
        output_queue.put(
            (
                request_id,
                "result",
                {"file_path": file_path, "analyzed_at": int(time.time())},
            )
        )


@pytest.mark.asyncio
async def test_successful_analysis_clears_in_flight(monkeypatch):
    """Sanity: the new 3-tuple protocol doesn't leak in-flight entries
    when a worker completes normally."""
    from app.core.config import settings
    from app.services.analysis_worker import AnalysisWorkerPool

    monkeypatch.setattr(settings, "ANALYSIS_TIMEOUT", 5)

    pool = AnalysisWorkerPool(num_workers=1)
    ctx = mp.get_context("spawn")
    pool._input_queue = ctx.Queue(maxsize=4)
    pool._output_queue = ctx.Queue()
    p = ctx.Process(
        target=_quick_worker,
        args=(pool._input_queue, pool._output_queue, 0),
        name="test-quick-0",
        daemon=True,
    )
    p.start()
    pool._workers.append(p)
    pool._running = True
    pool._collector_task = asyncio.create_task(pool._collect_results())

    try:
        result = await pool.analyze("/tmp/grooveiq-test-quick.flac")
        assert result is not None
        assert result.get("file_path") == "/tmp/grooveiq-test-quick.flac"

        # Allow the collector loop one tick to drain any trailing messages.
        await asyncio.sleep(0.1)
        assert pool._in_flight == {}
        assert pool._pending == {}

    finally:
        pool._running = False
        if pool._collector_task:
            pool._collector_task.cancel()
            try:
                await pool._collector_task
            except asyncio.CancelledError:
                pass
        for w in pool._workers:
            if w.is_alive():
                w.kill()
            try:
                w.join(timeout=2)
            except Exception:
                pass
        pool._workers.clear()
        pool._pending.clear()
        pool._in_flight.clear()
