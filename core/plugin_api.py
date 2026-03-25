"""插件通用 API：由主程序实现并注入，避免插件 ``from bluetooth...`` / ``import bluetooth``。

设计原则：
- 纯工具函数（如 MAC/UUID 规范化）委托 ``bluetooth.service``，与 ``ServiceManager`` 解耦。
- 后续可在此增加与业务无关的稳定接口；需要连接/发送等仍用注入的 ``services.bluetooth``。
"""

from __future__ import annotations


class PluginAPI:
    """单例式能力集合，通过 ``PluginManager`` 注入插件 ``__init__(..., plugin_api=...)``。"""

    __slots__ = ()

    def normalize_mac(self, mac: str) -> str:
        """统一 MAC 为 ``AA:BB:CC:DD:EE:FF`` 大写格式。"""
        from bluetooth.service import normalize_mac as _fn

        return _fn(mac)

    def normalize_uuid(self, uuid: str) -> str:
        """统一 UUID 字符串（小写、去花括号等）。"""
        from bluetooth.service import normalize_uuid as _fn

        return _fn(uuid)


# 模块级单例，供 PluginManager 持有并注入
_plugin_api = PluginAPI()


def get_plugin_api() -> PluginAPI:
    """与注入等价；仅当无法改 ``__init__`` 签名时使用（不推荐）。"""
    return _plugin_api


__all__ = ["PluginAPI", "get_plugin_api"]
