# 03 - Agent 运行环境设计

> 每个子 Agent 的文件系统、运行环境、资源分配方案
> 版本: v0.5 | 日期: 2026-06-08

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
AgentRuntime 在 subprocess 中启动

  ├─ 环境变量注入（由 Dispatcher 在 subprocess 启动时传入）:
  │   ├─ NODE_ID=T-0E1321
  │   ├─ DAG_ID=a8204202-xxx
  │   ├─ ASSIGNED_ROLES=["backend"]
  │   ├─ REQUIRED_SKILLS=code_generator,test_runner
  │   ├─ TASK_DEFINITION_JSON=... (节点完整定义，含 goal / acceptance_criteria)
  │   ├─ AGENT_ROLE=backend      (当前子 Agent 的角色)
  │   ├─ MASTER_API=http://localhost:5000 (主 Agent HTTP API)
  │   ├─ LLM_ENDPOINT=http://localhost:8000/v1
  │   ├─ LLM_MODEL=Qwen3.5-9B
  │   └── LLM_API_KEY=... (如需要)
  │
  ├─ AgentRuntime.__init__()
  │   ├─ 解析所有环境变量
  │   ├─ 检测运行环境（OS 类型、uv/pip/npm）
  │   ├─ 构建 self._ctx 上下文字典（注入 prompt 模板）
  │   ├─ 创建工作区 _workspace/{dag_id}/{node_id}/
  │   ├─ 复制上游产出物到工作区
  │   ├─ 初始化日志文件 _logs/{dag_id}/{node_id}.log
  │   └── 启动心跳 daemon 线程（每 10s POST /heartbeat）
  │
  ├─ run() 主流程
  │   ├─ set_status("running") — 通过 HTTP API
  │   ├─ _plan_steps() — LLM tool calling 一次性规划全部步骤
  │   │   └─ 失败时降级：文本 LLM → fallback 解析
  │   ├─ 逐步骤执行（Loop Agent while 循环）:
  │   │   ├─ 每步前 check_abort() → 收到 ABORT 则优雅终止
  │   │   ├─ 每步：_execute_step(step_def)
  │   │   │   ├─ action=create_dir → _step_create_dir
  │   │   │   ├─ action=write_file  → _step_write_file (LLM 生成内容)
  │   │   │   ├─ action=execute_code → _step_execute (subprocess.run)
  │   │   │   │   └─ 无 command 时 _infer_command() LLM 推断
  │   │   │   └─ action=analyze/review → _step_llm_generate
  │   │   ├─ 异常 → 转 fail dict 统一处理
  │   │   ├─ ok → 收集输出文件路径，step_idx++
  │   │   ├─ fail → write_memory → Recovery 闭环:
  │   │   │   ├─ _is_fix 修复步失败 → 直接 abort
  │   │   │   ├─ retry → step_idx 不变，重试
  │   │   │   ├─ fix_and_retry → plan 中插入修复步
  │   │   │   ├─ skip → step_idx++
  │   │   │   └─ abort → set_status("failed")
  │   │   └─ 全局上限：最多 20 步，每步最多重试 3 次
  │   ├─ _run_self_check() — 按验收标准逐条自检
  │   ├─ _publish_outputs() — 复制到 _outputs/{dag_id}/
  │   └─ set_status("done") — MVP 跳过 reviewing，直接终态
  │
  ▼
退出（subprocess 终止）
```

子 Agent **没有待命模式**。每次启动都有明确的 `NODE_ID` 指派。完成后退出/回池，不监听后续任务。

---

## 三、运行环境选型

| 方案 | 隔离级别 | 启动速度 | 资源效率 | 复杂度 | 推荐场景 |
|------|---------|---------|---------|--------|---------|
| Docker 容器 | OS 级 | ~1-3s | 低 | 高 | **生产 (首选)** |
| Python subprocess | 进程级 | <10ms | 高 | 低 | **MVP / 本地开发（当前方案）** |
| Nix flake | 依赖级 | ~1-3s | 中 | 高 | Unix-Like 专有 |
---

## 四、MVP 实现：subprocess + Dispatcher

### 4.1 Dispatcher 调度流程（事件驱动，异步，subprocess）

```
主 Agent 规划出执行层节点，assigned_roles: ["backend"]
  │
  ▼
节点依赖满足 → 状态变 ready
  │
  ▼
Dispatcher（事件驱动，独立线程）：
  │
  ├─ 1. 轮询检测 ready 节点（每 2s）
  │
  ├─ 2. 读取节点需求：roles=["backend"], skills=[...]
  │
  ├─ 3. MVP 直接创建 subprocess（无预热池）
  │     └─ subprocess.Popen([sys.executable, "agent_runner.py"], env={...})
  │
  ├─ 4. 注入环境变量（node_id, dag_id, task_def, roles 等）
  │
  ├─ 5. 设置 DAG 节点状态：assigned
  │
  ├─ 6. 启动 assigned 超时检测（30s 未变 running → failed）
  │     └─ 修复：同时检查 assigned 和 running 状态
  │
  └─ 7. 继续处理下一个事件
```

Dispatcher 是主 Agent 进程内的一个组件，运行在独立线程中，所有通信通过 localhost HTTP API 完成。

### 4.2 预热池（Phase 2 规划）

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


### 4.3 Docker 镜像（Phase 2 规划）

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

实际实现在 `backend/runner/agent_runner.py`（约 1100 行），核心类 `AgentRuntime`。

**关键设计差异（vs 设计文档伪代码）：**

| 方面 | 设计文档 | 实际实现 |
|------|---------|---------|
| Prompt 渲染 | 字符串拼接 | Jinja2 模板引擎 + 独立 `.md` 文件 |
| 规划方式 | 文本 LLM | Tool calling (function calling)，fallback 文本 |
| 恢复决策 | 文本 JSON | Tool calling，更可靠的函数式 API |
| 执行环境 | httpx 客户端 | urllib（绕过代理干扰） |
| L1 写入 | HTTP API | 本地 JSONL 文件 |
| 心跳 | — | daemon 线程，每 10s POST |
| 工作区 | /workspace | _workspace/{dag_id}/{node_id}/ |
| 复原保护 | 3 次重试 | 3 层：retry_counts + _fix_attempts + _is_fix abort |
| 命令推断 | — | LLM 推断 shell 命令 (_infer_command) |
| 产出物发布 | — | _outputs/{dag_id}/ 目录，下游可读 |
| 密码安全 | — | urllib + ProxyHandler({}) 绕过系统代理 |

**核心执行循环（简化）：**

```python
while step_idx < len(plan):
    if self.check_abort():
        return  # 优雅终止
    if total_steps >= MAX_TOTAL_STEPS:  # 20
        set_status("failed")

    step = plan[step_idx]
    try:
        result = self._execute_step(step)
    except Exception as e:
        result = {"status": "fail", "exit_code": -1, "stderr": str(e)}

    if result["status"] == "ok":
        step_idx += 1
        continue

    # Fix step failure → immediate abort
    if step.get("_is_fix"):
        set_status("failed")
        return

    # Recovery
    retry_counts[step_idx] += 1
    if retry_counts[step_idx] > 3:
        set_status("failed")
        return

    recovery = self._recover_from_error(step, result, retry_counts[step_idx])
    action = recovery.get("action", "abort")

    if action == "retry":
        pass  # step_idx unchanged
    elif action == "fix_and_retry":
        plan[step_idx:step_idx] = build_fix_steps(recovery)
    elif action == "skip":
        step_idx += 1
    else:  # abort
        set_status("failed", error=reason)
        return
```
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

## 五、子 Agent LLM 调用方式

```
┌─────────────────────────────────────────────┐
│  GPU 主机 (共享)                              │
│                                              │
│  Ollama / vLLM / llama.cpp 运行               │
│  (加载 Qwen3.5-9B 等小模型)             │
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
├── LLM 输出过滤: markdown 代码块包裹 (```...) 写入文件前自动剥离
├── 端口安全: 禁止启动 HTTP 服务（主后端已占用 5000 端口）
├── 禁止后台进程: 不允许 &、nohup 等方式
├── API 测试要求: 使用框架测试客户端 (test_client)，不启动真实服务器
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
