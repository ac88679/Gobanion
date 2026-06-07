"""
DAG API Router — 子 Agent / 外部通过 HTTP 操作 DAG
"""

from pathlib import Path
from typing import Optional
from fastapi import APIRouter, HTTPException

from pydantic import BaseModel
from services import DagService

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
        "nodes": [
            {
                "node_id": n.node_id,
                "title": getattr(n, "title", ""),
                "goal": n.goal,
                "error": getattr(n, "error", None),
                "assigned_roles": n.assigned_roles,
                "required_skills": n.required_skills,
                "dependencies": n.dependencies,
                "acceptance_criteria": n.acceptance_criteria,
                "status": n.status,
                "assigned_agents": n.assigned_agents,
                "channel_id": n.channel_id,
                "outputs": n.outputs,
                "self_check": n.self_check,
                "created_at": n.created_at.isoformat(),
                "updated_at": n.updated_at.isoformat(),
            }
            for n in nodes
        ],
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


# ── Helpers ────────────────────────────────────────────────────────


def _node_to_dict(node) -> dict:
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
        "created_at": node.created_at.isoformat(),
        "updated_at": node.updated_at.isoformat(),
    }
