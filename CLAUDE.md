# CLAUDE.md

本文件指导 Claude Code 在此仓库中工作时使用。

## 常用命令

```bash
# 后端
cd backend
uv sync                       # 安装依赖
uv run python main.py         # 启动服务（端口 5000）
uv run pytest tests/          # 运行所有测试
uv run pytest tests/test_dag_service.py -xvs  # 运行单个测试文件

# 前端
cd frontend
npm install
npm run dev                   # Vite 开发服务器
npm run build                 # 生产构建
```

## 项目结构

```
Gobanion/
├── backend/
│   ├── main.py                     # FastAPI 入口
│   ├── config/settings.py          # pydantic-settings（加载链：.env.shared → .env.{ENV} → .env.local）
│   ├── models/dag.py               # SQLModel：DagNode、Dag、DagEvent + 状态机
│   ├── services/
│   │   ├── dag_service.py          # DAG 运行时引擎（SQLite CRUD、状态转换、级联 ready）
│   │   ├── dispatcher.py           # 基于 subprocess 的 Agent 调度器（轮询 + 心跳 + ABORT）
│   │   ├── llm_client.py           # 双模型 LLM 客户端（本地 Qwen + 公网 DeepSeek）
│   │   ├── agents/master_planner.py # 主 Agent：目标 → DAG（调用 DeepSeek）
│   │   ├── logger.py               # 结构化日志（StructLogger）
│   │   └── prompt_loader.py        # 从 prompts/ 加载 .md 提示词模板
│   ├── runner/agent_runner.py      # 子 Agent 运行时（带恢复闭环的 Loop Agent）
│   ├── api/
│   │   ├── dag_router.py           # DAG CRUD + 调度 + ABORT + 日志 + 心跳
│   │   └── plan_router.py          # 目标 → 规划端点
│   ├── prompts/                    # Jinja2 提示词模板
│   │   ├── planner.md              # 主 Agent 规划提示词
│   │   ├── agent_step_planner.md   # 子 Agent 步骤规划
│   │   ├── agent_write_file.md     # 文件生成
│   │   ├── agent_generate.md       # 分析/文档生成
│   │   ├── agent_error_recovery.md # 错误恢复决策
│   │   └── agent_self_check.md     # 验收标准自检
│   └── tests/                      # pytest 测试
├── frontend/
│   ├── src/
│   │   ├── api/index.ts            # Axios HTTP 客户端
│   │   ├── types/index.ts          # TypeScript 类型定义
│   │   ├── main.ts                 # Vue 入口
│   │   └── components/
│   │       ├── DAGCanvas.vue       # D3.js DAG 可视化
│   │       ├── Sidebar.vue         # DAG 列表侧边栏
│   │       ├── CreateModal.vue     # 创建 DAG 对话框
│   │       └── NodeSheet.vue       # 节点详情面板
│   └── vite.config.ts
└── docs/design/                    # 架构设计文档
```

## 系统架构

### 两层 Agent 系统

- **主 Agent**（DeepSeek 公网 API）：接收用户目标 → LLM 一次性产出 DAG（2-8 个节点）→ 持久化到 SQLite → 触发 Dispatcher 调度
- **子 Agent**（本地 Qwen3.5-9B）：每个节点 spawn 一个 subprocess，通过环境变量接收任务，通过 localhost HTTP API 与主后端通信

### 节点状态机

```
pending → ready → assigned → running → done → completed（MVP 跳过 reviewing）
                                          → failed
              → aborting → interrupted
```

状态转换定义在 `models/dag.py` 的 `VALID_TRANSITIONS` 字典中。非法转换会抛 `ValueError`。

### Dispatcher 调度器

- 独立线程中运行 asyncio 事件循环
- 每 2s 轮询 `ready` 节点
- 通过 `subprocess.Popen` 启动 `agent_runner.py`
- 心跳检测：5s 周期，30s 无心跳 → send_abort → interrupted
- assigned 超时：30s 内未变 `running` → failed
- ABORT：设置 `aborting` 状态 → agent 通过 `check_abort()` API 检测 → 优雅停止

### Agent Runner（Loop Agent）

```
while step_idx < len(plan):
    执行步骤 → ok → step_idx++
    执行步骤 → fail → LLM 恢复决策:
        retry         → step_idx 不变，重试
        fix_and_retry → 向 plan 插入修复步骤
        skip          → step_idx++
        abort         → set_status("failed")
    限制: 每步最多重试 3 次，全局最多 20 步，修复步骤失败直接 abort
```

### 提示词管理

所有提示词是 `backend/prompts/` 目录下的 Jinja2 模板。子 Agent 通过 `_ctx` 字典（OS 类型、uv/pip/npm 版本、上游文件）注入模板变量。

### 运行时目录

- `_workspace/{dag_id}/{node_id}/` — 每个节点的工作目录（agent_runner 自动创建）
- `_logs/{dag_id}/{node_id}.log` — 每个节点的日志文件
- `_outputs/{dag_id}/` — 发布的输出文件（下游节点可读）
- `_workspace/{dag_id}/{node_id}/_memory.jsonl` — L1 过程记忆（JSONL 格式，尚未迁移到 SQLite）

### 子 Agent 环境变量（关键）

`NODE_ID`、`DAG_ID`、`MASTER_API`、`ASSIGNED_ROLES`、`AGENT_ROLE`、`REQUIRED_SKILLS`、`TASK_DEFINITION_JSON`、`LLM_ENDPOINT`、`LLM_MODEL`

### 安全约束

- 命令黑名单：`setx /M`、`sudo`、`choco install`
- 禁止启动 HTTP 服务（5000 端口已被主后端占用）
- 禁止后台进程（`&`、`nohup`）
- API 测试必须使用框架测试客户端（如 Flask test_client），不要启动真实服务器
- LLM 输出的 markdown 代码块包裹（```` ```python ````）写入文件前自动剥离
- uv 环境下 `python` 开头的命令自动加 `uv run` 前缀

## 配置

配置通过 pydantic-settings 按优先级加载：`.env.shared` → `.env.{APP_ENV}` → `.env.local` → OS 环境变量。详见 `backend/config/settings.py`。
