"""
Test plan API via httpx client.
"""

import os
# Nuke proxy env vars so httpx doesn't use them
for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(key, None)
os.environ["no_proxy"] = "*"

import httpx
import json

client = httpx.Client(base_url="http://localhost:5000", timeout=60)

# 1. Health
r = client.get("/health")
print(f"Health: {r.status_code} {r.json()}")

# 2. Plan
print("\n=== Plan ===")
r = client.post("/api/v1/plan", json={"goal": "Write a Python script that prints 'Hello Gobanion' to a file called hello.txt"})
print(f"Status: {r.status_code}")
if r.status_code == 200:
    data = r.json()
    print(f"DAG: {data['dag_id']}, status={data['status']}, nodes={len(data['nodes'])}")
    for n in data["nodes"]:
        print(f"  {n['node_id']}: [{n['status']}] {n['goal'][:60]}... deps={n['dependencies']}")
else:
    print(f"Error: {r.text[:500]}")

# 3. Dispatch
dag_id = data.get("dag_id", "")
if dag_id:
    print("\n=== Dispatch ===")
    r = client.post(f"/api/v1/dag/{dag_id}/dispatch-all")
    print(f"Dispatch: {r.json()}")

    # 4. Poll for completion
    print("\n=== Polling ===")
    import time
    for i in range(30):
        r = client.get(f"/api/v1/dag/{dag_id}/nodes")
        nodes = r.json()
        statuses = {n["node_id"]: n["status"] for n in nodes}
        print(f"  Poll {i+1}: {statuses}")
        all_terminal = all(s in ("completed", "failed", "interrupted", "done", "reviewing") for s in statuses.values())
        if all_terminal:
            break
        time.sleep(2)

    print("\n=== Final ===")
    r = client.get(f"/api/v1/dag/{dag_id}/nodes")
    for n in r.json():
        print(f"  {n['node_id']}: status={n['status']}, outputs={n['outputs']}")

print("\nDone")
