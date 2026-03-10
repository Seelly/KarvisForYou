# -*- coding: utf-8 -*-
"""
用户节奏管理 — 作息学习、nudge 状态更新、打卡超时检测。
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

from config import CHECKIN_TIMEOUT_SECONDS, SCHEDULER_RHYTHM_WINDOW
from infra.logging import get_logger

logger = get_logger(__name__)


def update_nudge_state(state: dict) -> None:
    """F5: 每次收到用户消息时更新 nudge_state（连续记录天数 + 精确时间）"""
    beijing_tz = timezone(timedelta(hours=8))
    now = datetime.now(beijing_tz)
    today_str = now.strftime("%Y-%m-%d")

    nudge = state.setdefault("nudge_state", {
        "streak": 0,
        "last_message_date": "",
        "last_message_time": "",
        "last_companion_time": "",
        "companion_count_today": 0,
        "yesterday_mood_score": None,
        "people_last_mentioned": {}
    })

    nudge["last_message_time"] = now.strftime("%Y-%m-%d %H:%M")

    last_date = nudge.get("last_message_date", "")
    if last_date != today_str:
        nudge["companion_count_today"] = 0
        nudge["mood_followed_today"] = False

        if last_date:
            try:
                last_dt = datetime.strptime(last_date, "%Y-%m-%d").date()
                today_dt = datetime.strptime(today_str, "%Y-%m-%d").date()
                if (today_dt - last_dt).days == 1:
                    nudge["streak"] = nudge.get("streak", 0) + 1
                elif (today_dt - last_dt).days > 1:
                    nudge["streak"] = 1
            except Exception as e:
                logger.warning("解析 nudge streak 日期失败: %s", e)
                nudge["streak"] = 1
        else:
            nudge["streak"] = 1
        nudge["last_message_date"] = today_str


def check_checkin_timeout(state: dict) -> None:
    """检查打卡是否超时"""
    if not state.get("checkin_pending"):
        return

    sent_at = state.get("checkin_sent_at", "")
    if not sent_at:
        return

    try:
        beijing_tz = timezone(timedelta(hours=8))
        now = datetime.now(beijing_tz)
        sent_time = datetime.strptime(sent_at, "%Y-%m-%d %H:%M")
        sent_time = sent_time.replace(tzinfo=beijing_tz)
        diff = (now - sent_time).total_seconds()
        if diff > CHECKIN_TIMEOUT_SECONDS:
            logger.info("[Brain] checkin timeout (%.0fs)", diff)
            from skills import checkin_flow
            checkin_flow.finish(state, timeout=True)
    except Exception as e:
        logger.error("[Brain] checkin timeout check exception: %s", e)


def update_user_rhythm(state: dict) -> None:
    """V8: 从用户行为中学习作息节奏（每次消息后调用）

    收集数据：
    - 每小时活跃计数（hour_counts）
    - 每日首条消息时间 → 滑动平均 avg_wake_time
    - 每日末条消息时间 → 次日回看更新 avg_sleep_time
    """
    beijing_tz = timezone(timedelta(hours=8))
    now = datetime.now(beijing_tz)
    hour = now.hour
    today_str = now.strftime("%Y-%m-%d")

    sched = state.setdefault("scheduler", {})
    rhythm = sched.setdefault("user_rhythm", {})

    # 1. 更新活跃时段统计
    hour_counts = rhythm.setdefault("hour_counts", {})
    hour_str = str(hour)
    hour_counts[hour_str] = hour_counts.get(hour_str, 0) + 1

    # 2. 推算起床时间
    if rhythm.get("_last_wake_date") != today_str:
        last_active = rhythm.get("_last_active_time")
        last_active_date = rhythm.get("_last_active_date")
        if last_active and last_active_date and last_active_date != today_str:
            try:
                la_parts = last_active.split(":")
                la_min = int(la_parts[0]) * 60 + int(la_parts[1])
                if la_min >= 1200 or la_min < 240:
                    _update_avg_time(rhythm, "avg_sleep_time", last_active,
                                     window=SCHEDULER_RHYTHM_WINDOW)
            except (ValueError, IndexError):
                pass

        rhythm["_last_wake_date"] = today_str

        if 5 <= hour <= 11:
            rhythm["_today_wake"] = now.strftime("%H:%M")
            _update_avg_time(rhythm, "avg_wake_time", now.strftime("%H:%M"),
                             window=SCHEDULER_RHYTHM_WINDOW)
        else:
            logger.debug("[Brain][V8] first message today at %s:xx, skip wake_time update", hour)

        if now.weekday() >= 5 and 5 <= hour <= 13:
            _update_weekend_shift(rhythm, now.strftime("%H:%M"))

    # 3. 记录最后活跃时间
    rhythm["_last_active_time"] = now.strftime("%H:%M")
    rhythm["_last_active_date"] = today_str

    logger.debug("[Brain][V8] rhythm update: hour=%s, wake=%s, sleep=%s",
                 hour, rhythm.get("avg_wake_time", "N/A"), rhythm.get("avg_sleep_time", "N/A"))


def _update_avg_time(rhythm: dict, key: str, new_time_str: str, window: int = 7) -> None:
    """滑动平均更新时间（加权：新数据权重更高）"""
    samples_key = f"_{key}_samples"
    samples = rhythm.setdefault(samples_key, [])
    samples.append(new_time_str)
    if len(samples) > window:
        samples[:] = samples[-window:]

    minutes_list = []
    for t in samples:
        try:
            parts = t.split(":")
            m = int(parts[0]) * 60 + int(parts[1])
            if "wake" in key and m >= 720:
                continue
            minutes_list.append(m)
        except (ValueError, IndexError):
            continue

    if not minutes_list:
        return

    if "sleep" in key:
        adjusted = []
        for m in minutes_list:
            if m < 360:
                adjusted.append(m + 1440)
            else:
                adjusted.append(m)
        minutes_list = adjusted

    total_weight = 0
    weighted_sum = 0
    for i, m in enumerate(minutes_list):
        w = 1 + i * 0.5
        weighted_sum += m * w
        total_weight += w

    avg_minutes = int(weighted_sum / total_weight)
    if avg_minutes >= 1440:
        avg_minutes -= 1440

    avg_h = avg_minutes // 60
    avg_m = avg_minutes % 60
    rhythm[key] = f"{avg_h:02d}:{avg_m:02d}"


def _update_weekend_shift(rhythm: dict, wake_time_str: str) -> None:
    """更新周末晚起偏移量"""
    avg_wake = rhythm.get("avg_wake_time")
    if not avg_wake:
        return

    try:
        wake_parts = wake_time_str.split(":")
        wake_min = int(wake_parts[0]) * 60 + int(wake_parts[1])
        avg_parts = avg_wake.split(":")
        avg_min = int(avg_parts[0]) * 60 + int(avg_parts[1])
        shift = wake_min - avg_min
        if shift > 0:
            old_shift = rhythm.get("weekend_shift", 60)
            rhythm["weekend_shift"] = int(old_shift * 0.7 + shift * 0.3)
    except (ValueError, IndexError):
        pass
