"""
DAG 数据模型 — SQLModel + SQLite

节点状态机:
  pending → ready → assigned → running → done → reviewing → completed
                                                       → failed
                            → stuck
                            → aborting → interrupted
"""

from datetime import datetime, timezone
from typing import Optional
from sqlmodel import SQLModel, Field, Column, JSON, Text
import uuid


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return f"T-{uuid.uuid4().hex[:6].upper()}"


# ── DAG Node ───────────────────────────────────────────────────────


class DagNode(SQLModel, table=True):
    """A single node in the DAG."""

    __tablename__ = "dag_nodes"

    node_id: str = Field(default_factory=_new_id, primary_key=True)
    dag_id: str = Field(index=True)
    goal: str = Field(max_length=2048)
    title: str = Field(default="", max_length=128)
    assigned_roles: list[str] = Field(default_factory=list, sa_type=JSON)
    required_skills: list[str] = Field(default_factory=list, sa_type=JSON)
    dependencies: list[str] = Field(default_factory=list, sa_type=JSON)
    collaborators: list[str] = Field(default_factory=list, sa_type=JSON)
    acceptance_criteria: str = Field(default="", max_length=4096)

    # ── State machine ──
    status: str = Field(default="pending", max_length=32)
    assigned_agents: dict = Field(default_factory=dict, sa_type=JSON)
    channel_id: Optional[str] = Field(default=None)

    # ── Outputs ──
    outputs: list[str] = Field(default_factory=list, sa_type=JSON)

    # ── Self-check report (from sub-agent) ──
    self_check: Optional[str] = Field(default=None, sa_type=Text)

    # ── Error info (when status=failed) ──
    error: Optional[str] = Field(default=None, sa_type=Text)

    # ── Metadata ──
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


# ── Status transitions ─────────────────────────────────────────────

VALID_TRANSITIONS: dict[str, list[str]] = {
    "pending":      ["ready"],
    "ready":        ["assigned", "pending", "aborting"],       # can revert if deps broken
    "assigned":     ["running", "failed", "aborting"],
    "running":      ["done", "stuck", "aborting", "failed"],
    "done":         ["reviewing"],
    "reviewing":    ["completed", "failed"],
    "completed":    [],
    "failed":       [],
    "stuck":        ["aborting", "running"],       # resolved back to running
    "aborting":     ["interrupted"],
    "interrupted":  [],
}


def can_transition(from_status: str, to_status: str) -> bool:
    allowed = VALID_TRANSITIONS.get(from_status, [])
    return to_status in allowed


# ── DAG (container for a project's DAG) ────────────────────────────


class Dag(SQLModel, table=True):
    """A DAG = a project / a top-level plan."""

    __tablename__ = "dags"

    dag_id: str = Field(default_factory=lambda: f"DAG-{uuid.uuid4().hex[:8]}", primary_key=True)
    title: str = Field(max_length=256)
    description: str = Field(default="", max_length=4096)
    goal: str = Field(default="", max_length=4096)

    # Overall state
    status: str = Field(default="planning", max_length=32)  # planning / running / completed / failed

    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


# ── Events log (for signal/broadcast history) ──────────────────────


class DagEvent(SQLModel, table=True):
    """Persisted event log for DAG signals and broadcasts."""

    __tablename__ = "dag_events"

    id: Optional[int] = Field(default=None, primary_key=True)
    dag_id: str = Field(index=True)
    event_type: str = Field(max_length=64)   # node.status_changed / dag.structure_changed / ...
    source_node_id: Optional[str] = Field(default=None)
    data: str = Field(sa_type=Text, default="{}")
    created_at: datetime = Field(default_factory=_utcnow)
