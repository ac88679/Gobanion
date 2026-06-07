"""Dispatcher 单元测试（mock DAG Service）"""

import os
import sys
from unittest.mock import MagicMock, Mock

os.environ["APP_ENV"] = "development"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.dispatcher import Dispatcher


class _MockNode:
    def __init__(self, node_id, status, goal="", assigned_roles=None):
        self.node_id = node_id
        self.dag_id = "DAG-TEST"
        self.status = status
        self.goal = goal
        self.assigned_roles = assigned_roles or ["generic"]
        self.required_skills = []
        self.acceptance_criteria = ""
        self.channel_id = None
        self.outputs = []


def make_mock_service():
    svc = MagicMock()
    svc.get_ready_nodes = MagicMock(return_value=[])
    svc.transition_node = MagicMock()
    svc.get_node = MagicMock()
    svc.get_dag = MagicMock()
    svc.list_dags = MagicMock(return_value=[])
    return svc


def test_start_stop():
    """启动和停止不抛异常"""
    svc = make_mock_service()
    d = Dispatcher(svc)
    d.start()
    import time
    time.sleep(0.5)
    # 停止（需要 async，但在 sync 测试里直接标记 active）
    assert d._loop.is_running()
    # 不能直接 await stop，但验证线程活着
    import asyncio as aio
    future = aio.run_coroutine_threadsafe(d.stop(), d._loop)
    future.result(timeout=3)
    assert not d._loop.is_running()


def test_trigger_dispatches_ready_nodes():
    """trigger 应 dispatch 所有 ready 节点"""
    svc = make_mock_service()
    svc.get_ready_nodes = MagicMock(return_value=[
        _MockNode("N1", "ready", goal="Task A"),
        _MockNode("N2", "ready", goal="Task B"),
    ])
    svc.get_node = MagicMock(side_effect=lambda nid: _MockNode(nid, "ready"))

    d = Dispatcher(svc)
    dispatched = d.trigger("DAG-TEST")
    assert len(dispatched) == 2
    assert "N1" in dispatched
    assert "N2" in dispatched
    # transition_node 应被调用两次（设为 assigned）
    assert svc.transition_node.call_count == 2


def test_trigger_skips_non_ready():
    """只有 ready 节点被 dispatch"""
    svc = make_mock_service()
    svc.get_ready_nodes = MagicMock(return_value=[])  # 没有 ready
    d = Dispatcher(svc)
    dispatched = d.trigger("DAG-TEST")
    assert dispatched == []
    assert svc.transition_node.call_count == 0


if __name__ == "__main__":
    test_start_stop()
    test_trigger_dispatches_ready_nodes()
    test_trigger_skips_non_ready()
    print("[PASS] test_dispatcher.py all passed")
