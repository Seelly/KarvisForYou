# -*- coding: utf-8 -*-
"""
主动推送模块。

职责：
1. F2 主动陪伴系统（companion_check）
2. F5 轻推系统（nudge_check）
3. F3 时间胶囊（time_capsule）
4. F13 天气信息（weather_context）
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import requests

import brain
import channel_router
from config import (
    COMPANION_SILENT_HOURS, COMPANION_INTERVAL_HOURS,
    COMPANION_MAX_DAILY, COMPANION_RECENT_HOURS,
    WEATHER_API_KEY, WEATHER_CITY,
)
from log_utils import BEIJING_TZ, get_logger
from memory import read_state_cached, write_state_and_update_cache
import prompts

logger = get_logger(__name__)


# ============ F3: 时间胶囊 ============

def build_time_capsule(ctx) -> dict:
    """
    F3: 读取历史同日的笔记，供 morning_report 注入。
    返回 dict: {"7d_ago": {...}, "30d_ago": {...}, "365d_ago": {...}}
    """
    today = datetime.now(BEIJING_TZ).date()
    offsets = {
        "7d_ago": 7,
        "30d_ago": 30,
        "365d_ago": 365,
    }

    capsule = {}
    files_to_read = {}

    for key, days in offsets.items():
        past_date = today - timedelta(days=days)
        date_str = past_date.strftime("%Y-%m-%d")
        files_to_read[f"{key}_daily"] = (date_str, f"{ctx.daily_notes_dir}/{date_str}.md")

    files_to_read["quick_notes"] = (None, ctx.quick_notes_file)

    # 并发读取
    results = {}
    try:
        executor = brain._executor
    except AttributeError:
        executor = ThreadPoolExecutor(max_workers=4)

    futures = {k: executor.submit(ctx.IO.read_text, v[1]) for k, v in files_to_read.items()}

    for k, fut in futures.items():
        try:
            results[k] = fut.result(timeout=15) or ""
        except Exception as e:
            logger.warning("读取时间胶囊文件失败 (%s): %s", k, e)
            results[k] = ""

    qn_text = results.get("quick_notes", "")

    for key, days in offsets.items():
        past_date = today - timedelta(days=days)
        date_str = past_date.strftime("%Y-%m-%d")

        daily_content = results.get(f"{key}_daily", "")
        qn_entries = _extract_date_entries_for_capsule(qn_text, date_str)

        content_parts = []
        if qn_entries:
            content_parts.append(qn_entries[:500])
        if daily_content:
            if "## 📊 今日总结" in daily_content:
                summary_section = daily_content.split("## 📊 今日总结")[1]
                end_idx = summary_section.find("\n## ")
                if end_idx >= 0:
                    summary_section = summary_section[:end_idx]
                content_parts.append(summary_section.strip()[:500])

        if content_parts:
            capsule[key] = {
                "date": date_str,
                "notes": "\n\n".join(content_parts)[:800]
            }
        else:
            capsule[key] = None

    return capsule


def _extract_date_entries_for_capsule(text: str, date_str: str) -> str:
    """从 Quick-Notes 中提取指定日期的条目（时间胶囊用）"""
    if not text:
        return ""
    entries = []
    sections = text.split("\n## ")
    for section in sections[1:]:
        first_line = section.split("\n")[0].strip()
        if first_line.startswith(date_str):
            body = "\n".join(section.split("\n")[1:]).strip()
            if body and body != "---":
                entries.append(body)
    return "\n".join(entries[:5])


# ============ F5: 轻推系统 ============

def build_nudge_context(ctx) -> dict:
    """
    F5: 构建 nudge 上下文信号，注入 morning_report / evening_checkin 的 context。
    """
    state = read_state_cached(ctx) or {}

    nudge = state.get("nudge_state", {})
    mood_scores = state.get("mood_scores", [])

    today = datetime.now(BEIJING_TZ).date()
    yesterday_str = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday_mood = None
    for s in mood_scores:
        if s.get("date") == yesterday_str:
            yesterday_mood = {"score": s.get("score"), "label": s.get("label", "")}
            break

    streak = nudge.get("streak", 0)

    last_msg_date = nudge.get("last_message_date", "")
    hours_since_last = None
    if last_msg_date:
        try:
            last_dt = datetime.strptime(last_msg_date, "%Y-%m-%d")
            last_dt = last_dt.replace(tzinfo=BEIJING_TZ)
            now = datetime.now(BEIJING_TZ)
            hours_since_last = round((now - last_dt).total_seconds() / 3600, 1)
        except Exception as e:
            logger.debug("解析 last_message_date 失败: %s", e)
            pass

    people_to_follow = []
    people_last = nudge.get("people_last_mentioned", {})
    for name, last_date_str in people_last.items():
        try:
            last_d = datetime.strptime(last_date_str, "%Y-%m-%d").date()
            if (today - last_d).days >= 7:
                people_to_follow.append(name)
        except Exception as e:
            logger.debug("解析 people_last_mentioned 日期失败: %s", e)
            pass

    checkin_stats = state.get("checkin_stats", {})

    return {
        "yesterday_mood": yesterday_mood,
        "streak": streak,
        "last_message_hours_ago": hours_since_last,
        "people_to_follow_up": people_to_follow,
        "checkin_streak": checkin_stats.get("streak", 0),
    }


def run_nudge_check(ctx) -> list:
    """
    F5: 独立轻推检测（每天 14:00 执行）— 纯规则引擎，不走 LLM。
    返回要推送的消息列表。
    """
    state = read_state_cached(ctx) or {}

    nudge = state.get("nudge_state", {})
    mood_scores = state.get("mood_scores", [])
    messages = []

    today = datetime.now(BEIJING_TZ).date()
    today_str = today.strftime("%Y-%m-%d")
    yesterday_str = (today - timedelta(days=1)).strftime("%Y-%m-%d")

    # 场景1: 沉默检测
    last_msg_date = nudge.get("last_message_date", "")
    if last_msg_date != today_str:
        messages.append("今天很安静呀，是忙还是累了？随时可以来聊两句~")

    # 场景2: 情绪跟进
    for s in mood_scores:
        if s.get("date") == yesterday_str and s.get("score") is not None:
            if s["score"] <= 4:
                label = s.get("label", "")
                hint = f"（{label}）" if label else ""
                messages.append(f"昨天好像有点低落{hint}，今天好点了吗？")
            break

    # 场景3: 连续记录鼓励
    streak = nudge.get("streak", 0)
    if streak > 0 and streak % 7 == 0:
        messages.append(f"你已经连续记录 {streak} 天了！这个习惯太棒了 ✨")
    elif streak == 3:
        messages.append("连续记录 3 天了~坚持下去，会看到很棒的变化！")

    return messages


# ============ F2: 主动陪伴系统 ============

def _parse_companion_datetime(time_str: str | None) -> datetime | None:
    """解析 nudge_state 中的时间字符串"""
    if not time_str:
        return None
    try:
        dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M")
        return dt.replace(tzinfo=BEIJING_TZ)
    except Exception as e:
        logger.warning("解析 companion 时间失败: %s", e)
        return None


def run_companion_check(ctx) -> str | None:
    """
    F2: 每 2 小时执行一次的智能陪伴检查。
    核心原则: 有事才发，没事 return None 静默跳过。
    """
    state = read_state_cached(ctx) or {}
    nudge = state.get("nudge_state", {})
    now = datetime.now(BEIJING_TZ)

    # ── 防骚扰层 ──
    if now.hour < 8:
        logger.info("[Companion] 安静时间(%s:00), 跳过", now.hour)
        return None

    last_msg_time = _parse_companion_datetime(nudge.get("last_message_time"))
    if last_msg_time and (now - last_msg_time).total_seconds() < COMPANION_RECENT_HOURS * 3600:
        logger.info("[Companion] 近期有互动(%s), 跳过", nudge.get("last_message_time"))
        return None

    last_companion = _parse_companion_datetime(nudge.get("last_companion_time"))
    if last_companion and (now - last_companion).total_seconds() < COMPANION_INTERVAL_HOURS * 3600:
        logger.info("[Companion] 推送间隔不足(%s), 跳过", nudge.get("last_companion_time"))
        return None

    companion_count = nudge.get("companion_count_today", 0)
    if companion_count >= COMPANION_MAX_DAILY:
        logger.info("[Companion] 今日已推送%s次, 达到上限, 跳过", companion_count)
        return None

    # ── 信号收集 ──
    signals = []

    if last_msg_time:
        silent_hours = (now - last_msg_time).total_seconds() / 3600
        if silent_hours > COMPANION_SILENT_HOURS:
            signals.append({
                "type": "silence",
                "detail": f"已经 {silent_hours:.0f} 小时没消息"
            })

    pending_todos = _check_pending_todos(ctx)
    if pending_todos:
        signals.append({
            "type": "todo_reminder",
            "detail": f"有 {len(pending_todos)} 个待办未完成",
            "items": pending_todos[:3]
        })

    yesterday_mood = nudge.get("yesterday_mood_score")
    mood_followed = nudge.get("mood_followed_today", False)
    if yesterday_mood and int(yesterday_mood) <= 4 and not mood_followed:
        signals.append({
            "type": "mood_followup",
            "detail": f"昨天情绪评分 {yesterday_mood}/10"
        })

    if not signals:
        logger.info("[Companion] 无触发信号, 静默跳过")
        return None

    logger.info("[Companion] 触发信号: %s", json.dumps(signals, ensure_ascii=False)[:200])

    context = _build_companion_context(state, ctx)
    message = _generate_companion_message(signals, context, state)

    if message:
        nudge["last_companion_time"] = now.strftime("%Y-%m-%d %H:%M")
        nudge["companion_count_today"] = companion_count + 1
        if any(s["type"] == "mood_followup" for s in signals):
            nudge["mood_followed_today"] = True
        state["nudge_state"] = nudge
        write_state_and_update_cache(state, ctx)
        logger.info("[Companion] 消息已生成, 计数=%s", companion_count + 1)

    return message


def _build_companion_context(state: dict, ctx) -> dict:
    """为陪伴消息收集丰富上下文"""
    context = {}

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            "memory": executor.submit(ctx.IO.read_text, ctx.memory_file),
            "quick_notes": executor.submit(ctx.IO.read_text, ctx.quick_notes_file),
            "todo": executor.submit(ctx.IO.read_text, ctx.todo_file),
        }
        for key, future in futures.items():
            try:
                content = future.result(timeout=5)
                if content:
                    if key == "quick_notes":
                        lines = content.strip().split("\n")
                        recent = lines[-20:] if len(lines) > 20 else lines
                        context[key] = "\n".join(recent)
                    else:
                        context[key] = content
            except Exception as e:
                logger.error("[Companion] 读取 %s 失败: %s", key, e)

    recent_msgs = state.get("recent_messages", [])
    if recent_msgs:
        context["recent_messages"] = recent_msgs[-5:]

    return context


def _generate_companion_message(signals: list, context: dict, state: dict) -> str | None:
    """F2: 基于信号 + 上下文，调 Qwen Flash 生成自然的关怀消息。"""
    system_parts = []
    system_parts.append(f"## 你的人设\n{prompts.SOUL}")

    memory = context.get("memory", "")
    if memory:
        system_parts.append(f"## 你对用户的了解\n{memory}")

    system_parts.append(prompts.COMPANION_TASK)
    system_prompt = "\n\n".join(system_parts)

    user_parts = []
    signal_text = json.dumps(signals, ensure_ascii=False)
    user_parts.append(f"**触发信号**: {signal_text}")

    quick_notes = context.get("quick_notes", "")
    if quick_notes:
        user_parts.append(f"**近期速记**:\n{quick_notes}")

    todo = context.get("todo", "")
    if todo:
        user_parts.append(f"**待办清单**:\n{todo}")

    recent_msgs = context.get("recent_messages", [])
    if recent_msgs:
        msg_text = "\n".join([f"- {m.get('role','')}: {m.get('text','')[:80]}"
                              for m in recent_msgs])
        user_parts.append(f"**最近对话**:\n{msg_text}")

    now = datetime.now(BEIJING_TZ)
    period = "上午" if now.hour < 12 else ("下午" if now.hour < 18 else "晚上")
    user_parts.append(f"**当前时间**: {now.strftime('%Y-%m-%d %H:%M')} {period}")

    user_message = "\n\n".join(user_parts)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message}
    ]

    logger.info("[Companion] 调用 Flash 生成关怀消息, signals=%s", len(signals))
    return brain.call_llm(messages, model_tier="flash", max_tokens=200, temperature=0.7)


def _check_pending_todos(ctx) -> list:
    """F2: 从 Todo.md 读取未完成待办"""
    try:
        todo_content = ctx.IO.read_text(ctx.todo_file)
        if not todo_content:
            return []
        pending = []
        for line in todo_content.split("\n"):
            line = line.strip()
            if line.startswith("- [ ]"):
                pending.append(line[5:].strip())
        return pending
    except Exception as e:
        logger.error("[Companion] 读取待办失败: %s", e)
        return []


# ============ F13: 天气信息 ============

def build_weather_context() -> dict:
    """V3-F13: 获取天气信息，供 morning_report 注入。"""
    if not WEATHER_API_KEY:
        return {}
    try:
        resp = requests.get(
            "https://api.seniverse.com/v3/weather/daily.json",
            params={
                "key": WEATHER_API_KEY,
                "location": WEATHER_CITY,
                "language": "zh-Hans",
                "unit": "c",
                "start": 0,
                "days": 1
            },
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()["results"][0]["daily"][0]
            weather = {
                "city": WEATHER_CITY,
                "weather_day": data.get("text_day", ""),
                "weather_night": data.get("text_night", ""),
                "high": data.get("high", ""),
                "low": data.get("low", ""),
            }
            logger.info("[Weather] %s: %s %s~%s°C",
                        WEATHER_CITY, weather["weather_day"], weather["low"], weather["high"])
            return weather
        else:
            logger.warning("[Weather] API 返回非 200: %s %s", resp.status_code, resp.text[:100])
    except Exception as e:
        logger.error("[Weather] 获取天气失败: %s", e)
    return {}
