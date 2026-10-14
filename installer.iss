; Inno Setup script for Taisync Telemetry.
; Build with:  iscc installer.iss
; Produces:    dist_installer\TaisyncTelemetry-Setup-1.39.exe
;
; Run TaisyncTelemetry.spec through PyInstaller first -- this packages
; dist\TaisyncTelemetry\, it does not build it.

#define AppName       "Taisync Telemetry"
#define AppVersion    "1.39"
#define AppPublisher  "Taysinc FPV"
#define AppExe        "TaisyncTelemetry.exe"

[Setup]
AppId={{8E4C1F27-9A6B-4D3E-B1C5-7F2A0D9E6B41}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\TaisyncTelemetry
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=dist_installer
OutputBaseFilename=TaisyncTelemetry-Setup-{#AppVersion}
SetupIconFile=fpv_telemetry_icon.ico
UninstallDisplayIcon={app}\{#AppExe}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible

; Default to a per-user install so the app can be put on a field laptop
; without administrator rights; the user can still pick a machine-wide
; install in the first wizard page.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "uk"; MessagesFile: "compiler:Languages\Ukrainian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; The whole PyInstaller one-folder output.
Source: "dist\TaisyncTelemetry\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\TaisyncTelemetry\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Settings and logs live in %APPDATA%\TaisyncTelemetry and are intentionally
; left in place on uninstall so a reinstall keeps the configured IPs.
Type: dirifempty; Name: "{app}\_internal"
Type: dirifempty; Name: "{app}"
