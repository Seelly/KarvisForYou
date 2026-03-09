# -*- coding: utf-8 -*-
"""
Karvis 主入口
职责：Flask 初始化、路由注册、渠道初始化、调度器启动。
所有业务逻辑已迁移到独立模块：
  - gateway.py   — 消息网关
  - channel/     — IM 渠道实现
  - scheduler.py — V8 智能调度引擎
  - proactive.py — 主动推送
  - media.py     — 语音识别 / URL 抓取
"""
# 加载 .env 文件（Lite 模式 / 本地开发）
import os
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_env_file = os.path.join(_project_root, ".env")
if os.path.exists(_env_file):
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_file)
    except ImportError:
        pass

from flask import Flask, request
import json
import time
import logging
import threading

import brain
import channel_router
import gateway
from config import (
    ACTIVE_CHANNELS, SERVER_PORT,
    TELEGRAM_BOT_TOKEN, FEISHU_APP_ID,
)
from user_context import (
    get_or_create_user, get_all_active_users,
    SYSTEM_DIR,
)
from log_utils import BEIJING_TZ, get_logger, set_request_id
from proactive import (
    build_time_capsule, build_nudge_context,
    run_nudge_check, run_companion_check, build_weather_context,
)

logger = get_logger(__name__)

app = Flask(__name__)
_start_time = time.time()


# ============ 过滤 Web 页面/API 读请求的 HTTP 访问日志 ============

class _QuietWebFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        if '"GET / ' in msg or '"GET /health' in msg:
            return False
        if '"GET /web/' in msg or '"GET /api/' in msg or 'favicon' in msg:
            return False
        if '"POST /api/auth/verify' in msg:
            return False
        if any(x in msg for x in ['SSH-2.0', 'security.txt', 'robots.txt',
                                    '.well-known', 'MGLNDD', 'boaform',
                                    'SCRIPT_FILENAME', 'mstshash']):
            return False
        if 'code 400' in msg or 'code 505' in msg:
            return False
        return True

logging.getLogger('werkzeug').addFilter(_QuietWebFilter())
logging.getLogger('werkzeug').setLevel(logging.ERROR)

# 注册 Web 路由 Blueprint
from web_routes import web_bp, api_bp
app.register_blueprint(web_bp, url_prefix="/web")
app.register_blueprint(api_bp, url_prefix="/api")


# ============ Flask 路由 ============

@app.route('/process', methods=['POST'])
def process_endpoint():
    """内部异步处理端点：接收消息并调用 brain 处理"""
    try:
        set_request_id()
        data = request.get_json(force=True)
        msg = data.get("msg", {})
        user_id = data.get("user_id", "")
        logger.info("[/process] 开始处理 type=%s, user=%s", msg.get('msg_type'), user_id)
        gateway.handle_message(msg, user_id)
        logger.info("[/process] 处理完成")
        return "ok"
    except Exception as e:
        logger.exception("[/process] 异常: %s", e)
        return "error"


@app.route('/system', methods=['POST'])
def system_endpoint():
    """系统端点：定时器/手动触发的 system action（支持多用户遍历）"""
    try:
        set_request_id()
        data = request.get_json(force=True)
        action = data.get("action", "")
        target_user = data.get("user_id", "")
        logger.info("[/system] action=%s, user=%s", action, target_user or 'all')

        if action == "refresh_cache":
            from memory import invalidate_all_caches
            from services.token_service import cleanup_expired_tokens
            invalidate_all_caches()
            removed = cleanup_expired_tokens()
            if removed > 0:
                logger.info("[/system] refresh_cache: 清理过期令牌 %s 个", removed)
            return json.dumps({"ok": True, "action": "refresh_cache", "tokens_cleaned": removed})

        # V8: 智能调度引擎
        if action in ("daily_init", "scheduler_tick"):
            from scheduler import daily_init, scheduler_tick
            user_ids = [target_user] if target_user else get_all_active_users()
            results = []
            for uid in user_ids:
                try:
                    ctx, _ = get_or_create_user(uid)
                    if action == "daily_init":
                        r = daily_init(uid, ctx)
                    else:
                        r = scheduler_tick(uid, ctx)
                    results.append({"user_id": uid, **r})
                except Exception as e:
                    logger.error("[/system] V8 %s 用户 %s 失败: %s", action, uid, e)
                    results.append({"user_id": uid, "ok": False, "error": str(e)})
            return json.dumps({"ok": True, "action": action, "results": results}, ensure_ascii=False)

        # 如果指定了 user_id，只处理该用户；否则遍历所有活跃用户
        if target_user:
            user_ids = [target_user]
        else:
            user_ids = get_all_active_users()
            logger.info("[/system] 遍历 %s 个活跃用户", len(user_ids))

        total_results = []

        for uid in user_ids:
            try:
                ctx, _ = get_or_create_user(uid)
                result = _run_system_action_for_user(action, data, uid, ctx)
                total_results.append({"user_id": uid, **result})
            except Exception as e:
                logger.error("[/system] 用户 %s 执行 %s 失败: %s", uid, action, e)
                total_results.append({"user_id": uid, "ok": False, "error": str(e)})

            # 多用户遍历时随机延迟，避免 API 限流
            if len(user_ids) > 1:
                import random
                time.sleep(random.uniform(1, 3))

        logger.info("[/system] %s 完成, 共处理 %s 个用户", action, len(total_results))
        return json.dumps({"ok": True, "action": action, "results": total_results}, ensure_ascii=False)

    except Exception as e:
        logger.exception("[/system] 异常: %s", e)
        return json.dumps({"ok": False, "error": str(e)})


def _run_system_action_for_user(action, data, uid, ctx):
    """为单个用户执行系统动作，返回结果 dict"""
    from memory import read_state_cached, write_state_and_update_cache
    from datetime import datetime
    logger.info("[system_action] 开始执行: action=%s, user=%s", action, uid)
    t0 = time.time()

    if action == "todo_remind":
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

    if action in ("morning_report", "evening_checkin", "daily_report"):
        context = {}
        try:
            todo_content = ctx.IO.read_text(ctx.todo_file)
            if todo_content:
                context["todo"] = todo_content[:2000]
            quick_notes = ctx.IO.read_text(ctx.quick_notes_file)
            if quick_notes:
                context["quick_notes"] = quick_notes[:1000]
        except Exception as e:
            logger.warning("[/system] [%s] 读取上下文失败: %s", uid, e)

        if action == "morning_report":
            try:
                context["time_capsule"] = build_time_capsule(ctx)
            except Exception as e:
                logger.warning("[/system] [%s] 时间胶囊读取失败: %s", uid, e)
            try:
                weather = build_weather_context()
                if weather:
                    context["weather"] = weather
            except Exception as e:
                logger.error("[/system] [%s] 天气获取失败: %s", uid, e)
            try:
                from skills.decision_track import get_due_decisions
                _state = read_state_cached(ctx) or {}
                due_decisions = get_due_decisions(_state)
                if due_decisions:
                    context["due_decisions"] = due_decisions
            except Exception as e:
                logger.warning("[/system] [%s] 到期决策读取失败: %s", uid, e)
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
                logger.warning("[/system] [%s] 实验上下文读取失败: %s", uid, e)

        if action in ("morning_report", "evening_checkin"):
            try:
                context["nudge"] = build_nudge_context(ctx)
            except Exception as e:
                logger.warning("[/system] [%s] nudge 上下文读取失败: %s", uid, e)

        if action == "evening_checkin":
            try:
                _state = read_state_cached(ctx) or {}
                daily_top3 = _state.get("daily_top3", {})
                today_str = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
                if daily_top3 and daily_top3.get("date") == today_str:
                    context["daily_top3"] = daily_top3
            except Exception as e:
                logger.warning("[/system] [%s] daily_top3 读取失败: %s", uid, e)

        payload = {
            "type": "system",
            "action": action,
            "user_id": uid,
            "context": context
        }
        result = brain.process(payload, ctx=ctx)
        reply = result.get("reply") if result else None
        if reply:
            channel_router.send_message(uid, reply)
        return {"ok": True, "has_reply": bool(reply)}

    if action == "reflect_push":
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

    if action == "mood_generate":
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

    if action == "weekly_review":
        from skills.weekly_review import execute as weekly_execute
        state = read_state_cached(ctx) or {}
        result = weekly_execute(data, state, ctx)
        write_state_and_update_cache(state, ctx)
        reply = result.get("reply") if result else None
        if reply:
            channel_router.send_message(uid, reply)
        return {"ok": True, "has_reply": bool(reply)}

    if action == "nudge_check":
        messages = run_nudge_check(ctx)
        for msg in messages:
            channel_router.send_message(uid, msg)
        return {"ok": True, "sent": len(messages)}

    if action == "monthly_review":
        from skills.monthly_review import execute as monthly_execute
        state = read_state_cached(ctx) or {}
        result = monthly_execute(data, state, ctx)
        write_state_and_update_cache(state, ctx)
        reply = result.get("reply") if result else None
        if reply:
            channel_router.send_message(uid, reply)
        return {"ok": True, "has_reply": bool(reply)}

    if action == "companion_check":
        message = run_companion_check(ctx)
        if message:
            channel_router.send_message(uid, message)
        return {"ok": True, "sent": 1 if message else 0}

    if action == "finance_monthly_report":
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

    logger.warning("[system_action] 未知 action: %s", action)
    return {"ok": False, "error": f"unknown action: {action}"}


@app.route('/', methods=['GET'])
def health():
    """健康检查（基础）"""
    return "Karvis is alive"


@app.route('/health', methods=['GET'])
def health_detail():
    """深度健康检查"""
    import shutil
    from datetime import datetime
    checks = {}
    overall = True

    from config import DEEPSEEK_API_KEY, QWEN_API_KEY
    checks["deepseek_key"] = bool(DEEPSEEK_API_KEY)
    checks["qwen_key"] = bool(QWEN_API_KEY)
    if not DEEPSEEK_API_KEY:
        overall = False

    # 渠道状态
    checks["active_channels"] = list(channel_router.get_registered_channels().keys())

    try:
        usage = shutil.disk_usage("/root")
        free_gb = usage.free / (1024 ** 3)
        checks["disk_free_gb"] = round(free_gb, 1)
        if free_gb < 1:
            overall = False
            checks["disk_warning"] = "磁盘空间不足 1GB"
    except Exception as e:
        logger.warning("获取磁盘使用量失败: %s", e)
        checks["disk_free_gb"] = -1

    try:
        active_count = len(get_all_active_users())
        checks["active_users"] = active_count
    except Exception as e:
        logger.warning("获取活跃用户数失败: %s", e)
        checks["active_users"] = -1

    try:
        from config import LOG_FILE_KARVISFORALL
        if os.path.exists(LOG_FILE_KARVISFORALL):
            log_size_mb = os.path.getsize(LOG_FILE_KARVISFORALL) / (1024 * 1024)
            checks["log_size_mb"] = round(log_size_mb, 1)
    except Exception as e:
        logger.debug("读取日志文件大小失败: %s", e)
        pass

    checks["uptime_s"] = int(time.time() - _start_time)

    status_code = 200 if overall else 503
    return json.dumps({
        "status": "healthy" if overall else "degraded",
        "checks": checks,
        "timestamp": datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S"),
    }, ensure_ascii=False), status_code, {"Content-Type": "application/json"}


# ============ 启动初始化 ============

def _init_system_dirs():
    """确保系统级目录存在"""
    os.makedirs(SYSTEM_DIR, exist_ok=True)
    logger.info("[Init] 系统目录已就绪: %s", SYSTEM_DIR)


if __name__ == '__main__':
    _init_system_dirs()

    # ============ 渠道注册 ============
    logger.info("[Init] 活跃渠道配置: %s", ACTIVE_CHANNELS)

    if not ACTIVE_CHANNELS:
        logger.warning("[WARNING] 没有启用任何 IM 渠道，请设置 ACTIVE_CHANNELS 环境变量")

    # 企微渠道
    if "wework" in ACTIVE_CHANNELS:
        from channel.wework import WeWorkChannel
        wework_ch = WeWorkChannel()
        channel_router.register_channel(wework_ch)
        wework_ch.start(app, gateway.handle_message)

    # Telegram 渠道
    if "telegram" in ACTIVE_CHANNELS and TELEGRAM_BOT_TOKEN:
        from channel.telegram import TelegramChannel
        tg_ch = TelegramChannel()
        channel_router.register_channel(tg_ch)
        tg_ch.start(app, gateway.handle_message)

    # 飞书渠道（长连接模式）
    if "feishu" in ACTIVE_CHANNELS and FEISHU_APP_ID:
        from channel.feishu import FeishuChannel
        fs_ch = FeishuChannel()
        channel_router.register_channel(fs_ch)
        fs_ch.start(app, gateway.handle_message)

    # 启动调度器
    from scheduler import setup_scheduler
    setup_scheduler()

    app.run(host='0.0.0.0', port=SERVER_PORT, threaded=True)
