# Python 插件化工具箱平台

本项目是一个基于 **PySide6** 的工程级插件化 GUI 框架，用于将大量 Python 脚本快速封装为可视化工具插件，统一在一个桌面应用中进行管理、搜索和使用。

## 主要特性

- 插件目录扫描与动态加载（支持子目录）
- 插件热重载（`importlib.reload`）
- 插件生命周期管理：`on_load` / `on_unload`
- 事件总线（EventBus）支持插件间解耦通信
- 插件独立配置（JSON）及统一配置管理接口
- 基于 `logging` 的工程级日志系统（包含插件名称前缀）
- 类微信布局的主界面：左侧插件列表 + 右侧插件内容区
- 自定义插件列表项（图标 + 名称 + 描述），支持 hover / 选中高亮
- 插件搜索过滤、启用/禁用、分类和图标支持

## 运行方式

```bash
pip install -r requirements.txt
python main.py
```

### PyInstaller 单文件打包（`pyinstaller -F main.py`）

- 单文件 exe 运行时，代码会解压到**临时目录**（`sys._MEIPASS`），不能用 `Path(__file__)` 当作「exe 所在目录」。
- 本项目已统一通过 **`core.runtime_paths.get_app_base_dir()`** 解析根目录：
  - **开发**：项目根目录
  - **打包后**：`main.exe` 所在目录
- 因此请将 **`plugins/`** 放在 **`main.exe` 同目录**（与 `configs/`、`logs/` 一致），即可被扫描加载。
- **目录型插件**（`plugins/某目录/plugin.py`）：加载器会把**插件根目录**加入 `sys.path`，并注册包名，便于同目录下的 `helper.py` 等被 `import`（与单文件插件仅依赖 `plugins/` 在 `sys.path` 不同）。
- 若插件仍不加载，请确认：
  1. 目录名为 `plugins`（小写），且与 exe 同级；
  2. 插件 `.py` 或 `plugins/xxx/plugin.py` 符合 `Plugin` 规范；
  3. 查看 exe 同目录下 `logs/app.log` 中的 `[ERROR] plugin.PluginManager` 行。
  4. 若报编码或非法字符相关错误：插件 `.py` 须为 **UTF-8** 文本（勿用 UTF-16）。请用编辑器另存为 UTF-8，或从仓库 `plugins/` 覆盖一份。
- **插件里若 `import bluetooth.xxx`（主程序包）**：PyInstaller **不会**从外部插件文件做依赖分析，`xxx` 子模块可能未打进 exe。`main.py` 已对 `bluetooth.protocols.cyfit_watch_profile` 做显式导入；若你新增其它 `bluetooth` 子模块，请在 `main.py` 同样增加一行 `import`，或在打包命令中加 `--hidden-import 模块名`。

### Qt BLE 连接报 `Error.ConnectionError`

在 **Windows** 上仅用 MAC 直连容易失败。本项目在 **未设置环境变量时，Windows 默认启用「先 LE 扫描再连接」**（`BLE_CONNECT_USE_DISCOVERED` 隐式为开）。

- **`BLE_CONNECT_USE_DISCOVERED=0`**：强制 **仅 MAC 直连**（与旧行为一致，仍可能 ConnectionError）。
- **`BLE_CONNECT_USE_DISCOVERED=1`**：强制 **扫描后连接**（非 Windows 也可用）。
- **`BLE_SCAN_TIMEOUT_MS`**（默认 `12000`）：扫描超时（毫秒）。
- **`BLE_DEBUG_SCAN=1`**：打印扫描到的设备，核对 MAC / 是否在广播。

## 蓝牙后端选择

默认使用真实 BLE 后端（QtBluetooth）。

如需切回模拟后端，可设置环境变量：

`BLE_BACKEND=sim`

插件默认放置在 `plugins/` 目录中，符合约定的 `Plugin` 类会被自动识别和加载。

## 蓝牙公共服务（手表 / Central 场景）

- **`BluetoothService`**（`bluetooth/service.py`）：统一 `connect(mac, owner)`、`disconnect(mac, owner)`、`send(mac, data)`；按 MAC 连接池 + **owner 引用计数**，同一 MAC 多插件共享连接，仅最后一个 owner 释放时真正断开；可配置 `max_connections`。
- **`DeviceManager`**（`device_manager.py`）：订阅 `bluetooth.*` 事件，维护每设备状态快照；可对某 MAC 开启 `set_auto_reconnect`（非「owner 全部释放」类断开时定时重连）。
- **`EventBus`**：标准事件名见 `bluetooth/constants.py`：`bluetooth.connected`、`bluetooth.disconnected`、`bluetooth.data_received`、`bluetooth.error`、`bluetooth.state_changed`。
- **`ServiceManager`**（`service_manager.py`）：在 `QApplication` 创建后 `init_core(app)` 注册服务；插件 `__init__` 若包含形参 **`services`**，将自动注入 `get_service_manager()`。
- **`PluginAPI`**（`core/plugin_api.py`）：通用稳定接口，避免插件写 `from bluetooth.service import ...`。在 `__init__` 中声明形参 **`plugin_api`**（或别名 **`api`**）即可注入，例如 `plugin_api.normalize_mac(s)`、`plugin_api.normalize_uuid(s)`；连接/收发仍通过 **`services.bluetooth`**。
- **底层实现**：默认使用 **`SimulatedBluetoothBackend`**（无真机也可联调）；真实 BLE 可后续实现 `BluetoothBackend` 子类（如 Qt BLE / bleak）并在构造 `BluetoothService` 时注入。

演示插件：`plugins/ble_watch_demo_plugin.py`。

### 预留扩展

- 可在各插件目录增加 **`plugin.json`** 描述元数据，由 `ServiceManager.register` 或后续加载器解析（当前已留 `register`/`get` 接口）。
- 插件 **zip 分发**：解压到 `plugins/<id>/` 后沿用目录插件规则加载即可。

