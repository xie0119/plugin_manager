import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from core.runtime_paths import get_app_base_dir


# logs/ 放在应用根目录；打包后位于 exe 同目录
LOG_DIR = get_app_base_dir() / "logs"
LOG_DIR.mkdir(exist_ok=True)


def setup_logging(level: int = logging.INFO) -> None:
    """初始化全局 logging 配置，主程序和插件共用。

    - 主日志文件：logs/app.log
    - 控制台输出简化格式
    """

    log_file = LOG_DIR / "app.log"

    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)

    # 避免重复添加 handler
    if not root.handlers:
        file_handler = RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
        root.addHandler(console_handler)


def get_plugin_logger(plugin_name: str) -> logging.Logger:
    """为插件返回带前缀的 logger。"""

    return logging.getLogger(f"plugin.{plugin_name}")


__all__ = ["setup_logging", "get_plugin_logger"]

