# 03 - Agent 运行环境设计

> 每个子 Agent 的文件系统、运行环境、资源分配方案
> 版本: v0.4 | 日期: 2026-06-07

---

## 一、子 Agent 需要什么

```
每个子 Agent 需要:
├── 文件系统     → 读/写/删 代码、文档、数据文件
├── 运行环境     → 能编译、测试、lint、git 操作
├── LLM 调用     → 连接本地的 LLM 推理服务
├── 网络访问     → 连接 LLM API + 通信设施
├── 资源限制     → CPU/内存/磁盘配额
├── 安全边界     → 不越权访问其他 Agent 的工作区
└── 启动上下文   → 拿到 DAG 节点定义 + 自己的角色 + 负责的 Skill
```

---

## 二、核心流程：子 Agent 如何执行被指派的 DAG 节点

```
Dispatcher 从预热池获取空闲容器（或创建新容器）

  ├─ 注入环境变量:
  │   ├─ NODE_ID=T-003
  │   ├─ DAG_ID=project-xxx
  │   ├─ ASSIGNED_ROLES=["backend","frontend"]
  │   ├─ REQUIRED_SKILLS=code_generator,git_operator,test_runner
  │   ├─ TASK_DEFINITION_JSON=... (节点完整定义)
  │   ├─ CHANNEL_ID=chan-T-003  (如有多 Agent 协作)
  │   ├─ AGENT_ROLE=backend      (当前子 Agent 的角色)
  │   ├─ MASTER_API=http://localhost:xxxx (主 Agent HTTP API)
  │   └─ LLM_ENDPOINT=http://ollama:11434
  │
  ├─ 容器启动 agent_runner.py 进入执行模式
  │
  ▼
执行模式
  │
  ├─ 从环境变量解析 NODE_ID、ROLE、SKILLS
  ├─ 从 TASK_DEFINITION_JSON 读取节点定义
  ├─ 标记 DAG 节点状态为 running
  ├─ 加载 Skill 组合
  ├─ 加载角色 Prompt（根据 ASSIGNED_ROLE + 节点 goal）
  │
  ├─ (可选) 加入频道 → 与其他协作子 Agent 对齐
  │   └─ 使用结构化消息协议：proposal/response/issue/synced
  │
  ├─ 一次性规划全部执行步骤 → 逐步骤执行：
  │   ├─ 每步：执行 Skill → 写 L1（通过 HTTP API）
  │   ├─ 每步前检查 ABORT 信号
  │   └─ LLM 失败时按三级回退策略处理
  │
  ├─ 全部步骤执行完 → 按验收标准逐条自检（生成自检报告）
  ├─ 节点完成 → 产出物写入 _outputs/
  ├─ 标记 DAG 节点状态为 done（附带自检报告，通过 HTTP API）
  ├─ 广播节点间信号（下游节点检查依赖）
  │
  ▼
退出 / 回预热池（等待下一次指派）
```

子 Agent **没有待命模式**。每次启动都有明确的 `NODE_ID` 指派。完成后退出/回池，不监听后续任务。

---

## 三、运行环境选型

| 方案 | 隔离级别 | 启动速度 | 资源效率 | 复杂度 | 推荐场景 |
|------|---------|---------|---------|--------|---------|
| Docker 容器 | OS 级 | ~1-3s | 低 | 高 | **生产 (首选)** |
| Python subprocess | 进程级 | <10ms | 高 | 低 | MVP / 本地开发 |
| Nix flake | 依赖级 | ~1-3s | 中 | 高 | Unix-Like 专有 |

---

## 四、推荐方案：Docker + 预热池 + Dispatcher

### 4.1 Dispatcher 调度流程（事件驱动，异步）

```
主 Agent 规划出执行层节点 T-003，assigned_roles: ["backend"]
  │
  ▼
节点依赖满足 → 状态变 ready
  │
  ▼
Dispatcher（事件驱动）：
  │
  ├─ 1. 监听节点状态变更，检测到 ready 节点
  │
  ├─ 2. 读取节点需求：roles=["backend"], skills=[...]
  │
  ├─ 3. 从预热池查找：
  │     ├─ 精确匹配 "backend" 角色的空闲容器 → 命中
  │     ├─ 未命中 → 查找 "generic" 兜底
  │     └─ 仍未命中 → 创建新容器（带角色标签）
  │
  ├─ 4. 注入环境变量（见上面启动流程）
  │
  ├─ 5. 设置 DAG 节点状态：pending → ready → assigned
  │
  ├─ 6. 异步启动 agent_runner.py（不等待就绪）
  │
  └─ 7. 立刻返回，继续处理下一个事件
```

Dispatcher 是主 Agent 进程内的一个组件，负责将"角色需求"映射到"实际运行实例"。所有通信通过 localhost HTTP API 完成。

### 4.2 预热池

```python
class ContainerPool:
    min_idle: 3        # 至少保持 3 个空闲容器
    max_total: 20      # 最多 20 个
    image: "go-banion-worker:latest"

    def acquire(role: str) -> Container:
        """按角色找空闲容器：精确匹配 → generic 兜底 → 创建新容器"""

    def release(container):
        """清理 workspace，放回池子"""

    def create_with_role(role: str) -> Container:
        """创建带角色标签的新容器"""
```

容器带角色标签（创建时通过 `AGENT_ROLE` 环境变量固定），Dispatcher 按角色匹配。

### 4.3 Docker 镜像

```dockerfile
FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    git curl jq vim-tiny \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY agent_runner.py .
COPY skills/ /skills/
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

VOLUME /workspace
VOLUME /skills:ro

CMD ["python", "agent_runner.py"]
```

### 4.4 agent_runner.py 核心逻辑

```python
import json, os, httpx
from pathlib import Path

MASTER_API = os.environ["MASTER_API"]

class AgentRuntime:
    def __init__(self):
        self.workspace = Path("/workspace") if os.name != "nt" else Path(os.getcwd())
        self.node_id = os.environ["NODE_ID"]
        self.dag_id = os.environ["DAG_ID"]
        self.assigned_roles = json.loads(os.environ["ASSIGNED_ROLES"])
        self.my_role = os.environ["AGENT_ROLE"]
        self.required_skills = os.environ["REQUIRED_SKILLS"].split(",")
        self.task_def = json.loads(os.environ["TASK_DEFINITION_JSON"])
        self.channel_id = os.environ.get("CHANNEL_ID")

        # 加载 Skill
        self.skills = self.load_skills(self.required_skills)
        # 连接频道（如需）
        self.channel = self.connect_channel(self.channel_id) if self.channel_id else None

        # HTTP API 客户端
        self.api = httpx.Client(base_url=MASTER_API)

    # ─── HTTP API 封装 ──────────────────────────
    def set_status(self, status: str, **extra):
        self.api.post("/api/v1/node/status", json={
            "node_id": self.node_id, "status": status, **extra
        })

    def write_memory(self, step, action, result):
        self.api.post("/api/v1/memory/write", json={
            "node_id": self.node_id, "step": step,
            "action": action, "result": result
        })

    def check_abort(self) -> bool:
        r = self.api.get(f"/api/v1/node/check-abort?node_id={self.node_id}")
        return r.json().get("aborted", False)

    def check_stuck_arbitration(self) -> dict | None:
        """查是否有仲裁结果"""
        r = self.api.get(f"/api/v1/channel/arbitration?channel_id={self.channel_id}")
        return r.json() if r.status_code == 200 else None

    # ─── 主流程（Loop Agent）─────────────────────────────────
    def run(self):
        logger.info(f"Starting node {self.node_id} as {self.my_role}")
        self.set_status("running")

        # 频道对齐（如需协作）
        if self.channel:
            self.sync_with_collaborators()

        # 一次性规划全部步骤
        plan = self.llm_plan_all_steps()
        self.write_memory(0, "plan", {"steps": len(plan)})

        # Loop Agent: while + step pointer + LLM recovery
        retry_counts = {}
        step_idx = 0
        while step_idx < len(plan):
            if self.check_abort():
                self.handle_abort()
                return
            step = plan[step_idx]
            # 异常转 fail dict，与正常 fail 统一走 recovery
            try:
                result = self.execute_step(step)
            except Exception as e:
                result = {"status": "fail", "exit_code": -1, "stderr": str(e)}

            if result.get("status") == "ok":
                step_idx += 1
                continue

            # 错误恢复闭环（retry / fix_and_retry / skip / abort）
            retry_counts[step_idx] = retry_counts.get(step_idx, 0) + 1
            if retry_counts[step_idx] > 3:
                self.set_status("failed")
                return
            recovery = self.recover_from_error(step, result, retry_counts[step_idx])
            action = recovery.get("action", "abort")
            if action == "retry":
                pass  # 重试当前步
            elif action == "fix_and_retry":
                plan.insert(step_idx, {"action": "execute_code",
                                       "command": recovery["fix_command"]})
            elif action == "skip":
                step_idx += 1
            else:  # abort
                self.set_status("failed", error=recovery.get("reason", ""))
                return

            # 频道中汇报进展（如有多 Agent）
            if self.channel:
                self.channel.post_progress(result)

        # 自检
        self_check = self.run_self_check()
        self.write_memory(-1, "self_check", self_check)

        # 完成
        self.write_outputs()
        self.set_status("done", self_check=self_check)
```

```
                                                    ┌─ retry ──→ 重试当前步
                                                    │
          run() → plan → while step_idx < len(plan) ─┼─ fix_and_retry → 插入修复步 → 重试
                                                    │
               ↑                  ↓                 ├─ skip ──→ 跳过当前步
               │            result == fail           │
               │                 │                   └─ abort ──→ set_status(failed)
               │        LLM 分析错误决策
               │                 │
               └────── 根据 action 处理 ────→ 继续循环

        retry_counts[step_idx] > 3 → 直接 failed
        total_steps > 20 → 直接 failed
```

    def run_self_check(self) -> dict:
        """按验收标准逐条自检，返回自检报告"""
        criteria = self.task_def.get("acceptance_criteria", "")
        return self.llm_check(criteria)

    def handle_abort(self):
        self.write_memory(-1, "abort", {"reason": "received ABORT from master"})
        result = self.finish_current_step()
        self.write_memory(-2, "interrupted", result)
        self.set_status("interrupted")

    def sync_with_collaborators(self):
        """频道中对齐（结构化消息协议）"""
        self.channel.post("proposal", {"content": self.propose_interface()})
        round = 0
        while not self.channel.is_synced():
            response = self.channel.wait_for_response(timeout=30)
            if response["type"] == "accept":
                self.channel.archive()
                break
            elif response["type"] == "reject":
                round += 1
                if round >= 3:
                    # stuck → 写 evidence，等仲裁
                    evidence = self.summarize_position()
                    self.channel.post("evidence", {"content": evidence})
                    self.set_status("stuck", reason="频道对齐超 3 轮")
                    # 等仲裁结果
                    arbitration = self.wait_for_arbitration(timeout=120)
                    if arbitration:
                        self.channel.post("synced", {"arbitration": arbitration})
                    return
                self.channel.post("proposal", {
                    "in_response_to": response["id"],
                    "content": self.revise_proposal(response)
                })
        self.channel.post("synced", {})

    def wait_for_arbitration(self, timeout=120):
        """等待主 Agent 仲裁结果"""
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            result = self.check_stuck_arbitration()
            if result:
                return result
            time.sleep(3)
        return None
```

---

## 四-X. 新增特性（v0.4）

### 环境检测

子 Agent 启动时自动检测运行环境，注入到 LLM 上下文：

```
self._os_type = "Windows" / "Linux/Mac"
self._pip_cmd = "uv pip install" / "pip install" / ""
self._pip_run_prefix = "uv run" / ""
```

检测结果影响三处：
- planner prompt：告知 LLM 操作系统类型和包管理工具
- 执行层：自动给 `python` 开头的命令加 `uv run` 前缀
- recovery prompt：告知 LLM 可用的包管理命令

### 执行安全

**命令黑名单**：拦截需要管理员权限的命令（`setx /M`、`sudo`、`choco install`）

**Markdown 剥离**：写入文件前自动去掉 LLM 输出的 ```python ... ``` 包裹

### 日志实时流

子 Agent 的每条日志通过 `print(flush=True)` 实时输出到 stdout，Dispatcher 通过 `subprocess.PIPE` + 后台 reader 线程逐行转发到服务器控制台。

日志 API：`GET /api/v1/dag/{dag_id}/nodes/{node_id}/logs?tail=N&offset=N`

支持 tail（取最后 N 行）和 offset（增量读取）两种轮询方式。

### Session 上下文组装

每步 LLM 调用前读取 `_memory.jsonl`，构建累积步骤历史传入 `{history}` 变量：

```
已完成步骤：
  步骤 1: write_file → ok  - Wrote routes.py (5107 bytes)
  步骤 2: execute_code → ok  - exit=0, output=...
```

各 prompt 模板均增加 `{history}` 占位符，让 LLM 知道前面已经做了什么。

---

## 五、子 Agent LLM 调用方式

```
┌─────────────────────────────────────────────┐
│  GPU 主机 (共享)                              │
│                                              │
│  Ollama / vLLM / llama.cpp 运行               │
│  (加载 Qwen2.5-Coder-7B 等小模型)             │
│                                              │
│  API: /v1/chat/completions                   │
│  API: /v1/embeddings                         │
└───────────────────────┬─────────────────────┘
                        │ HTTP/gRPC
                        │
    ┌───────────────────┼───────────────────┐
    │   子 Agent 容器    │  子 Agent 容器    │
    │   通过 env 知道    │  通过 env 知道    │
    │   LLM_ENDPOINT    │   LLM_ENDPOINT    │
    └───────────────────┘───────────────────┘
```

优势：
- 1 块 GPU 服务 N 个子 Agent
- 容器不需要 GPU 直通，portable
- 模型切换统一管理

### 5.1 LLM 调用失败回退

| 失败类型 | 重试次数 | 策略 |
|---------|---------|------|
| 格式错误（非 JSON） | 2 次 | 注入原始输出，要求"修正格式" |
| 内容不符合约束 | 1 次 | 注入更严格的约束说明 |
| 连续失败 / 超时 | 降级 | 用预设模板 / 报"无法完成" |

可配置 `FALLBACK_MODELS` 列表，主模型失败 3 次后自动切换。

---

## 六、安全边界

```
容器层面:
├── 不使用 --privileged
├── 只读文件系统 (--read-only)，/workspace 除外
├── 网络限制: 只允许 LLM API + 通信设施
├── 内存限制 --memory=1g, --memory-swap=0
├── 移除非必要 Linux capabilities
└── 不挂载宿主机的敏感路径

文件层面:
├── 每个容器独立的 /workspace
├── 不同 Agent 的 workspace 不可互相访问
├── 产出物通过 _outputs/ 目录持久化
└── /skills 只读挂载，子 Agent 不可修改

进程层面:
├── 单节点超时: 每个节点最长 300s (可配置)
├── 每步重试限制: 最多 3 次
├── 全局执行上限: 最多 20 步（含修复步骤）
├── 无响应检测: 10s 无心跳 → 标记 interrupted
├── 命令黑名单: setx /M、sudo、choco install 等提权命令
├── uv run 隔离: 通过 uv run 运行 Python 命令，不污染系统环境
├── 通信设施有独立的安全通道
└── uvicorn 热重载排除: _workspace/***、_logs/***、.venv/***
```

---

## 七、混合运行模式

```
轻量任务 (读/规划/文档):  内存模式 → <10ms
重量任务 (编码/编译/测试): Docker 模式 → ~1-3s + 预热池
```

MVP 阶段可以直接跳过 Docker，用 `tempdir + subprocess` 把 80% 的路跑通。

---

## 八、Windows 开发支持

```
Windows 下:
├── Docker Desktop (WSL2 后端) → Linux 容器
├── Ollama 可安装在 Windows 或 WSL2
├── 容器通过 host.docker.internal 访问宿主机服务
└── MVP 阶段可不用 Docker，直接 subprocess 跑
```
