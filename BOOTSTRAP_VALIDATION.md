# 安装式启动器验证

已在当前 Windows 工程生成 `dist/PluginManager-2.1.0-Setup-x64.exe`，42,830,720 字节。

SHA256：`98b5e1e7cff4b6b58bb8874e1bab93f8a618d557d4c43a004597296e16c79154`。

验证结果：

| 检查 | 结果 |
| --- | --- |
| 只编译 start.py，PyInstaller onedir，不编译业务界面 | 通过 |
| 9 项启动器单元测试：路径、配置、环境隔离、并发锁、取消、失败处理等 | 通过 |
| 移除测试进程系统 Python/Git 的 PATH，运行已编译启动器 | 通过 |
| 便携 Python / SSL / venv / ensurepip / Tk / Git 校验 | 通过 |
| 实际克隆指定 GitHub 仓库 | 通过 |
| 在独立 .venv 安装真实 requirements.txt | 通过 |
| 重复启动、复用独立环境 | 通过 |
| 实际 main.py 的 Qt 主窗口离屏创建、事件循环、正常退出 | 通过，退出码 0 |
| 实际安装包解包，4722 个内置文件逐项 SHA256 核对 | 通过 |
| 从安装包解出的启动器检查其便携 Python/Git | 通过 |
| 从固定 SHA256 的 NSIS x64 包重新解压构建编译器 | 通过 |

目标仓库验证提交：`5f7999911e7b165a3ac9edf34246af5a958ef420`。

机器可读记录位于 `dist/smoke-report.json`、`dist/verification-report.json` 和 `dist/installer-info.json`。离屏测试使用独立目录中的临时探针调用真实 main.py，并在主窗口创建后退出；没有修改业务入口源码。

未执行系统级安装、桌面快捷方式注册或卸载测试。实际安装包的文件释放结果通过解包逐项核验，启动器业务流程在 Windows 上实测通过。
