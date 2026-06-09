"""Gobanion 后端入口"""

import asyncio

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from pathlib import Path

from config import get_settings
from services import DagService
from services.dispatcher import Dispatcher
from services.logger import init_logger, get_logger
from api.dag_router import router as dag_router, init as init_dag_router
from api.plan_router import router as plan_router, init as init_plan_router

init_logger()
log = get_logger("main")

settings = get_settings()
PROJECT_ROOT = Path(__file__).resolve().parent.parent  # D:\work\Gobanion\


def _suppress_windows_pipe_error(loop, context):
    """Suppress harmless ConnectionResetError from asyncio pipe cleanup on Windows."""
    exc = context.get("exception")
    if isinstance(exc, ConnectionResetError):
        msg = context.get("message", "")
        if "_call_connection_lost" in msg or "_ProactorBasePipeTransport" in msg:
            return  # Known Windows asyncio bug, skip silently
    loop.default_exception_handler(context)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting Gobanion backend", env=settings.APP_ENV, debug=settings.DEBUG)
    # Suppress Windows asyncio pipe cleanup noise
    asyncio.get_event_loop().set_exception_handler(_suppress_windows_pipe_error)
    # Startup: init services
    dag_service = DagService()
    dispatcher = Dispatcher(dag_service)
    dispatcher.start()
    init_dag_router(dag_service)
    init_plan_router(dag_service)
    app.state.dag_service = dag_service
    app.state.dispatcher = dispatcher
    log.info("Services initialized", dag_service=True, dispatcher=True)
    yield
    # Shutdown
    log.info("Shutting down...")
    await dispatcher.stop()
    log.info("Shutdown complete")


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="多 Agent 协作系统后端服务",
    debug=settings.DEBUG,
    lifespan=lifespan,
)

# ── CORS ──
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ──
app.include_router(dag_router)
app.include_router(plan_router)

# ── Static files (frontend) ──
frontend_dir = PROJECT_ROOT / "frontend"
if frontend_dir.exists():
    app.mount("/app", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")


# ── Info endpoints ──


@app.get("/")
def root():
    return {
        "service": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "env": settings.APP_ENV,
        "status": "running",
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/config")
def config():
    """Show non-sensitive config values (safe for debug)."""
    s = settings
    return {
        "env": s.APP_ENV,
        "debug": s.DEBUG,
        "host": s.HOST,
        "port": s.PORT,
        "llm_model": s.llm.MODEL,
        "llm_api_base": s.llm.API_BASE,
        "llm_max_tokens": s.llm.MAX_TOKENS,
        "llm_api_key_set": bool(s.llm.API_KEY),
        "database_url": str(s.database.URL),
    }


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=False,
        reload_excludes=["_workspace/**", "_logs/**", ".venv/**"],
    )
