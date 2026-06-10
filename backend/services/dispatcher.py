"""
Dispatcher — 事件驱动的子 Agent 调度器

职责:
  1. 监听 DAG 中状态变为 ready 的节点
  2. 匹配合适的角色（预热池 / 新建 subprocess）
  3. 注入环境变量，启动 agent_runner.py (subprocess)
  4. 管理心跳检测（assigned 30s 超时 → failed）
  5. 管理 ABORT 信号

设计:
  - MVP 阶段使用 Python subprocess，不用 Docker
  - 每个节点新建 subprocess，不预热池复用
  - 通过 polling + asyncio 实现事件驱动
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread
from typing import Optional, Any

from config import get_settings
from models.dag import Dag
from services.dag_service import DagService
from services.logger import get_logger

log = get_logger("dispatcher")


class Dispatcher:
    """
    Sub-agent dispatcher. Runs in its own thread.
    Polls DAG for ready nodes and spawns agent_runner.py subprocesses.
    """

    def __init__(self, dag_service: DagService):
        self.dag_service = dag_service
        settings = get_settings()

        self.dispatcher_timeout = settings.agent.DISPATCHER_TIMEOUT  # 30s
        self.heartbeat_interval = settings.agent.HEARTBEAT_INTERVAL   # 10s
        self.max_retries = settings.agent.MAX_RETRIES                 # 3

        self._running: dict[str, Any] = {}   # node_id → task/future
        self._processes: dict[str, subprocess.Popen] = {}  # node_id → process
        self._start_times: dict[str, float] = {}      # node_id → timestamp
        self._heartbeats: dict[str, float] = {}        # node_id → last heartbeat
        self._review_start: dict[str, float] = {}      # node_id → when reviewing started
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None

    # ── Lifecycle ────────────────────────────────────────────────────

    def start(self):
        """Start dispatcher in background thread."""
        self._loop = asyncio.new_event_loop()
        t = Thread(target=self._run_loop, daemon=True)
        t.start()
        log.info("Dispatcher started", poll_interval="2s", heartbeat_interval="5s")

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())

    async def stop(self):
        """Graceful shutdown."""
        if self._poll_task:
            self._poll_task.cancel()
        if self._heartbeat_task:
            self._heartbeat_task.cancel()

    async def _serve(self):
        """Main loop: poll + heartbeat."""
        self._poll_task = asyncio.create_task(self._poll_cycle())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_cycle())
        await asyncio.gather(self._poll_task, self._heartbeat_task)

    # ── Polling ──────────────────────────────────────────────────────

    async def _poll_cycle(self):
        """Periodically check for ready nodes to dispatch."""
        while True:
            try:
                await self._dispatch_loop()
            except Exception as e:
                print(f"[Dispatcher] Poll error: {e}", flush=True)
            await asyncio.sleep(2)

    async def _dispatch_loop(self):
        """Find all ready nodes across all DAGs and dispatch them."""
        dags = self.dag_service.list_dags(limit=50)
        for dag in dags:
            if dag.status not in ("running",):
                continue
            ready_nodes = self.dag_service.get_ready_nodes(dag.dag_id)
            if ready_nodes:
                log.info("Found ready nodes", dag_id=dag.dag_id, count=len(ready_nodes),
                         node_ids=[n.node_id for n in ready_nodes])
            for node in ready_nodes:
                if node.node_id in self._running:
                    continue  # already handling
                await self._dispatch_node(dag.dag_id, node)

    async def _dispatch_node(self, dag_id: str, node):
        """Dispatch a single node: assign + spawn subprocess."""
        if node.status != "ready":
            return

        # 1. Assign
        self.dag_service.transition_node(node.node_id, "assigned")
        self._start_times[node.node_id] = time.time()

        log.info("Dispatching node", dag_id=dag_id, node_id=node.node_id,
                 title=node.title or node.goal[:40])

        # 2. Spawn subprocess
        task = asyncio.create_task(self._run_agent(dag_id, node))
        self._running[node.node_id] = task

    async def _run_agent(self, dag_id: str, node):
        """Spawn agent_runner.py as subprocess and wait for completion."""
        backend_dir = str(Path(__file__).resolve().parent.parent)
        settings = get_settings()

        # Subprocess needs 127.0.0.1, not 0.0.0.0
        master_host = "127.0.0.1"
        master_port = settings.PORT

        # Build env — strip proxy env vars so subprocess httpx works with localhost
        env = os.environ.copy()
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy"):
            env.pop(key, None)
        env.update({
            "NODE_ID": node.node_id,
            "DAG_ID": dag_id,
            "MASTER_API": f"http://{master_host}:{master_port}",
            "ASSIGNED_ROLES": json.dumps(node.assigned_roles),
            "AGENT_ROLE": node.assigned_roles[0] if node.assigned_roles else "generic",
            "REQUIRED_SKILLS": ",".join(node.required_skills),
            "TASK_DEFINITION_JSON": json.dumps({
                "goal": node.goal,
                "acceptance_criteria": node.acceptance_criteria,
                "assigned_roles": node.assigned_roles,
                "required_skills": node.required_skills,
            }),
            "LLM_ENDPOINT": settings.llm.API_BASE,
            "LLM_MODEL": settings.llm.MODEL,
            "LLM_API_KEY": settings.llm.API_KEY,
            "AGENT_EXECUTE_TIMEOUT": str(settings.agent.EXECUTE_TIMEOUT),
        })
        if node.channel_id:
            env["CHANNEL_ID"] = node.channel_id

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "runner.agent_runner",
                env=env,
                cwd=backend_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._processes[node.node_id] = proc
            self._heartbeats[node.node_id] = time.time()

            # Read output line-by-line in real-time, with 300s timeout
            async def _stream_reader(stream, label: str):
                while True:
                    raw = await stream.readline()
                    if not raw:
                        break
                    line = raw.decode("utf-8", errors="replace").rstrip()
                    if line:
                        log.info("Agent log", node_id=node.node_id, label=label, line=line)

            await asyncio.wait_for(
                asyncio.gather(
                    _stream_reader(proc.stdout, "out"),
                    _stream_reader(proc.stderr, "err"),
                ),
                timeout=300,
            )
            returncode = await proc.wait()

            if proc.returncode != 0:
                log.warning("Agent exited with non-zero", node_id=node.node_id, code=proc.returncode)
                try:
                    current = self.dag_service.get_node(node.node_id).status
                    # If agent crashed before set_status("running"), still mark as failed
                    if current in ("running", "assigned"):
                        self.dag_service.transition_node(node.node_id, "failed",
                            error=f"Agent exited with code {proc.returncode}")
                except Exception as tx_e:
                    log.error("Failed to mark node as failed after exit",
                              node_id=node.node_id, error=str(tx_e))

        except asyncio.TimeoutError:
            log.warning("Agent timed out", node_id=node.node_id, timeout=300)
            proc = self._processes.get(node.node_id)
            if proc:
                proc.kill()
            try:
                current = self.dag_service.get_node(node.node_id).status
                if current == "running":
                    self.dag_service.transition_node(node.node_id, "failed")
                # else: heartbeat cycle already handled it (aborting → interrupted)
            except Exception as tx_e:
                log.error("Failed to mark timed-out node as failed",
                          node_id=node.node_id, error=str(tx_e))
        except Exception as e:
            log.error("Agent run failed", node_id=node.node_id, error=str(e))
        finally:
            self._running.pop(node.node_id, None)
            self._processes.pop(node.node_id, None)
            self._heartbeats.pop(node.node_id, None)
            self._start_times.pop(node.node_id, None)

    # ── Abort ─────────────────────────────────────────────────────────

    def send_abort(self, node_id: str) -> None:
        """Send abort signal to a running/assigned/ready node.

        Sets state to aborting (→ agent sees via check_abort → graceful exit for running).
        For ready/assigned nodes, there's no subprocess to kill — just sets the state
        so downstream cascade doesn't wait on them."""
        node = self.dag_service.get_node(node_id)
        if not node:
            log.warning("send_abort: node not found", node_id=node_id)
            return
        if node.status not in ("assigned", "running", "ready"):
            log.warning("send_abort: node not in abortable state",
                        node_id=node_id, status=node.status)
            return

        # 1. Transition to aborting
        self.dag_service.transition_node(node_id, "aborting")

        # 2. Clean up tracking
        self._heartbeats.pop(node_id, None)
        self._start_times.pop(node_id, None)
        self._running.pop(node_id, None)

        # 3. Kill subprocess if still alive (running nodes only)
        proc = self._processes.pop(node_id, None)
        if proc:
            # asyncio.subprocess.Process vs subprocess.Popen
            is_alive = proc.returncode is None if hasattr(proc, 'returncode') else proc.poll() is None
            if is_alive:
                log.info("Killing agent process", node_id=node_id, pid=proc.pid)
                proc.kill()

        # 4. For ready/assigned (no agent process), go straight to interrupted
        if node.status in ("ready", "assigned"):
            self.dag_service.transition_node(node_id, "interrupted")

        log.info("Abort sent", node_id=node_id, status="aborting", had_process=proc is not None)

    # ── Heartbeat / Timeout ──────────────────────────────────────────

    def heartbeat(self, node_id: str) -> None:
        """Update heartbeat timestamp for a running agent (called via API)."""
        self._heartbeats[node_id] = time.time()

    async def _heartbeat_cycle(self):
        """Check assigned timeout and heartbeat staleness."""
        while True:
            now = time.time()
            try:
                # Check assigned timeouts (30s)
                for node_id, start in list(self._start_times.items()):
                    if node_id in self._running:
                        continue  # process already running
                    elapsed = now - start
                    if elapsed > self.dispatcher_timeout:
                        node = self.dag_service.get_node(node_id)
                        if node and node.status == "assigned":
                            log.warning("Assigned timeout", node_id=node_id, elapsed=f"{elapsed:.0f}s")
                            self.dag_service.transition_node(node_id, "failed")

                # Check heartbeat staleness: 10s no heartbeat → aborting → interrupted
                for node_id, last_hb in list(self._heartbeats.items()):
                    elapsed = now - last_hb
                    if elapsed > self.heartbeat_interval * 3:  # 30s grace
                        node = self.dag_service.get_node(node_id)
                        if node and node.status == "running":
                            log.warning("Heartbeat lost, aborting", node_id=node_id, silent=f"{elapsed:.0f}s")
                            self.send_abort(node_id)
                            # If agent doesn't come back to set interrupted, clean up here
                            # after a short wait — just mark interrupted as final state
                            self.dag_service.transition_node(node_id, "interrupted")

                # Check reviewing timeout: 300s stuck → failed
                REVIEW_TIMEOUT = 300
                for dag in self.dag_service.list_dags(limit=50):
                    if dag.status != "running":
                        continue
                    for node in self.dag_service.get_nodes_by_dag(dag.dag_id):
                        if node.status != "reviewing":
                            self._review_start.pop(node.node_id, None)
                            continue
                        # First time we see this reviewing node — record start
                        if node.node_id not in self._review_start:
                            self._review_start[node.node_id] = now
                            continue
                        elapsed = now - self._review_start[node.node_id]
                        if elapsed > REVIEW_TIMEOUT:
                            log.warning("Review timeout", node_id=node.node_id,
                                        elapsed=f"{elapsed:.0f}s")
                            self.dag_service.transition_node(
                                node.node_id, "failed",
                                error=f"Review timed out after {int(elapsed)}s"
                            )
                            self._review_start.pop(node.node_id, None)
            except Exception as e:
                log.error("Heartbeat cycle error", error=str(e))

            await asyncio.sleep(5)

    # ── Manual dispatch trigger ──────────────────────────────────────

    def trigger(self, dag_id: str) -> list[str]:
        """Synchronously dispatch all ready nodes for a DAG.
        Spawns subprocess directly (sync), without going through the async loop.

        Returns list of dispatched node_ids."""
        dispatched = []
        nodes = self.dag_service.get_ready_nodes(dag_id)
        if not nodes:
            return dispatched

        # Mark DAG as running so async poll loop picks up subsequent nodes
        dag = self.dag_service.get_dag(dag_id)
        if dag and dag.status == "planning":
            dag.status = "running"
            dag.updated_at = datetime.now(timezone.utc)
            with self.dag_service.new_session() as s:
                s.add(dag)
                s.commit()

        backend_dir = str(Path(__file__).resolve().parent.parent)
        settings = get_settings()

        for node in nodes:
            self.dag_service.transition_node(node.node_id, "assigned")
            self._start_times[node.node_id] = time.time()
            dispatched.append(node.node_id)
            log.info("Trigger dispatch", dag_id=dag_id, node_id=node.node_id,
                     title=node.title or node.goal[:40])

            # Build env — strip proxy
            env = os.environ.copy()
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                        "NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy"):
                env.pop(key, None)
            env["no_proxy"] = "*"

            master_host = "127.0.0.1"
            master_port = settings.PORT
            env.update({
                "NODE_ID": node.node_id,
                "DAG_ID": dag_id,
                "MASTER_API": f"http://{master_host}:{master_port}",
                "ASSIGNED_ROLES": json.dumps(node.assigned_roles),
                "AGENT_ROLE": node.assigned_roles[0] if node.assigned_roles else "generic",
                "REQUIRED_SKILLS": ",".join(node.required_skills),
                "TASK_DEFINITION_JSON": json.dumps({
                    "goal": node.goal,
                    "acceptance_criteria": node.acceptance_criteria,
                    "assigned_roles": node.assigned_roles,
                    "required_skills": node.required_skills,
                }),
                "LLM_ENDPOINT": settings.llm.API_BASE,
                "LLM_MODEL": settings.llm.MODEL,
                "LLM_API_KEY": settings.llm.API_KEY,
            })
            if node.channel_id:
                env["CHANNEL_ID"] = node.channel_id

            # Spawn subprocess with pipe — forward output to server console in real-time
            try:
                proc = subprocess.Popen(
                    [sys.executable, "-m", "runner.agent_runner"],
                    env=env,
                    cwd=backend_dir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
                self._processes[node.node_id] = proc
                self._heartbeats[node.node_id] = time.time()

                # Background reader thread: stream sub-agent output to server console
                def _reader(pid: int, stream, nid: str):
                    try:
                        for raw_line in iter(stream.readline, b""):
                            line = raw_line.decode("utf-8", errors="replace").rstrip()
                            if line:
                                log.info("Agent log", node_id=nid, line=line)
                    except Exception:
                        pass
                    finally:
                        stream.close()

                import threading as _t
                t = _t.Thread(target=_reader, args=(proc.pid, proc.stdout, node.node_id), daemon=True)
                t.start()
                log.info("Agent subprocess spawned", node_id=node.node_id, pid=proc.pid)
            except Exception as e:
                log.error("Failed to spawn agent", node_id=node.node_id, error=str(e))

        return dispatched
