Unicode true
Target amd64-unicode
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"
!include "WinVer.nsh"
Name "PluginManager"
OutFile "${OUTPUT}"
InstallDir "$LOCALAPPDATA\Programs\PluginManager"
InstallDirRegKey HKCU "Software\PluginManagerBootstrap" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma
SetCompressorDictSize 32
ShowInstDetails show
ShowUninstDetails show
VIProductVersion "2.1.3.0"
VIAddVersionKey /LANG=2052 "ProductName" "PluginManager"
VIAddVersionKey /LANG=2052 "FileDescription" "PluginManager 启动器与便携 Python/Git 安装程序"
VIAddVersionKey /LANG=2052 "FileVersion" "2.1.3"
VIAddVersionKey /LANG=2052 "LegalCopyright" "PluginManager"
!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TEXT "安装程序将释放启动器、便携 Python 和 Git。$\r$\n$\r$\n安装后打开启动器，会将仓库克隆到安装路径，安装项目依赖并运行 main.py。$\r$\n$\r$\n安装阶段无需下载 Python 或 Git；首次拉取仓库和安装依赖需要联网。"
!define MUI_FINISHPAGE_RUN "$INSTDIR\PluginManager.exe"
!define MUI_FINISHPAGE_RUN_TEXT "打开 PluginManager"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"

!macro CheckClosed suffix
  IfFileExists "$INSTDIR\launcher.lock" 0 application_${suffix}
  System::Call 'kernel32::CreateFileW(w "$INSTDIR\launcher.lock", i 0xC0000000, i 0, p 0, i 3, i 0, p 0) p .r0'
  ${If} $0 == -1
    MessageBox MB_ICONSTOP "请先关闭此目录中的 PluginManager，再继续安装或卸载。"
    Abort
  ${EndIf}
  System::Call 'kernel32::CloseHandle(p r0)'
  application_${suffix}:
  IfFileExists "$INSTDIR\application.lock" 0 closed_${suffix}
  System::Call 'kernel32::CreateFileW(w "$INSTDIR\application.lock", i 0xC0000000, i 0, p 0, i 3, i 0, p 0) p .r0'
  ${If} $0 == -1
    MessageBox MB_ICONSTOP "请先关闭主程序，再继续安装或卸载。"
    Abort
  ${EndIf}
  System::Call 'kernel32::CloseHandle(p r0)'
  closed_${suffix}:
!macroend

Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "需要 64 位 Windows 10 或 Windows 11。"
    Abort
  ${EndIf}
  ${IfNot} ${AtLeastWin10}
    MessageBox MB_ICONSTOP "需要 Windows 10 或 Windows 11。"
    Abort
  ${EndIf}
  SetRegView 64
  SetShellVarContext current
FunctionEnd

Section "PluginManager"
  SetRegView 64
  SetShellVarContext current
  !insertmacro CheckClosed install
  SetOutPath "$INSTDIR"
  ClearErrors
  SetOverwrite on
  File /r /x launcher-config.json "${PAYLOAD}\*"
  SetOverwrite off
  File "${PAYLOAD}\launcher-config.json"
  SetOverwrite on
  ${If} ${Errors}
    MessageBox MB_ICONSTOP "释放文件失败，请检查磁盘空间或选择可写目录。"
    Abort
  ${EndIf}
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateDirectory "$SMPROGRAMS\PluginManager"
  CreateShortcut "$SMPROGRAMS\PluginManager\PluginManager.lnk" "$INSTDIR\PluginManager.exe"
  CreateShortcut "$DESKTOP\PluginManager.lnk" "$INSTDIR\PluginManager.exe"
  WriteRegStr HKCU "Software\PluginManagerBootstrap" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\PluginManagerBootstrap" "DisplayName" "PluginManager 启动器"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\PluginManagerBootstrap" "DisplayVersion" "2.1.3"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\PluginManagerBootstrap" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\PluginManagerBootstrap" "DisplayIcon" "$INSTDIR\PluginManager.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\PluginManagerBootstrap" "UninstallString" '$\"$INSTDIR\Uninstall.exe$\"'
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\PluginManagerBootstrap" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\PluginManagerBootstrap" "NoRepair" 1
SectionEnd

Function un.onInit
  SetRegView 64
  SetShellVarContext current
  !insertmacro CheckClosed uninstall
FunctionEnd

Section "Uninstall"
  !include "${UNINSTALL_PAYLOAD}"
  ; 不删除仓库、.venv、日志或 launcher-config.json。
  Delete "$DESKTOP\PluginManager.lnk"
  Delete "$SMPROGRAMS\PluginManager\PluginManager.lnk"
  RMDir "$SMPROGRAMS\PluginManager"
  DeleteRegKey HKCU "Software\PluginManagerBootstrap"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\PluginManagerBootstrap"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
SectionEnd
