"""Quick test: DAG status after planning"""
import os
if os.path.exists("./test_x.db"):
    os.remove("./test_x.db")

from services import DagService
from services.agents.master_planner import MasterPlanner
import json

s = DagService(db_url="sqlite:///./test_x.db")
planner = MasterPlanner(dag_service=s)
result = planner.plan("Make a hello world web server")
print("dag_id:", result["dag_id"])
print("status:", result["status"])
print("nodes:", len(result["nodes"]))

# cleanup
try: os.remove("./test_x.db")
except: pass
