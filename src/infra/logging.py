# -*- coding: utf-8 -*-
"""
KarvisForYou 统一日志工具

集中定义：
- BEIJING_TZ 常量
- Request ID 管理（线程本地存储）
- BeijingFormatter — 自动注入北京时间 + request ID
- get_logger(name) — 工厂函数
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone

# ============ 北京时区常量 ============
BEIJING_TZ = timezone(timedelta(hours=8))


# ============ Request ID 线程本地存储 ============
_request_local = threading.local()


def get_request_id() -> str | None:
    """获取当前线程的 Request ID"""
    return getattr(_request_local, "request_id", None)


def set_request_id(rid: str | None = None) -> str:
    """设置当前线程的 Request ID，不传则自动生成短 ID"""
    _request_local.request_id = rid or uuid.uuid4().hex[:8]
    return _request_local.request_id


# ============ 自定义 Formatter ============

class BeijingFormatter(logging.Formatter):
    """日志格式化器：北京时间 + request ID + 模块名"""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.now(BEIJING_TZ).strftime("%H:%M:%S")
        rid = get_request_id()
        module = record.name
        msg = record.getMessage()

        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)

        parts = [ts]
        if rid:
            parts.append(f"[{rid}]")
        parts.append(f"[{module}]")
        parts.append(msg)

        result = " ".join(parts)
        if record.exc_text:
            result = result + "\n" + record.exc_text
        return result


# ============ Logger 工厂 ============

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

_handler: logging.StreamHandler | None = None
_handler_lock = threading.Lock()


def _get_handler() -> logging.StreamHandler:
    """获取共享的 stderr handler（单例）"""
    global _handler
    if _handler is not None:
        return _handler
    with _handler_lock:
        if _handler is not None:
            return _handler
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(BeijingFormatter())
        _handler = h
        return _handler


def get_logger(name: str) -> logging.Logger:
    """获取配置好的 logger。

    Args:
        name: 模块名，通常传 __name__

    Returns:
        配置了 BeijingFormatter 的 Logger 实例
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.addHandler(_get_handler())
        logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
        logger.propagate = False
    return logger
