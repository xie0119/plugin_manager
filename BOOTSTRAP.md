# 只编译启动器的安装包

`main.py` 继续作为业务入口；新增 `start.py` 负责检查环境、拉取仓库、安装依赖和运行入口。PyInstaller 只分析 `start.py`，使用 onedir 目录模式，不编译业务界面和插件，不加密或混淆源码。

安装后布局：

```text
安装目录/
  PluginManager.exe            start.py 编译的启动器
  _internal/                   启动器自己的 Python/Tk
  start.py                     明文源码
  launcher-config.json         仓库配置
  runtime/python/              便携 Python 3.12.10（pip / venv / Tk）
  runtime/git/                 MinGit 2.56.1
  repository/                 首次启动时克隆，包含 main.py
  .venv/                      首次启动时创建，安装业务依赖
  logs/start.log               日志
```

用户直接安装 `PluginManager-2.1.1-Setup-x64.exe`，不需要安装 Python、Git 或执行下载脚本，也不依赖额外 .NET 安装。安装时不下载 Python/Git，不修改系统 PATH。打开启动器后联网克隆仓库、安装依赖，再使用 `.venv/Scripts/python.exe` 运行 `repository/main.py`，工作目录设为仓库根目录。

默认仓库为 `https://github.com/xie0119/plugin_manager.git`，分支跟随仓库 HEAD。可修改 `launcher-config.json`。本版不嵌入账号密码、不提供登录输入，沿用现有 Git 配置和凭据。

更新仅允许快进合并，有本地修改时保留修改并跳过更新；更新网络失败时运行现有版本。依赖安装失败时不运行 main.py。准备阶段关闭启动器会取消当前启动。主窗口就绪后启动器自动退出，main.py 独立运行；业务输出持续写入 logs/application.log。同一安装目录的主程序仍保持单实例。

卸载只删除打包的文件，保留仓库、独立环境、日志和配置。

## 开发构建

使用 Windows x64 Python 3.10+：

```powershell
python -m pip install "pyinstaller>=6,<7" "zstandard>=0.22,<1"
python tools/build_release.py
python tools/build_installer.py --skip-build
```

也可直接 `python tools/build_installer.py` 自动完成两步。安装包输出为 `dist/PluginManager-2.1.1-Setup-x64.exe`；目录版为 `dist/PluginManager/`。`python tools/build_release.py --zip` 可额外生成便携 ZIP。

只有构建阶段下载官方 CPython MSI、MinGit ZIP 和 NSIS 的 MSYS2 原生 x64 发行包，验证固定 SHA256，缓存在 `build/downloads`。CPython 通过只读 MSI 数据库 API 和系统 CAB 提取工具展开，不调用安装服务、不注册系统 Python。NSIS 解压使用，不安装完整 MSYS2，不安装到系统。

源码开发仍可 `python main.py`。测试启动器可使用：

```powershell
python start.py --headless --check-environment --install-dir dist/PluginManager
python start.py --headless --prepare-only --install-dir dist/PluginManager
```

`--prepare-only` 只准备仓库和依赖，不启动业务界面。验收请使用单独安装目录，避免把测试仓库和日志再次打包。

保留各组件原始许可证：Python 来源为 python.org，Git 来源为 git-for-windows/git Release，NSIS 原生编译器来源为 MSYS2 官方软件仓库。业务插件所需额外依赖和外部程序仍需由仓库声明。

自动更新最多等待 update_timeout（默认 5 秒），超时或连接失败直接回退本地代码；不重复输出同一错误。startup_timeout 默认 60 秒，用于等待主窗口就绪。proxy_url 默认留空，沿用环境变量及 Windows 手动代理；需要时可填 HTTP/HTTPS 代理地址。首次没有本地仓库时仍必须联网，启动器不能修复网络本身的不可达。
