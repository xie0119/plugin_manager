from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Dict, List

from core.logging_config import get_plugin_logger

EventHandler = Callable[[str, Dict[str, Any]], None]


@dataclass
class _Subscriber:
    plugin_name: str
    callback: EventHandler


class EventBus:
    """简单但工程可用的事件总线。

    - 支持按事件名订阅/取消订阅
    - 线程安全
    - 事件数据为 dict，附带 source_plugin 信息
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subscribers: Dict[str, List[_Subscriber]] = defaultdict(list)
        self._logger = get_plugin_logger("EventBus")

    def subscribe(self, event: str, plugin_name: str, callback: EventHandler) -> None:
        with self._lock:
            self._subscribers[event].append(_Subscriber(plugin_name, callback))
            self._logger.debug("Plugin %s subscribed to event '%s'", plugin_name, event)

    def unsubscribe(self, event: str, callback: EventHandler) -> None:
        with self._lock:
            subs = self._subscribers.get(event)
            if not subs:
                return
            self._subscribers[event] = [s for s in subs if s.callback is not callback]

    def publish(
        self,
        event: str,
        data: Dict[str, Any] | None = None,
        source_plugin: str | None = None,
    ) -> None:
        with self._lock:
            subs = list(self._subscribers.get(event, []))

        payload = dict(data or {})
        payload.setdefault("source_plugin", source_plugin)

        for sub in subs:
            try:
                sub.callback(event, payload)
            except Exception as exc:  # 插件异常不能影响主程序
                self._logger.error(
                    "Error dispatching event '%s' to plugin '%s': %s",
                    event,
                    sub.plugin_name,
                    exc,
                )


_event_bus = EventBus()


def get_event_bus() -> EventBus:
    return _event_bus


__all__ = ["EventBus", "get_event_bus"]

