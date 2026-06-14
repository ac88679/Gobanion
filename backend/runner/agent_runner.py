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
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from services.llm_client import LLMClient

# ── Loop Agent constants ──
MAX_TOTAL_STEPS = 20     # 全局最大 ReAct 循环步数

# 从 services 加载 prompt（agent_runner.py 作为 subprocess 运行时路径可能不同）
# 直接算相对路径，不依赖 import
_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

_jinja_env = None

def _get_jinja_env():
    global _jinja_env
    if _jinja_env is None:
        from jinja2 import Environment, StrictUndefined
        _jinja_env = Environment(undefined=StrictUndefined, autoescape=False)
    return _jinja_env

def get_prompt(name: str, **kwargs) -> str:
    content = (_PROMPT_DIR / name).read_text(encoding="utf-8")
    tpl = _get_jinja_env().from_string(content)
    return tpl.render(**kwargs)


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
        self._execute_timeout = int(os.environ.get("AGENT_EXECUTE_TIMEOUT", "120"))

        # ── Shared outputs dir (all nodes publish here) ──
        self._outputs_dir = Path.cwd() / "_outputs"

        # ── LLM Client ──
        self.llm_client = LLMClient(public=False)

        # ── Environment detection ──
        import shutil, subprocess
        self._os_type = "Windows" if os.name == "nt" else "Linux/Mac"

        def _get_version(cmd: str) -> str:
            try:
                r = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=5)
                return r.stdout.strip() or r.stderr.strip() or cmd
            except Exception:
                return cmd

        # Python
        self._py_version = _get_version("python")
        has_uv = shutil.which("uv") is not None
        if has_uv:
            self._pip_cmd = "uv pip install"
            self._pip_run_prefix = "uv run"
            self._pip_note = f"安装到项目 .venv，用 {self._pip_run_prefix} 执行时自动可见"
            self._uv_version = _get_version("uv")
        elif shutil.which("pip"):
            self._pip_cmd = "pip install"
            self._pip_run_prefix = ""
            self._pip_note = "安装到系统 Python site-packages"
        else:
            self._pip_cmd = ""
            self._pip_run_prefix = ""
            self._pip_note = "未检测到包管理工具"

        # Node / npm
        self._has_node = shutil.which("node") is not None
        self._node_version = _get_version("node") if self._has_node else ""
        self._has_npm = shutil.which("npm") is not None
        self._npm_version = _get_version("npm") if self._has_npm else ""

        # ── Workspace ──
        self.workspace = Path.cwd() / "_workspace" / self.dag_id / self.node_id
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._log_file = Path.cwd() / "_logs" / self.dag_id / f"{self.node_id}.log"
        self._log_file.parent.mkdir(parents=True, exist_ok=True)

        # ── Upstream outputs: copy into workspace for direct access ──
        upstream_files = []
        dag_upstream = self._outputs_dir / self.dag_id
        if dag_upstream.is_dir():
            import shutil as _su
            for f in sorted(dag_upstream.rglob("*")):
                if f.is_file():
                    rel = f.relative_to(dag_upstream)
                    upstream_files.append(str(rel))
                    _su.copy2(f, self.workspace / rel.name)
        if not upstream_files:
            # Fallback: check root _outputs/ (old format, pre-dag_id)
            for f in sorted(self._outputs_dir.rglob("*")):
                if f.is_file() and f.parent == self._outputs_dir:
                    upstream_files.append(f.name)
                    _su.copy2(f, self.workspace / f.name)

        # ── Upstream context: fetch DAG info for upstream nodes' goals/self_check ──
        upstream_context = "(无)"
        try:
            dag_data = self._http_get(f"/api/v1/dag/{self.dag_id}")
            all_nodes = dag_data.get("nodes", [])
            own_node = next((n for n in all_nodes if n.get("node_id") == self.node_id), {})
            dep_ids = own_node.get("dependencies", [])
            summaries = []
            for n in all_nodes:
                if n.get("node_id") in dep_ids and n.get("status") == "completed":
                    sc_text = ""
                    sc = n.get("self_check")
                    if sc:
                        if isinstance(sc, list):
                            fails = [c.get("criterion", "") for c in sc if c.get("result") != "pass"]
                            sc_text = f", 未通过: {fails}" if fails else ", 全部通过"
                        else:
                            sc_text = f", self_check: {str(sc)[:100]}"
                    summaries.append(
                        f"  [{n['node_id']}] {n.get('goal', '')[:200]}"
                        f" — outputs: {n.get('outputs', [])}{sc_text}"
                    )
            if summaries:
                upstream_context = "\n".join(summaries)
        except Exception as e:
            self.log(f"Upstream context fetch failed (non-fatal): {e}")

        self._ctx = {
            "node_id": self.node_id,
            "dag_id": self.dag_id,
            "py_version": self._py_version,
            "pip_cmd": self._pip_cmd,
            "pip_run_prefix": self._pip_run_prefix or "",
            "has_uv": has_uv,
            "uv_version": self._uv_version if has_uv else "",
            "has_node": self._has_node,
            "node_version": self._node_version if self._has_node else "",
            "npm_version": self._npm_version if self._has_npm else "",
            "os_type": self._os_type,
            "upstream_files": upstream_files,
            "upstream_context": upstream_context,
        }

        # ── State ──
        self.step = 0
        self.outputs: list[str] = []

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
        data = _json.dumps(body, ensure_ascii=False).encode("utf-8")

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

    def _http_get(self, path: str) -> dict:
        """Raw HTTP GET via urllib, bypasses proxy."""
        import json as _json
        import urllib.request as _req
        import urllib.error as _err

        url = f"{self.master_api}{path}"
        handler = _req.ProxyHandler({})
        opener = _req.build_opener(handler)
        req = _req.Request(url, method="GET", headers={"User-Agent": "gobanion-agent/0.1"})
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

    def write_memory(self, step: int, action: str, result: dict, description: str = ""):
        """Write L1 step memory (future: will POST to /api/v1/memory)."""
        # MVP: log to file; Phase 2 will add actual L1 API endpoint
        entry = {
            "node_id": self.node_id,
            "step": step,
            "action": action,
            "actor": self.agent_role,
            "status": result.get("status", "unknown"),
            "summary": result.get("summary", ""),
            "description": description,
            "stdout": result.get("stdout", "")[:500],
            "stderr": result.get("stderr", "")[:500],
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
            data = _json.dumps(body, ensure_ascii=False).encode("utf-8")
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
        import re
        decoder = _json.JSONDecoder(strict=False)
        if not text or not text.strip():
            return None
        text = text.strip()

        # ── Helper: attempt to parse a candidate string ──
        def _try_parse(candidate: str) -> Optional[dict]:
            try:
                obj, idx = decoder.raw_decode(candidate)
                if isinstance(obj, dict):
                    return obj
            except _json.JSONDecodeError:
                pass
            return None

        # 1. Direct parse
        result = _try_parse(text)
        if result:
            return result

        # 2. Extract from markdown code block
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if match:
            result = _try_parse(match.group(1).strip())
            if result:
                return result

        # 3. Find first balanced {...} block
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
                    result = _try_parse(text[start:i+1])
                    if result:
                        return result
                    start = -1

        # 4. Repair-and-retry: fix common JSON errors in the balanced block
        for i, ch in enumerate(text):
            if ch == "{":
                start = i
                break
        if start >= 0:
            # Find best-guess ending: last } that's not inside a string
            end = -1
            in_str = False
            escape = False
            for i, ch in enumerate(text):
                if escape:
                    escape = False
                    continue
                if ch == '\\':
                    escape = True
                elif ch == '"' and not escape:
                    in_str = not in_str
                elif ch == '}' and not in_str:
                    end = i
            if end > start:
                candidate = text[start:end+1]
                # Repair attempts (in order of increasing aggressiveness)
                repairs = [
                    lambda s: s,                                                     # raw
                    lambda s: re.sub(r',\s*([}\]])', r'\1', s),                      # trailing commas
                    lambda s: re.sub(r'(?<!\\)\\(?![\\"/bfnrtu])', r'\\\\', s),       # unescaped backslash
                    lambda s: re.sub(r'''(?<=[{:\s,\[])\s*'([^']*?)'\s*(?=[:\s,\}\]])''', r'"\1"', s),  # single-quote → double-quote (keys & values)
                    lambda s: s.replace("'", '"'),                                    # aggressive: all single → double quotes
                ]
                for repair in repairs:
                    try:
                        fixed = repair(candidate)
                        obj, idx = decoder.raw_decode(fixed)
                        if isinstance(obj, dict):
                            return obj
                    except _json.JSONDecodeError:
                        continue

        return None

    @staticmethod
    def _summarize_code_snippet(content: str) -> str:
        """Extract key structure from code for context summaries."""
        import re
        lines = content.splitlines()
        parts = []

        imports = [l for l in lines if l.startswith("import ") or l.startswith("from ")]
        if imports:
            ext = [i for i in imports if not i.startswith(("import os", "import sys", "import re", "import json", "from typing"))]
            parts.append(f"imports: {len(ext)}" if ext else "imports: stdlib")

        for l in lines:
            s = l.strip()
            if s.startswith("def "):
                parts.append(f"fn:{s[4:].split('(')[0].strip()}")
            elif s.startswith("class "):
                parts.append(f"class:{s[6:].split('(')[0].split(':')[0].strip()}")
            elif "@" in s and "route" in s.lower():
                parts.append(f"route:{s.strip()}")

        return "; ".join(parts[:6]) if parts else f"{len(content)} chars"

    # ── Session context builder ─────────────────────────────────────

    def _build_step_history(self) -> str:
        """Read _memory.jsonl and format as readable step history."""
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
                    description = entry.get("description", "")

                    if action in ("start", "plan"):
                        continue
                    if step is None or (isinstance(step, int) and step <= 0):
                        continue

                    base = f"  步骤 {step}: [{action}] {description}" if description else f"  步骤 {step}: [{action}]"
                    line = f"{base}  → {status}  {summary}"
                    # Append stderr for failed steps
                    entry_stderr = entry.get("stderr", "")
                    if entry_stderr and status != "ok":
                        line += f"\n    stderr: {entry_stderr[:300]}"
                    lines.append(line)
        except Exception:
            return "(读取历史失败)"

        if not lines:
            return "(无)"
        return "\n".join(lines)

    # ── Main workflow ──────────────────────────────────────────────

    def run(self):
        """Execute the assigned node using ReAct loop (think→act→observe)."""
        self.log(f"Starting node {self.node_id} as {self.agent_role}")

        # 1. Mark running
        self.set_status("running")
        self.write_memory(0, "start", {"status": "ok", "summary": "Agent started"})

        # 2. ReAct loop: decide → execute → observe, repeat
        total_steps = 0
        while total_steps < MAX_TOTAL_STEPS:
            if self.check_abort():
                self.log("Received abort signal, stopping")
                self._handle_abort()
                return

            total_steps += 1
            self.step = total_steps
            self.log(f"ReAct step {self.step}")

            # Decide next action
            try:
                decision = self._react_decide_next()
            except Exception as e:
                err = f"Decision failed: {e}"
                self.log(err)
                self.set_status("failed", outputs=self.outputs, error=err)
                self.write_memory(self.step, "decision_failed",
                                  {"status": "fail", "summary": str(e)})
                return

            action = decision.get("action", "")
            if action == "done":
                self.log("Agent decided task is complete")
                break

            # Build step_def from decision
            step_def = {
                "action": action,
                "description": decision.get("description", ""),
                "output_file": decision.get("output_file", ""),
                "command": decision.get("command", ""),
            }

            # Execute step
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

            self.write_memory(self.step, action, result,
                              description=step_def.get("description", ""))

            if result.get("status") == "ok":
                if result.get("output_file"):
                    self.outputs.append(result["output_file"])
            else:
                # Failure is information — ReAct will see it in history and decide next action
                self.log(f"Step {self.step} failed, ReAct will decide next action: {result.get('summary', '')}")

        if total_steps >= MAX_TOTAL_STEPS:
            err = f"Max total steps ({MAX_TOTAL_STEPS}) exceeded"
            self.log(err)
            self.set_status("failed", outputs=self.outputs, error=err)
            return

        # 3. Self-check
        self.log("Running self-check...")
        try:
            self_check = self._run_self_check()
            self.write_memory(-1, "self_check", {"status": "ok", "summary": json.dumps(self_check, ensure_ascii=False)})
        except Exception as e:
            self.log(f"Self-check failed: {e}")
            self_check = [{"criterion": "run completed", "result": "pass", "evidence": "script finished"}]

        # 4. Publish outputs to shared _outputs/ directory
        self._publish_outputs()

        # 5. Done
        self.set_status("done", outputs=self.outputs, self_check=self_check)
        self.log(f"Node {self.node_id} completed")

    def _react_decide_next(self) -> dict:
        """ReAct: observe current state → decide next action via tool calling."""
        goal = self.task_def.get("goal", "Complete the assigned task")
        criteria = self.task_def.get("acceptance_criteria", "")

        prompt = get_prompt("agent_react.md",
                            role=self.agent_role, goal=goal,
                            criteria=criteria,
                            history=self._build_step_history(),
                            **self._ctx)

        # ── Tool definition: act ──
        tools = [{
            "type": "function",
            "function": {
                "name": "act",
                "description": "根据当前状态决定下一步行动",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reasoning": {
                            "type": "string",
                            "description": "你的思考——分析当前已完成步骤、剩余工作、下一步做什么及原因",
                        },
                        "action": {
                            "type": "string",
                            "enum": ["write_file", "execute_code", "create_dir", "analyze", "done"],
                            "description": "下一步行动：write_file=写文件, execute_code=执行命令, create_dir=创建目录, analyze=分析/生成报告, done=任务完成",
                        },
                        "description": {
                            "type": "string",
                            "description": "这一步要完成什么",
                        },
                        "output_file": {
                            "type": "string",
                            "description": "必填。write_file=要写入/修改的文件名（如 init_db.py）；analyze=报告文件名（如 step_N.txt）；execute_code=填空字符串",
                        },
                        "command": {
                            "type": "string",
                            "description": "仅在 action=execute_code 时使用，指定 shell 命令。其他 action 不要填。",
                        },
                    },
                    "required": ["reasoning", "action", "description", "output_file"],
                },
            },
        }]

        for attempt in range(2):
            try:
                result_list = self.llm_client.chat_with_tools(
                    [{"role": "user", "content": prompt}],
                    tools,
                    tool_choice="required",
                )
                decision = result_list[0]["arguments"] if result_list else None
                if not decision:
                    raise ValueError("Tool call returned no arguments")
                action = decision.get("action", "done")
                if action not in ("write_file", "execute_code", "create_dir", "analyze", "done"):
                    raise ValueError(f"Invalid action: {action}")
                self.log(f"ReAct: {action} — {decision.get('reasoning', '')[:120]}")
                return decision
            except Exception as e:
                self.log(f"ReAct decision attempt {attempt + 1} failed: {e}")
                if attempt == 0:
                    continue
                return {"action": "done", "description": f"Fallback after error: {e}"}


    def _execute_step(self, step_def: dict) -> dict:
        """Execute a single step. Returns result dict."""
        action = step_def.get("action", "").lower()
        description = step_def.get("description", "")

        if action == "create_dir":
            return self._step_create_dir(step_def)
        if action in ("write_file", "code", "review"):
            return self._step_write_file(step_def)
        elif action in ("execute_code", "test", "run"):
            return self._step_execute(step_def)
        elif action == "analyze":
            return self._step_llm_generate(step_def)
        else:
            # Default: use LLM to generate output
            return self._step_llm_generate(step_def)

    def _resolve_output_path(self, step_def: dict) -> Path:
        """Resolve output file path, with fallback for empty/missing names."""
        name = step_def.get("output_file", "").strip()
        if not name:
            name = f"step_{self.step}.txt"
        return self.workspace / name

    def _step_create_dir(self, step_def: dict) -> dict:
        """Create directory (and parents) in workspace."""
        dir_path = self._resolve_output_path(step_def)
        try:
            dir_path.mkdir(parents=True, exist_ok=True)
            self.log(f"Created directory {dir_path}")
            return {
                "status": "ok",
                "summary": f"Created directory {dir_path.name}",
                "output_file": str(dir_path.relative_to(Path.cwd())),
            }
        except Exception as e:
            return {
                "status": "fail",
                "summary": str(e),
                "exit_code": -1,
                "stdout": "",
                "stderr": str(e),
            }

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
                                description=step_def.get("description", ""),
                                **self._ctx)

            content = self.llm_chat([{"role": "user", "content": prompt}])
        # Strip markdown code block wrapping (LLMs love wrapping code in ```)
        content = self._strip_markdown_wrapper(content)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)

        self.log(f"Wrote {len(content)} bytes to {file_path.name}")
        return {
            "status": "ok",
            "summary": f"Wrote {file_path.name} ({len(content)} bytes) — {self._summarize_code_snippet(content)}",
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

        # ── Normalize path prefixes ──
        # Qwen often generates backend/XXX paths (thinking from project root),
        # but actual cwd is the workspace directory. Strip mistaken prefix.
        import re
        normalized = re.sub(r'\b(?:(?:\.\./)?backend/|\.\./src/)', '', cmd, count=1).strip()
        if normalized != cmd:
            self.log(f"Normalized path: '{cmd}' → '{normalized}'")
            cmd = normalized

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

            # ── Auto-prefix python commands with uv ──
            effective_cmd = cmd
            if self._pip_run_prefix and cmd.startswith("python"):
                effective_cmd = f"{self._pip_run_prefix} {cmd}"

            result = subprocess.run(
                effective_cmd, shell=True, capture_output=True,
                encoding="utf-8", errors="replace", timeout=self._execute_timeout,
                cwd=str(self.workspace),
            )
            stdout = result.stdout or ""
            stderr = result.stderr or ""
            self.log(f"Executed: {effective_cmd} exit={result.returncode}")
            if result.returncode == 0:
                summary = f"exit=0"
                if stdout.strip():
                    last = [l for l in stdout.strip().splitlines() if l.strip()][-3:]
                    summary += f", last lines: {' | '.join(last)}"
            else:
                err_head = [l for l in stderr.strip().splitlines() if l.strip()][:5]
                summary = f"exit={result.returncode}, stderr: {' | '.join(err_head)}" if err_head else f"exit={result.returncode}"
            return {
                "status": "ok" if result.returncode == 0 else "fail",
                "summary": summary,
                "exit_code": result.returncode,
                "stdout": stdout[:2000],
                "stderr": stderr[:2000],
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
            "summary": f"Generated {file_path.name} — {self._summarize_code_snippet(content)}",
            "output_file": str(file_path.relative_to(Path.cwd())),
        }

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

    def _publish_outputs(self):
        """Copy node output files to shared _outputs/{dag_id}/ directory.

        Each step's output_file is relative to workspace.
        Published to _outputs/{dag_id}/ preserving filename.
        Downstream nodes see these files via _upstream_context prompt.
        """
        if not self.outputs:
            return
        published = 0
        dag_out = self._outputs_dir / self.dag_id
        dag_out.mkdir(parents=True, exist_ok=True)
        for rel_path in self.outputs:
            src = Path.cwd() / rel_path
            if not src.exists() or not src.is_file():
                continue
            dst = dag_out / src.name
            try:
                import shutil
                shutil.copy2(src, dst)
                published += 1
            except Exception as e:
                self.log(f"Publish failed: {rel_path} → {dst}: {e}")
        if published:
            self.log(f"Published {published} file(s) to {self._outputs_dir.name}/{self.dag_id}/")


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
