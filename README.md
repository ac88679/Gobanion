# Gobanion（官弈）

多 Agent 协作编排系统 —— 主 Agent 拆任务，子 Agent 干活。

## 名字由来

围棋盘上收官阶段，每一手都在精细编排 —— 看似零散的落子，指向同一个终局目标。Gobanion 想做的，就是智能体之间的"系统编排"。

## 核心架构

### 两层 Agent

| 角色 | 模型 | 职责 |
|------|------|------|
| **主 Agent** | DeepSeek（公网） | 分层规划、指派、监控 |
| **子 Agent** | Qwen3.5-9B（本地） | 接收节点 → 规划步骤 → 逐个执行 |

主 Agent 决定"做什么"，子 Agent 决定"怎么做"。每个子 Agent 只看到自己被指派的节点上下文，互不干扰。同一节点内多个子 Agent 可通过频道协作（设计中，尚未实现）。

### 节点状态机

```
pending → ready → assigned → running → done → completed
                                        → failed
             → aborting → interrupted
```

### Loop Agent（子 Agent 自愈）

子 Agent 不是一次跑完的脚本，而是 while 循环 + LLM 错误恢复：

```
plan → step1 → step2(fail) → LLM 分析 → retry/fix/skip/abort → step3 → done
```

最多重试 3 次，总步数不超过 20。

## 当前状态

**MVP 开发中**，核心链路已通但远未生产可用。

能做的事：
- 输入一个目标 → 主 Agent（DeepSeek）拆成 DAG → 子 Agent（本地 Qwen）逐个执行
- 子 Agent 出错能自动重试或修复（Loop Agent 闭环）
- 前端实时看 DAG 状态和日志

还不能做的事：
- 多节点并行调度
- 节点间通信和协作
- 产出物管理和持久化
- 完整 Pipeline 的全自动执行

## 实施计划与进度

### Phase 1 — 核心验证（MVP）

#### ✅ 已完成

| 模块 | 说明 | 备注 |
|------|------|------|
| 主 Agent 分层规划 | 目标层 → 执行层一次性产出（DAG） | `master_planner.py`，调用 DeepSeek 公网模型 |
| DAG Service 基础 | SQLite 存储 + 节点状态机 | `dag_service.py`，支持 CRUD + 状态转换 + 级联 ready |
| DAG 可视化 | Vue 3 + D3.js 拓扑布局 | `DAGCanvas.vue` |
| DB 迁移 | 自动 `ALTER TABLE` + 字段扩展 | `title`、`error` 字段均已添加 |
| 统一日志系统 | 结构化日志、全 LLM IO 记录 | `logger.py` + `StructLogger` |
| 失败原因链路 | 子 Agent error → DB → API → 前端展示 | 所有失败点传 error 字段 |
| Prompt 外置 | 全部提示词提取到独立 `.md` 文件 | `backend/prompts/` × 7 个文件 |
| 前端基础 | 侧边栏、DAG 列表、创建 modal、节点详情卡片 | `Sidebar.vue`、`CreateModal.vue`、`NodeSheet.vue` |
| Loop Agent 恢复闭环 | 子 Agent 出错 → LLM 分析 → retry/fix_and_retry/skip/abort | `agent_runner.py` |
| 日志实时流 | Dispatcher 逐行转发子 Agent stdout，日志 API 端点 | `dispatcher.py` PIPE + reader 线程 |
| Session 上下文组装 | 每步 LLM 调用携带累积步骤历史 | `agent_runner.py` |
| uv 环境检测 | 自动检测 uv/pip，执行层自动加前缀 | `agent_runner.py` |
| 执行安全 | 命令黑名单（`setx /M`、`sudo`），markdown 剥离，uv run 隔离 | `agent_runner.py` |
| ABORT 优雅中止 | API 端点 + dispatcher send_abort + 前端按钮 + 状态机路径 | dispatcher + agent_runner |

#### 📋 待办（按预计实现顺序）

| 优先级 | 模块 | 说明 | 备注 |
|--------|------|------|------|
| P1 | L1 过程记忆 | node_memory / channel_memory / blockers 三张表 + HTTP API | 设计文档已定 |
| P1 | 节点内频道 | 结构化消息 5 种类型 + 举证摘要仲裁 | 完全未实现 |
| P1 | 产出物管理 | `_outputs/` 目录按 `{node_id}/{step}_{filename}` 组织 | 当前走到 workspace 临时目录 |
| P1 | 重规划引擎 | DAG 增删改 + ABORT + stuck 仲裁 + 监听 failed 节点重新指派 | 完全未实现 |
| P1 | reviewing 环节 | 主 Agent EventHandler 监听到 done → 复核自检报告 → completed/failed | 当前跳过 |
| P1 | EventHandler 框架 | 目前只有 Dispatcher polling，无通用事件回调机制 | |
| P2 | 最简单 Demo | 主 Agent 拆任务 → 子 Agent 接活 → 写文件 → 完成 | 单个节点已跑通，全链路还需修复上述问题 |
| P2 | DAG 级别状态转换 | Dag.status 缺少 can_transition 校验和转换函数 | |

### Phase 2 — 完善基础设施

| 任务 | 状态 |
|------|------|
| Docker 容器化子 Agent + 预热池（按角色分池） | ❌ |
| 混合运行模式（内存轻量 + Docker 重量） | ❌ |
| L2 沉淀记忆 + 主 Agent 审核沉淀流程（含向量检索） | ❌ |
| Skill Registry + 版本解析器 | ❌ |
| Memory Service 独立化 | ❌ |
| WebSocket 实时推送 DAG 状态（替换 polling） | ❌ |
| Git 集成（主 Agent 统一 commit + push） | ❌ |

### Phase 3 — 生产增强

| 任务 | 状态 |
|------|------|
| L3 元记忆（Agent 画像、模式库） | ❌ |
| AgentProfile 与预热池角色动态映射 | ❌ |
| Web UI / 用户交互接口 | ❌ |
| MCP 协议端点暴露 | ❌ |
| 多租户 / 多项目 | ❌ |

## 适用场景

目前适合：
- **研究多 Agent 协作**：看主 Agent 怎么拆任务、子 Agent 怎么执行恢复
- **本地开发试验**：全部可本地跑，不需要 GPU 集群
- **小规模任务编排**：单个节点的工作流已经能跑

不适合：
- **生产环境部署**：稳定性、安全性都还没到
- **大规模并行任务**：没有预热池和容器化
- **需要 Agent 间协作的场景**：频道协议还没实现

## 快速开始

```bash
cd backend
uv sync
uv run python main.py
```

需要本地跑一个兼容 OpenAI API 的 LLM 服务（如 vLLM、Ollama），以及配置 DeepSeek API key 用于主 Agent 规划。

## 设计文档

详见 `docs/design/`：

| 文档 | 版本 | 内容 |
|------|------|------|
| [系统架构](docs/design/01-系统架构.md) | v0.6 | DAG 节点、指派机制、通信设施、MVP 状态 |
| [记忆系统](docs/design/02-记忆系统设计.md) | v0.4 | 四层记忆、MVP JSONL 实现说明 |
| [运行环境](docs/design/03-Agent运行环境设计.md) | v0.5 | agent_runner、Loop Agent、安全、subprocess |
| [待办清单](docs/design/04-待办清单.md) | v0.6 | 全部决策记录和实施计划 |
