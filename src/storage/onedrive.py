# -*- coding: utf-8 -*-
"""
KarvisForAll V12 — OneDrive 统一读写层（实例模式）
从 Karvis 单用户版移植，改造要点：
  1. 所有 @classmethod → 实例方法，凭证由构造函数注入
  2. token 缓存为实例级别（支持多个 OneDrive 账号并发）
  3. 内置三级缓存 (内存 → /tmp → OneDrive API) 降低延迟
  4. HTTP Session 复用（全局共享，减少 TLS 握手）
"""
import os
import time
import json
import hashlib
import threading
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter

from infra.logging import BEIJING_TZ, get_logger
from storage.base import StorageBackend

logger = get_logger(__name__)


# 全局 Session：所有 OneDriveIO 实例共享 TCP 连接池
_graph_session = requests.Session()
_graph_adapter = HTTPAdapter(
    pool_connections=8,
    pool_maxsize=8,
    max_retries=0
)
_graph_session.mount("https://graph.microsoft.com", _graph_adapter)

# token 刷新用独立 session
_auth_session = requests.Session()

# /tmp 磁盘缓存目录
_DISK_CACHE_DIR = "/tmp/karvis_od_cache"


class OneDriveIO(StorageBackend):
    """OneDrive 统一读写，每用户一个实例，持有独立凭证。

    继承 StorageBackend 抽象协议，上层代码无感知切换。
    """

    def __init__(self, onedrive_config: dict):
        self.client_id = onedrive_config.get("client_id", "")
        self.client_secret = onedrive_config.get("client_secret", "")
        self.refresh_token = onedrive_config.get("refresh_token", "")
        self._token_cache = {"token": None, "expire_time": 0}
        self._token_lock = threading.Lock()

        # 三级缓存：内存层
        self._mem_cache = {}
        self._mem_cache_ttl = 300  # 5 分钟

    # ================================================================
    #  三级缓存 helpers
    # ================================================================

    def _cache_key(self, path: str) -> str:
        return hashlib.md5(f"{self.client_id}:{path}".encode()).hexdigest()

    def _get_from_mem_cache(self, path: str):
        key = self._cache_key(path)
        cached = self._mem_cache.get(key)
        if cached and time.time() < cached["expire"]:
            return cached["data"], True
        return None, False

    def _put_mem_cache(self, path: str, data):
        key = self._cache_key(path)
        self._mem_cache[key] = {"data": data, "expire": time.time() + self._mem_cache_ttl}

    def _get_from_disk_cache(self, path: str):
        key = self._cache_key(path)
        disk_path = os.path.join(_DISK_CACHE_DIR, key)
        try:
            if os.path.exists(disk_path):
                mtime = os.path.getmtime(disk_path)
                if time.time() - mtime < self._mem_cache_ttl * 2:
                    with open(disk_path, "r", encoding="utf-8") as f:
                        return f.read(), True
        except Exception as e:
            logger.debug("读取磁盘缓存失败: %s", e)
            pass
        return None, False

    def _put_disk_cache(self, path: str, data: str):
        key = self._cache_key(path)
        try:
            os.makedirs(_DISK_CACHE_DIR, exist_ok=True)
            disk_path = os.path.join(_DISK_CACHE_DIR, key)
            with open(disk_path, "w", encoding="utf-8") as f:
                f.write(data)
        except Exception as e:
            logger.debug("写入磁盘缓存失败: %s", e)
            pass

    def _invalidate_cache(self, path: str):
        key = self._cache_key(path)
        self._mem_cache.pop(key, None)
        try:
            disk_path = os.path.join(_DISK_CACHE_DIR, key)
            if os.path.exists(disk_path):
                os.remove(disk_path)
        except Exception as e:
            logger.debug("清除磁盘缓存文件失败: %s", e)
            pass

    # ================================================================
    #  Token 管理
    # ================================================================

    def get_token(self):
        """获取 access_token（带内存缓存，线程安全）"""
        now = time.time()
        if self._token_cache["token"] and self._token_cache["expire_time"] > now:
            return self._token_cache["token"]

        with self._token_lock:
            now = time.time()
            if self._token_cache["token"] and self._token_cache["expire_time"] > now:
                return self._token_cache["token"]

            logger.info("[OneDrive] 开始刷新 token...")
            t0 = time.time()
            url = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
            data = {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
                "scope": "Files.ReadWrite offline_access"
            }
            try:
                resp = _auth_session.post(url, data=data, timeout=30)
                t1 = time.time()
                result = resp.json()
                token = result.get("access_token")
                if token:
                    expires_in = result.get("expires_in", 3600)
                    self._token_cache = {
                        "token": token,
                        "expire_time": now + expires_in - 120
                    }
                    logger.info("[OneDrive] token 刷新成功: %.1fs", t1 - t0)
                    return token
                logger.error("[OneDrive] token 获取失败(%.1fs): %s", t1 - t0, result)
            except Exception as e:
                logger.error("[OneDrive] token 请求异常(%.1fs): %s", time.time() - t0, e)
            return None

    # ================================================================
    #  文本文件读写
    # ================================================================

    def read_text(self, file_path, _retries=3):
        """读取文本文件（三级缓存：内存 → /tmp → OneDrive API）。
        返回字符串。文件不存在返回空字符串，失败返回 None。
        """
        # L1: 内存缓存
        data, hit = self._get_from_mem_cache(file_path)
        if hit:
            return data

        # L2: 磁盘缓存
        data, hit = self._get_from_disk_cache(file_path)
        if hit:
            self._put_mem_cache(file_path, data)
            return data

        # L3: OneDrive API
        token = self.get_token()
        if not token:
            return None
        url = f"https://graph.microsoft.com/v1.0/me/drive/root:{file_path}:/content"
        headers = {"Authorization": f"Bearer {token}"}
        for attempt in range(1, _retries + 1):
            try:
                t0 = time.time()
                resp = _graph_session.get(url, headers=headers, timeout=(5, 10))
                elapsed = time.time() - t0
                if resp.status_code == 200:
                    logger.info("[OneDrive] 读取OK %s: %.1fs", file_path, elapsed)
                    text = resp.text
                    self._put_mem_cache(file_path, text)
                    self._put_disk_cache(file_path, text)
                    return text
                elif resp.status_code == 404:
                    return ""
                logger.warning("[OneDrive] 读取失败 %s: %s (%.1fs)", file_path, resp.status_code, elapsed)
                return None
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, ConnectionError):
                logger.warning("[OneDrive] 读取超时(第%d次) %s: %.1fs", attempt, file_path, time.time() - t0)
                if attempt < _retries:
                    continue
                return None
            except Exception as e:
                logger.error("[OneDrive] 读取异常 %s: %s", file_path, e)
                return None

    def write_text(self, file_path, content, _retries=3):
        """写入文本文件（覆盖），同步更新缓存。返回 True/False。"""
        token = self.get_token()
        if not token:
            return False
        url = f"https://graph.microsoft.com/v1.0/me/drive/root:{file_path}:/content"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "text/plain; charset=utf-8"
        }
        data = content.encode('utf-8')
        for attempt in range(1, _retries + 1):
            try:
                t0 = time.time()
                resp = _graph_session.put(url, headers=headers, data=data, timeout=(5, 15))
                elapsed = time.time() - t0
                ok = resp.status_code in (200, 201)
                if ok:
                    logger.info("[OneDrive] 写入OK %s: %.1fs", file_path, elapsed)
                    self._put_mem_cache(file_path, content)
                    self._put_disk_cache(file_path, content)
                else:
                    logger.warning("[OneDrive] 写入失败 %s: %s (%.1fs)", file_path, resp.status_code, elapsed)
                return ok
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, ConnectionError):
                logger.warning("[OneDrive] 写入超时(第%d次) %s: %.1fs", attempt, file_path, time.time() - t0)
                if attempt < _retries:
                    continue
                return False
            except Exception as e:
                logger.error("[OneDrive] 写入异常 %s: %s", file_path, e)
                return False

    # ================================================================
    #  JSON 文件读写
    # ================================================================

    def read_json(self, file_path):
        """读取 JSON 文件。文件不存在返回空 dict，失败返回 None。"""
        text = self.read_text(file_path)
        if text is None:
            return None
        if not text.strip():
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            logger.error("[OneDrive] JSON 解析失败 %s: %s", file_path, e)
            return None

    def write_json(self, file_path, data):
        """写入 JSON 文件。"""
        content = json.dumps(data, ensure_ascii=False, indent=2)
        return self.write_text(file_path, content)

    # ================================================================
    #  追加到文件指定 section
    # ================================================================

    def append_to_section(self, file_path, section_header, content):
        """追加内容到文件的指定 section（以 ## 开头）。"""
        existing = self.read_text(file_path)
        if existing is None:
            return False

        if section_header in existing:
            parts = existing.split(section_header, 1)
            before = parts[0]
            after = parts[1]
            next_section_idx = after.find("\n## ")
            if next_section_idx >= 0:
                section_content = after[:next_section_idx]
                rest = after[next_section_idx:]
                new_content = before + section_header + section_content.rstrip() + "\n" + content + "\n" + rest
            else:
                new_content = before + section_header + after.rstrip() + "\n" + content + "\n"
        else:
            new_content = existing.rstrip() + f"\n\n{section_header}\n{content}\n"

        return self.write_text(file_path, new_content)

    # ================================================================
    #  追加到 Quick-Notes（带去重）
    # ================================================================

    def append_to_quick_notes(self, file_path, message):
        """追加一条笔记到 Quick-Notes，格式化为 ## 时间戳 + 内容"""
        existing = self.read_text(file_path)
        if existing is None:
            return False

        if not existing.strip():
            existing = "# Quick Notes\n\n快速笔记，从微信同步。\n\n---\n\n"

        # 内容去重：检查最近 5 条
        sections = existing.split('## ')
        for section in sections[1:6]:
            lines = section.strip().split('\n')
            if len(lines) >= 2:
                content_lines = '\n'.join(lines[1:]).strip().rstrip('-').strip()
                if content_lines == message.strip():
                    logger.info("[OneDrive] Quick-Notes 内容重复，跳过: %s...", message[:30])
                    return True

        # 追加新条目
        now = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M")
        new_entry = f"## {now}\n\n{message}\n\n---\n\n"

        lines = existing.split('\n')
        header_end = 0
        for i, line in enumerate(lines):
            if line.strip() == "---":
                header_end = i + 1
                break

        new_content = '\n'.join(lines[:header_end]) + '\n\n' + new_entry + '\n'.join(lines[header_end:])
        return self.write_text(file_path, new_content)

    # ================================================================
    #  目录列表
    # ================================================================

    def list_children(self, folder_path, _retries=3):
        """列出 OneDrive 文件夹下的子项。
        返回 list[dict]。文件夹不存在返回空列表，失败返回 None。
        """
        token = self.get_token()
        if not token:
            return None
        url = f"https://graph.microsoft.com/v1.0/me/drive/root:{folder_path}:/children"
        headers = {"Authorization": f"Bearer {token}"}
        for attempt in range(1, _retries + 1):
            try:
                t0 = time.time()
                resp = _graph_session.get(url, headers=headers, timeout=(5, 10))
                elapsed = time.time() - t0
                if resp.status_code == 200:
                    items = resp.json().get("value", [])
                    logger.info("[OneDrive] 列目录OK %s: %d项 (%.1fs)", folder_path, len(items), elapsed)
                    return items
                elif resp.status_code == 404:
                    logger.warning("[OneDrive] 目录不存在 %s", folder_path)
                    return []
                logger.warning("[OneDrive] 列目录失败 %s: %s (%.1fs)", folder_path, resp.status_code, elapsed)
                return None
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, ConnectionError):
                logger.warning("[OneDrive] 列目录超时(第%d次) %s: %.1fs", attempt, folder_path, time.time() - t0)
                if attempt < _retries:
                    continue
                return None
            except Exception as e:
                logger.error("[OneDrive] 列目录异常 %s: %s", folder_path, e)
                return None

    # ================================================================
    #  二进制文件下载
    # ================================================================

    def download_binary(self, file_path, _retries=3):
        """下载文件二进制内容。文件不存在返回 None。"""
        token = self.get_token()
        if not token:
            return None
        url = f"https://graph.microsoft.com/v1.0/me/drive/root:{file_path}:/content"
        headers = {"Authorization": f"Bearer {token}"}
        for attempt in range(1, _retries + 1):
            try:
                t0 = time.time()
                resp = _graph_session.get(url, headers=headers, timeout=(5, 30))
                elapsed = time.time() - t0
                if resp.status_code == 200:
                    logger.info("[OneDrive] 下载OK %s: %dB (%.1fs)", file_path, len(resp.content), elapsed)
                    return resp.content
                elif resp.status_code == 404:
                    logger.warning("[OneDrive] 文件不存在 %s", file_path)
                    return None
                logger.warning("[OneDrive] 下载失败 %s: %s (%.1fs)", file_path, resp.status_code, elapsed)
                return None
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, ConnectionError):
                logger.warning("[OneDrive] 下载超时(第%d次) %s: %.1fs", attempt, file_path, time.time() - t0)
                if attempt < _retries:
                    continue
                return None
            except Exception as e:
                logger.error("[OneDrive] 下载异常 %s: %s", file_path, e)
                return None

    # ================================================================
    #  文件删除
    # ================================================================

    def delete_item(self, file_path, _retries=3):
        """删除 OneDrive 上的文件或文件夹。返回 True/False。"""
        token = self.get_token()
        if not token:
            return False
        url = f"https://graph.microsoft.com/v1.0/me/drive/root:{file_path}"
        headers = {"Authorization": f"Bearer {token}"}
        for attempt in range(1, _retries + 1):
            try:
                t0 = time.time()
                resp = _graph_session.delete(url, headers=headers, timeout=(5, 10))
                elapsed = time.time() - t0
                if resp.status_code in (200, 204):
                    logger.info("[OneDrive] 删除OK %s (%.1fs)", file_path, elapsed)
                    self._invalidate_cache(file_path)
                    return True
                elif resp.status_code == 404:
                    logger.warning("[OneDrive] 删除目标不存在 %s", file_path)
                    return True
                logger.warning("[OneDrive] 删除失败 %s: %s (%.1fs)", file_path, resp.status_code, elapsed)
                return False
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, ConnectionError):
                logger.warning("[OneDrive] 删除超时(第%d次) %s: %.1fs", attempt, file_path, time.time() - t0)
                if attempt < _retries:
                    continue
                return False
            except Exception as e:
                logger.error("[OneDrive] 删除异常 %s: %s", file_path, e)
                return False

    # ================================================================
    #  二进制文件上传
    # ================================================================

    def upload_binary(self, file_path, data, content_type="application/octet-stream"):
        """统一二进制上传入口（自动选择简单/分片上传）"""
        if len(data) <= 4 * 1024 * 1024:
            return self._upload_small(file_path, data, content_type)
        else:
            return self._upload_large(file_path, data)

    def _upload_small(self, file_path, data, content_type, _retries=3):
        """简单上传（<=4MB），带重试"""
        for attempt in range(1, _retries + 1):
            token = self.get_token()
            if not token:
                if attempt < _retries:
                    logger.warning("[OneDrive] 上传token获取失败(第%d次)，%ds后重试", attempt, 2 * attempt)
                    time.sleep(2 * attempt)
                    continue
                return False
            url = f"https://graph.microsoft.com/v1.0/me/drive/root:{file_path}:/content"
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": content_type
            }
            try:
                t0 = time.time()
                resp = _graph_session.put(url, headers=headers, data=data, timeout=60)
                ok = resp.status_code in (200, 201)
                logger.info("[OneDrive] 上传 %s size=%d status=%s (%.1fs)", file_path, len(data), resp.status_code, time.time() - t0)
                if ok:
                    return True
                # 401 token 过期，清缓存重试
                if resp.status_code == 401:
                    self._token_cache = {"token": None, "expire_time": 0}
                    if attempt < _retries:
                        continue
                return False
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                logger.warning("[OneDrive] 上传超时(第%d次) %s: %.1fs", attempt, file_path, time.time() - t0)
                if attempt < _retries:
                    time.sleep(2 * attempt)
                    continue
                return False
            except Exception as e:
                logger.error("[OneDrive] 上传异常 %s: %s", file_path, e)
                return False
        return False

    def _upload_large(self, file_path, data):
        """分片上传（>4MB，每片 3.2MB）"""
        token = self.get_token()
        if not token:
            return False

        url = f"https://graph.microsoft.com/v1.0/me/drive/root:{file_path}:/createUploadSession"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        body = {"item": {"@microsoft.graph.conflictBehavior": "replace"}}
        try:
            resp = _graph_session.post(url, headers=headers, json=body, timeout=30)
            if resp.status_code != 200:
                logger.error("[OneDrive] 创建上传会话失败: %s", resp.status_code)
                return False
            upload_url = resp.json().get("uploadUrl")
            if not upload_url:
                return False
        except Exception as e:
            logger.error("[OneDrive] 创建上传会话异常: %s", e)
            return False

        chunk_size = 3276800
        total_size = len(data)
        logger.info("[OneDrive] 分片上传 %s total=%d", file_path, total_size)

        for start in range(0, total_size, chunk_size):
            end = min(start + chunk_size, total_size)
            chunk = data[start:end]
            chunk_headers = {
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {start}-{end-1}/{total_size}"
            }
            try:
                resp = requests.put(upload_url, headers=chunk_headers,
                                    data=chunk, timeout=60)
                if resp.status_code not in (200, 201, 202):
                    logger.error("[OneDrive] 分片失败: %s", resp.status_code)
                    return False
            except Exception as e:
                logger.error("[OneDrive] 分片异常: %s", e)
                return False

        logger.info("[OneDrive] 分片上传完成: %s", file_path)
        return True
