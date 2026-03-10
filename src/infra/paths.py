# -*- coding: utf-8 -*-
"""
KarvisForAll 路径常量。

集中管理系统级路径，避免模块间因路径常量产生循环依赖。
所有需要 DATA_DIR / SYSTEM_DIR 等路径的模块统一从此处导入。
"""
import os

# DATA_DIR 是所有用户数据的根目录
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(_project_root, "data"))

SYSTEM_DIR = os.path.join(DATA_DIR, "_karvis_system")
USER_REGISTRY_FILE = os.path.join(SYSTEM_DIR, "users.json")
TOKENS_FILE = os.path.join(SYSTEM_DIR, "tokens.json")
USAGE_LOG_FILE = os.path.join(SYSTEM_DIR, "usage_log.jsonl")
