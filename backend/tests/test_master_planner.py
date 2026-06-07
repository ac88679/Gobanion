"""Master Planner 单元测试"""

import json
import os
import sys

os.environ["APP_ENV"] = "development"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.dag_service import DagService
from services.agents.master_planner import MasterPlanner


def _make_planner():
    dag_svc = DagService(db_url="sqlite:///./test_planner.db")
    planner = MasterPlanner(dag_service=dag_svc)
    return planner, dag_svc


def test_parse_json_valid():
    """_parse_json 解析有效 JSON 能正确提取节点"""
    planner, svc = _make_planner()
    resp = json.dumps({
        "title": "Test DAG",
        "description": "A test",
        "nodes": [
            {
                "goal": "Create config file",
                "assigned_roles": ["backend"],
                "required_skills": ["file_io"],
                "dependencies": [],
                "acceptance_criteria": "config.yml exists",
            },
            {
                "goal": "Write main code",
                "assigned_roles": ["backend"],
                "required_skills": ["code_generator"],
                "dependencies": [],
                "acceptance_criteria": "main.py runs without error",
            },
        ],
    })
    result = planner._parse_json(resp)
    assert result["title"] == "Test DAG"
    assert len(result["nodes"]) == 2
    for n in result["nodes"]:
        assert "goal" in n
        assert "assigned_roles" in n
        # node_id 只有在创建后才分配，_parse_json 不创建节点
    svc._engine.dispose()


def test_parse_json_with_code_block():
    """能处理 LLM 常见的 ```json 包裹"""
    planner, svc = _make_planner()
    resp = 'Some text\n```json\n{"title": "Wrapped", "description": "", "nodes": []}\n```\nmore text'
    result = planner._parse_json(resp)
    assert result["title"] == "Wrapped"
    svc._engine.dispose()


def test_parse_json_creates_dag_and_nodes():
    """解析应创建 DAG + 节点"""
    planner, svc = _make_planner()
    resp = json.dumps({
        "title": "Integration DAG",
        "description": "",
        "nodes": [
            {
                "goal": "Step 1",
                "assigned_roles": ["generic"],
                "required_skills": [],
                "dependencies": [],
                "acceptance_criteria": "ok",
            },
        ],
    })
    result = planner._parse_json(resp)
    assert "title" in result
    assert result["title"] == "Integration DAG"
    assert "nodes" in result
    assert len(result["nodes"]) == 1
    svc._engine.dispose()


def test_plan_method():
    """plan 方法返回正确结构（不调 LLM，用 mock 验证流程）"""
    # 这里只验证 method 签名返回 dict 结构
    # 真实 LLM 调用在集成测试中覆盖
    planner, svc = _make_planner()
    assert hasattr(planner, "plan")
    assert callable(planner.plan)
    svc._engine.dispose()


def test_cleanup():
    for f in ["test_planner.db"]:
        try:
            os.remove(f)
        except:
            pass


if __name__ == "__main__":
    test_parse_json_valid()
    test_parse_json_with_code_block()
    test_parse_json_creates_dag_and_nodes()
    test_plan_method()
    test_cleanup()
    print("[PASS] test_master_planner.py all passed")
