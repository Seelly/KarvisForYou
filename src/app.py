# -*- coding: utf-8 -*-
"""
Karvis 主入口
职责：Flask 初始化、路由注册、渠道初始化、调度器启动。
所有业务逻辑已迁移到独立模块：
  - web/gateway.py   — 消息网关
  - channel/         — IM 渠道实现
  - core/scheduler.py — V8 智能调度引擎
  - core/proactive.py — 主动推送
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

from channel import router as channel_router
from web import gateway
from config import (
    ACTIVE_CHANNELS, SERVER_PORT,
    TELEGRAM_BOT_TOKEN, FEISHU_APP_ID,
)
from user import get_or_create_user, get_all_active_users
from infra.paths import SYSTEM_DIR
from infra.logging import BEIJING_TZ, get_logger, set_request_id
from core.system_actions import run_system_action

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
from web.routes import web_bp, api_bp
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
            from core.scheduler import daily_init, scheduler_tick
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
                result = run_system_action(action, data, uid, ctx)
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
        channel_router.register(wework_ch)
        wework_ch.start(app, gateway.handle_message)

    # Telegram 渠道
    if "telegram" in ACTIVE_CHANNELS and TELEGRAM_BOT_TOKEN:
        from channel.telegram import TelegramChannel
        tg_ch = TelegramChannel()
        channel_router.register(tg_ch)
        tg_ch.start(app, gateway.handle_message)

    # 飞书渠道（长连接模式）
    if "feishu" in ACTIVE_CHANNELS and FEISHU_APP_ID:
        from channel.feishu import FeishuChannel
        fs_ch = FeishuChannel()
        channel_router.register(fs_ch)
        fs_ch.start(app, gateway.handle_message)

    # 启动调度器
    from core.scheduler import setup_scheduler
    setup_scheduler()

    app.run(host='0.0.0.0', port=SERVER_PORT, threaded=True)
