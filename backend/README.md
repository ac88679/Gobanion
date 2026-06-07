# Gobanion Backend

多 Agent 协作系统后端服务。

## 初始化

```bash
uv init --python 3.12.1
uv add fastapi httpx uvicorn
```

## 启动

```bash
uv run uvicorn main:app --host 0.0.0.0 --port 5000 --reload
```

或使用入口模块：

```bash
cd D:\work\Gobanion\backend
.venv\Scripts\python main.py
```

启动后访问 `http://localhost:8080/health` 确认服务正常。
