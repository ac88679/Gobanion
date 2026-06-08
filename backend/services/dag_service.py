"""
DAG Service — DAG 的核心运行时引擎

职责：
  1. SQLite 持久化（建表、CRUD）
  2. 节点状态机（transition + 级联 ready 检测）
  3. 事件记录与广播
  4. Dispatcher 接口：发现 ready 节点、指派

设计要点：
  - 主 Agent 进程内持有唯一 engine
  - 子 Agent 通过 HTTP API 调用，不直连 DB
  - 事件驱动：状态变更后自动触发级联逻辑
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlmodel import SQLModel, Session, create_engine, select, desc

from config import get_settings
from models import DagNode, Dag, DagEvent, can_transition
from services.logger import get_logger

log = get_logger("dag_service")


class DagService:
    """DAG 运行时引擎（单例）"""

    def __init__(self, db_url: Optional[str] = None):
        settings = get_settings()
        db_url = db_url or str(settings.database.URL)
        self._engine = create_engine(
            db_url,
            echo=settings.database.ECHO,
            connect_args={"check_same_thread": False, "timeout": 10},
            pool_pre_ping=True,
        )
        self._create_tables()

    # ── Database init ──────────────────────────────────────────────

    def _create_tables(self) -> None:
        SQLModel.metadata.create_all(self._engine)

    def new_session(self) -> Session:
        return Session(self._engine)

    # ── DAG CRUD ──────────────────────────────────────────────────

    def create_dag(self, title: str, goal: str, description: str = "") -> Dag:
        """Create a new DAG (project)."""
        dag = Dag(title=title, goal=goal, description=description)
        with self.new_session() as session:
            session.add(dag)
            session.commit()
            session.refresh(dag)
            log.info("DAG created", dag_id=dag.dag_id, title=title, status=dag.status)
            return dag

    def get_dag(self, dag_id: str) -> Optional[Dag]:
        with self.new_session() as session:
            return session.get(Dag, dag_id)

    def list_dags(self, limit: int = 20) -> list[Dag]:
        with self.new_session() as session:
            stmt = select(Dag).order_by(desc(Dag.created_at)).limit(limit)
            return list(session.exec(stmt))

    # ── Node CRUD ─────────────────────────────────────────────────

    def create_node(self, dag_id: str, goal: str, **kwargs) -> DagNode:
        """Add a node to a DAG. Status defaults to 'pending'."""
        node = DagNode(dag_id=dag_id, goal=goal, **kwargs)
        with self.new_session() as session:
            session.add(node)
            session.commit()
            session.refresh(node)
            self._record_event(dag_id, "node.created", node.node_id,
                               {"node_id": node.node_id, "status": node.status})
            log.info("Node created", dag_id=dag_id, node_id=node.node_id,
                     title=node.title, status=node.status)
            return node

    def get_node(self, node_id: str) -> Optional[DagNode]:
        with self.new_session() as session:
            return session.get(DagNode, node_id)

    def get_nodes_by_dag(self, dag_id: str) -> list[DagNode]:
        with self.new_session() as session:
            stmt = select(DagNode).where(DagNode.dag_id == dag_id)
            return list(session.exec(stmt))

    def get_ready_nodes(self, dag_id: str) -> list[DagNode]:
        """Find all ready nodes in a DAG."""
        with self.new_session() as session:
            stmt = select(DagNode).where(
                DagNode.dag_id == dag_id,
                DagNode.status == "ready",
            )
            return list(session.exec(stmt))

    # ── State machine ──────────────────────────────────────────────

    def transition_node(self, node_id: str, new_status: str, **extra) -> DagNode:
        """
        Transition a node to a new status.
        Returns updated node or raises ValueError if transition invalid.
        """
        with self.new_session() as session:
            node = session.get(DagNode, node_id)
            if not node:
                raise ValueError(f"Node {node_id} not found")

            old_status = node.status
            if old_status == new_status:
                return node  # no-op

            if not can_transition(old_status, new_status):
                raise ValueError(
                    f"Invalid transition: {old_status} → {new_status}"
                )

            log.info("Node transition", node_id=node_id, dag_id=node.dag_id,
                     title=node.title or node.goal[:40],
                     from_status=old_status, to_status=new_status)

            # Apply transition
            node.status = new_status
            node.updated_at = datetime.now(timezone.utc)

            # Extra fields
            if "self_check" in extra and new_status == "done":
                node.self_check = json.dumps(extra["self_check"], ensure_ascii=False)
            if "outputs" in extra:
                node.outputs = extra["outputs"]
            if "assigned_agents" in extra:
                node.assigned_agents = extra["assigned_agents"]
            if "error" in extra and new_status in ("failed", "interrupted"):
                node.error = extra["error"][:2000]

            session.add(node)
            session.commit()
            session.refresh(node)

            self._record_event(
                node.dag_id, "node.status_changed", node.node_id,
                {"node_id": node_id, "from": old_status, "to": new_status, **extra}
            )

            # Cascading logic
            self._on_transition(session, node, old_status, new_status)

            return node

    def _on_transition(self, session: Session, node: DagNode, old: str, new: str) -> None:
        """Side effects on status transition."""
        # MVP: auto-advance done → completed (no reviewing step yet)
        if new == "done":
            node.status = "completed"
            node.updated_at = datetime.now(timezone.utc)
            session.add(node)
            session.commit()
            new = "completed"

        if new == "completed":
            # Check all nodes in this DAG to see if everything is done
            stmt = select(DagNode).where(
                DagNode.dag_id == node.dag_id,
                DagNode.status.not_in(["completed", "failed", "interrupted"]),
            )
            remaining = list(session.exec(stmt))
            if not remaining:
                dag = session.get(Dag, node.dag_id)
                if dag:
                    dag.status = "completed"
                    dag.updated_at = datetime.now(timezone.utc)
                    session.add(dag)
                    session.commit()

        # Cascade: evaluate readiness for dependents
        self.evaluate_readiness(node.dag_id)

    def bulk_create_nodes(self, dag_id: str, nodes: list[dict]) -> list[DagNode]:
        """Create multiple nodes at once and evaluate ready transitions."""
        created = []
        with self.new_session() as session:
            for data in nodes:
                node = DagNode(dag_id=dag_id, **data)
                session.add(node)
                created.append(node)
            session.commit()
            for node in created:
                session.refresh(node)
            self._record_event(dag_id, "dag.nodes_created", None,
                               {"count": len(nodes)})
        # Evaluate readiness for any newly created nodes
        self.evaluate_readiness(dag_id)

        # Re-fetch to get updated status
        return self.get_nodes_by_dag(dag_id)[-len(created):]

    def evaluate_readiness(self, dag_id: str) -> list[DagNode]:
        """
        Check all pending nodes: if all dependencies are completed,
        transition to ready.
        Returns list of newly ready nodes.
        """
        newly_ready = []
        with self.new_session() as session:
            stmt = select(DagNode).where(
                DagNode.dag_id == dag_id,
                DagNode.status == "pending",
            )
            pending = list(session.exec(stmt))

            for node in pending:
                if self._deps_met(session, node):
                    node.status = "ready"
                    node.updated_at = datetime.now(timezone.utc)
                    session.add(node)
                    newly_ready.append(node)

            if newly_ready:
                session.commit()
                for node in newly_ready:
                    session.refresh(node)
                    self._record_event(
                        dag_id, "node.status_changed", node.node_id,
                        {"node_id": node.node_id, "from": "pending", "to": "ready"}
                    )

        return newly_ready

    def _deps_met(self, session: Session, node: DagNode) -> bool:
        """Check if all dependency nodes are completed."""
        if not node.dependencies:
            return True
        for dep_id in node.dependencies:
            dep = session.get(DagNode, dep_id)
            if dep is None:
                return False
            if dep.status != "completed":
                return False
        return True

    # ── Events ─────────────────────────────────────────────────────

    def get_events(self, dag_id: str, limit: int = 50) -> list[DagEvent]:
        with self.new_session() as session:
            stmt = select(DagEvent).where(
                DagEvent.dag_id == dag_id
            ).order_by(desc(DagEvent.created_at)).limit(limit)
            return list(session.exec(stmt))

    def _record_event(self, dag_id: str, event_type: str,
                      source_node_id: Optional[str], data: dict) -> None:
        with self.new_session() as session:
            event = DagEvent(
                dag_id=dag_id,
                event_type=event_type,
                source_node_id=source_node_id,
                data=json.dumps(data),
            )
            session.add(event)
            session.commit()

    # ── Dispatcher integration ─────────────────────────────────────

    def dispatch_next(self, dag_id: str) -> Optional[DagNode]:
        """
        Find the next ready node and mark it as assigned.
        Returns the assigned node, or None if nothing to dispatch.
        """
        ready = self.get_ready_nodes(dag_id)
        if not ready:
            return None
        # Simple FIFO for MVP
        node = ready[0]
        return self.transition_node(node.node_id, "assigned")
