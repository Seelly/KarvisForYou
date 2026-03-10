# -*- coding: utf-8 -*-
"""
KarvisForAll 存储后端抽象基类
所有存储后端（Local / OneDrive / FeishuDrive）统一实现此协议。
"""
from abc import ABC, abstractmethod
from typing import Any


class StorageBackend(ABC):
    """存储后端抽象协议。

    所有具体后端必须继承此类并实现全部抽象方法。
    上层代码统一通过 ctx.IO.read_text(path) 等方式调用，无需感知具体实现。
    """

    @abstractmethod
    def get_token(self) -> str | None:
        """获取访问令牌（本地模式返回 'local'，云端模式返回真实 token）"""
        ...

    # ---- 文本文件 ----

    @abstractmethod
    def read_text(self, file_path: str, _retries: int = 3) -> str | None:
        """读取文本文件。文件不存在返回空字符串，失败返回 None。"""
        ...

    @abstractmethod
    def write_text(self, file_path: str, content: str, _retries: int = 3) -> bool:
        """写入文本文件（覆盖）。返回 True/False。"""
        ...

    # ---- JSON 文件 ----

    @abstractmethod
    def read_json(self, file_path: str) -> dict | None:
        """读取 JSON 文件。文件不存在返回空 dict，失败返回 None。"""
        ...

    @abstractmethod
    def write_json(self, file_path: str, data: dict) -> bool:
        """写入 JSON 文件。返回 True/False。"""
        ...

    # ---- 内容追加 ----

    @abstractmethod
    def append_to_section(self, file_path: str, section_header: str, content: str) -> bool:
        """追加内容到文件的指定 section（## 开头）。返回 True/False。"""
        ...

    @abstractmethod
    def append_to_quick_notes(self, file_path: str, message: str) -> bool:
        """追加一条笔记到 Quick-Notes（带去重）。返回 True/False。"""
        ...

    # ---- 二进制文件 ----

    @abstractmethod
    def upload_binary(self, file_path: str, data: bytes, content_type: str = "application/octet-stream") -> bool:
        """上传（保存）二进制文件。返回 True/False。"""
        ...

    @abstractmethod
    def download_binary(self, file_path: str, _retries: int = 3) -> bytes | None:
        """下载（读取）二进制文件。文件不存在返回 None。"""
        ...

    # ---- 目录操作 ----

    @abstractmethod
    def list_children(self, folder_path: str, _retries: int = 3) -> list | None:
        """列出文件夹下的子项。返回 list[dict]，失败返回 None。"""
        ...
