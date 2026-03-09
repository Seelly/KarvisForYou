# -*- coding: utf-8 -*-
"""公告管理服务 — 从 user_context.py 拆分"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime

from log_utils import BEIJING_TZ, get_logger

logger = get_logger(__name__)

from user_context import SYSTEM_DIR

ANNOUNCEMENTS_FILE = os.path.join(SYSTEM_DIR, "announcements.json")
_announce_lock = threading.Lock()


def _now_str() -> str:
    return datetime.now(BEIJING_TZ).isoformat(timespec="seconds")


def _read_announcements() -> list:
    try:
        if os.path.exists(ANNOUNCEMENTS_FILE):
            with open(ANNOUNCEMENTS_FILE, "r", encoding="utf-8") as f:
                return json.load(f).get("announcements", [])
    except Exception as e:
        logger.error("[Announce] 读取失败: %s", e)
    return []


def _write_announcements(announcements: list):
    try:
        os.makedirs(os.path.dirname(ANNOUNCEMENTS_FILE), exist_ok=True)
        with open(ANNOUNCEMENTS_FILE, "w", encoding="utf-8") as f:
            json.dump({"announcements": announcements}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("[Announce] 写入失败: %s", e)


def create_announcement(title: str, content: str) -> dict:
    ann = {
        "id": str(uuid.uuid4())[:8],
        "title": title,
        "content": content,
        "created_at": _now_str(),
    }
    with _announce_lock:
        anns = _read_announcements()
        anns.insert(0, ann)
        _write_announcements(anns)
    return ann


def get_announcements() -> list:
    return _read_announcements()


def delete_announcement(ann_id: str) -> bool:
    with _announce_lock:
        anns = _read_announcements()
        new_anns = [a for a in anns if a["id"] != ann_id]
        if len(new_anns) < len(anns):
            _write_announcements(new_anns)
            return True
    return False
