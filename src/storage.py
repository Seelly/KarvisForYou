# -*- coding: utf-8 -*-
"""
KarvisForAll 统一存储接口
支持 Local / OneDrive / FeishuDrive 三种后端，通过工厂方法按用户配置创建。
所有后端继承 StorageBackend 抽象基类，提供统一接口。
"""
from local_io import LocalFileIO
from log_utils import get_logger
from storage_base import StorageBackend

logger = get_logger(__name__)


def create_storage(storage_mode: str, backend_config: dict = None) -> StorageBackend:
    """工厂方法：根据模式创建存储实例。

    Args:
        storage_mode: "local" | "onedrive" | "feishu"
        backend_config: 后端配置字典（onedrive / feishu 模式需要）
            onedrive: {client_id, client_secret, refresh_token, obsidian_base}
            feishu: {app_id, app_secret, root_folder_token}

    Returns:
        StorageBackend 实例
    """
    if storage_mode == "onedrive":
        if not backend_config:
            logger.warning("[Storage] onedrive 模式缺少配置，回退到 local")
            return LocalFileIO()
        from onedrive_io import OneDriveIO
        return OneDriveIO(backend_config)

    elif storage_mode == "feishu":
        if not backend_config or not backend_config.get("root_folder_token"):
            logger.warning("[Storage] feishu 模式缺少配置（需要 root_folder_token），回退到 local")
            return LocalFileIO()
        from feishu_drive_io import FeishuDriveIO
        return FeishuDriveIO(backend_config)

    else:
        if storage_mode and storage_mode != "local":
            logger.warning("[Storage] 未知的 storage_mode '%s'，回退到 local", storage_mode)
        return LocalFileIO()
