# -*- coding: utf-8 -*-
"""
Prompt / Memory 文件缓存模块。

提供 PromptCache 三级缓存（内存 → /tmp → 存储后端回源），
以及 load_memory() 便捷函数。
"""
import os
import time
import threading

from config import PROMPT_CACHE_TTL
from infra.logging import get_logger
from storage import create_storage

logger = get_logger(__name__)

_TMP_CACHE_DIR = "/tmp/karvis_prompts"


class PromptCache:
    """Memory 文件缓存：内存 → /tmp 磁盘 → 本地文件（三级缓存）"""

    def __init__(self):
        self._cache = {}
        self._lock = threading.Lock()
        os.makedirs(_TMP_CACHE_DIR, exist_ok=True)

    def _tmp_path(self, file_path):
        safe = file_path.replace("/", "_").replace(" ", "_").replace(os.sep, "_")
        return os.path.join(_TMP_CACHE_DIR, safe)

    def get(self, file_path, io=None):
        """读取文件内容（三级缓存）。io: 可选的存储 IO 对象，用于 L3 回源读取。"""
        now = time.time()

        # 1. 内存缓存
        cached = self._cache.get(file_path)
        if cached and cached["expire_time"] > now:
            return cached["content"]

        # 2. /tmp 磁盘缓存
        tmp_file = self._tmp_path(file_path)
        try:
            if os.path.exists(tmp_file):
                mtime = os.path.getmtime(tmp_file)
                if now - mtime < PROMPT_CACHE_TTL:
                    with open(tmp_file, "r", encoding="utf-8") as f:
                        content = f.read()
                    with self._lock:
                        self._cache[file_path] = {"content": content, "expire_time": now + PROMPT_CACHE_TTL}
                    return content
        except Exception as e:
            logger.debug("读取 /tmp 磁盘缓存失败: %s", e)
            pass

        # 3. 通过 IO 对象回源读取
        if io is None:
            io = create_storage("local")
        content = io.read_text(file_path)
        if content is not None:
            with self._lock:
                self._cache[file_path] = {"content": content, "expire_time": now + PROMPT_CACHE_TTL}
            try:
                with open(tmp_file, "w", encoding="utf-8") as f:
                    f.write(content)
            except Exception as e:
                logger.debug("写入 /tmp 磁盘缓存失败: %s", e)
                pass
        return content or ""

    def invalidate(self, file_path=None):
        """清除缓存（全部或指定文件）"""
        with self._lock:
            if file_path:
                self._cache.pop(file_path, None)
                try:
                    tmp_file = self._tmp_path(file_path)
                    if os.path.exists(tmp_file):
                        os.remove(tmp_file)
                except Exception as e:
                    logger.debug("删除 /tmp 缓存文件失败: %s", e)
                    pass
            else:
                self._cache.clear()
                try:
                    for f in os.listdir(_TMP_CACHE_DIR):
                        os.remove(os.path.join(_TMP_CACHE_DIR, f))
                except Exception as e:
                    logger.debug("清除 /tmp 缓存目录失败: %s", e)
                    pass


# 模块级单例
_prompt_cache = PromptCache()


def get_prompt_cache() -> PromptCache:
    """获取全局 PromptCache 单例"""
    return _prompt_cache


def load_memory(ctx):
    """加载某个用户的 memory.md"""
    return _prompt_cache.get(ctx.memory_file, io=ctx.IO)
