# -*- coding: utf-8 -*-
"""
监控告警模块 — 慢请求检测、异常告警、月度预算检查。
"""
from __future__ import annotations

import json
import os
import time as _time
from datetime import datetime, timezone, timedelta

from channel import router as channel_router
from config import (
    ADMIN_USER_ID, ALERT_SLOW_THRESHOLD,
    ALERT_SLOW_CONSECUTIVE, ALERT_COOLDOWN_SECONDS,
)
from infra.logging import get_logger
from infra.paths import USAGE_LOG_FILE

logger = get_logger(__name__)

# 月度预算上限（元），可通过环境变量覆写
_MONTHLY_BUDGET = float(os.environ.get("MONTHLY_BUDGET", "50"))

# 告警状态
_alert_state = {
    "slow_count": 0,
    "last_alert_time": {},
    "_call_count": 0,
}


def send_admin_alert(alert_type: str, message: str) -> None:
    """
    向管理员推送告警消息。
    支持冷却机制：同类告警 ALERT_COOLDOWN_SECONDS 内不重复发送。
    """
    if not ADMIN_USER_ID:
        logger.warning("[Alert] no ADMIN_USER_ID, skipping alert: %s", alert_type)
        return

    now = _time.time()
    last = _alert_state["last_alert_time"].get(alert_type, 0)
    if now - last < ALERT_COOLDOWN_SECONDS:
        logger.info("[Alert] cooling down, skip: %s (%.0fs since last)", alert_type, now - last)
        return

    try:
        results = channel_router.send_alert(f"🚨 Karvis 告警\n\n{message}")
        if any(ok for _, ok in results):
            _alert_state["last_alert_time"][alert_type] = now
            logger.info("[Alert] sent: %s", alert_type)
        else:
            logger.warning("[Alert] send failed: %s", alert_type)
    except Exception as e:
        logger.error("[Alert] send exception: %s", e)


def check_and_alert(elapsed: float, user_id: str, skill: str,
                    user_text: str, error=None) -> None:
    """
    请求完成后检查是否需要告警。
    支持：慢请求（连续N次 > 阈值）、Traceback/异常、月度预算超限。
    """
    # 1. 慢请求检测
    if elapsed > ALERT_SLOW_THRESHOLD:
        _alert_state["slow_count"] += 1
        if _alert_state["slow_count"] >= ALERT_SLOW_CONSECUTIVE:
            send_admin_alert("slow_request",
                f"⏱ 连续 {_alert_state['slow_count']} 次慢请求 (>{ALERT_SLOW_THRESHOLD}s)\n"
                f"最新: {elapsed:.1f}s\n"
                f"用户: {user_id}\n"
                f"技能: {skill}\n"
                f"输入: {(user_text or '')[:50]}")
    else:
        _alert_state["slow_count"] = 0

    # 2. 异常告警
    if error:
        send_admin_alert("error",
            f"❌ 处理异常\n"
            f"用户: {user_id}\n"
            f"错误: {str(error)[:200]}\n"
            f"输入: {(user_text or '')[:50]}")

    # 3. 月度预算检查（每 50 次调用检查一次，避免频繁 IO）
    _alert_state["_call_count"] = _alert_state.get("_call_count", 0) + 1
    if _alert_state["_call_count"] % 50 == 0:
        check_monthly_budget()


def check_monthly_budget() -> None:
    """检查当月 API 成本是否超过预算的 80%，超过则推告警"""
    try:
        if not os.path.exists(USAGE_LOG_FILE):
            return

        now = datetime.now(timezone(timedelta(hours=8)))
        month_str = now.strftime("%Y-%m")
        month_cost = 0.0

        with open(USAGE_LOG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                    if e.get("ts", "")[:7] != month_str:
                        continue
                    m = e.get("model", "").lower()
                    pt = e.get("prompt_tokens", 0)
                    ct = e.get("completion_tokens", 0)
                    if "deepseek" in m:
                        month_cost += pt / 1e6 * 2 + ct / 1e6 * 8
                    elif "vl" in m:
                        month_cost += pt / 1e6 * 3 + ct / 1e6 * 9
                except (json.JSONDecodeError, KeyError):
                    continue

        pct = month_cost / _MONTHLY_BUDGET * 100 if _MONTHLY_BUDGET > 0 else 0
        if pct >= 80:
            send_admin_alert("budget_warning",
                f"💰 月度预算预警\n\n"
                f"当月已用: ¥{month_cost:.2f} / ¥{_MONTHLY_BUDGET:.0f}\n"
                f"使用率: {pct:.0f}%\n"
                f"月份: {month_str}")
    except Exception as e:
        logger.error("[Alert] budget check exception: %s", e)
