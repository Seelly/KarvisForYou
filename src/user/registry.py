# -*- coding: utf-8 -*-
"""
用户注册表管理模块。

职责：
- 用户注册表的 CRUD（get_or_create_user、increment_message_count 等）
- 新用户目录和默认文件的初始化
"""
import os
import json
import threading
from datetime import datetime

from infra.logging import BEIJING_TZ, get_logger
from infra.paths import USER_REGISTRY_FILE
from config import FEISHU_ADMIN_OPEN_ID
from user.context import UserContext

logger = get_logger(__name__)

# 不活跃天数阈值
INACTIVE_DAYS_THRESHOLD = int(os.environ.get("INACTIVE_DAYS_THRESHOLD", "7"))
# 每日消息上限
DAILY_MESSAGE_LIMIT = int(os.environ.get("DAILY_MESSAGE_LIMIT", "50"))

_registry_lock = threading.Lock()


def _now_str() -> str:
    return datetime.now(BEIJING_TZ).isoformat(timespec="seconds")


def _today_str() -> str:
    return datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")


def _read_registry() -> dict:
    """读取用户注册表"""
    try:
        if os.path.exists(USER_REGISTRY_FILE):
            with open(USER_REGISTRY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.error("[UserContext] 读取注册表失败: %s", e)
    return {"users": {}}


def _write_registry(registry: dict):
    """写入用户注册表"""
    try:
        os.makedirs(os.path.dirname(USER_REGISTRY_FILE), exist_ok=True)
        with open(USER_REGISTRY_FILE, "w", encoding="utf-8") as f:
            json.dump(registry, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("[UserContext] 写入注册表失败: %s", e)


def get_or_create_user(user_id: str) -> tuple:
    """
    获取或创建用户。
    返回 (UserContext, is_new_user: bool)
    """
    with _registry_lock:
        registry = _read_registry()
        is_new = user_id not in registry.get("users", {})

        ctx = UserContext(user_id)

        if is_new:
            # 创建目录结构
            logger.info("[UserContext] 新用户 %s: 创建目录结构...", user_id)
            for d in ctx.all_dirs():
                os.makedirs(d, exist_ok=True)
            logger.info("[UserContext] 新用户 %s: 创建 %s 个目录完成", user_id, len(ctx.all_dirs()))

            # 创建默认文件
            _init_default_files(ctx)
            logger.info("[UserContext] 新用户 %s: 默认文件初始化完成", user_id)

            # 写入注册表
            if "users" not in registry:
                registry["users"] = {}
            registry["users"][user_id] = {
                "created_at": _now_str(),
                "last_active": _now_str(),
                "nickname": "",
                "status": "active",
                "message_count_today": 0,
                "message_count_date": _today_str(),
                "total_messages": 0,
            }
            _write_registry(registry)
            logger.info("[UserContext] 新用户注册完成: %s, base_dir=%s", user_id, ctx.base_dir)
        else:
            # 更新活跃时间
            user_data = registry["users"][user_id]
            user_data["last_active"] = _now_str()

            # 重置每日计数（如果跨天了）
            if user_data.get("message_count_date") != _today_str():
                user_data["message_count_today"] = 0
                user_data["message_count_date"] = _today_str()

            _write_registry(registry)

        return ctx, is_new


def _init_default_files(ctx: UserContext):
    """为新用户创建默认文件（兼容 Local 和 OneDrive 模式）"""
    # Quick-Notes
    existing = ctx.IO.read_text(ctx.quick_notes_file)
    if not existing:
        ctx.IO.write_text(ctx.quick_notes_file, "# Quick Notes\n\n快速笔记，从微信同步。\n\n---\n\n")

    # Todo
    existing = ctx.IO.read_text(ctx.todo_file)
    if not existing:
        ctx.IO.write_text(ctx.todo_file, "# Todo\n\n")

    # State
    existing = ctx.IO.read_text(ctx.state_file)
    if not existing:
        ctx.IO.write_text(ctx.state_file, "{}")

    # Memory
    existing = ctx.IO.read_text(ctx.memory_file)
    if not existing:
        ctx.IO.write_text(ctx.memory_file, "# Memory\n\n")

    # User Config
    if not os.path.exists(ctx.user_config_file):
        if ctx.user_id.startswith("tg_"):
            channel = "telegram"
        elif ctx.user_id.startswith("fs_"):
            channel = "feishu"
        else:
            channel = "wework"
        config_data = {
            "nickname": "",
            "ai_name": "Karvis",
            "soul_override": "",
            "channel": channel,
            "role": "user",
            "storage_mode": "local",
            "onedrive": {},
            "skills": {
                "mode": "blacklist",
                "list": [],
            },
            "info": {},
            "onboarding_step": 1,
            "preferences": {
                "morning_report": True,
                "evening_checkin": True,
                "companion_enabled": True,
            },
        }
        if channel == "telegram":
            config_data["telegram_chat_id"] = ctx.user_id[3:]
        elif channel == "feishu":
            open_id = ctx.user_id[3:]
            config_data["feishu_open_id"] = open_id
            if FEISHU_ADMIN_OPEN_ID and open_id == FEISHU_ADMIN_OPEN_ID:
                config_data["role"] = "admin"
                logger.info("[UserContext] 飞书用户 %s 匹配管理员 open_id，自动提升为 admin", ctx.user_id)
        ctx.save_user_config(config_data)


def increment_message_count(user_id: str) -> tuple:
    """
    增加用户今日消息计数。
    返回 (current_count, is_over_limit)
    """
    with _registry_lock:
        registry = _read_registry()
        user_data = registry.get("users", {}).get(user_id)
        if not user_data:
            logger.warning("[increment_message_count] 用户 %s 不在注册表中，跳过计数", user_id)
            return 0, False

        # 跨天重置
        if user_data.get("message_count_date") != _today_str():
            logger.info("[increment_message_count] 用户 %s 跨天重置计数 "
                        "(旧日期=%s, 新日期=%s)", user_id,
                        user_data.get('message_count_date'), _today_str())
            user_data["message_count_today"] = 0
            user_data["message_count_date"] = _today_str()

        user_data["message_count_today"] = user_data.get("message_count_today", 0) + 1
        user_data["total_messages"] = user_data.get("total_messages", 0) + 1
        _write_registry(registry)

        count = user_data["message_count_today"]
        over = count > DAILY_MESSAGE_LIMIT
        return count, over


def get_all_active_users() -> list:
    """获取所有活跃用户 ID（定时任务用）"""
    registry = _read_registry()
    active = []
    now = datetime.now(BEIJING_TZ)

    for uid, data in registry.get("users", {}).items():
        if data.get("status") != "active":
            logger.info("[get_all_active_users] 跳过非活跃用户: %s (status=%s)", uid, data.get('status'))
            continue
        last_active_str = data.get("last_active", "")
        try:
            last_active = datetime.fromisoformat(last_active_str)
            days_inactive = (now - last_active).days
            if days_inactive <= INACTIVE_DAYS_THRESHOLD:
                active.append(uid)
            else:
                logger.info("[get_all_active_users] 跳过不活跃用户: %s (不活跃 %s 天)", uid, days_inactive)
        except (ValueError, TypeError):
            active.append(uid)

    return active


def get_all_users() -> dict:
    """获取所有用户数据（管理员用）"""
    return _read_registry().get("users", {})


def update_user_status(user_id: str, status: str):
    """更新用户状态（active/suspended）"""
    with _registry_lock:
        registry = _read_registry()
        if user_id in registry.get("users", {}):
            registry["users"][user_id]["status"] = status
            _write_registry(registry)


def update_user_nickname(user_id: str, nickname: str):
    """更新注册表中的昵称"""
    with _registry_lock:
        registry = _read_registry()
        if user_id in registry.get("users", {}):
            registry["users"][user_id]["nickname"] = nickname
            _write_registry(registry)


def is_user_suspended(user_id: str) -> bool:
    """检查用户是否被挂起"""
    registry = _read_registry()
    user_data = registry.get("users", {}).get(user_id, {})
    return user_data.get("status") == "suspended"