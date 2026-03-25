"""
Core layer: 基础设施/容器层。

该层包含事件总线、插件管理器、服务注册与注入、配置与日志等公共能力，
供上层 GUI 与插件层复用。
"""

from .event_bus import EventBus, get_event_bus
from .plugin_manager import PluginManager, PluginMeta
from .service_manager import ServiceManager, get_service_manager, reset_service_manager_for_tests
from .config_manager import ConfigManager, get_config_manager
from .logging_config import setup_logging, get_plugin_logger
from .plugin_api import PluginAPI, get_plugin_api

__all__ = [
    "EventBus",
    "get_event_bus",
    "PluginManager",
    "PluginMeta",
    "ServiceManager",
    "get_service_manager",
    "reset_service_manager_for_tests",
    "ConfigManager",
    "get_config_manager",
    "setup_logging",
    "get_plugin_logger",
    "PluginAPI",
    "get_plugin_api",
]

