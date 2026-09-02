; Inno Setup script - 유튜브 악보 PDF 변환기 (Kmong 1775529 / order 7589200)
;
; --onedir + installer, deliberately NOT --onefile: a onefile exe unpacks itself
; into %TEMP% on every launch, which costs the customer a minute of staring at
; nothing and trips Defender's "unknown program writing executables" heuristic.
; A folder install starts instantly and has nothing to unpack.
;
; PrivilegesRequired=lowest installs into %LocalAppData%\Programs, so there is no
; UAC prompt and the silent auto-update can replace the folder without elevation.

#define AppName "유튜브 악보 PDF 변환기"
#define AppSlug "youtube-score-pdf"
#define AppVersion "1.0.0"

[Setup]
AppId={{7E5B1C64-3F8A-4C51-9E2D-1775529A0001}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Neoworks
DefaultDirName={autopf}\{#AppSlug}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=no
PrivilegesRequired=lowest
OutputDir=installer
OutputBaseFilename={#AppSlug}-setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
WizardStyle=modern
CloseApplications=yes
RestartApplications=yes
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppSlug}.exe
VersionInfoVersion={#AppVersion}
VersionInfoCompany=Neoworks
VersionInfoDescription={#AppName} 설치 프로그램

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: "desktopicon"; Description: "바탕화면에 바로가기 만들기"; GroupDescription: "추가 작업:"

[Files]
Source: "dist\{#AppSlug}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "_읽어주세요.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppSlug}.exe"
Name: "{group}\사용 설명서"; Filename: "{app}\_읽어주세요.txt"
Name: "{group}\{#AppName} 제거"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppSlug}.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppSlug}.exe"; Description: "지금 실행하기"; Flags: nowait postinstall skipifsilent
