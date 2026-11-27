# 启动器 2.1.3 验证记录

安装包：dist/PluginManager-2.1.3-Setup-x64.exe
大小：43011482 字节
SHA256：5552ab8b05e02bdf6b9ba17820f2eb8593079a8539b04f049b56fe5c09c63288

- 16 项仓库重置、界面交互、启动生命周期和无控制台测试通过。
- “重置插件”按钮位于启动器 PluginManager 标题同行最右侧；准备中点击会先取消当前任务，再执行重置。
- 重置先克隆并验证新仓库，再将原仓库完整移入 repository-backups，最后切换新仓库。克隆失败保留原仓库，切换失败恢复备份。
- 使用编译后的启动器实际执行重置，验证本地新增文件保留在备份中，新仓库没有该文件。
- 从 PATH 移除系统 Python、Git 后，便携环境检查、真实仓库克隆、依赖安装及主程序窗口检查通过。
- PluginManager.exe 和运行主程序的 .venv/Scripts/pythonw.exe 均为 Windows GUI 子系统；实际主程序 GetConsoleWindow() 为 0，没有控制台窗口。
- 实测主程序就绪后启动器自动退出，main.py 继续运行并可继续写日志。
- 模拟自动更新网络不可达，仍可回退本地版本并完成主程序启动。
- 从最终安装包解出 4727 个文件，逐个 SHA256 校验通过，解出的启动器环境检查通过。
- 业务主程序保持源码，未编译 PySide6 业务界面。安装包和绿色版目录均已更新。

目标仓库提交：9293f2fd7a770713774f5c15a397851ed5ea5703

业务日志写入 logs/application.log；准备日志写入 logs/start.log。自动更新默认 5 秒超时，失败使用本地代码。

未执行系统级安装/卸载操作。机器可读验证结果位于 dist/smoke-report.json、dist/verification-report.json、dist/installer-info.json。
