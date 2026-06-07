"""日志配置 — 统一 logger，支持结构化键值对

用法:
    from services.logger import get_logger
    log = get_logger(__name__)
    log.info("dag created", dag_id="xxx", node_count=5)
    log.warning("timeout", node_id="yyy", elapsed="30s")
    log.error("something wrong", exc_info=True)
    log.debug("raw response", body=text)
"""

import logging
import sys
from typing import Any


class StructLogger:
    """Thin wrapper around logging.Logger that supports key=val structured logging."""

    def __init__(self, logger: logging.Logger):
        self._logger = logger

    @staticmethod
    def _fmt(msg: str, **kwargs: Any) -> str:
        if kwargs:
            kv = "  " + "  ".join(f"{k}={v!r}" for k, v in kwargs.items())
            return msg + kv
        return msg

    def info(self, msg: str, **kwargs: Any) -> None:
        self._logger.info(self._fmt(msg, **kwargs))

    def warning(self, msg: str, **kwargs: Any) -> None:
        self._logger.warning(self._fmt(msg, **kwargs))

    def error(self, msg: str, **kwargs: Any) -> None:
        self._logger.error(self._fmt(msg, **kwargs))

    def debug(self, msg: str, **kwargs: Any) -> None:
        self._logger.debug(self._fmt(msg, **kwargs))

    @property
    def isEnabledFor(self):
        return self._logger.isEnabledFor


def init_logger(level: str = "DEBUG") -> None:
    logger = logging.getLogger("gobanion")
    logger.setLevel(getattr(logging, level.upper(), logging.DEBUG))
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(
        "[%(asctime)s] [%(levelname)-5s] [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    ))
    logger.handlers.clear()
    logger.addHandler(handler)


def get_logger(name: str) -> StructLogger:
    if not logging.getLogger("gobanion").hasHandlers():
        init_logger()
    raw = logging.getLogger(f"gobanion.{name}")
    return StructLogger(raw)
