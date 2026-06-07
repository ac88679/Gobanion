# Gobanion 前端

Vue 3 + TypeScript + Vite。

## 环境要求

- Node.js >= 18
- npm >= 9

## 初始化

```bash
cd frontend
npm install
```

## 开发模式

```bash
npm run dev
```

启动后访问 `http://localhost:5173`。Vite 已配置 `/api` 和 `/health` 代理到后端 `http://127.0.0.1:5000`，开发期间需先启动 FastAPI 后端。

## 构建

```bash
npm run build
```

产物输出到 `dist/`，可直接部署（配合 nginx 反向代理或后端静态文件服务）。

## 项目结构

```
src/
├── main.ts              # 入口
├── style.css            # iOS 风格主题变量
├── App.vue              # 主布局
├── types/index.ts       # TypeScript 类型定义
├── api/index.ts         # API 封装（axios）
└── components/
    ├── NavBar.vue       # 顶部导航栏
    ├── Sidebar.vue      # 侧栏任务列表
    ├── StatusBadge.vue  # 状态标签
    ├── DAGCanvas.vue    # DAG 力导向图（D3.js）
    ├── NodeSheet.vue    # 节点详情弹出面板
    └── CreateModal.vue  # 新建任务弹窗
```
