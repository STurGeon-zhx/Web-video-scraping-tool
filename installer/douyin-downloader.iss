#define MyAppName "视频批量下载工具"
#define MyAppVersion "1.1.0"
#define MyAppExeName "视频批量下载工具.exe"

[Setup]
AppId={{7F381B63-8EE8-4E79-B4F2-68F4C5926D32}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=DouyinBatchDownloader
DefaultDirName={localappdata}\Programs\DouyinBatchDownloader
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
UninstallDisplayIcon={app}\{#MyAppExeName}
OutputDir=..\release\installer
OutputBaseFilename=视频批量下载工具-Setup-1.1.0-Windows-x64
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
InfoBeforeFile=使用说明.txt
VersionInfoVersion=1.1.0.0
VersionInfoDescription={#MyAppName} 安装程序
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}
VersionInfoCompany=DouyinBatchDownloader

[Files]
Source: "..\release\desktop\app\视频批量下载工具\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\视频批量下载工具"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\视频批量下载工具"; Filename: "{app}\{#MyAppExeName}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动视频批量下载工具"; Flags: nowait postinstall skipifsilent
