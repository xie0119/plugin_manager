# 蓝牙批量 OTA 插件

来自 `D:\actions\custom_tool\BtBatchOtaTool`，原工具版本 `2026.11.25.1`。工具已复制到本插件目录，不依赖原路径或原来的 EXE。

在 PluginManager 左侧选择“蓝牙批量 OTA”，界面会嵌入右侧。Qt 界面继承宿主主题；业务协议、调度、传输、版本核验等复用原工具代码。两个内部包使用插件相对导入，不修改宿主的 `sys.path`，也不注册全局同名业务模块。

支持原工具的手表 OTA、单独 modem 升级、手表后接 modem 升级、1～20 台并发、同版本跳过、ShineFit UUID、升级完成亮屏长震、回连核验、超时、结果与日志查看及 JSON 记录导出。未连接真实设备进行刷写验证。

## 运行依赖

项目根目录的 `requirements.txt` 已包含本插件依赖。启动器拉取含本插件的仓库后，会一并安装这些依赖；源码开发也可执行：

```powershell
python -m pip install -r requirements.txt
python main.py
```

Bleak 固定为原工具环境使用的 `0.22.3`，与蓝牙资源释放适配代码保持一致。不需要 PyInstaller 作为运行依赖。

插件也可以独立打开 Qt 界面：

```powershell
python plugins/bt_batch_ota_tool/plugin.py
```

## 运行任务时刷新和退出

切换到其他插件不会中断当前任务。点击“停止”后停止接收新设备，等待正在处理的设备完成。

F5 刷新期间如仍有任务，会请求停止并保留现有插件界面，状态栏提示等待，任务完成后可再次刷新。关闭主窗口时同样请求停止，保持窗口直至工作线程结束后自动退出，不强制中断设备刷写。

“导出记录”写入用户选择的位置，包含设备信息、各设备结果、完整日志和核验诊断。显示日志最多保留 3000 行，导出保留本轮全部日志。

原 `btbatchtool/gui.py` 为保留的 Tkinter 界面源文件，嵌入运行使用 `plugin.py` 中的 Qt 界面。
