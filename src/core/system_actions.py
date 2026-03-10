# -*- coding: utf-8 -*-
"""
系统动作分发模块。

从 app.py 中抽离，负责处理各种定时/手动触发的系统动作，
如晨报、晚检、待办提醒、companion check 等。
"""
from __future__ import annotations

import time
from datetime import datetime

from channel import router as channel_router
from core import engine as brain
from infra.logging import BEIJING_TZ, get_logger
from memory import read_state_cached, write_state_and_update_cache
from core.proactive import (
    build_time_capsule, build_nudge_context,
    run_nudge_check, run_companion_check, build_weather_context,
)

logger = get_logger(__name__)


def run_system_action(action: str, data: dict, uid: str, ctx) -> dict:
    """为单个用户执行系统动作，返回结果 dict"""
    logger.info("[system_action] 开始执行: action=%s, user=%s", action, uid)

    if action == "todo_remind":
        return _action_todo_remind(uid, ctx)

    if action in ("morning_report", "evening_checkin", "daily_report"):
        return _action_report(action, data, uid, ctx)

    if action == "reflect_push":
        return _action_reflect_push(uid, ctx)

    if action == "mood_generate":
        return _action_mood_generate(data, uid, ctx)

    if action == "weekly_review":
        return _action_weekly_review(data, uid, ctx)

    if action == "nudge_check":
        return _action_nudge_check(uid, ctx)

    if action == "monthly_review":
        return _action_monthly_review(data, uid, ctx)

    if action == "companion_check":
        return _action_companion_check(uid, ctx)

    if action == "finance_monthly_report":
        return _action_finance_report(uid, ctx)

    logger.warning("[system_action] 未知 action: %s", action)
    return {"ok": False, "error": f"unknown action: {action}"}


# ---- 具体动作实现 ----

def _action_todo_remind(uid: str, ctx) -> dict:
    from skills.todo_manage import check_todos
    state = read_state_cached(ctx) or {}
    result = check_todos(state, ctx=ctx, todo_file=ctx.todo_file)
    messages = result.get("messages", [])
    state_updates = result.get("state_updates", {})
    if messages:
        combined = "📋 待办提醒\n\n" + "\n".join(messages)
        channel_router.send_message(uid, combined)
    if state_updates:
        for k, v in state_updates.items():
            state[k] = v
        write_state_and_update_cache(state, ctx)
    return {"ok": True, "sent": len(messages)}


def _action_report(action: str, data: dict, uid: str, ctx) -> dict:
    context = {}
    try:
        todo_content = ctx.IO.read_text(ctx.todo_file)
        if todo_content:
            context["todo"] = todo_content[:2000]
        quick_notes = ctx.IO.read_text(ctx.quick_notes_file)
        if quick_notes:
            context["quick_notes"] = quick_notes[:1000]
    except Exception as e:
        logger.warning("[system_action] [%s] 读取上下文失败: %s", uid, e)

    if action == "morning_report":
        _enrich_morning_context(uid, ctx, context)

    if action in ("morning_report", "evening_checkin"):
        try:
            context["nudge"] = build_nudge_context(ctx)
        except Exception as e:
            logger.warning("[system_action] [%s] nudge 上下文读取失败: %s", uid, e)

    if action == "evening_checkin":
        _enrich_evening_context(uid, ctx, context)

    payload = {
        "type": "system",
        "action": action,
        "user_id": uid,
        "context": context,
    }
    result = brain.process(payload, ctx=ctx)
    reply = result.get("reply") if result else None
    if reply:
        channel_router.send_message(uid, reply)
    return {"ok": True, "has_reply": bool(reply)}


def _enrich_morning_context(uid: str, ctx, context: dict) -> None:
    try:
        context["time_capsule"] = build_time_capsule(ctx)
    except Exception as e:
        logger.warning("[system_action] [%s] 时间胶囊读取失败: %s", uid, e)
    try:
        weather = build_weather_context()
        if weather:
            context["weather"] = weather
    except Exception as e:
        logger.error("[system_action] [%s] 天气获取失败: %s", uid, e)
    try:
        from skills.decision_track import get_due_decisions
        _state = read_state_cached(ctx) or {}
        due_decisions = get_due_decisions(_state)
        if due_decisions:
            context["due_decisions"] = due_decisions
    except Exception as e:
        logger.warning("[system_action] [%s] 到期决策读取失败: %s", uid, e)
    try:
        from skills.habit_coach import check_experiment_expiry, get_experiment_summary_for_review
        _state = read_state_cached(ctx) or {}
        expiry_msg = check_experiment_expiry(_state)
        if expiry_msg:
            context["experiment_expired"] = expiry_msg
        exp_summary = get_experiment_summary_for_review(_state)
        if exp_summary:
            context["active_experiment"] = exp_summary
    except Exception as e:
        logger.warning("[system_action] [%s] 实验上下文读取失败: %s", uid, e)


def _enrich_evening_context(uid: str, ctx, context: dict) -> None:
    try:
        _state = read_state_cached(ctx) or {}
        daily_top3 = _state.get("daily_top3", {})
        today_str = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
        if daily_top3 and daily_top3.get("date") == today_str:
            context["daily_top3"] = daily_top3
    except Exception as e:
        logger.warning("[system_action] [%s] daily_top3 读取失败: %s", uid, e)


def _action_reflect_push(uid: str, ctx) -> dict:
    from skills.reflect import push as reflect_push
    state = read_state_cached(ctx) or {}
    result = reflect_push({}, state, ctx)
    su = result.get("state_updates", {})
    if su:
        state.update(su)
        write_state_and_update_cache(state, ctx)
    reply = result.get("reply") if result else None
    if reply:
        channel_router.send_message(uid, reply)
    return {"ok": True, "has_reply": bool(reply)}


def _action_mood_generate(data: dict, uid: str, ctx) -> dict:
    from skills.mood_diary import execute as mood_execute
    state = read_state_cached(ctx) or {}
    today_str = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
    scores = state.get("mood_scores", [])
    if any(s.get("date") == today_str for s in scores):
        return {"ok": True, "skipped": True}
    result = mood_execute(data, state, ctx)
    write_state_and_update_cache(state, ctx)
    reply = result.get("reply") if result else None
    if reply:
        channel_router.send_message(uid, reply)
    return {"ok": True, "has_reply": bool(reply)}


def _action_weekly_review(data: dict, uid: str, ctx) -> dict:
    from skills.weekly_review import execute as weekly_execute
    state = read_state_cached(ctx) or {}
    result = weekly_execute(data, state, ctx)
    write_state_and_update_cache(state, ctx)
    reply = result.get("reply") if result else None
    if reply:
        channel_router.send_message(uid, reply)
    return {"ok": True, "has_reply": bool(reply)}


def _action_nudge_check(uid: str, ctx) -> dict:
    messages = run_nudge_check(ctx)
    for msg in messages:
        channel_router.send_message(uid, msg)
    return {"ok": True, "sent": len(messages)}


def _action_monthly_review(data: dict, uid: str, ctx) -> dict:
    from skills.monthly_review import execute as monthly_execute
    state = read_state_cached(ctx) or {}
    result = monthly_execute(data, state, ctx)
    write_state_and_update_cache(state, ctx)
    reply = result.get("reply") if result else None
    if reply:
        channel_router.send_message(uid, reply)
    return {"ok": True, "has_reply": bool(reply)}


def _action_companion_check(uid: str, ctx) -> dict:
    message = run_companion_check(ctx)
    if message:
        channel_router.send_message(uid, message)
    return {"ok": True, "sent": 1 if message else 0}


def _action_finance_report(uid: str, ctx) -> dict:
    user_cfg = ctx.get_user_config() if hasattr(ctx, "get_user_config") else {}
    if user_cfg.get("role") != "admin":
        return {"ok": True, "skipped": True, "reason": "not_admin"}
    from skills.finance_report import execute as finance_execute
    state = read_state_cached(ctx) or {}
    result = finance_execute({}, state, ctx)
    write_state_and_update_cache(state, ctx)
    reply = result.get("reply") if result else None
    if reply:
        channel_router.send_message(uid, reply)
    return {"ok": True, "has_reply": bool(reply)}
