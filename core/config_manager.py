from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from core.logging_config import get_plugin_logger
from core.runtime_paths import get_app_base_dir


class ConfigManager:
    """统一的配置读写接口。

    每个插件一个独立 JSON 文件：
    configs/<plugin_name>.json
    """

    def __init__(self, base_dir: Path | None = None) -> None:
        # configs/ 放在应用根目录；打包后位于 exe 同目录（与 plugins/ 一致）
        self._base_dir = base_dir or get_app_base_dir()
        self._config_dir = self._base_dir / "configs"
        self._config_dir.mkdir(exist_ok=True)
        self._logger = get_plugin_logger("ConfigManager")
        # 每个插件可注册一个默认配置文件路径（通常为插件目录下的 config.json）
        self._default_paths: Dict[str, Path] = {}

    def _file_for(self, plugin_name: str) -> Path:
        return self._config_dir / f"{plugin_name}.json"

    def register_default_config(self, plugin_name: str, default_path: Path) -> None:
        """注册插件自带的默认配置文件路径（如 plugins/foo/config.json）。"""

        self._default_paths[plugin_name] = default_path

    def load(self, plugin_name: str) -> Dict[str, Any]:
        path = self._file_for(plugin_name)
        if not path.exists():
            # 若无独立运行时配置，则尝试使用插件自身目录下的默认 config.json
            default_path: Optional[Path] = self._default_paths.get(plugin_name)
            if default_path and default_path.exists():
                try:
                    with default_path.open("r", encoding="utf-8") as f:
                        data = json.load(f)
                    # 将默认配置写入独立 configs 目录，之后读写都走统一路径
                    self.save(plugin_name, data)
                    return data
                except Exception as exc:
                    self._logger.error(
                        "Failed to load default config for plugin '%s' from '%s': %s",
                        plugin_name,
                        default_path,
                        exc,
                    )
                    return {}
            return {}

        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            self._logger.error(
                "Failed to load config for plugin '%s': %s", plugin_name, exc
            )
            return {}

    def save(self, plugin_name: str, config: Dict[str, Any]) -> None:
        path = self._file_for(plugin_name)
        try:
            with path.open("w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            self._logger.error(
                "Failed to save config for plugin '%s': %s", plugin_name, exc
            )


_config_manager = ConfigManager()


def get_config_manager() -> ConfigManager:
    return _config_manager


__all__ = ["ConfigManager", "get_config_manager"]

