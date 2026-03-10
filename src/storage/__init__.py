# -*- coding: utf-8 -*-
"""
存储子包 — 提供统一的存储后端抽象和工厂方法。

用法：
    from storage import create_storage, StorageBackend
"""
from storage.base import StorageBackend
from storage.factory import create_storage

__all__ = ["StorageBackend", "create_storage"]
