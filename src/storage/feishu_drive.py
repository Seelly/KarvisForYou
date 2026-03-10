# -*- coding: utf-8 -*-
"""
KarvisForAll 飞书云空间存储后端
通过飞书开放平台 Drive API 实现文件读写，支持路径 → token 映射。

核心特点：
  1. 飞书使用 folder_token / file_token 体系，而非路径
  2. 内部维护 路径 → token 映射缓存（内存级别）
  3. 首次写入自动逐级创建文件夹
  4. 复用飞书 App 的 tenant_access_token
  5. 重试策略与 OneDriveIO 一致
"""
import json
import time
import threading
import requests
from requests.adapters import HTTPAdapter

from infra.logging import BEIJING_TZ, get_logger
from storage.base import StorageBackend

logger = get_logger(__name__)

# 全局 Session：复用 TCP 连接池
_feishu_session = requests.Session()
_feishu_adapter = HTTPAdapter(pool_connections=8, pool_maxsize=8, max_retries=0)
_feishu_session.mount("https://open.feishu.cn", _feishu_adapter)

# 飞书 API 基地址
_API_BASE = "https://open.feishu.cn/open-apis"


class FeishuDriveIO(StorageBackend):
    """飞书云空间存储后端。

    通过飞书 Drive API 实现与 LocalFileIO / OneDriveIO 相同的接口协议。
    路径格式为相对于 root_folder 的虚拟路径，如 '00-Inbox/Quick-Notes.md'。
    """

    def __init__(self, config: dict):
        """初始化飞书云空间存储后端。

        Args:
            config: 配置字典，包含：
                - app_id: 飞书应用 ID
                - app_secret: 飞书应用密钥
                - root_folder_token: 根文件夹 token
        """
        self._app_id = config.get("app_id", "")
        self._app_secret = config.get("app_secret", "")
        self._root_folder_token = config.get("root_folder_token", "")

        # tenant_access_token 缓存
        self._token_cache = {"token": None, "expire_time": 0}
        self._token_lock = threading.Lock()

        # 路径 → token 映射缓存: {"path": {"token": "xxx", "type": "file|folder", "expire": ts}}
        self._path_cache = {}
        self._path_cache_ttl = 600  # 10 分钟

        # 内存内容缓存（类似 OneDrive 的内存层）
        self._mem_cache = {}
        self._mem_cache_ttl = 300  # 5 分钟

        logger.info("[FeishuDrive] 初始化完成: root_folder_token=%s", self._root_folder_token[:8] + "..." if self._root_folder_token else "空")
        if not self._root_folder_token:
            logger.warning("[FeishuDrive] root_folder_token 未配置，请在飞书云空间创建根文件夹并配置 token")

    # ================================================================
    #  Token 管理
    # ================================================================

    def get_token(self) -> str | None:
        """获取 tenant_access_token（带缓存 + 自动刷新）"""
        now = time.time()
        if self._token_cache["token"] and self._token_cache["expire_time"] > now:
            return self._token_cache["token"]

        with self._token_lock:
            now = time.time()
            if self._token_cache["token"] and self._token_cache["expire_time"] > now:
                return self._token_cache["token"]

            logger.info("[FeishuDrive] 开始获取 tenant_access_token...")
            url = f"{_API_BASE}/auth/v3/tenant_access_token/internal"
            payload = {"app_id": self._app_id, "app_secret": self._app_secret}
            try:
                resp = _feishu_session.post(url, json=payload, timeout=10)
                result = resp.json()
                if result.get("code") == 0:
                    token = result.get("tenant_access_token")
                    expire = result.get("expire", 7200)
                    self._token_cache = {
                        "token": token,
                        "expire_time": now + expire - 120,  # 提前 2 分钟刷新
                    }
                    logger.info("[FeishuDrive] tenant_access_token 获取成功, 有效期=%ds", expire)
                    return token
                logger.error("[FeishuDrive] tenant_access_token 获取失败: %s", result)
            except Exception as e:
                logger.error("[FeishuDrive] tenant_access_token 请求异常: %s", e)
            return None

    def _auth_headers(self) -> dict | None:
        """构建带 Authorization 的请求头，token 获取失败返回 None"""
        token = self.get_token()
        if not token:
            return None
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}

    # ================================================================
    #  路径 → Token 映射
    # ================================================================

    def _get_cached_token(self, path: str) -> dict | None:
        """从缓存获取路径对应的 token 信息"""
        cached = self._path_cache.get(path)
        if cached and time.time() < cached.get("expire", 0):
            return cached
        return None

    def _put_cached_token(self, path: str, token: str, item_type: str):
        """缓存路径对应的 token 信息"""
        self._path_cache[path] = {
            "token": token,
            "type": item_type,
            "expire": time.time() + self._path_cache_ttl,
        }

    def _invalidate_path_cache(self, path: str):
        """清除指定路径的 token 缓存"""
        self._path_cache.pop(path, None)

    def _resolve_folder_token(self, folder_path: str, auto_create: bool = False) -> str | None:
        """将文件夹相对路径解析为 folder_token。

        例如 '00-Inbox/attachments' → 逐级查找/创建 → 返回 folder_token。

        Args:
            folder_path: 相对于 root_folder 的文件夹路径
            auto_create: 如果文件夹不存在，是否自动创建

        Returns:
            folder_token 或 None
        """
        if not folder_path or folder_path in (".", "/", ""):
            return self._root_folder_token

        # 检查缓存
        cached = self._get_cached_token(folder_path)
        if cached and cached["type"] == "folder":
            return cached["token"]

        # 逐级解析
        parts = [p for p in folder_path.split("/") if p]
        current_token = self._root_folder_token

        for i, part in enumerate(parts):
            partial_path = "/".join(parts[:i + 1])

            # 检查中间路径缓存
            cached = self._get_cached_token(partial_path)
            if cached and cached["type"] == "folder":
                current_token = cached["token"]
                continue

            # 查询飞书 API: 列出当前文件夹的子项，找到名为 part 的子文件夹
            child_token = self._find_child_folder(current_token, part)
            if child_token:
                self._put_cached_token(partial_path, child_token, "folder")
                current_token = child_token
            elif auto_create:
                # 自动创建文件夹
                new_token = self._create_folder(current_token, part)
                if new_token:
                    self._put_cached_token(partial_path, new_token, "folder")
                    current_token = new_token
                else:
                    logger.error("[FeishuDrive] 创建文件夹失败: %s (parent=%s)", part, current_token)
                    return None
            else:
                return None

        return current_token

    def _find_child_folder(self, parent_token: str, name: str) -> str | None:
        """在指定文件夹下查找名为 name 的子文件夹，返回其 folder_token"""
        headers = self._auth_headers()
        if not headers:
            return None

        url = f"{_API_BASE}/drive/v1/files"
        params = {"folder_token": parent_token, "page_size": 200}
        try:
            resp = _feishu_session.get(url, headers=headers, params=params, timeout=10)
            result = resp.json()
            if result.get("code") != 0:
                logger.warning("[FeishuDrive] 列目录失败: parent=%s, result=%s", parent_token, result)
                return None
            files = result.get("data", {}).get("files", [])
            for f in files:
                if f.get("name") == name and f.get("type") == "folder":
                    return f.get("token")
        except Exception as e:
            logger.error("[FeishuDrive] 查找子文件夹异常: %s", e)
        return None

    def _find_child_file(self, parent_token: str, name: str) -> str | None:
        """在指定文件夹下查找名为 name 的文件，返回其 file_token"""
        headers = self._auth_headers()
        if not headers:
            return None

        url = f"{_API_BASE}/drive/v1/files"
        params = {"folder_token": parent_token, "page_size": 200}
        try:
            resp = _feishu_session.get(url, headers=headers, params=params, timeout=10)
            result = resp.json()
            if result.get("code") != 0:
                logger.warning("[FeishuDrive] 列目录失败: parent=%s, result=%s", parent_token, result)
                return None
            files = result.get("data", {}).get("files", [])
            for f in files:
                if f.get("name") == name and f.get("type") != "folder":
                    return f.get("token")
        except Exception as e:
            logger.error("[FeishuDrive] 查找文件异常: %s", e)
        return None

    def _resolve_file_token(self, file_path: str) -> str | None:
        """将文件相对路径解析为 file_token。

        例如 '00-Inbox/Quick-Notes.md' → 找到 00-Inbox 的 folder_token → 在其中找 Quick-Notes.md

        Returns:
            file_token 或 None（文件不存在时）
        """
        # 检查缓存
        cached = self._get_cached_token(file_path)
        if cached and cached["type"] == "file":
            return cached["token"]

        # 拆分目录和文件名
        parts = file_path.rsplit("/", 1)
        if len(parts) == 2:
            folder_path, file_name = parts
        else:
            folder_path, file_name = "", parts[0]

        # 先解析文件夹
        folder_token = self._resolve_folder_token(folder_path)
        if not folder_token:
            return None

        # 在文件夹中查找文件
        file_token = self._find_child_file(folder_token, file_name)
        if file_token:
            self._put_cached_token(file_path, file_token, "file")
        return file_token

    def _create_folder(self, parent_token: str, name: str) -> str | None:
        """在指定文件夹下创建子文件夹"""
        headers = self._auth_headers()
        if not headers:
            return None

        url = f"{_API_BASE}/drive/v1/files/create_folder"
        body = {"name": name, "folder_token": parent_token}
        try:
            resp = _feishu_session.post(url, headers=headers, json=body, timeout=10)
            result = resp.json()
            if result.get("code") == 0:
                token = result.get("data", {}).get("token")
                logger.info("[FeishuDrive] 文件夹创建成功: %s -> %s", name, token)
                return token
            logger.warning("[FeishuDrive] 创建文件夹失败: %s, result=%s", name, result)
        except Exception as e:
            logger.error("[FeishuDrive] 创建文件夹异常: %s", e)
        return None

    # ================================================================
    #  内存缓存 helpers
    # ================================================================

    def _get_mem_cache(self, path: str):
        """获取内存缓存的内容"""
        cached = self._mem_cache.get(path)
        if cached and time.time() < cached.get("expire", 0):
            return cached["data"], True
        return None, False

    def _put_mem_cache(self, path: str, data):
        """设置内存缓存"""
        self._mem_cache[path] = {"data": data, "expire": time.time() + self._mem_cache_ttl}

    def _invalidate_mem_cache(self, path: str):
        """清除内存缓存"""
        self._mem_cache.pop(path, None)

    # ================================================================
    #  文件上传/下载核心方法
    # ================================================================

    def _upload_file(self, folder_token: str, file_name: str, content: bytes,
                     existing_file_token: str | None = None, _retries: int = 3) -> str | None:
        """上传文件到飞书云空间（新建或覆盖）。

        飞书不支持直接覆盖，如果文件已存在需要先删除再上传。

        Returns:
            成功返回 file_token，失败返回 None
        """
        token = self.get_token()
        if not token:
            return None

        # 如果文件已存在，先删除
        if existing_file_token:
            self._delete_file(existing_file_token)

        for attempt in range(1, _retries + 1):
            try:
                url = f"{_API_BASE}/drive/v1/files/upload_all"
                headers = {"Authorization": f"Bearer {token}"}
                # 使用 multipart/form-data 上传
                files_payload = {
                    "file_name": (None, file_name),
                    "parent_type": (None, "explorer"),
                    "parent_node": (None, folder_token),
                    "size": (None, str(len(content))),
                    "file": (file_name, content, "application/octet-stream"),
                }
                t0 = time.time()
                resp = _feishu_session.post(url, headers=headers, files=files_payload, timeout=30)
                elapsed = time.time() - t0
                result = resp.json()

                if result.get("code") == 0:
                    file_token = result.get("data", {}).get("file_token")
                    logger.info("[FeishuDrive] 上传成功 %s: token=%s (%.1fs)", file_name, file_token, elapsed)
                    return file_token
                else:
                    logger.warning("[FeishuDrive] 上传失败(第%d次) %s: code=%s, msg=%s (%.1fs)",
                                   attempt, file_name, result.get("code"), result.get("msg"), elapsed)
                    # token 过期，清缓存重试
                    if result.get("code") in (99991663, 99991664):
                        self._token_cache = {"token": None, "expire_time": 0}
                        token = self.get_token()
                        if token:
                            headers = {"Authorization": f"Bearer {token}"}
                            continue
                    if attempt < _retries:
                        time.sleep(2 * attempt)
                        continue
                    return None
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                logger.warning("[FeishuDrive] 上传超时(第%d次) %s: %.1fs", attempt, file_name, time.time() - t0)
                if attempt < _retries:
                    time.sleep(2 * attempt)
                    continue
                return None
            except Exception as e:
                logger.error("[FeishuDrive] 上传异常 %s: %s", file_name, e)
                return None
        return None

    def _download_file(self, file_token: str, _retries: int = 3) -> bytes | None:
        """从飞书云空间下载文件内容"""
        for attempt in range(1, _retries + 1):
            token = self.get_token()
            if not token:
                return None

            url = f"{_API_BASE}/drive/v1/files/{file_token}/download"
            headers = {"Authorization": f"Bearer {token}"}
            try:
                t0 = time.time()
                resp = _feishu_session.get(url, headers=headers, timeout=(5, 30))
                elapsed = time.time() - t0

                if resp.status_code == 200:
                    logger.info("[FeishuDrive] 下载成功: token=%s, size=%d (%.1fs)",
                                file_token, len(resp.content), elapsed)
                    return resp.content
                elif resp.status_code == 404:
                    logger.warning("[FeishuDrive] 文件不存在: token=%s", file_token)
                    return None
                else:
                    logger.warning("[FeishuDrive] 下载失败(第%d次): token=%s, status=%s (%.1fs)",
                                   attempt, file_token, resp.status_code, elapsed)
                    # 尝试解析错误码，处理 token 过期
                    try:
                        err = resp.json()
                        if err.get("code") in (99991663, 99991664):
                            self._token_cache = {"token": None, "expire_time": 0}
                    except Exception:
                        pass
                    if attempt < _retries:
                        time.sleep(2 * attempt)
                        continue
                    return None
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                logger.warning("[FeishuDrive] 下载超时(第%d次): token=%s, %.1fs",
                               attempt, file_token, time.time() - t0)
                if attempt < _retries:
                    time.sleep(2 * attempt)
                    continue
                return None
            except Exception as e:
                logger.error("[FeishuDrive] 下载异常: token=%s, %s", file_token, e)
                return None
        return None

    def _delete_file(self, file_token: str) -> bool:
        """删除飞书云空间中的文件"""
        headers = self._auth_headers()
        if not headers:
            return False
        url = f"{_API_BASE}/drive/v1/files/{file_token}"
        params = {"type": "file"}
        try:
            resp = _feishu_session.delete(url, headers=headers, params=params, timeout=10)
            result = resp.json()
            if result.get("code") == 0:
                logger.debug("[FeishuDrive] 文件已删除: token=%s", file_token)
                return True
            logger.warning("[FeishuDrive] 删除失败: token=%s, result=%s", file_token, result)
        except Exception as e:
            logger.warning("[FeishuDrive] 删除异常: token=%s, %s", file_token, e)
        return False

    # ================================================================
    #  StorageBackend 接口实现
    # ================================================================

    def read_text(self, file_path: str, _retries: int = 3) -> str | None:
        """读取文本文件。文件不存在返回空字符串，失败返回 None。"""
        # 内存缓存
        data, hit = self._get_mem_cache(file_path)
        if hit:
            return data

        # 解析 file_token
        file_token = self._resolve_file_token(file_path)
        if not file_token:
            # 文件不存在
            return ""

        # 下载
        content_bytes = self._download_file(file_token, _retries)
        if content_bytes is None:
            # 下载失败（可能是缓存的 token 已失效，清缓存后重试一次）
            self._invalidate_path_cache(file_path)
            file_token = self._resolve_file_token(file_path)
            if not file_token:
                return ""
            content_bytes = self._download_file(file_token, _retries)
            if content_bytes is None:
                return None

        try:
            text = content_bytes.decode("utf-8")
        except UnicodeDecodeError:
            logger.error("[FeishuDrive] 文件非 UTF-8 编码: %s", file_path)
            return None

        self._put_mem_cache(file_path, text)
        return text

    def write_text(self, file_path: str, content: str, _retries: int = 3) -> bool:
        """写入文本文件（覆盖）。自动创建中间文件夹。返回 True/False。"""
        # 拆分目录和文件名
        parts = file_path.rsplit("/", 1)
        if len(parts) == 2:
            folder_path, file_name = parts
        else:
            folder_path, file_name = "", parts[0]

        # 解析（或创建）文件夹
        folder_token = self._resolve_folder_token(folder_path, auto_create=True)
        if not folder_token:
            logger.error("[FeishuDrive] 无法解析文件夹: %s", folder_path)
            return False

        # 查找已有文件
        existing_token = self._resolve_file_token(file_path)

        # 上传
        content_bytes = content.encode("utf-8")
        new_token = self._upload_file(folder_token, file_name, content_bytes,
                                      existing_file_token=existing_token, _retries=_retries)
        if new_token:
            self._put_cached_token(file_path, new_token, "file")
            self._put_mem_cache(file_path, content)
            return True
        return False

    def read_json(self, file_path: str) -> dict | None:
        """读取 JSON 文件。文件不存在返回空 dict，失败返回 None。"""
        text = self.read_text(file_path)
        if text is None:
            return None
        if not text.strip():
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            logger.error("[FeishuDrive] JSON 解析失败 %s: %s", file_path, e)
            return None

    def write_json(self, file_path: str, data: dict) -> bool:
        """写入 JSON 文件。"""
        content = json.dumps(data, ensure_ascii=False, indent=2)
        return self.write_text(file_path, content)

    def append_to_section(self, file_path: str, section_header: str, content: str) -> bool:
        """追加内容到文件的指定 section（## 开头）。"""
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

    def append_to_quick_notes(self, file_path: str, message: str) -> bool:
        """追加一条笔记到 Quick-Notes（带去重）。"""
        from datetime import datetime

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
                    logger.info("[FeishuDrive] Quick-Notes 内容重复，跳过: %s...", message[:30])
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

    def upload_binary(self, file_path: str, data: bytes,
                      content_type: str = "application/octet-stream") -> bool:
        """上传二进制文件。"""
        parts = file_path.rsplit("/", 1)
        if len(parts) == 2:
            folder_path, file_name = parts
        else:
            folder_path, file_name = "", parts[0]

        folder_token = self._resolve_folder_token(folder_path, auto_create=True)
        if not folder_token:
            return False

        existing_token = self._resolve_file_token(file_path)
        new_token = self._upload_file(folder_token, file_name, data,
                                      existing_file_token=existing_token)
        if new_token:
            self._put_cached_token(file_path, new_token, "file")
            return True
        return False

    def download_binary(self, file_path: str, _retries: int = 3) -> bytes | None:
        """下载二进制文件。文件不存在返回 None。"""
        file_token = self._resolve_file_token(file_path)
        if not file_token:
            return None

        data = self._download_file(file_token, _retries)
        if data is None:
            # 缓存失效重试
            self._invalidate_path_cache(file_path)
            file_token = self._resolve_file_token(file_path)
            if not file_token:
                return None
            data = self._download_file(file_token, _retries)
        return data

    def list_children(self, folder_path: str, _retries: int = 3) -> list | None:
        """列出文件夹下的子项。返回格式与 Local/OneDrive 一致。"""
        folder_token = self._resolve_folder_token(folder_path)
        if not folder_token:
            return []

        headers = self._auth_headers()
        if not headers:
            return None

        url = f"{_API_BASE}/drive/v1/files"
        params = {"folder_token": folder_token, "page_size": 200}

        for attempt in range(1, _retries + 1):
            try:
                t0 = time.time()
                resp = _feishu_session.get(url, headers=headers, params=params, timeout=10)
                elapsed = time.time() - t0
                result = resp.json()

                if result.get("code") == 0:
                    files = result.get("data", {}).get("files", [])
                    items = []
                    for f in files:
                        item = {"name": f.get("name", "")}
                        if f.get("type") == "folder":
                            item["folder"] = {"childCount": 0}
                            # 缓存子文件夹 token
                            child_path = f"{folder_path}/{f['name']}" if folder_path else f["name"]
                            self._put_cached_token(child_path, f.get("token", ""), "folder")
                        else:
                            item["file"] = {"mimeType": f.get("mime_type", "application/octet-stream")}
                            item["size"] = f.get("size", 0)
                            # 缓存文件 token
                            child_path = f"{folder_path}/{f['name']}" if folder_path else f["name"]
                            self._put_cached_token(child_path, f.get("token", ""), "file")
                        items.append(item)
                    logger.info("[FeishuDrive] 列目录成功 %s: %d项 (%.1fs)", folder_path, len(items), elapsed)
                    return items
                else:
                    logger.warning("[FeishuDrive] 列目录失败(第%d次) %s: code=%s (%.1fs)",
                                   attempt, folder_path, result.get("code"), elapsed)
                    if attempt < _retries:
                        time.sleep(2 * attempt)
                        continue
                    return None
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                logger.warning("[FeishuDrive] 列目录超时(第%d次) %s: %.1fs",
                               attempt, folder_path, time.time() - t0)
                if attempt < _retries:
                    time.sleep(2 * attempt)
                    continue
                return None
            except Exception as e:
                logger.error("[FeishuDrive] 列目录异常 %s: %s", folder_path, e)
                return None
        return None
