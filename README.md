# PluginManager

一个轻量的 PySide6 桌面工具启动器。保留原有浅灰背景、白色侧栏、蓝色选中态和左右分栏界面；宿主只负责发现插件、展示列表和嵌入界面。

插件是独立的工具，不依赖启动器的业务服务。当前示例支持独立运行和嵌入运行。

## 运行

需要 Python 3.10+。

```powershell
python -m pip install -r requirements.txt
python main.py
```

也可使用 `python -m launcher`。指定外部插件目录：

```powershell
python main.py --plugins-dir D:\my-tools
```

窗口初次打开只读取描述文件；点击左侧工具才导入其代码。F5 刷新插件，Ctrl+F 搜索。刷新会关闭已加载的插件界面，再扫描并恢复当前选择；尚未提交的界面状态由插件自行处理。

## 插件结构

每个插件使用一个独立目录：

```text
plugins/
  my_tool/
    config.json
    plugin.py
    helpers.py          # 可选，插件自己的业务模块
    resources/          # 可选
    requirements.txt    # 可选，插件自己声明依赖
    settings.json       # 可选，运行设置，由插件自行管理
```

启动器只扫描 `plugins/` 的直接子目录，不执行没有描述文件的脚本，不自动安装依赖。

### config.json

```json
{
  "id": "my_tool",
  "name": "我的工具",
  "description": "工具用途",
  "entry": "plugin.py",
  "version": "1.0.0",
  "category": "常用工具",
  "icon": "",
  "enabled": true
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| id | 是 | 唯一标识，1–64 位，以字母或数字开头，可含点、短横线和下划线 |
| name | 是 | 左侧显示名称 |
| entry | 是 | 插件目录内的 Python 入口文件，相对路径 |
| description | 否 | 描述，可搜索 |
| version | 否 | 展示用版本号 |
| category | 否 | 搜索关键词 |
| icon | 否 | 插件目录内的图标相对路径，留空则不显示图标 |
| enabled | 否 | 布尔值，默认 true；false 时不加载 |

缺失入口、错误字段、重复 ID 等问题会显示在启动器中，不影响其他有效插件。

### 最小入口

```python
import sys
from PySide6.QtWidgets import QApplication, QLabel


def create_widget(parent=None):
    return QLabel("我的工具", parent)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = create_widget()
    window.setWindowTitle("我的工具")
    window.resize(800, 600)
    window.show()
    sys.exit(app.exec())
```

启动器唯一要求是 `create_widget(parent=None)` 返回有效的 `QWidget`。导入时不要创建 `QApplication`、打开窗口或启动业务任务。独立启动逻辑放在 `if __name__ == "__main__"` 内。

插件资源以 `Path(__file__).resolve().parent` 为基准，不能依赖当前工作目录，也不要导入 `launcher`。

嵌入时，每个插件拥有独立的 Python 包命名空间，可用 `from .helpers import ...` 导入自己的辅助模块，避免插件间同名模块冲突。直接执行 `plugin.py` 时没有包上下文；复杂的多文件工具可以使用自己的包入口或提供独立启动脚本。示例插件不依赖相对导入，可以直接运行：

```powershell
python plugins/folder_text_tool/plugin.py
```

### 界面风格

启动器通过 `QApplication.setStyleSheet()` 应用 `launcher/ui/style.qss`。嵌入的标准 Qt 控件会继承该样式。插件无需加载主题；局部 QSS 和自绘控件由插件自行负责。

独立运行时插件管理自己的主题，示例会在存在本地 `style.qss` 时加载它。

### 任务与资源

嵌入界面在 GUI 主线程创建。文件批处理、网络请求等耗时任务由插件自行放入工作线程，界面更新通过 Qt 信号回到主线程。

刷新和退出会对已加载的插件调用 `QWidget.close()`，然后释放界面并清理其包及子模块。插件应在自己的 `closeEvent()` 中停止定时器、结束工作任务、关闭连接，接受关闭事件，并且不要启用 `WA_QuitOnClose`。启动器不能替插件安全终止未清理的线程。

`config.json` 只用于描述插件。业务数据、用户设置、日志和依赖全部由插件管理；示例将输入保存在自己的 `settings.json`，不会覆盖描述文件。

### 运行边界

嵌入插件与宿主共用一个进程和 Python 环境。命名空间隔离避免同名辅助模块冲突，不提供进程、权限或第三方依赖版本隔离。加载可信的本地插件；不同插件的第三方依赖需彼此兼容。

## 开发与验证

```powershell
python -m pip install -e ".[dev]"
python -m ruff check .
python -m unittest discover -s tests -v
```

测试使用 Qt offscreen，覆盖描述文件校验、延迟导入、模块隔离、失败重试、刷新清理、样式继承、示例功能和独立运行。

项目代码分为：

| 位置 | 职责 |
| --- | --- |
| launcher/manifest.py | 描述文件校验与目录扫描，无 Qt 依赖 |
| launcher/plugin_manager.py | 入口导入、控件缓存、模块和界面清理 |
| launcher/ui/ | 左右分栏界面、搜索和主题 |
| launcher/app.py | QApplication 创建与启动参数 |
| plugins/folder_text_tool/ | 无宿主依赖的完整示例 |
| tests/ | 回归测试 |

不再包含共享蓝牙、设备管理、事件总线、配置服务、运行时依赖安装或自定义二进制插件包。

## Windows 安装包

现在只编译 `start.py` 启动器，使用 PyInstaller onedir 目录版。`main.py` 和业务界面不编译；安装包附带完整便携 Python 与 Git，安装后由启动器拉取仓库、安装依赖并执行 main.py。

```powershell
python -m pip install "pyinstaller>=6,<7" "zstandard>=0.22,<1"
python tools/build_installer.py
```

也可双击 `build.bat`。输出 `dist/PluginManager-2.1.1-Setup-x64.exe`；目录版为 `dist/PluginManager/`。最终用户无需预装 Python、Git，也无需运行下载脚本。

源码开发仍使用 `python main.py`。完整目录、仓库配置、更新与卸载说明见 [BOOTSTRAP.md](BOOTSTRAP.md)。
