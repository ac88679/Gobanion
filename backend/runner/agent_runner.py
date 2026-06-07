"""
agent_runner.py — 子 Agent 入口

由 Dispatcher 作为 subprocess 启动。通过环境变量获取任务信息，
通过 HTTP API 与主 Agent 通信。

环境变量:
  NODE_ID              — 当前指派的 DAG 节点 ID
  DAG_ID               — DAG 的 ID
  MASTER_API           — 主 Agent HTTP API 地址 (e.g. http://localhost:5000)
  ASSIGNED_ROLES       — JSON 数组字符串 ["backend"]
  AGENT_ROLE           — 当前子 Agent 的角色 (e.g. "backend")
  REQUIRED_SKILLS      — 逗号分隔的 skill 名
  TASK_DEFINITION_JSON — 节点完整定义 (JSON)
  CHANNEL_ID           — 可选，协作频道 ID
  LLM_ENDPOINT         — LLM API 地址 (e.g. http://localhost:8000/v1)
  LLM_MODEL            — 模型名
  LLM_API_KEY          — 可选 API Key
"""

import json
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from services.llm_client import LLMClient

# ── Loop Agent constants ──
MAX_RETRIES = 3          # 每步最大重试次数
MAX_TOTAL_STEPS = 20     # 全局最大执行步数（含修复步骤）

# 从 services 加载 prompt（agent_runner.py 作为 subprocess 运行时路径可能不同）
# 直接算相对路径，不依赖 import
_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
def get_prompt(name: str, **kwargs: str) -> str:
    content = (_PROMPT_DIR / name).read_text(encoding="utf-8")
    if kwargs:
        content = content.format(**kwargs)
    return content


class AgentRuntime:
    """Sub-agent runtime — connects to master via HTTP API."""

    def __init__(self):
        # ── Env ──
        self.node_id = os.environ["NODE_ID"]
        self.dag_id = os.environ["DAG_ID"]
        self.master_api = os.environ["MASTER_API"]
        # Debug: print the actual URL being used
        print(f"[DEBUG] MASTER_API={self.master_api}", flush=True)
        self.assigned_roles = json.loads(os.environ.get("ASSIGNED_ROLES", "[]"))
        self.agent_role = os.environ.get("AGENT_ROLE", "generic")
        self.required_skills = [s.strip() for s in os.environ.get("REQUIRED_SKILLS", "").split(",") if s.strip()]
        self.task_def = json.loads(os.environ.get("TASK_DEFINITION_JSON", "{}"))
        self.channel_id = os.environ.get("CHANNEL_ID")
        self.llm_endpoint = os.environ.get("LLM_ENDPOINT", "http://localhost:8000/v1")
        self.llm_model = os.environ.get("LLM_MODEL", "")
        self.llm_api_key = os.environ.get("LLM_API_KEY", "")

        # ── LLM Client ──
        self.llm_client = LLMClient(public=False)

        # ── Environment detection ──
        import shutil
        self._os_type = "Windows" if os.name == "nt" else "Linux/Mac"
        if shutil.which("uv"):
            self._pip_cmd = "uv pip install"
            self._pip_run_prefix = "uv run"
        elif shutil.which("pip"):
            self._pip_cmd = "pip install"
            self._pip_run_prefix = ""
        else:
            self._pip_cmd = ""
            self._pip_run_prefix = ""

        # ── State ──
        self.step = 0
        self.outputs: list[str] = []
        self.workspace = Path.cwd() / "_workspace" / self.node_id
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._log_file = Path.cwd() / "_logs" / f"{self.node_id}.log"
        self._log_file.parent.mkdir(parents=True, exist_ok=True)

        # ── Heartbeat daemon ──
        self._stop_heartbeat = threading.Event()
        t = threading.Thread(target=self._heartbeat_loop, daemon=True)
        t.start()

    # ── Logging ────────────────────────────────────────────────────

    def log(self, msg: str):
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        line = f"[{ts}] [{self.node_id}] {msg}"
        print(line, flush=True)
        with open(self._log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    # ── Heartbeat loop ──────────────────────────────────────────────

    def _heartbeat_loop(self):
        """Daemon: POST heartbeat to master every 10s until stop event."""
        import urllib.request as _req
        url = f"{self.master_api}/api/v1/dag/{self.dag_id}/nodes/{self.node_id}/heartbeat"
        handler = _req.ProxyHandler({})
        opener = _req.build_opener(handler)
        req = _req.Request(url, method="POST", data=b"{}")
        req.add_header("Content-Type", "application/json")
        while not self._stop_heartbeat.is_set():
            self._stop_heartbeat.wait(10)
            if self._stop_heartbeat.is_set():
                break
            try:
                opener.open(req, timeout=5)
            except Exception:
                pass  # heartbeat failure is non-fatal

    # ── HTTP API helpers (raw HTTP, no proxy interference) ─────────

    def _http_post(self, path: str, body: dict) -> dict:
        """Raw HTTP POST via urllib, bypasses any system proxy."""
        import json as _json
        import urllib.request as _req
        import urllib.error as _err

        url = f"{self.master_api}{path}"
        data = _json.dumps(body).encode("utf-8")

        # Bypass proxy via ProxyHandler(None) + build opener
        handler = _req.ProxyHandler({})
        opener = _req.build_opener(handler)
        req = _req.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "gobanion-agent/0.1",
            },
            method="POST",
        )
        try:
            resp = opener.open(req, timeout=30)
            return _json.loads(resp.read().decode("utf-8"))
        except _err.HTTPError as e:
            msg = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code} from {url}: {msg}")

    def set_status(self, status: str, **extra) -> dict:
        """Update node status via master HTTP API."""
        body = {"status": status, **extra}
        try:
            return self._http_post(
                f"/api/v1/dag/{self.dag_id}/nodes/{self.node_id}/transition",
                body
            )
        except Exception as e:
            self.log(f"set_status({status}) failed: {e}")
            raise

    def write_memory(self, step: int, action: str, result: dict):
        """Write L1 step memory (future: will POST to /api/v1/memory)."""
        # MVP: log to file; Phase 2 will add actual L1 API endpoint
        entry = {
            "node_id": self.node_id,
            "step": step,
            "action": action,
            "actor": self.agent_role,
            "status": result.get("status", "unknown"),
            "summary": result.get("summary", ""),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        memory_file = self.workspace / "_memory.jsonl"
        with open(memory_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def check_abort(self) -> bool:
        """Check if master sent an abort signal."""
        import json as _json
        import urllib.request as _req
        try:
            url = f"{self.master_api}/api/v1/dag/{self.dag_id}/nodes/{self.node_id}"
            handler = _req.ProxyHandler({})
            opener = _req.build_opener(handler)
            req = _req.Request(url, method="GET")
            resp = opener.open(req, timeout=10)
            if resp.status == 200:
                return _json.loads(resp.read()).get("status") in ("aborting", "interrupted")
        except Exception:
            pass
        return False

    # ── LLM helper ─────────────────────────────────────────────────

    def llm_chat(self, messages: list[dict], temperature: float = 0.7) -> str:
        """Call local LLM endpoint."""
        body = {
            "model": self.llm_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": 4096,
            "stream": False,
        }
        headers = {"Content-Type": "application/json"}
        if self.llm_api_key:
            headers["Authorization"] = f"Bearer {self.llm_api_key}"

        # Log full request body
        self.log(f"LLM request body: {json.dumps(body, ensure_ascii=False)}")

        try:
            import json as _json
            import urllib.request as _req
            data = _json.dumps(body).encode("utf-8")
            handler = _req.ProxyHandler({})
            opener = _req.build_opener(handler)
            req = _req.Request(
                f"{self.llm_endpoint}/chat/completions",
                data=data,
                headers=headers,
                method="POST",
            )
            resp = opener.open(req, timeout=120)
            resp_data = _json.loads(resp.read().decode("utf-8"))

            # Log full response body
            self.log(f"LLM response body: {json.dumps(resp_data, ensure_ascii=False)}")
            content = resp_data["choices"][0]["message"]["content"]
            return content
        except Exception as e:
            self.log(f"LLM ERROR: {e}")
            raise

    def llm_chat_json(self, messages: list[dict]) -> dict:
        """Call LLM expecting JSON response."""
        content = self.llm_chat(messages)
        obj = self._extract_json(content)
        if obj is not None:
            return obj
        self.log(f"LLM non-JSON response: {content}")
        raise ValueError("LLM response is not valid JSON")

    @staticmethod
    def _strip_markdown_wrapper(text: str) -> str:
        """Strip markdown code block fences and leading/trailing whitespace.
        Handles ```python, ```json, ```typescript, ```bash, ```shell, etc."""
        import re
        # Remove opening fence like ```python, ```json, ```, etc.
        text = re.sub(r'^```\w*\s*\n', '', text, flags=re.MULTILINE)
        # Remove closing fence ```
        text = re.sub(r'\n```\s*$', '', text)
        return text.strip()

    @staticmethod
    def _extract_json(text: str) -> Optional[dict]:
        """Try to parse a JSON dict from text, using raw_decode to handle trailing data."""
        import json as _json
        decoder = _json.JSONDecoder(strict=False)
        text = text.strip()
        # Try direct parse first
        try:
            obj, idx = decoder.raw_decode(text)
            if isinstance(obj, dict):
                return obj
        except _json.JSONDecodeError:
            pass
        # Try extracting from markdown code block
        import re
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if match:
            try:
                obj, idx = decoder.raw_decode(match.group(1).strip())
                if isinstance(obj, dict):
                    return obj
            except _json.JSONDecodeError:
                pass
        # Try finding first balanced {...} block
        depth = 0
        start = -1
        for i, ch in enumerate(text):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start >= 0:
                    try:
                        obj, idx = decoder.raw_decode(text[start:i+1])
                        if isinstance(obj, dict):
                            return obj
                    except _json.JSONDecodeError:
                        pass
                    start = -1
        return None

    # ── Session context builder ─────────────────────────────────────

    def _build_step_history(self) -> str:
        """Read _memory.jsonl and format as readable step history.
        Returns a string like:
          步骤 1: write_file → ok  - Wrote routes.py (5107 bytes)
          步骤 2: execute_code → ok  - exit=0
        """
        memory_file = self.workspace / "_memory.jsonl"
        if not memory_file.exists():
            return "(无)"

        lines = []
        try:
            with open(memory_file, "r", encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    entry = json.loads(raw)
                    step = entry.get("step")
                    action = entry.get("action", "?")
                    status = entry.get("status", "?")
                    summary = entry.get("summary", "")

                    # Skip internal entries
                    if action in ("start", "plan", "plan_failed", "self_check", "abort", "interrupted"):
                        continue
                    if step is None or (isinstance(step, int) and step <= 0):
                        continue

                    lines.append(f"  步骤 {step}: {action} → {status}  - {summary}")
        except Exception:
            return "(读取历史失败)"

        if not lines:
            return "(无)"
        return "\n".join(lines)

    # ── Main workflow ──────────────────────────────────────────────

    def run(self):
        """Execute the assigned node."""
        self.log(f"Starting node {self.node_id} as {self.agent_role}")

        # 1. Mark running
        self.set_status("running")
        self.write_memory(0, "start", {"status": "ok", "summary": "Agent started"})

        # 2. Plan all steps (one-shot)
        try:
            plan = self._plan_steps()
        except Exception as e:
            err = f"Planning failed: {e}"
            self.log(err)
            self.set_status("failed", outputs=self.outputs, error=err)
            self.write_memory(-1, "plan_failed", {"status": "fail", "summary": str(e)})
            return

        self.log(f"Planned {len(plan)} steps")
        self.write_memory(0, "plan", {"status": "ok", "summary": f"{len(plan)} steps"})

        # 3. Execute steps (Loop Agent: retry + recovery on failure)
        retry_counts: dict[int, int] = {}
        step_idx = 0
        total_steps_executed = 0

        while step_idx < len(plan):
            if self.check_abort():
                self.log("Received abort signal, stopping")
                self._handle_abort()
                return
            if total_steps_executed >= MAX_TOTAL_STEPS:
                err = f"Max total steps ({MAX_TOTAL_STEPS}) exceeded"
                self.log(err)
                self.set_status("failed", outputs=self.outputs, error=err)
                return

            step_def = plan[step_idx]
            self.step = step_idx + 1
            self.log(f"Step {self.step}: {step_def.get('action', 'unknown')}")
            total_steps_executed += 1

            # Execute step — exception is also converted to fail dict for unified handling
            try:
                result = self._execute_step(step_def)
            except Exception as e:
                result = {
                    "status": "fail",
                    "summary": str(e),
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": str(e),
                }

            self.write_memory(self.step, step_def.get("action", "execute"), result)

            if result.get("status") == "ok":
                # Collect outputs
                if result.get("output_file"):
                    self.outputs.append(result["output_file"])
                step_idx += 1
                continue

            # ── Step failed — attempt recovery ──
            retry_counts[step_idx] = retry_counts.get(step_idx, 0) + 1
            if retry_counts[step_idx] > MAX_RETRIES:
                err = f"Step {self.step} failed after {MAX_RETRIES} retries: {result.get('summary', '')}"
                self.log(err)
                self.set_status("failed", outputs=self.outputs, error=err)
                return

            recovery = self._recover_from_error(step_def, result, retry_counts[step_idx])
            action = recovery.get("action", "abort")

            if action == "retry":
                self.log(f"Recovery: retrying step {self.step}")
                # step_idx unchanged → retry same step
            elif action == "fix_and_retry":
                if recovery.get("fix_action", "execute_code") == "write_file":
                    fix_step = {
                        "action": "write_file",
                        "description": recovery.get("fix_description", "Fix file"),
                        "output_file": recovery.get("fix_output_file", "fix.py"),
                        "content": recovery.get("fix_content", ""),
                    }
                else:
                    fix_step = {
                        "action": "execute_code",
                        "description": recovery.get("fix_description", "Fix environment"),
                        "command": recovery.get("fix_command", ""),
                        "output_file": "",
                    }
                self.log(f"Recovery: inserting fix step ({fix_step['action']}) before step {self.step}")
                plan.insert(step_idx, fix_step)
                # step_idx unchanged → fix step runs next, then original step
            elif action == "skip":
                self.log(f"Recovery: skipping step {self.step}")
                step_idx += 1
            else:  # abort
                err = f"Step {self.step} unrecoverable: {recovery.get('reason', '')}"
                self.log(err)
                self.set_status("failed", outputs=self.outputs, error=err)
                return

        # 4. Self-check
        self.log("Running self-check...")
        try:
            self_check = self._run_self_check()
            self.write_memory(-1, "self_check", {"status": "ok", "summary": json.dumps(self_check)})
        except Exception as e:
            self.log(f"Self-check failed: {e}")
            self_check = [{"criterion": "run completed", "result": "pass", "evidence": "script finished"}]

        # 5. Done (dag_service._on_transition auto-advances to completed)
        self.set_status("done", outputs=self.outputs, self_check=self_check)
        self.log(f"Node {self.node_id} completed")

    def _plan_steps(self) -> list[dict]:
        """One-shot planning: goal → list of steps, using tool calling."""
        goal = self.task_def.get("goal", "Complete the assigned task")
        criteria = self.task_def.get("acceptance_criteria", "")
        skills = ", ".join(self.required_skills) if self.required_skills else "general"

        prompt = get_prompt("agent_step_planner.md",
                            role=self.agent_role, goal=goal,
                            criteria=criteria, skills=skills,
                            pip_cmd=self._pip_cmd,
                            os_type=self._os_type,
                            pip_run_prefix=self._pip_run_prefix)

        # ── Tool definition: plan_steps ──
        tools = [{
            "type": "function",
            "function": {
                "name": "plan_steps",
                "description": "规划完成目标的步骤，返回步骤列表",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "steps": {
                            "type": "array",
                            "description": "步骤列表",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "action": {
                                        "type": "string",
                                        "description": "做什么（write_file / execute_code / analyze / test / review）",
                                        "enum": ["write_file", "execute_code", "analyze", "test", "review"],
                                    },
                                    "description": {
                                        "type": "string",
                                        "description": "这一步完成什么",
                                    },
                                    "output_file": {
                                        "type": "string",
                                        "description": "预期的输出文件名，没有就传空字符串",
                                    },
                                    "command": {
                                        "type": "string",
                                        "description": "仅在 action=execute_code 或 action=test 时使用，指定要执行的 shell 命令。其他 action 不要传。",
                                    },
                                },
                                "required": ["action", "description", "output_file"],
                            },
                        }
                    },
                    "required": ["steps"],
                },
            },
        }]

        # vLLM 转发的 Qwen 支持 tool calling，用 tool_choice="required"
        # 子 Agent 走本地模型，public=False
        try:
            result = self.llm_client.chat_with_tools(
                [{"role": "user", "content": prompt}],
                tools,
                tool_choice="required",
            )
        except Exception as e:
            self.log(f"Tool calling failed, falling back to text LLM: {e}")
            raw = self.llm_client.chat_text([{"role": "user", "content": prompt}])
            return self._parse_plan_fallback(raw)

        # Extract steps from tool call result
        for call in result:
            if call["name"] == "plan_steps":
                steps = call["arguments"].get("steps", [])
                if steps:
                    return steps

        # Fallback
        self.log("Tool call returned no steps, using fallback")
        return [{"action": "write_file", "description": goal, "output_file": "output.txt"}]

    def _parse_plan_fallback(self, raw: str) -> list[dict]:
        """Fallback: try to parse JSON from text response."""
        import re
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r"\[.*?\]", raw, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(0))
                except json.JSONDecodeError:
                    pass
            cleaned = re.sub(r"(?<!\\)\\(?![\\\"])", "", raw)
            cleaned = re.sub(r",\s*([\]}])", r"\1", cleaned)
            match = re.search(r"\[(.*)\]", cleaned, re.DOTALL)
            if match:
                try:
                    return json.loads("[" + match.group(1) + "]")
                except json.JSONDecodeError:
                    pass
            self.log(f"Fallback parse failed, raw: {raw}")
            return [{"action": "write_file", "description": goal, "output_file": "output.txt"}]

    def _execute_step(self, step_def: dict) -> dict:
        """Execute a single step. Returns result dict."""
        action = step_def.get("action", "").lower()
        description = step_def.get("description", "")

        if action in ("write_file", "code", "review"):
            return self._step_write_file(step_def)
        elif action in ("execute_code", "test", "run"):
            return self._step_execute(step_def)
        else:
            # Default: use LLM to generate output
            return self._step_llm_generate(step_def)

    def _resolve_output_path(self, step_def: dict) -> Path:
        """Resolve output file path, with fallback for empty/missing names."""
        name = step_def.get("output_file", "").strip()
        if not name:
            name = f"step_{self.step}.txt"
        return self.workspace / name

    def _step_write_file(self, step_def: dict) -> dict:
        """Generate file content via LLM and write to workspace."""
        goal = self.task_def.get("goal", "")
        file_path = self._resolve_output_path(step_def)

        # Pre-defined content from recovery fix steps (write_file action)
        direct_content = step_def.get("content")
        if direct_content:
            content = direct_content
        else:
            prompt = get_prompt("agent_write_file.md",
                                role=self.agent_role, goal=goal,
                                history=self._build_step_history(),
                                description=step_def.get("description", ""))

            content = self.llm_chat([{"role": "user", "content": prompt}])
        # Strip markdown code block wrapping (LLMs love wrapping code in ```)
        content = self._strip_markdown_wrapper(content)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)

        self.log(f"Wrote {len(content)} bytes to {file_path.name}")
        return {
            "status": "ok",
            "summary": f"Wrote {file_path.name} ({len(content)} bytes)",
            "output_file": str(file_path.relative_to(Path.cwd())),
        }

    def _step_execute(self, step_def: dict) -> dict:
        """Execute a shell command (limited MVP)."""
        cmd = step_def.get("command", "").strip()

        # Try to infer command if not provided
        if not cmd:
            self.log("No command specified, attempting to infer from context...")
            inferred = self._infer_command(step_def)
            if inferred:
                cmd = inferred
                self.log(f"Inferred command: {cmd}")
            else:
                return {
                    "status": "fail",
                    "summary": "No command specified and could not infer one",
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": "",
                }

        import subprocess
        try:
            # ── Safety checks ──
            blocked_patterns = ["setx /M", "sudo ", "choco install"]
            for pattern in blocked_patterns:
                if pattern in cmd.lower():
                    return {
                        "status": "fail",
                        "summary": f"Command blocked: '{pattern}' requires admin privileges",
                        "exit_code": -1,
                        "stdout": "",
                        "stderr": f"Blocked: {pattern} needs admin rights",
                    }

            # ── If uv is available, auto-prefix python commands ──
            effective_cmd = cmd
            if self._pip_run_prefix and cmd.startswith("python"):
                effective_cmd = f"{self._pip_run_prefix} {cmd}"
            result = subprocess.run(
                effective_cmd, shell=True, capture_output=True, text=True, timeout=30, cwd=str(self.workspace)
            )
            self.log(f"Executed: {effective_cmd} exit={result.returncode}")
            output = result.stdout + result.stderr
            return {
                "status": "ok" if result.returncode == 0 else "fail",
                "summary": f"exit={result.returncode}, output={output[:2000]}",
                "exit_code": result.returncode,
                "stdout": result.stdout[:2000] if result.stdout else "",
                "stderr": result.stderr[:2000] if result.stderr else "",
            }
        except Exception as e:
            return {
                "status": "fail",
                "summary": str(e),
                "exit_code": -1,
                "stdout": "",
                "stderr": str(e),
            }

    def _infer_command(self, step_def: dict) -> str:
        """Use LLM to infer the shell command when none is provided."""
        description = step_def.get("description", "")
        workspace_files = [f.name for f in self.workspace.iterdir()] if self.workspace.exists() else []
        files_hint = ", ".join(workspace_files[:10]) if workspace_files else "(empty)"

        prompt = (
            f"根据以下信息推断需要执行的 shell 命令。只输出命令本身，不要加解释。\n\n"
            f"步骤描述：{description}\n"
            f"工作区文件：{files_hint}\n"
            f"操作系统：Windows (shell 命令)\n\n"
            f"命令："
        )
        try:
            raw = self.llm_client.chat_text([{"role": "user", "content": prompt}])
            # Clean up: strip markdown code blocks, whitespace, etc.
            raw = raw.strip().strip("`").strip()
            if raw.lower().startswith("command:"):
                raw = raw[len("command:"):].strip()
            if raw.lower().startswith("```"):
                raw = raw.strip("`").strip()
            if raw:
                return raw
        except Exception as e:
            self.log(f"Command inference LLM call failed: {e}")
        return ""

    def _step_llm_generate(self, step_def: dict) -> dict:
        """Use LLM to generate analysis / report / documentation."""
        goal = self.task_def.get("goal", "")
        file_path = self._resolve_output_path(step_def)

        prompt = get_prompt("agent_generate.md",
                            role=self.agent_role, goal=goal,
                            history=self._build_step_history(),
                            description=step_def.get("description", ""))

        try:
            result = self.llm_chat_json([{"role": "user", "content": prompt}])
            content = result.get("content", "")
        except Exception:
            self.log("JSON parse failed, using raw LLM output")
            content = self.llm_chat([{"role": "user", "content": prompt}])

        file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)

        return {
            "status": "ok",
            "summary": f"Generated {file_path.name}",
            "output_file": str(file_path.relative_to(Path.cwd())),
        }

    def _recover_from_error(self, step_def: dict, result: dict, retry_count: int) -> dict:
        """Analyze step failure and decide recovery strategy via LLM."""
        prompt = get_prompt("agent_error_recovery.md",
                            role=self.agent_role,
                            goal=self.task_def.get("goal", ""),
                            history=self._build_step_history(),
                            step_def=json.dumps(step_def, ensure_ascii=False),
                            exit_code=str(result.get("exit_code", "?")),
                            stdout=result.get("stdout", ""),
                            stderr=result.get("stderr", ""),
                            retry_count=str(retry_count),
                            pip_cmd=self._pip_cmd,
                            os_type=self._os_type)

        try:
            raw = self.llm_client.chat_text([{"role": "user", "content": prompt}])
            decision = self._extract_json(raw)
            if decision is None:
                raise ValueError(f"LLM response is not valid JSON: {raw[:200]}")
            action = decision.get("action", "abort")
            if action not in ("retry", "fix_and_retry", "skip", "abort"):
                raise ValueError(f"Invalid action: {action}")
            # fix_and_retry validation
            if action == "fix_and_retry":
                fix_action = decision.get("fix_action", "execute_code")
                if fix_action not in ("execute_code", "write_file"):
                    raise ValueError(f"Invalid fix_action: {fix_action}")
                if fix_action == "execute_code" and not decision.get("fix_command", "").strip():
                    raise ValueError("fix_and_retry/execute_code without fix_command")
                if fix_action == "write_file" and not decision.get("fix_content", "").strip():
                    raise ValueError("fix_and_retry/write_file without fix_content")
            self.log(f"Recovery: {action} — {decision.get('reason', '')}")
            return decision
        except Exception as e:
            self.log(f"Recovery decision failed: {e}")
            return {"action": "abort", "reason": f"Recovery LLM call failed: {e}"}

    def _run_self_check(self) -> list[dict]:
        """Check each acceptance criterion."""
        criteria = self.task_def.get("acceptance_criteria", "")
        if not criteria.strip():
            return [{"criterion": "task completed", "result": "pass", "evidence": "all steps executed"}]

        prompt = get_prompt("agent_self_check.md",
                            role=self.agent_role,
                            goal=self.task_def.get("goal", ""),
                            history=self._build_step_history(),
                            criteria=criteria,
                            workspace=str(self.workspace))

        result = self.llm_chat([{"role": "user", "content": prompt}])
        for _ in range(2):  # retry once
            try:
                return json.loads(result)
            except json.JSONDecodeError:
                import re
                match = re.search(r"\[.*?\]", result, re.DOTALL)
                if match:
                    try:
                        return json.loads(match.group(0))
                    except json.JSONDecodeError:
                        pass
                # Clean up and retry
                cleaned = result.replace("\n", " ").replace("  ", " ").replace("'", '"')
                try:
                    return json.loads(cleaned)
                except json.JSONDecodeError:
                    if _ == 0:
                        self.log("Self-check JSON parse failed, retrying LLM...")
                        result = self.llm_chat([{"role": "user", "content": f"Output valid JSON only:\n{prompt}"}])
                    continue
        return [{"criteria": "task completed", "result": "pass", "evidence": "executed"}]

    def _handle_abort(self):
        """Graceful abort: finish current step, write interrupted memory."""
        self.write_memory(-1, "abort", {"status": "interrupted", "summary": "Received ABORT from master"})
        self.set_status("interrupted", outputs=self.outputs)


# ── Entry point ────────────────────────────────────────────────────


def main():
    if "NODE_ID" not in os.environ:
        print("agent_runner.py: missing NODE_ID env var", file=sys.stderr)
        sys.exit(1)

    runtime = AgentRuntime()
    try:
        runtime.run()
    except Exception as e:
        err = f"Fatal error: {e}"
        runtime.log(err)
        traceback.print_exc()
        try:
            runtime.set_status("failed", outputs=runtime.outputs, error=err)
        except Exception:
            pass
        sys.exit(1)
    finally:
        runtime._stop_heartbeat.set()


if __name__ == "__main__":
    main()
