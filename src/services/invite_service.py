# -*- coding: utf-8 -*-
"""邀请码管理服务 — 从 user_context.py 拆分"""
from __future__ import annotations

import json
import os
import random
import string
import threading
from datetime import datetime

from infra.logging import BEIJING_TZ, get_logger

logger = get_logger(__name__)

from infra.paths import SYSTEM_DIR

INVITE_CODES_FILE = os.path.join(SYSTEM_DIR, "invite_codes.json")
_invite_lock = threading.Lock()


def _now_str() -> str:
    return datetime.now(BEIJING_TZ).isoformat(timespec="seconds")


def _read_invite_codes() -> list:
    """读取邀请码列表"""
    try:
        if os.path.exists(INVITE_CODES_FILE):
            with open(INVITE_CODES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("codes", [])
    except Exception as e:
        logger.error("[InviteCode] 读取失败: %s", e)
    return []


def _write_invite_codes(codes: list):
    """写入邀请码列表"""
    try:
        os.makedirs(os.path.dirname(INVITE_CODES_FILE), exist_ok=True)
        with open(INVITE_CODES_FILE, "w", encoding="utf-8") as f:
            json.dump({"codes": codes}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("[InviteCode] 写入失败: %s", e)


def create_invite_code(created_by: str = "admin") -> str:
    """生成一个 8 位邀请码"""
    code = "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
    with _invite_lock:
        codes = _read_invite_codes()
        codes.append({
            "code": code,
            "created_at": _now_str(),
            "created_by": created_by,
            "used": False,
            "used_by": "",
            "used_at": "",
        })
        _write_invite_codes(codes)
    logger.info("[InviteCode] 生成邀请码: %s", code)
    return code


def get_all_invite_codes() -> list:
    """获取所有邀请码"""
    return _read_invite_codes()


def use_invite_code(code: str, user_id: str) -> bool:
    """使用邀请码，成功返回 True"""
    with _invite_lock:
        codes = _read_invite_codes()
        for c in codes:
            if c["code"] == code and not c["used"]:
                c["used"] = True
                c["used_by"] = user_id
                c["used_at"] = _now_str()
                _write_invite_codes(codes)
                logger.info("[InviteCode] 邀请码 %s 被 %s 使用", code, user_id)
                return True
    return False


def delete_invite_code(code: str) -> bool:
    """删除邀请码"""
    with _invite_lock:
        codes = _read_invite_codes()
        new_codes = [c for c in codes if c["code"] != code]
        if len(new_codes) < len(codes):
            _write_invite_codes(new_codes)
            logger.info("[InviteCode] 删除邀请码: %s", code)
            return True
    return False
