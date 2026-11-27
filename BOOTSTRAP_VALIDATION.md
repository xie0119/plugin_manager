# 启动器 2.1.1 验证记录

安装包：dist/PluginManager-2.1.1-Setup-x64.exe
大小：43017365 字节
SHA256：b2b519822530675959edbad910eb3c4618877bc4ee4167cca7b26b316c2198ef

已验证：

- 8 项生命周期测试通过：启动失败、正常完成、启动超时、取消、更新超时、旧配置兼容和代理隔离。
- 使用真实仓库与 PySide6，创建主窗口后启动器 GUI 自动退出。
- 启动器退出时主程序进程仍存活，且退出后仍能继续写 application.log。
- 显式使用不可达代理模拟自动更新失败；成功回退本地并完成上述界面启动。
- 移除系统 Python/Git 的 PATH 后，用内置环境完成真实仓库克隆和依赖安装。
- 安装包解出的 4727 个文件逐项 SHA256 一致，解包后的启动器环境检查通过。
- Python 语法检查和 git diff --check 通过。

验证仓库提交：2b06d2d4cd74a771df3150bc9bdee716d2cdb643

自动更新默认只等待 5 秒。GitHub 本身不可达时，仍无法获取新版本；此时本地代码和依赖完整即可继续使用。proxy_url 默认留空，支持现有环境变量及 Windows 手动代理，不强制用户配置代理。

应用窗口就绪后，业务输出写入 logs/application.log；准备过程写入 logs/start.log。应用通过单独的 application.lock 保持单实例。

未执行系统级安装/卸载操作；文件释放通过实际安装包解包核验。机器可读记录见 dist/smoke-report.json、dist/verification-report.json、dist/installer-info.json。
