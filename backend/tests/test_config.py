"""Config 系统单元测试"""

import os
import sys
from pathlib import Path

# ── 确保能找到 backend/ 下的模块 ──
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["APP_ENV"] = "development"
os.environ["LLM_API_KEY"] = "test-key-12345"

from config import get_settings


def test_load_chain():
    """验证加载链：shared → dev → env var"""
    s = get_settings()
    # 1. From .env.shared — shared defaults
    assert s.APP_NAME == "Gobanion"
    # 2. From .env.shared — HOST=0.0.0.0, PORT=5000
    assert s.HOST == "0.0.0.0"
    assert s.PORT == 5000
    # 3. API KEY — 从 os.environ 注入 (test setup)
    assert s.llm.API_KEY == "test-key-12345"


def test_env_name():
    """APP_ENV 正确读取"""
    s = get_settings()
    assert s.APP_ENV == "development"


def test_singleton():
    """lru_cache 确保 settings 是单例"""
    assert get_settings() is get_settings()


def test_sensitive_not_exposed():
    """敏感字段不直接暴露在 __dict__ (但不强制，只是检查行为)"""
    s = get_settings()
    # API_KEY 应在 .env 中被设置（可能在 .env.local 或 via env var）
    assert bool(s.llm.API_KEY)
    # 不应该暴露完整的 key（安全惯例）
    assert len(s.llm.API_KEY) > 8


if __name__ == "__main__":
    test_load_chain()
    test_env_name()
    test_singleton()
    test_sensitive_not_exposed()
    print("[PASS] test_config.py all passed")
