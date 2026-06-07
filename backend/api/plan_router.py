"""
规划 API Router — 用户输入目标 → 主 Agent 拆解为 DAG
"""

from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.agents.master_planner import MasterPlanner
from services import DagService

router = APIRouter(prefix="/api/v1/plan", tags=["plan"])

# ── Injected services ──
_planner: Optional[MasterPlanner] = None


def init(dag_service: DagService) -> None:
    global _planner
    _planner = MasterPlanner(dag_service=dag_service)


def get_planner() -> MasterPlanner:
    if _planner is None:
        raise RuntimeError("MasterPlanner not initialized")
    return _planner


# ── Schemas ──


class PlanRequest(BaseModel):
    goal: str
    context: str = ""


# ── Endpoints ──


@router.post("")
def plan(body: PlanRequest):
    """Analyze a goal, produce a DAG, persist it."""
    planner = get_planner()
    try:
        result = planner.plan(body.goal, context=body.context or None)
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Planning failed: {str(e)[:200]}")
