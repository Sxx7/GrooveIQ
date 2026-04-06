"""
GrooveIQ – Pipeline run state tracking.

In-memory store for pipeline run instrumentation.  Each pipeline run
records per-step timing, status, metrics, and errors so the dashboard
can visualise the full pipeline flow in real time.

SSE subscribers receive events as each step starts/completes/fails.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class RunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


# ── Step names (canonical order) ─────────────────────────────────────

PIPELINE_STEPS = [
    "sessionizer",
    "track_scoring",
    "taste_profiles",
    "collab_filter",
    "ranker",
    "session_embeddings",
    "lastfm_cache",
    "sasrec",
    "session_gru",
]


# ── Data classes ─────────────────────────────────────────────────────

@dataclass
class StepResult:
    name: str
    status: StepStatus = StepStatus.PENDING
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    duration_ms: Optional[int] = None
    error: Optional[str] = None
    metrics: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "metrics": self.metrics,
        }


@dataclass
class PipelineRun:
    run_id: str
    started_at: float
    ended_at: Optional[float] = None
    status: RunStatus = RunStatus.RUNNING
    steps: Dict[str, StepResult] = field(default_factory=dict)
    trigger: str = "scheduled"  # "scheduled" | "manual" | "startup"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "status": self.status.value,
            "duration_ms": int((self.ended_at - self.started_at) * 1000) if self.ended_at else None,
            "trigger": self.trigger,
            "steps": [
                self.steps[name].to_dict()
                for name in PIPELINE_STEPS
                if name in self.steps
            ],
        }


# ── Singleton state ──────────────────────────────────────────────────

_runs: List[PipelineRun] = []
_current_run: Optional[PipelineRun] = None
_sse_queues: List[asyncio.Queue] = []
MAX_HISTORY = 20


def _broadcast(event: Dict[str, Any]) -> None:
    """Push an SSE event to all connected subscribers."""
    dead: List[asyncio.Queue] = []
    for q in _sse_queues:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        _sse_queues.remove(q)


def subscribe() -> asyncio.Queue:
    """Create a new SSE subscriber queue."""
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _sse_queues.append(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    """Remove an SSE subscriber."""
    if q in _sse_queues:
        _sse_queues.remove(q)


# ── Pipeline lifecycle ───────────────────────────────────────────────

def start_run(trigger: str = "scheduled") -> PipelineRun:
    """Begin tracking a new pipeline run."""
    global _current_run
    run = PipelineRun(
        run_id=uuid.uuid4().hex[:12],
        started_at=time.time(),
        trigger=trigger,
    )
    # Pre-populate all steps as pending.
    for name in PIPELINE_STEPS:
        run.steps[name] = StepResult(name=name)

    _current_run = run
    _runs.append(run)

    # Trim history.
    while len(_runs) > MAX_HISTORY:
        _runs.pop(0)

    _broadcast({
        "event": "pipeline_start",
        "run_id": run.run_id,
        "trigger": trigger,
        "timestamp": run.started_at,
    })
    return run


def finish_run(run: PipelineRun) -> None:
    """Mark a pipeline run as finished."""
    global _current_run
    run.ended_at = time.time()
    has_failures = any(
        s.status == StepStatus.FAILED for s in run.steps.values()
    )
    run.status = RunStatus.FAILED if has_failures else RunStatus.COMPLETED

    _broadcast({
        "event": "pipeline_end",
        "run_id": run.run_id,
        "status": run.status.value,
        "duration_ms": int((run.ended_at - run.started_at) * 1000),
        "timestamp": run.ended_at,
    })

    if _current_run is run:
        _current_run = None


def step_start(run: PipelineRun, step_name: str) -> None:
    """Mark a step as running."""
    step = run.steps[step_name]
    step.status = StepStatus.RUNNING
    step.started_at = time.time()

    _broadcast({
        "event": "step_start",
        "run_id": run.run_id,
        "step": step_name,
        "timestamp": step.started_at,
    })


def step_complete(
    run: PipelineRun,
    step_name: str,
    metrics: Optional[Dict[str, Any]] = None,
) -> None:
    """Mark a step as successfully completed."""
    step = run.steps[step_name]
    step.status = StepStatus.COMPLETED
    step.ended_at = time.time()
    step.duration_ms = int((step.ended_at - step.started_at) * 1000) if step.started_at else None
    if metrics:
        step.metrics = metrics

    _broadcast({
        "event": "step_complete",
        "run_id": run.run_id,
        "step": step_name,
        "duration_ms": step.duration_ms,
        "metrics": step.metrics,
        "timestamp": step.ended_at,
    })


def step_failed(
    run: PipelineRun,
    step_name: str,
    error: str,
) -> None:
    """Mark a step as failed."""
    step = run.steps[step_name]
    step.status = StepStatus.FAILED
    step.ended_at = time.time()
    step.duration_ms = int((step.ended_at - step.started_at) * 1000) if step.started_at else None
    step.error = error

    _broadcast({
        "event": "step_failed",
        "run_id": run.run_id,
        "step": step_name,
        "duration_ms": step.duration_ms,
        "error": error,
        "timestamp": step.ended_at,
    })


# ── Queries ──────────────────────────────────────────────────────────

def get_current_run() -> Optional[Dict[str, Any]]:
    """Return the currently running pipeline, if any."""
    if _current_run:
        return _current_run.to_dict()
    return None


def get_run_history(limit: int = 10) -> List[Dict[str, Any]]:
    """Return the last N pipeline runs, most recent first."""
    return [r.to_dict() for r in reversed(_runs[-limit:])]


def get_last_run() -> Optional[Dict[str, Any]]:
    """Return the most recently completed run."""
    for run in reversed(_runs):
        if run.status != RunStatus.RUNNING:
            return run.to_dict()
    return None
