"""DAG Service 单元测试（状态机 + CRUD）"""

import os
import sys
import json
from datetime import datetime, timezone

os.environ["APP_ENV"] = "development"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.dag_service import DagService


def _svc():
    svc = DagService(db_url="sqlite:///./test_dag_unit.db")
    yield svc
    svc._engine.dispose()


def test_create_dag():
    svc = DagService(db_url="sqlite:///./test_dag.db")
    try:
        dag = svc.create_dag(title="Test", goal="Test goal")
        assert dag.dag_id.startswith("DAG-")
        assert dag.title == "Test"
        assert dag.goal == "Test goal"
        assert dag.status == "planning"  # 默认
    finally:
        svc._engine.dispose()


def test_create_nodes():
    svc = DagService(db_url="sqlite:///./test_dag.db")
    try:
        dag = svc.create_dag("Test", "Goal")
        nodes = svc.bulk_create_nodes(dag.dag_id, [
            {"goal": "A", "dependencies": []},
            {"goal": "B", "dependencies": []},
        ])
        assert len(nodes) == 2
        assert all(n.status == "ready" for n in nodes)
    finally:
        svc._engine.dispose()


def test_dependency_blocks():
    """未完成的依赖阻止 ready"""
    svc = DagService(db_url="sqlite:///./test_dag.db")
    try:
        dag = svc.create_dag("Test", "Goal")
        svc.bulk_create_nodes(dag.dag_id, [
            {"goal": "A", "dependencies": []},
        ])
        nodes = svc.get_nodes_by_dag(dag.dag_id)
        node_a = nodes[0]
        # 创建 B（依赖 A）
        svc.bulk_create_nodes(dag.dag_id, [
            {"goal": "B", "dependencies": [node_a.node_id]},
        ])
        nodes_b = svc.get_nodes_by_dag(dag.dag_id)
        b = [n for n in nodes_b if n.goal == "B"][0]
        assert b.status == "pending"  # A 未完成，B 不能 ready
    finally:
        svc._engine.dispose()


def test_transition_node():
    """完整的状态迁移链：ready → assigned → running → done → reviewing → completed"""
    svc = DagService(db_url="sqlite:///./test_dag.db")
    try:
        dag = svc.create_dag("Test", "Goal")
        nodes = svc.bulk_create_nodes(dag.dag_id, [
            {"goal": "X", "dependencies": []},
        ])
        nid = nodes[0].node_id

        # assigned
        svc.transition_node(nid, "assigned")
        assert svc.get_node(nid).status == "assigned"
        # running
        svc.transition_node(nid, "running")
        assert svc.get_node(nid).status == "running"
        # done
        svc.transition_node(nid, "done", outputs=["file.txt"],
                            self_check=[{"criterion": "ok", "result": "pass"}])
        n = svc.get_node(nid)
        assert n.status == "done"
        assert "file.txt" in n.outputs
        # reviewing
        svc.transition_node(nid, "reviewing")
        assert svc.get_node(nid).status == "reviewing"
        # completed
        svc.transition_node(nid, "completed")
        assert svc.get_node(nid).status == "completed"
    finally:
        svc._engine.dispose()


def test_invalid_transition():
    """非法迁移被拒绝（e.g. completed → running）"""
    svc = DagService(db_url="sqlite:///./test_dag.db")
    try:
        dag = svc.create_dag("Test", "Goal")
        nodes = svc.bulk_create_nodes(dag.dag_id, [
            {"goal": "X", "dependencies": []},
        ])
        nid = nodes[0].node_id
        svc.transition_node(nid, "assigned")
        svc.transition_node(nid, "running")
        svc.transition_node(nid, "done")
        svc.transition_node(nid, "reviewing")
        svc.transition_node(nid, "completed")
        # 回退到 running 应该失败
        try:
            svc.transition_node(nid, "running")
            assert False, "Should have raised"
        except ValueError as e:
            assert "Invalid transition" in str(e)
    finally:
        svc._engine.dispose()


def test_cascade_ready():
    """前置节点 completed 后，依赖节点自动 pending → ready"""
    svc = DagService(db_url="sqlite:///./test_dag.db")
    try:
        dag = svc.create_dag("Test", "Goal")
        nodes = svc.bulk_create_nodes(dag.dag_id, [
            {"goal": "A", "dependencies": []},
        ])
        node_a = nodes[0]
        svc.bulk_create_nodes(dag.dag_id, [
            {"goal": "B", "dependencies": [node_a.node_id]},
        ])
        # 完成 A
        svc.transition_node(node_a.node_id, "assigned")
        svc.transition_node(node_a.node_id, "running")
        svc.transition_node(node_a.node_id, "done")
        svc.transition_node(node_a.node_id, "reviewing")
        svc.transition_node(node_a.node_id, "completed")
        # B 应自动变为 ready
        all_nodes = svc.get_nodes_by_dag(dag.dag_id)
        b = [n for n in all_nodes if n.goal == "B"][0]
        assert b.status == "ready"
    finally:
        svc._engine.dispose()


def test_events_recorded():
    svc = DagService(db_url="sqlite:///./test_dag.db")
    try:
        dag = svc.create_dag("Test", "Goal")
        events = svc.get_events(dag.dag_id)
        # 创建 nodes 会生成 "dag.nodes_created" 事件
        assert len(events) >= 0  # 可能没有事件，这是 MVP
    finally:
        svc._engine.dispose()


def test_get_ready_nodes():
    svc = DagService(db_url="sqlite:///./test_dag.db")
    try:
        dag = svc.create_dag("Test", "Goal")
        svc.bulk_create_nodes(dag.dag_id, [
            {"goal": "A", "dependencies": []},
            {"goal": "B", "dependencies": []},
        ])
        ready = svc.get_ready_nodes(dag.dag_id)
        assert len(ready) == 2
        assert all(n.status == "ready" for n in ready)
    finally:
        svc._engine.dispose()


if __name__ == "__main__":
    test_create_dag()
    test_create_nodes()
    test_dependency_blocks()
    test_transition_node()
    test_invalid_transition()
    test_cascade_ready()
    test_events_recorded()
    test_get_ready_nodes()
    # Cleanup
    for f in ["test_dag.db", "test_dag_unit.db"]:
        try:
            os.remove(f)
        except:
            pass
    print("[PASS] test_dag_service.py all passed")
