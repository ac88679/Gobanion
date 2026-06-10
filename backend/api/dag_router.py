"""
DAG API Router — 子 Agent / 外部通过 HTTP 操作 DAG
"""

import json
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, HTTPException

from pydantic import BaseModel
from services import DagService
from services.logger import get_logger

log = get_logger("dag_router")

router = APIRouter(prefix="/api/v1/dag", tags=["dag"])

# ── Service instance (injected by main) ──
_service: Optional[DagService] = None


def init(service: DagService) -> None:
    global _service
    _service = service


def get_svc() -> DagService:
    if _service is None:
        raise RuntimeError("DagService not initialized")
    return _service


# ── Schemas ────────────────────────────────────────────────────────


class CreateDagRequest(BaseModel):
    title: str
    goal: str
    description: str = ""


class CreateNodeRequest(BaseModel):
    goal: str
    assigned_roles: list[str] = []
    required_skills: list[str] = []
    dependencies: list[str] = []
    acceptance_criteria: str = ""


class CreateNodesBatchRequest(BaseModel):
    nodes: list[CreateNodeRequest]


class TransitionRequest(BaseModel):
    status: str
    outputs: list[str] = []
    error: Optional[str] = None
    self_check: Optional[list] = None
    assigned_agents: dict = {}


# ── DAG endpoints ──────────────────────────────────────────────────


@router.post("")
def create_dag(body: CreateDagRequest):
    svc = get_svc()
    dag = svc.create_dag(title=body.title, goal=body.goal, description=body.description)
    return {"dag_id": dag.dag_id, "title": dag.title, "status": dag.status}


@router.get("")
def list_dags(limit: int = 20):
    svc = get_svc()
    dags = svc.list_dags(limit=limit)
    return [
        {
            "dag_id": d.dag_id, "title": d.title, "status": d.status,
            "created_at": d.created_at.isoformat(),
            "_node_count": len(svc.get_nodes_by_dag(d.dag_id)),
        }
        for d in dags
    ]


@router.get("/{dag_id}")
def get_dag(dag_id: str):
    svc = get_svc()
    dag = svc.get_dag(dag_id)
    if not dag:
        raise HTTPException(404, "DAG not found")
    nodes = svc.get_nodes_by_dag(dag_id)
    return {
        "dag_id": dag.dag_id,
        "title": dag.title,
        "goal": dag.goal,
        "status": dag.status,
        "created_at": dag.created_at.isoformat(),
        "nodes": [_node_to_dict(n) for n in nodes],
    }


# ── Node endpoints ─────────────────────────────────────────────────


@router.post("/{dag_id}/nodes")
def create_node(dag_id: str, body: CreateNodeRequest):
    svc = get_svc()
    node = svc.create_node(
        dag_id=dag_id,
        goal=body.goal,
        assigned_roles=body.assigned_roles,
        required_skills=body.required_skills,
        dependencies=body.dependencies,
        acceptance_criteria=body.acceptance_criteria,
    )
    return _node_to_dict(node)


@router.post("/{dag_id}/nodes/batch")
def create_nodes_batch(dag_id: str, body: CreateNodesBatchRequest):
    svc = get_svc()
    nodes = svc.bulk_create_nodes(dag_id, [n.model_dump() for n in body.nodes])
    return [_node_to_dict(n) for n in nodes]


@router.get("/{dag_id}/nodes")
def list_nodes(dag_id: str, status: Optional[str] = None):
    svc = get_svc()
    nodes = svc.get_nodes_by_dag(dag_id)
    if status:
        nodes = [n for n in nodes if n.status == status]
    return [_node_to_dict(n) for n in nodes]


@router.get("/{dag_id}/nodes/{node_id}")
def get_node(dag_id: str, node_id: str):
    svc = get_svc()
    node = svc.get_node(node_id)
    if not node or node.dag_id != dag_id:
        raise HTTPException(404, "Node not found")
    return _node_to_dict(node)


@router.post("/{dag_id}/nodes/{node_id}/transition")
def transition_node(dag_id: str, node_id: str, body: TransitionRequest):
    svc = get_svc()
    try:
        extra = {
            "outputs": body.outputs,
            "error": body.error,
            "self_check": body.self_check,
            "assigned_agents": body.assigned_agents,
        }
        node = svc.transition_node(node_id, body.status, **extra)

        # Auto-review: when node reaches reviewing, evaluate in background thread
        if node.status == "reviewing" and body.self_check:
            import threading
            t = threading.Thread(
                target=_run_review_async,
                args=(node_id, dag_id, node.goal, node.acceptance_criteria, body.self_check),
                daemon=True,
            )
            t.start()

        return _node_to_dict(node)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/{dag_id}/nodes/{node_id}/logs")
def get_node_logs(dag_id: str, node_id: str, tail: int = 0, offset: int = 0):
    """Read sub-agent log file. Supports polling:
    - ?tail=50  → last 50 lines
    - ?offset=N → skip first N characters (for incremental reading)
    Returns the log content as plain text.
    """
    log_path = Path.cwd() / "_logs" / f"{node_id}.log"
    if not log_path.exists():
        return {"content": "", "size": 0, "log_path": str(log_path)}

    content = log_path.read_text(encoding="utf-8", errors="replace")
    size = len(content)

    if offset > 0 and offset < size:
        content = content[offset:]

    if tail > 0 and content:
        lines = content.splitlines()
        content = "\n".join(lines[-tail:]) + "\n"

    return {"content": content, "size": size, "log_path": str(log_path)}


@router.post("/{dag_id}/nodes/{node_id}/heartbeat")
def heartbeat_node(dag_id: str, node_id: str):
    """Lightweight heartbeat from a running sub-agent."""
    from main import app as _app
    disp = getattr(_app.state, "dispatcher", None)
    if not disp:
        raise HTTPException(503, "Dispatcher not initialized")
    disp.heartbeat(node_id)
    return {"ok": True}


# ── Dispatch ───────────────────────────────────────────────────────


# ── Abort ──────────────────────────────────────────────────────────


@router.post("/{dag_id}/abort")
def abort_dag(dag_id: str):
    """Abort all running/assigned/ready nodes in a DAG."""
    from main import app as _app
    svc = get_svc()
    disp = getattr(_app.state, "dispatcher", None)
    if not disp:
        raise HTTPException(503, "Dispatcher not initialized")
    nodes = svc.get_nodes_by_dag(dag_id)
    to_abort = [n for n in nodes if n.status in ("assigned", "running", "ready")]
    for n in to_abort:
        disp.send_abort(n.node_id)
    return {"aborted": len(to_abort)}


@router.post("/{dag_id}/nodes/{node_id}/abort")
def abort_node(dag_id: str, node_id: str):
    """Abort a single running/assigned node."""
    from main import app as _app
    svc = get_svc()
    node = svc.get_node(node_id)
    if not node or node.dag_id != dag_id:
        raise HTTPException(404, "Node not found")
    if node.status not in ("assigned", "running", "ready"):
        raise HTTPException(400, f"Node status {node.status} cannot be aborted")
    disp = getattr(_app.state, "dispatcher", None)
    if not disp:
        raise HTTPException(503, "Dispatcher not initialized")
    disp.send_abort(node_id)
    return {"aborted": True, "node_id": node_id}


@router.post("/{dag_id}/dispatch")
def dispatch_next(dag_id: str):
    """Dispatch the next ready node."""
    svc = get_svc()
    node = svc.dispatch_next(dag_id)
    if not node:
        return {"dispatched": False, "node": None}
    return {"dispatched": True, "node": _node_to_dict(node)}


@router.post("/{dag_id}/dispatch-all")
def dispatch_all(dag_id: str):
    """Dispatch all ready nodes (triggers background subprocess spawning)."""
    from main import app as _app
    from fastapi import Request
    app = _app
    disp = getattr(app.state, "dispatcher", None)
    if not disp:
        return {"error": "Dispatcher not initialized"}
    dispatched = disp.trigger(dag_id)
    return {"dispatched": dispatched, "count": len(dispatched)}


@router.post("/{dag_id}/evaluate-readiness")
def evaluate_readiness(dag_id: str):
    """Re-evaluate all pending nodes for readiness."""
    svc = get_svc()
    ready = svc.evaluate_readiness(dag_id)
    return {"newly_ready": len(ready), "nodes": [_node_to_dict(n) for n in ready]}


# ── Events ─────────────────────────────────────────────────────────


@router.get("/{dag_id}/events")
def list_events(dag_id: str, limit: int = 50):
    svc = get_svc()
    events = svc.get_events(dag_id, limit=limit)
    return [
        {
            "id": e.id,
            "event_type": e.event_type,
            "source_node_id": e.source_node_id,
            "data": e.data,
            "created_at": e.created_at.isoformat(),
        }
        for e in events
    ]


# ── Auto-review ──────────────────────────────────────────────────────


_REVIEW_MAX_RETRIES = 3
_REVIEW_RETRY_DELAY = 10


def _run_review(goal: str, criteria: str, self_check: list) -> dict:
    """Call DeepSeek to evaluate a node's self-check report.

    Pure function — takes plain strings, returns parsed dict.
    Thread-safe: creates its own LLMClient per call.
    """
    import json as _json

    prompt = (
        f"Review this sub-agent's self-check report.\n\n"
        f"## Goal\n{goal}\n\n"
        f"## Acceptance criteria\n{criteria}\n\n"
        f"## Self-check results\n"
        f"{_json.dumps(self_check, ensure_ascii=False, indent=2)}\n\n"
        f"Evaluate: did this node actually accomplish its goal?\n"
        f"- If ALL criteria passed or only minor cosmetic issues, respond with: "
        f'{{"passed": true, "summary": "..."}}\n'
        f"- If ANY criterion clearly failed with substantive issues, respond with: "
        f'{{"passed": false, "summary": "..."}}\n\n'
        f"Output valid JSON only, no markdown."
    )

    from services.llm_client import LLMClient
    llm = LLMClient(public=True)
    content = llm.chat_text([{"role": "user", "content": prompt}])

    import re
    try:
        return _json.loads(content)
    except _json.JSONDecodeError:
        match = re.search(r'\{.*\}', content, re.DOTALL)
        if match:
            try:
                return _json.loads(match.group(0))
            except _json.JSONDecodeError:
                pass
        return {"passed": True, "summary": "Review parse failed, defaulting to pass"}


def _run_review_async(node_id: str, dag_id: str, goal: str, criteria: str, self_check: list):
    """Background thread: review self_check with retry, then transition node.

    Spawned in transition handler when node → reviewing.
    Retries up to _REVIEW_MAX_RETRIES times on transient failures.
    All retries exhausted → node transitions to failed.
    """
    import time

    svc = get_svc()

    for attempt in range(_REVIEW_MAX_RETRIES):
        try:
            result = _run_review(goal, criteria, self_check)
            # Check node hasn't been moved since (manual abort, timeout, etc.)
            node = svc.get_node(node_id)
            if not node or node.status != "reviewing":
                return

            if result.get("passed", False):
                log.info("Review passed", node_id=node_id)
                svc.transition_node(node_id, "completed")
            else:
                reason = result.get("summary", "Review failed")
                log.info("Review failed", node_id=node_id, reason=reason)
                svc.transition_node(node_id, "failed", error=reason[:2000])
            return  # success

        except Exception as e:
            log.warning("Review attempt failed", node_id=node_id,
                        attempt=attempt + 1, max_retries=_REVIEW_MAX_RETRIES, error=str(e))
            if attempt < _REVIEW_MAX_RETRIES - 1:
                time.sleep(_REVIEW_RETRY_DELAY)

    # All retries exhausted — mark failed
    try:
        node = svc.get_node(node_id)
        if node and node.status == "reviewing":
            svc.transition_node(node_id, "failed",
                                error=f"Review failed after {_REVIEW_MAX_RETRIES} retries")
    except Exception as e:
        log.error("Review final failover failed", node_id=node_id, error=str(e))


# ── Helpers ────────────────────────────────────────────────────────


def _node_to_dict(node) -> dict:
    # Parse self_check from JSON string to list for frontend convenience
    self_check = getattr(node, "self_check", None)
    if isinstance(self_check, str):
        try:
            self_check = json.loads(self_check)
        except (json.JSONDecodeError, TypeError):
            pass  # leave as string if parsing fails
    return {
        "node_id": node.node_id,
        "dag_id": node.dag_id,
        "title": getattr(node, "title", ""),
        "goal": node.goal,
        "error": getattr(node, "error", None),
        "assigned_roles": node.assigned_roles,
        "required_skills": node.required_skills,
        "dependencies": node.dependencies,
        "acceptance_criteria": node.acceptance_criteria,
        "status": node.status,
        "assigned_agents": node.assigned_agents,
        "channel_id": node.channel_id,
        "outputs": node.outputs,
        "self_check": self_check,
        "created_at": node.created_at.isoformat(),
        "updated_at": node.updated_at.isoformat(),
    }
