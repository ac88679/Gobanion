"""
主 Agent 分层规划器

流程:
  1. 接收用户目标（自然语言）
  2. 构造结构化 prompt
  3. 调 LLM (deepseek-v4-flash)
  4. 解析 JSON DAG
  5. 写入 DAG Service
"""

import json
from datetime import datetime, timezone
from typing import Optional

from services.llm_client import LLMClient
from services.dag_service import DagService
from services.prompt_loader import get_prompt
from services.logger import get_logger
from models import DagNode, Dag

log = get_logger("planner")
PLANNER_SYSTEM_PROMPT = get_prompt("planner.md")


class MasterPlanner:
    """主 Agent 规划器 — 目标 → DAG"""

    def __init__(self, dag_service: DagService, llm_client: Optional[LLMClient] = None):
        self.dag_service = dag_service
        self.llm = llm_client or LLMClient(public=True)  # 用 DeepSeek 公网模型

    def plan(self, user_goal: str, context: Optional[str] = None) -> dict:
        """
        Take a user goal, call LLM, get DAG, save to DAG Service.
        Returns the created DAG info with nodes.
        """
        log.info("Planning started", goal=user_goal[:100], has_context=bool(context))

        # ── 1. Build messages ──
        user_message = f"Goal: {user_goal}"
        if context:
            user_message += f"\n\nContext:\n{context}"

        messages = [
            {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]

        # ── 2. Call LLM ──
        log.info("Calling LLM for plan", model=self.llm.model)
        raw = self.llm.chat_text(messages)
        log.info("LLM plan response received", length=len(raw))
        if log.isEnabledFor(10):  # TRACE / DEBUG
            import textwrap
            log.debug(f"LLM raw output:\n{textwrap.indent(raw[:800], '  ')}")

        # ── 3. Parse JSON ──
        dag_spec = self._parse_json(raw)
        log.info("Plan parsed", title=dag_spec.get("title", "?"),
                 node_count=len(dag_spec.get("nodes", [])))

        # ── 4. Create DAG in service ──
        dag = self.dag_service.create_dag(
            title=dag_spec.get("title", "Untitled"),
            goal=user_goal,
            description=dag_spec.get("description", ""),
        )

        # ── 5. Create nodes ──
        node_defs = dag_spec.get("nodes", [])
        if not node_defs:
            raise ValueError("LLM returned empty nodes list")

        # Convert 0-based indices in dependencies to node IDs
        # We need to create all nodes first, then link
        node_objs = []
        for nd in node_defs:
            node = self.dag_service.create_node(
                dag_id=dag.dag_id,
                title=nd.get("title", ""),
                goal=nd["goal"],
                assigned_roles=nd.get("assigned_roles", []),
                required_skills=nd.get("required_skills", []),
                dependencies=nd.get("dependencies", []),  # will be indices, fix below
                acceptance_criteria=nd.get("acceptance_criteria", ""),
            )
            node_objs.append(node)

        # Now fix dependencies: replace indices with actual node_ids
        with self.dag_service._engine.connect() as conn:
            for i, nd in enumerate(node_defs):
                deps = nd.get("dependencies", [])
                if deps and all(isinstance(d, int) for d in deps):
                    actual_deps = [node_objs[d].node_id for d in deps if d < len(node_objs)]
                    from sqlmodel import update
                    from models import DagNode
                    conn.execute(
                        update(DagNode)
                        .where(DagNode.node_id == node_objs[i].node_id)
                        .values(dependencies=actual_deps)
                    )
            conn.commit()

        # Re-evaluate readiness after dependency fix
        self.dag_service.evaluate_readiness(dag.dag_id)

        # Set DAG to running
        with self.dag_service.new_session() as session:
            d = session.get(Dag, dag.dag_id)
            if d:
                d.status = "running"
                d.updated_at = datetime.now(timezone.utc)
                session.add(d)
                session.commit()

        # ── 6. Return result ──
        d = self.dag_service.get_dag(dag.dag_id)
        nodes = self.dag_service.get_nodes_by_dag(dag.dag_id)
        return {
            "dag_id": d.dag_id,
            "title": d.title,
            "status": d.status,
            "nodes": [
                {
                    "node_id": n.node_id,
                    "title": getattr(n, "title", ""),
                    "goal": n.goal,
                    "assigned_roles": n.assigned_roles,
                    "dependencies": n.dependencies,
                    "status": n.status,
                }
                for n in nodes
            ],
        }

    def _parse_json(self, raw: str) -> dict:
        """Parse JSON from LLM output with fallbacks."""
        # Try direct parse
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

        # Try to find JSON block in markdown
        import re
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if match:
            return json.loads(match.group(1))

        # Try to find any JSON object
        match = re.search(r"(\{.*\})", raw, re.DOTALL)
        if match:
            return json.loads(match.group(1))

        raise ValueError(f"Could not parse JSON from LLM output:\n{raw[:500]}")
