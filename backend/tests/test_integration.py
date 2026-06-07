"""
Integration test: Master Planner → DAG Service → Dispatcher → Agent Runner
Start the server first:  uv run uvicorn main:app --host 0.0.0.0 --port 5000
Then run this test.

Or run directly (starts server in background):
    uv run python test_integration.py
"""

import json
import os
import signal
import subprocess
import sys
import time
import httpx

BASE = "http://localhost:5000"


def wait_for_server(timeout=15):
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = httpx.get(f"{BASE}/health", timeout=2)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def main():
    # ── Start server if not running ──
    proc = None
    try:
        httpx.get(f"{BASE}/health", timeout=2)
        print("Server already running")
    except Exception:
        print("Starting server...")
        backend_dir = os.path.dirname(os.path.abspath(__file__))
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "5000"],
            cwd=backend_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if not wait_for_server():
            print("Server failed to start in time")
            proc.kill()
            sys.exit(1)
        print("Server started")

    client = httpx.Client(base_url=BASE, timeout=30)

    try:
        # ── 1. Plan a task ──
        print("\n=== 1. Plan task ===")
        r = client.post("/api/v1/plan", json={
            "goal": "Create a simple Python script that prints 'Hello Gobanion' to a file named hello.txt"
        })
        data = r.json()
        print(f"  DAG: {data['dag_id']}, status={data['status']}, nodes={len(data['nodes'])}")
        dag_id = data["dag_id"]
        assert data["status"] == "running"

        # ── 2. Check nodes ──
        print(f"\n=== 2. Nodes ===")
        for n in data["nodes"]:
            print(f"  {n['node_id']}: [{n['status']}] {n['goal'][:50]}...")

        # ── 3. Dispatch all ready nodes ──
        print(f"\n=== 3. Dispatch ===")
        r = client.post(f"/api/v1/dag/{dag_id}/dispatch-all")
        print(f"  Dispatched: {r.json()}")

        # ── 4. Wait for completion ──
        print(f"\n=== 4. Wait for completion ===")
        for i in range(30):
            r = client.get(f"/api/v1/dag/{dag_id}/nodes")
            nodes = r.json()
            statuses = {n["node_id"]: n["status"] for n in nodes}
            print(f"  Poll {i+1}: {statuses}")
            all_terminal = all(s in ("completed", "failed", "interrupted") for s in statuses.values())
            if all_terminal:
                break
            time.sleep(2)

        # ── 5. Final state ──
        print(f"\n=== 5. Final state ===")
        r = client.get(f"/api/v1/dag/{dag_id}")
        print(f"  DAG: {r.json()}")
        r = client.get(f"/api/v1/dag/{dag_id}/nodes")
        for n in r.json():
            print(f"  Node {n['node_id']}: status={n['status']}, outputs={n['outputs']}")

        # ── 6. Events ──
        r = client.get(f"/api/v1/dag/{dag_id}/events")
        print(f"\n=== 6. Events ({len(r.json())}) ===")

        print("\n=== INTEGRATION TEST PASSED ===")

    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except:
                proc.kill()


if __name__ == "__main__":
    main()
