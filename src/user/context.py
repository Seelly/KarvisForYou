# -*- coding: utf-8 -*-
"""
KarvisForAll V12 用户上下文管理
每个用户请求携带 UserContext，封装该用户的所有路径、IO 后端和配置。

V12 改造要点：
  1. 根据 user_config.storage_mode 路由 IO 后端（Local / OneDrive）
  2. OneDrive 用户使用远程路径体系，Local 用户使用本地路径体系
  3. 增加 Skill 过滤方法 (is_skill_allowed / get_allowed_skills)
  4. 增加 is_admin 属性
"""
import os
import json
import fnmatch
import threading
from datetime import datetime, timedelta

from infra.logging import BEIJING_TZ, get_logger
from storage.local import LocalFileIO
from storage import create_storage
from config import FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_DRIVE_ROOT_FOLDER_TOKEN, FEISHU_ADMIN_OPEN_ID
from infra.paths import DATA_DIR, SYSTEM_DIR, USER_REGISTRY_FILE, TOKENS_FILE, USAGE_LOG_FILE  # noqa: F401

logger = get_logger(__name__)

# 不活跃天数阈值
INACTIVE_DAYS_THRESHOLD = int(os.environ.get("INACTIVE_DAYS_THRESHOLD", "7"))
# 每日消息上限
DAILY_MESSAGE_LIMIT = int(os.environ.get("DAILY_MESSAGE_LIMIT", "50"))


class UserContext:
    """每个用户请求携带的上下文，封装该用户的所有路径、IO 后端和配置。

    V12：根据 user_config.storage_mode 自动选择 LocalFileIO 或 OneDriveIO，
    并设置对应的路径体系。上层代码统一通过 ctx.IO.read_text(ctx.xxx_file) 访问。
    """

    def __init__(self, user_id: str):
        self.user_id = user_id

        # ---- 本地基础目录（所有用户都有，用于存放 user_config 等系统文件） ----
        self.base_dir = os.path.join(DATA_DIR, "users", user_id)
        _karvis_local = os.path.join(self.base_dir, "_Karvis")
        self.user_config_file = os.path.join(_karvis_local, "user_config.json")
        self.decision_log_file = os.path.join(_karvis_local, "logs", "decisions.jsonl")

        # ---- 加载用户配置 ----
        self.config = self._load_config()
        storage_mode = self.config.get("storage_mode", "local")

        # ---- 根据 storage_mode 初始化 IO 后端和路径体系 ----
        if storage_mode == "onedrive":
            self._init_onedrive_mode()
        elif storage_mode == "feishu":
            self._init_feishu_mode()
        else:
            self._init_local_mode()

        # ---- Skill 过滤配置 ----
        self._skills_config = self.config.get("skills", {})

    def _init_local_mode(self):
        """本地存储模式：IO = LocalFileIO 实例，路径为本地文件系统路径"""
        self.IO = LocalFileIO()
        self.storage_mode = "local"

        # 00-Inbox
        self.inbox_path = os.path.join(self.base_dir, "00-Inbox")
        self.quick_notes_file = os.path.join(self.inbox_path, "Quick-Notes.md")
        self.state_file = os.path.join(self.inbox_path, ".ai-life-state.json")
        self.todo_file = os.path.join(self.inbox_path, "Todo.md")
        self.attachments_path = os.path.join(self.inbox_path, "attachments")
        self.misc_file = os.path.join(self.inbox_path, "碎碎念.md")

        # 01-Daily
        self.daily_notes_dir = os.path.join(self.base_dir, "01-Daily")

        # 02-Notes 各分类
        _notes = os.path.join(self.base_dir, "02-Notes")
        self.book_notes_dir = os.path.join(_notes, "读书笔记")
        self.media_notes_dir = os.path.join(_notes, "影视笔记")
        self.work_notes_dir = os.path.join(_notes, "工作笔记")
        self.emotion_notes_dir = os.path.join(_notes, "情感日记")
        self.fun_notes_dir = os.path.join(_notes, "生活趣事")
        self.voice_journal_dir = os.path.join(_notes, "语音日记")

        # _Karvis 系统文件（memory 走 IO，config/log 始终本地）
        self.memory_file = os.path.join(self.base_dir, "_Karvis", "memory", "memory.md")

        # 03-Finance（仅管理员可能使用，但路径先定义好）
        _finance = os.path.join(self.base_dir, "03-Finance")
        self.finance_dir = _finance
        self.finance_data_file = os.path.join(_finance, "finance_data.json")
        self.finance_inbox_dir = os.path.join(_finance, "inbox")
        self.finance_reports_dir = os.path.join(_finance, "reports")

    def _init_onedrive_mode(self):
        """OneDrive 存储模式：IO = OneDriveIO 实例，路径为 OneDrive 远程路径"""
        od_config = self.config.get("onedrive", {})
        self.IO = create_storage("onedrive", od_config)
        self.storage_mode = "onedrive"

        base = od_config.get("obsidian_base", "/应用/remotely-save/EmptyVault")

        # 00-Inbox
        self.inbox_path = f"{base}/00-Inbox"
        self.quick_notes_file = f"{base}/00-Inbox/Quick-Notes.md"
        self.state_file = f"{base}/00-Inbox/.ai-life-state.json"
        self.todo_file = f"{base}/00-Inbox/Todo.md"
        self.attachments_path = f"{base}/00-Inbox/attachments"
        self.misc_file = f"{base}/00-Inbox/碎碎念.md"

        # 01-Daily
        self.daily_notes_dir = f"{base}/01-Daily"

        # 02-Notes 各分类
        self.book_notes_dir = f"{base}/02-Notes/读书笔记"
        self.media_notes_dir = f"{base}/02-Notes/影视笔记"
        self.work_notes_dir = f"{base}/02-Notes/工作笔记"
        self.emotion_notes_dir = f"{base}/02-Notes/情感日记"
        self.fun_notes_dir = f"{base}/02-Notes/生活趣事"
        self.voice_journal_dir = f"{base}/02-Notes/语音日记"

        # _Karvis 系统文件
        self.memory_file = f"{base}/_Karvis/memory/memory.md"

        # 03-Finance
        self.finance_dir = f"{base}/03-Finance"
        self.finance_data_file = f"{base}/03-Finance/finance_data.json"
        self.finance_inbox_dir = f"{base}/03-Finance/inbox"
        self.finance_reports_dir = f"{base}/03-Finance/reports"

    def _init_feishu_mode(self):
        """飞书云空间存储模式：IO = FeishuDriveIO 实例，路径为相对于根文件夹的虚拟路径"""
        # 从 user_config 中读取飞书存储配置，若缺失则回退到全局环境变量
        fs_config = self.config.get("feishu_drive", {})
        feishu_config = {
            "app_id": fs_config.get("app_id") or FEISHU_APP_ID,
            "app_secret": fs_config.get("app_secret") or FEISHU_APP_SECRET,
            "root_folder_token": fs_config.get("root_folder_token") or FEISHU_DRIVE_ROOT_FOLDER_TOKEN,
        }

        self.IO = create_storage("feishu", feishu_config)
        self.storage_mode = "feishu"

        # 飞书云空间使用相对路径（相对于 root_folder_token 指向的根文件夹）
        # 00-Inbox
        self.inbox_path = "00-Inbox"
        self.quick_notes_file = "00-Inbox/Quick-Notes.md"
        self.state_file = "00-Inbox/.ai-life-state.json"
        self.todo_file = "00-Inbox/Todo.md"
        self.attachments_path = "00-Inbox/attachments"
        self.misc_file = "00-Inbox/碎碎念.md"

        # 01-Daily
        self.daily_notes_dir = "01-Daily"

        # 02-Notes 各分类
        self.book_notes_dir = "02-Notes/读书笔记"
        self.media_notes_dir = "02-Notes/影视笔记"
        self.work_notes_dir = "02-Notes/工作笔记"
        self.emotion_notes_dir = "02-Notes/情感日记"
        self.fun_notes_dir = "02-Notes/生活趣事"
        self.voice_journal_dir = "02-Notes/语音日记"

        # _Karvis 系统文件
        self.memory_file = "_Karvis/memory/memory.md"

        # 03-Finance
        self.finance_dir = "03-Finance"
        self.finance_data_file = "03-Finance/finance_data.json"
        self.finance_inbox_dir = "03-Finance/inbox"
        self.finance_reports_dir = "03-Finance/reports"

    def _load_config(self) -> dict:
        """从本地文件加载用户配置（user_config.json 始终存储在本地）"""
        try:
            if os.path.exists(self.user_config_file):
                with open(self.user_config_file, "r", encoding="utf-8") as f:
                    config = json.load(f)

                changed = False
                channel = config.get("channel")
                if not channel:
                    if self.user_id.startswith("tg_"):
                        config["channel"] = "telegram"
                        channel = "telegram"
                        changed = True
                    elif self.user_id.startswith("fs_"):
                        config["channel"] = "feishu"
                        channel = "feishu"
                        changed = True
                    else:
                        config["channel"] = "wework"
                        channel = "wework"
                        changed = True

                if channel == "telegram" and not config.get("telegram_chat_id") and self.user_id.startswith("tg_"):
                    config["telegram_chat_id"] = self.user_id[3:]
                    changed = True

                if channel == "feishu" and not config.get("feishu_open_id") and self.user_id.startswith("fs_"):
                    config["feishu_open_id"] = self.user_id[3:]
                    changed = True

                if changed:
                    try:
                        os.makedirs(os.path.dirname(self.user_config_file), exist_ok=True)
                        with open(self.user_config_file, "w", encoding="utf-8") as wf:
                            json.dump(config, wf, ensure_ascii=False, indent=2)
                    except Exception:
                        logger.exception("[UserContext] 回填 user_config 字段失败 %s", self.user_id)

                return config
        except Exception as e:
            logger.error("[UserContext] 读取 user_config 失败 %s: %s", self.user_id, e)
        return {}

    def get_user_config(self) -> dict:
        """读取用户配置（返回缓存的 self.config）"""
        return self.config

    def save_user_config(self, config: dict):
        """保存用户配置到本地文件，并更新内存缓存"""
        try:
            os.makedirs(os.path.dirname(self.user_config_file), exist_ok=True)
            with open(self.user_config_file, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            self.config = config
        except Exception as e:
            logger.error("[UserContext] 保存 user_config 失败 %s: %s", self.user_id, e)

    def get_nickname(self) -> str:
        return self.config.get("nickname", "")

    def get_soul_override(self) -> str:
        return self.config.get("soul_override", "")

    # ============ Skill 过滤 ============

    def _matches(self, skill_name: str, patterns: list) -> bool:
        """支持精确名与通配符（如 decision.*）"""
        return any(fnmatch.fnmatch(skill_name, p) for p in patterns)

    def is_skill_allowed(self, skill_name: str) -> bool:
        """检查该用户是否有权使用指定 Skill（不含 visibility 检查，visibility 由 skill_loader 处理）"""
        mode = self._skills_config.get("mode", "blacklist")
        skill_list = self._skills_config.get("list", [])

        if mode == "whitelist":
            return bool(skill_list) and self._matches(skill_name, skill_list)
        else:  # blacklist
            return not self._matches(skill_name, skill_list)

    def get_allowed_skills(self, all_skills: dict) -> dict:
        """从全量 Skill 元数据中过滤出该用户可用的"""
        return {k: v for k, v in all_skills.items() if self.is_skill_allowed(k)}

    @property
    def is_admin(self) -> bool:
        return self.config.get("role") == "admin"

    # ============ 目录创建 ============

    def all_dirs(self) -> list:
        """返回该用户需要创建的所有本地目录（仅 local 模式需要实际创建）"""
        base = self.base_dir
        inbox = os.path.join(base, "00-Inbox")
        _notes = os.path.join(base, "02-Notes")
        _karvis = os.path.join(base, "_Karvis")
        return [
            inbox,
            os.path.join(inbox, "attachments"),
            os.path.join(base, "01-Daily"),
            os.path.join(_notes, "读书笔记"),
            os.path.join(_notes, "影视笔记"),
            os.path.join(_notes, "工作笔记"),
            os.path.join(_notes, "情感日记"),
            os.path.join(_notes, "生活趣事"),
            os.path.join(_notes, "语音日记"),
            os.path.join(_karvis, "memory"),
            os.path.join(_karvis, "logs"),
        ]


