# -*- coding: utf-8 -*-
"""用户反馈管理服务 — 从 user_context.py 拆分"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime

from infra.logging import BEIJING_TZ, get_logger

logger = get_logger(__name__)

from infra.paths import SYSTEM_DIR

FEEDBACKS_FILE = os.path.join(SYSTEM_DIR, "feedbacks.json")
_feedback_lock = threading.Lock()


def _now_str() -> str:
    return datetime.now(BEIJING_TZ).isoformat(timespec="seconds")


def _read_feedbacks() -> list:
    try:
        if os.path.exists(FEEDBACKS_FILE):
            with open(FEEDBACKS_FILE, "r", encoding="utf-8") as f:
                return json.load(f).get("feedbacks", [])
    except Exception as e:
        logger.error("[Feedback] 读取失败: %s", e)
    return []


def _write_feedbacks(feedbacks: list):
    try:
        os.makedirs(os.path.dirname(FEEDBACKS_FILE), exist_ok=True)
        with open(FEEDBACKS_FILE, "w", encoding="utf-8") as f:
            json.dump({"feedbacks": feedbacks}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("[Feedback] 写入失败: %s", e)


def create_feedback(user_id: str, content: str) -> dict:
    fb = {
        "id": str(uuid.uuid4())[:8],
        "user_id": user_id,
        "content": content,
        "created_at": _now_str(),
        "reply": "",
        "replied_at": "",
    }
    with _feedback_lock:
        fbs = _read_feedbacks()
        fbs.insert(0, fb)
        _write_feedbacks(fbs)
    return fb


def get_feedbacks() -> list:
    return _read_feedbacks()


def reply_feedback(fb_id: str, reply: str) -> bool:
    with _feedback_lock:
        fbs = _read_feedbacks()
        for fb in fbs:
            if fb["id"] == fb_id:
                fb["reply"] = reply
                fb["replied_at"] = _now_str()
                _write_feedbacks(fbs)
                return True
    return False
