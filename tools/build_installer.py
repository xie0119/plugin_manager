"""Build a native Windows MSI using Python 3.11's Windows Installer bindings.

This is a build-time tool only. The installed launcher needs neither Python nor
msilib. User settings are excluded from the package and preserved on uninstall.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import shutil
import sys
import uuid
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", message="'msilib' is deprecated.*", category=DeprecationWarning)
if sys.platform != "win32" or not ((3, 10) <= sys.version_info[:2] <= (3, 12)):
    raise SystemExit("请使用 Windows 上的 Python 3.10–3.12 构建 MSI（推荐 Python 3.11）。")

import msilib
from msilib import Dialog, add_data, schema, sequence, text

ROOT = Path(__file__).resolve().parents[1]
VERSION = "2.0.0"
NAMESPACE = uuid.UUID("b7db0910-1254-48a3-b0d9-025054549f2b")
UPGRADE_CODE = "{" + str(uuid.uuid5(NAMESPACE, "upgrade")).upper() + "}"
PRODUCT_CODE = "{" + str(uuid.uuid5(NAMESPACE, "product:" + VERSION)).upper() + "}"


def guid(value: str) -> str:
    return "{" + str(uuid.uuid5(NAMESPACE, value)).upper() + "}"


def query(db, sql: str) -> None:
    view = db.OpenView(sql)
    view.Execute(None)
    view.Close()


def properties(db, values: dict[str, str]) -> None:
    add_data(db, "Property", list(values.items()))


def set_codepage(database: Path, folder: Path, filename: str) -> None:
    """The Python wrapper omits Import, so use the corresponding MSI API."""
    api = ctypes.WinDLL("msi")
    api.MsiOpenDatabaseW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
    api.MsiDatabaseImportW.argtypes = [ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_wchar_p]
    api.MsiDatabaseCommit.argtypes = [ctypes.c_uint]
    api.MsiCloseHandle.argtypes = [ctypes.c_uint]
    handle = ctypes.c_uint()
    error = api.MsiOpenDatabaseW(str(database), ctypes.c_void_p(2), ctypes.byref(handle))
    if error:
        raise OSError(error, "无法打开 MSI 数据库")
    try:
        error = api.MsiDatabaseImportW(handle.value, str(folder), filename)
        if error:
            raise OSError(error, "无法设置 MSI 中文代码页")
        error = api.MsiDatabaseCommit(handle.value)
        if error:
            raise OSError(error, "无法保存 MSI 代码页")
    finally:
        api.MsiCloseHandle(handle.value)


def file_hash(path: Path) -> tuple[int, int, int, int]:
    class HashInfo(ctypes.Structure):
        _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.c_uint32 * 4)]

    get_hash = ctypes.WinDLL("msi").MsiGetFileHashW
    get_hash.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.POINTER(HashInfo)]
    get_hash.restype = ctypes.c_uint32
    result = HashInfo()
    result.size = ctypes.sizeof(result)
    error = get_hash(str(path), 0, ctypes.byref(result))
    if error:
        raise OSError(error, f"无法计算 MSI 文件哈希：{path}")
    return tuple(ctypes.c_int32(value).value for value in result.data)


def add_payload(db, source: Path, work: Path) -> tuple[int, str]:
    cabinet = msilib.CAB("payload.cab")
    directories = {Path("."): "INSTALLDIR"}
    rows = [
        ("TARGETDIR", None, "SourceDir"),
        ("LocalAppDataFolder", "TARGETDIR", "."),
        ("ProgramsDir", "LocalAppDataFolder", "Programs"),
        ("INSTALLDIR", "ProgramsDir", "PluginManager"),
        ("ProgramMenuFolder", "TARGETDIR", "."),
        ("ProgramMenuDir", "ProgramMenuFolder", "PluginManager"),
        ("DesktopFolder", "TARGETDIR", "."),
    ]
    add_data(db, "Directory", rows)
    add_data(db, "Feature", [
        ("Main", None, "PluginManager", "启动器和自带插件", 1, 1, "INSTALLDIR", 0),
    ])
    files = sorted(path for path in source.rglob("*") if path.is_file())
    total = 0
    executable_id = ""
    for path in files:
        relative = path.relative_to(source)
        if (
            "settings.json" == path.name
            or path.name.endswith((".pyc", ".pyo", ".tmp"))
            or any(part.startswith(".") or part == "__pycache__" for part in relative.parts)
        ):
            continue
        if not path.resolve().is_relative_to(source.resolve()):
            raise ValueError(f"发布文件超出目录：{path}")
        for parent in reversed(relative.parents):
            if parent == Path(".") or parent in directories:
                continue
            parent_id = directories[parent.parent]
            directory_id = "D_" + hashlib.sha1(parent.as_posix().encode()).hexdigest()[:20]
            directories[parent] = directory_id
            short = "D" + str(len(directories)).zfill(6)
            add_data(db, "Directory", [(directory_id, parent_id, short + "|" + parent.name)])
        digest = hashlib.sha1(relative.as_posix().encode()).hexdigest()[:20]
        file_id = "F_" + digest
        component_id = "C_" + digest
        number = len(cabinet.files) + 1
        staged = work / f"payload{number:05d}.dat"
        shutil.copy2(path, staged)
        file_sequence, _ = cabinet.append(str(staged), file_id, file_id)
        directory_id = directories[relative.parent]
        add_data(db, "Component", [
            (component_id, guid("file:" + relative.as_posix().lower()), directory_id,
             256, None, file_id),
        ])
        add_data(db, "FeatureComponents", [("Main", component_id)])
        size = path.stat().st_size
        add_data(db, "File", [
            (file_id, component_id, f"F{number:06d}.DAT|{path.name}",
             size, None, None, 512, file_sequence),
        ])
        add_data(db, "MsiFileHash", [(file_id, 0, *file_hash(path))])
        total += size
        if relative.as_posix() == "PluginManager.exe":
            executable_id = file_id
    if not executable_id:
        raise RuntimeError("发布目录中缺少 PluginManager.exe")
    cabinet.commit(db)
    return total, executable_id


def add_shortcuts(db, executable_id: str) -> None:
    for label, directory, condition in (
        ("Desktop", "DesktopFolder", "DESKTOP_SHORTCUT"),
        ("StartMenu", "ProgramMenuDir", "STARTMENU_SHORTCUT"),
    ):
        component = "Shortcut" + label
        registry_id = "Reg" + label
        add_data(db, "Component", [
            (component, guid("shortcut:" + label), directory, 260, condition, registry_id),
        ])
        add_data(db, "FeatureComponents", [("Main", component)])
        add_data(db, "Registry", [
            (registry_id, 1, r"Software\PluginManager\Installer", label, "1", component),
        ])
        add_data(db, "Shortcut", [
            ("Link" + label, directory, "PLUGMA~1|PluginManager", component,
             "[#" + executable_id + "]", None, "PluginManager 工具启动器",
             None, None, None, 1, "INSTALLDIR"),
        ])
    # Only remove the installer's empty Start menu folder, never unknown user files.
    add_data(db, "RemoveFile", [
        ("RemoveMenuDir", "ShortcutStartMenu", None, "ProgramMenuDir", 2),
    ])


def dialog(db, name, first="Next", *, modeless=False):
    return Dialog(db, name, 50, 50, 390, 280, 1 if modeless else 3,
                  "[ProductName] 安装向导", first, "Cancel" if modeless else "Next", "Cancel")


def heading(dlg, title, body):
    dlg.text("Heading", 20, 17, 350, 24, 3, r"{\TitleFont}" + title)
    dlg.text("Body", 20, 50, 350, 48, 3, body)
    dlg.line("BottomLine", 0, 238, 390, 1)


def cancel_button(dlg, next_control=None):
    button = dlg.pushbutton("Cancel", 310, 250, 60, 18, 3, "取消", next_control)
    button.event("SpawnDialog", "CancelDlg")
    return button


def add_ui(db):
    properties(db, {
        "DefaultUIFont": "NormalFont", "ErrorDialog": "ErrorDlg",
        "DESKTOP_SHORTCUT": "1", "STARTMENU_SHORTCUT": "1",
        "MAINTENANCE_ACTION": "Repair",
    })
    add_data(db, "TextStyle", [
        ("NormalFont", "Microsoft YaHei", 9, None, 0),
        ("TitleFont", "Microsoft YaHei", 14, 0x00333333, 1),
    ])
    add_data(db, "CheckBox", [
        ("DESKTOP_SHORTCUT", "1"), ("STARTMENU_SHORTCUT", "1"),
    ])
    add_data(db, "ActionText", text.ActionText)
    add_data(db, "UIText", text.UIText)

    welcome = dialog(db, "WelcomeDlg")
    heading(welcome, "欢迎安装 PluginManager",
            "安装启动器和自带文本工具。程序自带运行环境，无需安装 Python。")
    welcome.text("Details", 20, 112, 350, 70, 3,
                 "默认仅为当前用户安装。\n插件保持独立，可在安装后添加到 plugins 文件夹。")
    welcome.pushbutton("Next", 240, 250, 60, 18, 3, "下一步", "Cancel").event("NewDialog", "OptionsDlg")
    cancel_button(welcome, "Next")

    options = dialog(db, "OptionsDlg", "Path")
    heading(options, "选择安装位置", "请选择当前用户可写的文件夹，便于插件保存设置。")
    options.text("PathLabel", 20, 98, 350, 15, 3, "安装到：")
    options.control("Path", "PathEdit", 20, 118, 350, 20, 3,
                    "INSTALLDIR", None, "Desktop", None)
    options.checkbox("Desktop", 20, 153, 350, 18, 3, "DESKTOP_SHORTCUT",
                     "创建桌面快捷方式", "StartMenu")
    options.checkbox("StartMenu", 20, 179, 350, 18, 3, "STARTMENU_SHORTCUT",
                     "创建开始菜单快捷方式", "Back")
    options.pushbutton("Back", 170, 250, 60, 18, 3, "上一步", "Next").event("NewDialog", "WelcomeDlg")
    install = options.pushbutton("Next", 240, 250, 60, 18, 3, "安装", "Cancel")
    install.event("SetTargetPath", "INSTALLDIR", ordering=1)
    install.event("EndDialog", "Return", ordering=2)
    cancel_button(options, "Path")

    progress = dialog(db, "ProgressDlg", "Cancel", modeless=True)
    heading(progress, "正在处理", "请稍候，安装程序正在复制文件或更新安装信息。")
    progress.text("Action", 20, 114, 350, 25, 3, "").mapping("ActionText", "Text")
    progress.control("Progress", "ProgressBar", 20, 154, 350, 14, 65537,
                     None, None, None, None).mapping("SetProgress", "Progress")
    cancel_button(progress)

    maintenance = dialog(db, "MaintenanceDlg", "Action")
    heading(maintenance, "维护 PluginManager", "选择修复或卸载。插件自己的设置和新增文件将保留。")
    choices = maintenance.radiogroup("Action", 20, 112, 350, 70, 3,
                                    "MAINTENANCE_ACTION", "", "Next")
    choices.add("Repair", 0, 0, 330, 20, "修复安装文件")
    choices.add("Remove", 0, 28, 330, 20, "卸载 PluginManager")
    proceed = maintenance.pushbutton("Next", 240, 250, 60, 18, 3, "继续", "Cancel")
    proceed.event("[REINSTALLMODE]", "amus", 'MAINTENANCE_ACTION="Repair"', 1)
    proceed.event("[REINSTALL]", "ALL", 'MAINTENANCE_ACTION="Repair"', 2)
    proceed.event("Reinstall", "ALL", 'MAINTENANCE_ACTION="Repair"', 3)
    proceed.event("[REMOVE]", "ALL", 'MAINTENANCE_ACTION="Remove"', 4)
    proceed.event("Remove", "ALL", 'MAINTENANCE_ACTION="Remove"', 5)
    proceed.event("EndDialog", "Return", ordering=6)
    cancel_button(maintenance, "Action")

    for name, title, body, action in (
        ("ExitDialog", "操作完成", "安装程序已成功完成操作。单击“完成”关闭向导。", "Return"),
        ("FatalError", "操作未完成", "发生错误，操作未能完成。请查看提示后重试。", "Exit"),
        ("UserExit", "操作已取消", "你已取消本次操作。可以稍后重新运行安装程序。", "Exit"),
    ):
        result = Dialog(db, name, 50, 50, 390, 280, 3,
                        "[ProductName] 安装向导", "Finish", "Finish", "Finish")
        heading(result, title, body)
        result.pushbutton("Finish", 310, 250, 60, 18, 3, "完成", None).event("EndDialog", action)

    cancel = Dialog(db, "CancelDlg", 50, 50, 280, 100, 3,
                    "取消安装", "No", "No", "No")
    cancel.text("Question", 20, 18, 240, 35, 3, "确定要取消当前操作吗？")
    cancel.pushbutton("Yes", 130, 65, 60, 18, 3, "是", "No").event("EndDialog", "Exit")
    cancel.pushbutton("No", 200, 65, 60, 18, 3, "否", "Yes").event("EndDialog", "Return")

    error = Dialog(db, "ErrorDlg", 50, 50, 340, 110, 65543,
                   "[ProductName]", "ErrorText", None, None)
    error.text("ErrorText", 15, 10, 310, 55, 3, "")
    for index, (name, caption, action) in enumerate((
        ("A", "中止", "ErrorAbort"), ("C", "取消", "ErrorCancel"),
        ("I", "忽略", "ErrorIgnore"), ("N", "否", "ErrorNo"),
        ("O", "确定", "ErrorOk"), ("R", "重试", "ErrorRetry"), ("Y", "是", "ErrorYes"),
    )):
        error.pushbutton(name, index * 45, 80, 43, 18, 3, caption, None).event("EndDialog", action)

    add_data(db, "InstallUISequence", [
        ("WelcomeDlg", "NOT Installed", 1230),
        ("MaintenanceDlg", "Installed AND NOT REMOVE AND NOT RESUME AND NOT Preselected", 1250),
        ("ProgressDlg", None, 1280),
    ])


def main() -> int:
    source = ROOT / "dist" / "PluginManager"
    if not (source / "PluginManager.exe").is_file():
        raise SystemExit("请先运行 tools/build_release.py 生成发布目录。")
    work = ROOT / "build" / "installer"
    if work.is_symlink() or not work.resolve().is_relative_to((ROOT / "build").resolve()):
        raise RuntimeError(f"构建目录不安全：{work}")
    work.mkdir(parents=True, exist_ok=True)
    output = ROOT / "dist" / f"PluginManager-{VERSION}-Setup-x64.msi"
    db = msilib.init_database(str(output), schema, "PluginManager",
                             PRODUCT_CODE, VERSION, "PluginManager Project")
    force_codepage = work / "_ForceCodepage.idt"
    force_codepage.write_bytes(b"\n\n936\t_ForceCodepage\n")
    del db
    set_codepage(output, work, force_codepage.name)
    db = msilib.OpenDatabase(str(output), msilib.MSIDBOPEN_TRANSACT)
    query(db, "UPDATE `Property` SET `Value`='2052' WHERE `Property`='ProductLanguage'")
    summary = db.GetSummaryInformation(20)
    summary.SetProperty(msilib.PID_CODEPAGE, 936)
    summary.SetProperty(msilib.PID_TITLE, "PluginManager 安装程序")
    summary.SetProperty(msilib.PID_TEMPLATE, "x64;2052")
    summary.SetProperty(msilib.PID_PAGECOUNT, 500)
    summary.SetProperty(msilib.PID_WORDCOUNT, 10)
    summary.Persist()
    msilib.add_tables(db, sequence)
    properties(db, {
        "UpgradeCode": UPGRADE_CODE,
        "ARPNOMODIFY": "1",
        "SecureCustomProperties": "INSTALLDIR;DESKTOP_SHORTCUT;STARTMENU_SHORTCUT;OLDPRODUCTS;NEWERPRODUCTS",
    })
    add_data(db, "LaunchCondition", [
        ("Installed OR VersionNT64", "此安装包需要 64 位 Windows。"),
        ("Installed OR NOT NEWERPRODUCTS", "已安装更新版本，不能降级安装。"),
    ])
    add_data(db, "Upgrade", [
        (UPGRADE_CODE, None, VERSION, None, 1, None, "OLDPRODUCTS"),
        (UPGRADE_CODE, VERSION, None, None, 2, None, "NEWERPRODUCTS"),
    ])
    total, executable_id = add_payload(db, source, work)
    properties(db, {"ARPSIZE": str((total + 1023) // 1024)})
    add_data(db, "Registry", [
        ("RegInstallDir", 1, r"Software\PluginManager\Installer", "InstallDir",
         "[INSTALLDIR]", "C_" + executable_id[2:]),
    ])
    add_data(db, "RegLocator", [
        ("ExistingInstallDir", 1, r"Software\PluginManager\Installer", "InstallDir", 18),
    ])
    add_data(db, "AppSearch", [("INSTALLDIR", "ExistingInstallDir")])
    add_shortcuts(db, executable_id)
    add_data(db, "CustomAction", [
        ("SetArpInstallLocation", 51, "ARPINSTALLLOCATION", "[INSTALLDIR]"),
    ])
    add_data(db, "InstallExecuteSequence", [
        ("SetArpInstallLocation", "NOT REMOVE", 1405),
    ])
    add_ui(db)
    db.Commit()
    # Drop references before external verification or MSI execution.
    del summary, db
    info = {
        "application": "PluginManager", "version": VERSION,
        "product_code": PRODUCT_CODE, "upgrade_code": UPGRADE_CODE,
        "installer": output.name,
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
    }
    (ROOT / "dist" / "installer-info.json").write_text(
        json.dumps(info, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Installer: {output}")
    print(f"ProductCode: {PRODUCT_CODE}")
    print(f"SHA256: {info['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
