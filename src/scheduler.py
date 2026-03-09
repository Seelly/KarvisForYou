# -*- coding: utf-8 -*-
"""
V8 智能调度引擎。

职责：
1. 生成每日意图队列（daily_init）
2. 心跳评估到期意图（scheduler_tick）
3. 内嵌 APScheduler 定时调度器（setup_scheduler）
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime

import requests

from config import (
    SCHEDULER_TICK_MINUTES, SCHEDULER_DEFAULT_WAKE, SCHEDULER_DEFAULT_SLEEP,
    SCHEDULER_WEEKEND_SHIFT, SCHEDULER_PUSH_MAX_DAILY, SCHEDULER_MIN_PUSH_GAP,
    SERVER_PORT,
)
from log_utils import BEIJING_TZ, get_logger

logger = get_logger(__name__)


# ============ 工具函数 ============

def _add_minutes(time_str: str, minutes: int) -> str:
    """给 HH:MM 格式的时间加减分钟数，返回 HH:MM"""
    try:
        parts = time_str.split(":")
        total = int(parts[0]) * 60 + int(parts[1]) + minutes
        total = max(0, min(total, 1439))
        return f"{total // 60:02d}:{total % 60:02d}"
    except (ValueError, IndexError):
        return time_str


# ============ 意图生成 ============

def _generate_daily_intents(state: dict) -> list:
    """V8: 基于用户节奏画像动态生成当天触达意图队列"""
    sched = state.get("scheduler", {})
    rhythm = sched.get("user_rhythm", {})
    now = datetime.now(BEIJING_TZ)
    is_weekend = now.weekday() >= 5

    wake_time = rhythm.get("avg_wake_time", SCHEDULER_DEFAULT_WAKE)
    sleep_time = rhythm.get("avg_sleep_time", SCHEDULER_DEFAULT_SLEEP)

    if is_weekend:
        shift = rhythm.get("weekend_shift", SCHEDULER_WEEKEND_SHIFT)
        wake_time = _add_minutes(wake_time, shift)

    intents = [
        {
            "type": "morning_report",
            "earliest": wake_time,
            "latest": _add_minutes(wake_time, 150),
            "ideal": _add_minutes(wake_time, 30),
            "priority": "normal",
            "status": "pending"
        },
        {
            "type": "todo_remind",
            "earliest": _add_minutes(wake_time, 60),
            "latest": "18:00",
            "ideal": _add_minutes(wake_time, 90),
            "priority": "normal",
            "max_times": 2,
            "sent_count": 0,
            "status": "pending"
        },
        {
            "type": "companion",
            "earliest": _add_minutes(wake_time, 120),
            "latest": _add_minutes(sleep_time, -60),
            "ideal": None,
            "priority": "low",
            "max_times": 2,
            "sent_count": 0,
            "conditions": {"silent_hours": 4},
            "status": "pending"
        },
        {
            "type": "nudge_check",
            "earliest": "13:00",
            "latest": "15:00",
            "ideal": "14:00",
            "priority": "low",
            "status": "pending"
        },
        {
            "type": "reflect_push",
            "earliest": _add_minutes(sleep_time, -210),
            "latest": _add_minutes(sleep_time, -120),
            "ideal": _add_minutes(sleep_time, -180),
            "priority": "normal",
            "status": "pending"
        },
        {
            "type": "evening_checkin",
            "earliest": _add_minutes(sleep_time, -120),
            "latest": _add_minutes(sleep_time, -30),
            "ideal": _add_minutes(sleep_time, -90),
            "priority": "normal",
            "status": "pending"
        },
        {
            "type": "daily_report",
            "earliest": _add_minutes(sleep_time, -90),
            "latest": _add_minutes(sleep_time, -15),
            "ideal": _add_minutes(sleep_time, -60),
            "priority": "normal",
            "status": "pending"
        },
    ]

    logger.info("[V8] 生成每日意图: wake=%s, sleep=%s, weekend=%s, intents=%s",
                wake_time, sleep_time, is_weekend, len(intents))
    return intents


# ============ daily_init / scheduler_tick ============

def daily_init(uid: str, ctx) -> dict:
    """V8: 每日初始化（多用户版）— 生成当天意图队列 + 重置计数器"""
    from memory import write_state_and_update_cache
    # 绕过缓存直接读文件，防止缓存中旧的 _init_date 导致重复初始化
    state = ctx.IO.read_json(ctx.state_file) or {}
    sched = state.setdefault("scheduler", {})
    now = datetime.now(BEIJING_TZ)
    today_str = now.strftime("%Y-%m-%d")

    if sched.get("_init_date") == today_str:
        logger.info("[V8][%s] daily_init 今天已执行，跳过", uid)
        return {"skipped": True, "date": today_str}

    # 额外防重复
    existing_intents = sched.get("intents", [])
    old_init_date = sched.get("_init_date", "")
    if old_init_date == today_str and existing_intents and any(
        i.get("status") not in ("pending", None) for i in existing_intents
    ):
        logger.info("[V8][%s] daily_init 检测到已有执行中/已完成的意图队列，跳过覆盖", uid)
        return {"skipped": True, "reason": "intents_already_active"}

    intents = _generate_daily_intents(state)

    # 过期意图标记 skipped
    now_min = now.hour * 60 + now.minute
    for intent in intents:
        latest = intent.get("latest", "23:59")
        try:
            latest_min = int(latest.split(":")[0]) * 60 + int(latest.split(":")[1])
        except (ValueError, IndexError):
            continue
        if now_min > latest_min:
            intent["status"] = "skipped"
            intent["_skip_reason"] = f"初始化时已过期（now={now.strftime('%H:%M')} > latest={latest}）"
            logger.info("[V8][%s] 意图 %s 已过期，标记 skipped", uid, intent["type"])

    sched["intents"] = intents
    sched["_init_date"] = today_str
    sched["_push_count_today"] = 0
    sched["_last_push_time"] = None

    state["scheduler"] = sched
    write_state_and_update_cache(state, ctx)

    logger.info("[V8][%s] daily_init 完成: %s 个意图已生成", uid, len(intents))
    return {"date": today_str, "intents_count": len(intents)}


def scheduler_tick(uid: str, ctx) -> dict:
    """V8: 每 30 分钟心跳（多用户版）— 检查到期意图并执行"""
    from memory import write_state_and_update_cache
    state = ctx.IO.read_json(ctx.state_file) or {}
    sched = state.setdefault("scheduler", {})
    now = datetime.now(BEIJING_TZ)
    now_str = now.strftime("%H:%M")
    today_str = now.strftime("%Y-%m-%d")

    # 兜底初始化
    if sched.get("_init_date") != today_str:
        logger.info("[V8][%s] tick 检测到未初始化，触发 daily_init", uid)
        daily_init(uid, ctx)
        state = ctx.IO.read_json(ctx.state_file) or {}
        sched = state.get("scheduler", {})

    intents = sched.get("intents", [])
    pending = [i for i in intents if i.get("status") == "pending"]

    if not pending:
        logger.info("[V8][%s] tick: 无 pending 意图", uid)
        return {"evaluated": 0, "executed": 0}

    push_count = sched.get("_push_count_today", 0)
    if push_count >= SCHEDULER_PUSH_MAX_DAILY:
        logger.info("[V8][%s] tick: 今日推送已达上限 %s/%s", uid, push_count, SCHEDULER_PUSH_MAX_DAILY)
        return {"evaluated": len(pending), "executed": 0, "reason": "daily_limit"}

    last_push = sched.get("_last_push_time")
    if last_push:
        try:
            last_parts = last_push.split(":")
            last_min = int(last_parts[0]) * 60 + int(last_parts[1])
            now_min = now.hour * 60 + now.minute
            if now_min - last_min < SCHEDULER_MIN_PUSH_GAP:
                logger.info("[V8][%s] tick: 距上次推送不足 %s 分钟，跳过", uid, SCHEDULER_MIN_PUSH_GAP)
                return {"evaluated": len(pending), "executed": 0, "reason": "min_gap"}
        except (ValueError, IndexError):
            pass

    ready = []
    for intent in pending:
        action = _rule_evaluate(intent, state, now)
        if action == "send":
            ready.append(intent)
        elif action == "skip":
            intent["status"] = "skipped"
            intent["_skip_reason"] = "rule_skip"

    if not ready:
        write_state_and_update_cache(state, ctx)
        logger.info("[V8][%s] tick: 评估 %s 个意图，无需执行", uid, len(pending))
        return {"evaluated": len(pending), "executed": 0}

    if len(ready) > 1:
        ready = _try_merge_intents(ready)

    executed = 0
    for intent in ready:
        if push_count + executed >= SCHEDULER_PUSH_MAX_DAILY:
            break
        try:
            _execute_intent(intent, uid)
            intent["status"] = "sent"
            intent["_sent_at"] = now_str
            executed += 1
        except Exception as e:
            logger.error("[V8][%s] 意图执行失败 %s: %s", uid, intent["type"], e)
            intent["_error"] = str(e)

    sched["_push_count_today"] = push_count + executed
    if executed > 0:
        sched["_last_push_time"] = now_str

    # 重新读取最新 state，避免覆盖子 action 的 state 更新
    if executed > 0:
        fresh_state = ctx.IO.read_json(ctx.state_file) or {}
        fresh_sched = fresh_state.setdefault("scheduler", {})
        fresh_sched["intents"] = sched["intents"]
        fresh_sched["_push_count_today"] = sched["_push_count_today"]
        fresh_sched["_last_push_time"] = sched.get("_last_push_time")
        write_state_and_update_cache(fresh_state, ctx)
    else:
        write_state_and_update_cache(state, ctx)
    logger.info("[V8][%s] tick 完成: 评估 %s, 执行 %s", uid, len(pending), executed)
    return {"evaluated": len(pending), "executed": executed}


# ============ 规则引擎 ============

def _rule_evaluate(intent: dict, state: dict, now: datetime) -> str:
    """V8 Layer 1: 规则引擎 — 返回 "send" | "skip" | "wait" """
    intent_type = intent.get("type", "")
    now_min = now.hour * 60 + now.minute

    earliest = intent.get("earliest", "00:00")
    latest = intent.get("latest", "23:59")
    ideal = intent.get("ideal")

    try:
        earliest_min = int(earliest.split(":")[0]) * 60 + int(earliest.split(":")[1])
        latest_min = int(latest.split(":")[0]) * 60 + int(latest.split(":")[1])
        ideal_min = None
        if ideal:
            ideal_min = int(ideal.split(":")[0]) * 60 + int(ideal.split(":")[1])
    except (ValueError, IndexError):
        return "wait"

    if now_min < earliest_min:
        return "wait"

    if now_min >= latest_min:
        intent["_trigger_reason"] = "兜底触发（已到 latest）"
        return "send"

    sched = state.get("scheduler", {})
    rhythm = sched.get("user_rhythm", {})
    avg_wake = rhythm.get("avg_wake_time", SCHEDULER_DEFAULT_WAKE)
    try:
        wake_min = int(avg_wake.split(":")[0]) * 60 + int(avg_wake.split(":")[1])
        if now_min < wake_min:
            return "wait"
    except (ValueError, IndexError):
        pass

    if intent_type in ("companion", "nudge_check"):
        nudge = state.get("nudge_state", {})
        last_msg = nudge.get("last_message_time", "")
        if last_msg:
            try:
                last_dt = datetime.strptime(last_msg, "%Y-%m-%d %H:%M")
                last_dt = last_dt.replace(tzinfo=BEIJING_TZ)
                if (now - last_dt).total_seconds() < 1800:
                    return "wait"
            except Exception as e:
                logger.debug("解析 last_message_time 失败: %s", e)
                pass

    if intent_type == "companion":
        conditions = intent.get("conditions", {})
        silent_hours = conditions.get("silent_hours", 4)
        nudge = state.get("nudge_state", {})
        last_msg = nudge.get("last_message_time", "")
        if last_msg:
            try:
                last_dt = datetime.strptime(last_msg, "%Y-%m-%d %H:%M")
                last_dt = last_dt.replace(tzinfo=BEIJING_TZ)
                hours_silent = (now - last_dt).total_seconds() / 3600
                if hours_silent < silent_hours:
                    return "wait"
            except Exception as e:
                logger.debug("解析 companion silent_hours 时间失败: %s", e)
                pass

    max_times = intent.get("max_times")
    if max_times and intent.get("sent_count", 0) >= max_times:
        return "skip"

    if ideal_min and now_min >= ideal_min:
        intent["_trigger_reason"] = "到达 ideal 时间"
        return "send"

    if not ideal_min:
        if intent_type == "companion":
            intent["_trigger_reason"] = "沉默条件满足"
            return "send"
        return "wait"

    return "wait"


_MERGEABLE = {
    ("evening_checkin", "daily_report"),
    ("morning_report", "todo_remind"),
    ("reflect_push", "evening_checkin"),
}


def _try_merge_intents(intents: list) -> list:
    """V8: 尝试合并相近的意图"""
    types = set(i["type"] for i in intents)
    consumed = set()
    for pair in _MERGEABLE:
        if pair[0] in types and pair[1] in types:
            consumed.add(pair[1])

    merged = []
    for intent in intents:
        if intent["type"] in consumed:
            intent["status"] = "merged"
            logger.info("[V8] 意图合并: %s 被合并", intent["type"])
        else:
            merged.append(intent)
    return merged


def _execute_intent(intent: dict, user_id: str | None = None) -> None:
    """V8: 分发执行一个到期意图 — 通过 /system 端点"""
    intent_type = intent.get("type", "")
    logger.info("[V8] 执行意图: %s, user=%s, reason=%s",
                intent_type, user_id, intent.get("_trigger_reason", "N/A"))

    action_map = {
        "morning_report": "morning_report",
        "todo_remind": "todo_remind",
        "companion": "companion_check",
        "nudge_check": "nudge_check",
        "reflect_push": "reflect_push",
        "evening_checkin": "evening_checkin",
        "daily_report": "daily_report",
    }

    action = action_map.get(intent_type)
    if not action:
        logger.warning("[V8] 未知意图类型: %s", intent_type)
        return

    try:
        payload = {"action": action}
        if user_id:
            payload["user_id"] = user_id
        requests.post(
            f"http://127.0.0.1:{SERVER_PORT}/system",
            json=payload,
            timeout=120
        )
    except Exception as e:
        logger.error("[V8] 意图执行失败 %s: %s", intent_type, e)
        raise


# ============ APScheduler 内嵌定时调度 ============

def setup_scheduler() -> None:
    """V8: 内嵌定时调度器 — 心跳驱动 + 少量固定任务"""
    if os.environ.get("SCF_RUNTIME") or os.environ.get("TENCENTCLOUD_RUNENV"):
        logger.warning("[Scheduler] 检测到 SCF 环境，跳过内置调度器")
        return

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError:
        logger.warning("[Scheduler] 未安装 apscheduler，跳过内置调度器。如需定时任务请: pip install apscheduler")
        return

    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")

    def _fire_system_action(action):
        """通过 HTTP 调用自身 /system 端点"""
        try:
            url = f"http://127.0.0.1:{SERVER_PORT}/system"
            resp = requests.post(url, json={"action": action}, timeout=600)
            if resp.status_code != 200:
                logger.error("[Scheduler] %s 异常: HTTP %s", action, resp.status_code)
        except Exception as e:
            logger.error("[Scheduler] %s 失败: %s", action, e)

    jobs = [
        # 保留：不依赖用户节奏的固定任务
        ("refresh_cache",   {"trigger": "interval", "minutes": 30}),
        ("mood_generate",   {"trigger": "cron", "hour": 22, "minute": 0}),
        ("weekly_review",   {"trigger": "cron", "day_of_week": "sun", "hour": 21, "minute": 30}),
        ("monthly_review",  {"trigger": "cron", "day": "last", "hour": 22, "minute": 0}),
        ("finance_monthly_report", {"trigger": "cron", "day": 8, "hour": 20, "minute": 0}),

        # V8 新增：智能调度心跳
        ("scheduler_tick",  {"trigger": "interval", "minutes": SCHEDULER_TICK_MINUTES}),

        # V8 新增：每日意图初始化
        ("daily_init",      {"trigger": "cron", "hour": 5, "minute": 0}),
    ]

    for action, kwargs in jobs:
        scheduler.add_job(
            _fire_system_action, args=[action],
            id=action, max_instances=1,
            misfire_grace_time=300,
            **kwargs
        )

    scheduler.start()
    logger.info("[Scheduler][V8] 已启动 %s 个任务 (心跳=%smin, 固定=5, 每日初始化=05:00)",
                len(jobs), SCHEDULER_TICK_MINUTES)

    # 启动时兜底触发一次 daily_init（延迟等待 Flask 就绪）
    def _deferred_daily_init():
        time.sleep(5)  # 等待 Flask 启动完成
        _fire_system_action("daily_init")

    threading.Thread(target=_deferred_daily_init, daemon=True).start()
