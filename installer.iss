; Inno Setup script for Taisync Telemetry.
; Build with:  iscc installer.iss           (build.ps1 -Installer passes /DAppVersion)
; Produces:    dist_installer\TaisyncTelemetry-Setup-<version>.exe
;
; Run TaisyncTelemetry.spec through PyInstaller first -- this packages
; dist\TaisyncTelemetry\, it does not build it.
;
; Upgrades: every version installs over the previous one instead of adding a
; second entry to "Apps & features". That relies on three things:
;   * AppId below never changes. Windows identifies the program by it, not by
;     the name or version. NEVER edit it.
;   * One install scope (per-user) for every version. A per-user and an
;     all-users install of the same AppId are two separate installs.
;   * [Code] below removes the copies left by builds made before this script
;     ("FPV Telemetry" 1.38, "TaisyncTelemetry" 1.37a), which used other AppIds
;     and so were never recognised as the same program.

#define AppName       "Taisync Telemetry"
#ifndef AppVersion
  #define AppVersion  "1.40"
#endif
#define AppPublisher  "Taysinc FPV"
#define AppExe        "TaisyncTelemetry.exe"
#define AppIdGuid     "{8E4C1F27-9A6B-4D3E-B1C5-7F2A0D9E6B41}"

[Setup]
AppId={{#AppIdGuid}
AppName={#AppName}
AppVersion={#AppVersion}
; Same display name for every version, so "Apps & features" shows one
; "Taisync Telemetry" whose version number moves, not a list of versions.
AppVerName={#AppName}
UninstallDisplayName={#AppName}
AppPublisher={#AppPublisher}
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\TaisyncTelemetry
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; Reinstall into wherever the previous version went, without asking again.
UsePreviousAppDir=yes
DisableDirPage=auto
OutputDir=dist_installer
OutputBaseFilename=TaisyncTelemetry-Setup-{#AppVersion}
SetupIconFile=fpv_telemetry_icon.ico
UninstallDisplayIcon={app}\{#AppExe}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible

; Always per-user: installs without administrator rights on a field laptop,
; and -- unlike the old "choose in a dialog" option -- can never produce a
; second, all-users copy next to the per-user one.
PrivilegesRequired=lowest

; The app holds this mutex while running (see INSTANCE_MUTEX in the .py), so
; setup asks to close it instead of failing to overwrite a locked .exe.
AppMutex=TaisyncTelemetry_SingleInstance
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "uk"; MessagesFile: "compiler:Languages\Ukrainian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; Wipe the bundled runtime before copying the new one, so files a newer
; PyInstaller/Qt no longer ships don't linger next to the new ones.
Type: filesandordirs; Name: "{app}\_internal"

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
; left in place on uninstall so a reinstall keeps the configured IPs and logs.
Type: dirifempty; Name: "{app}\_internal"
Type: dirifempty; Name: "{app}"

[Code]
const
  UninstKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\';

type
  TOldInstall = record
    RootKey: Integer;
    SubKey: String;
    Name: String;
    UninstallExe: String;
    Location: String;
  end;

var
  OldInstalls: array of TOldInstall;

{ Registry key names of installs that must be replaced by this one:
  builds from before this script, and this AppId installed for all users
  (1.39 offered that choice). This AppId per-user is the normal upgrade and
  is handled by Inno itself, so it is not in the list. }
function IsOldKey(RootKey: Integer; const Name: String): Boolean;
begin
  Result :=
    (CompareText(Name, 'FPV Telemetry_is1') = 0) or
    (CompareText(Name, 'TaisyncTelemetry_is1') = 0) or
    (CompareText(Name, 'Taisync Telemetry_is1') = 0) or
    ((CompareText(Name, '{#AppIdGuid}_is1') = 0) and (RootKey <> HKCU));
end;

procedure AddOld(RootKey: Integer; const KeyName: String);
var
  Exe, DisplayName, Location: String;
  N: Integer;
begin
  if not RegQueryStringValue(RootKey, UninstKey + KeyName, 'UninstallString', Exe) then
    Exit;
  Exe := RemoveQuotes(Exe);
  if not FileExists(Exe) then
    Exit;
  if not RegQueryStringValue(RootKey, UninstKey + KeyName, 'DisplayName', DisplayName) then
    DisplayName := KeyName;
  RegQueryStringValue(RootKey, UninstKey + KeyName, 'InstallLocation', Location);
  N := GetArrayLength(OldInstalls);
  SetArrayLength(OldInstalls, N + 1);
  OldInstalls[N].RootKey := RootKey;
  OldInstalls[N].SubKey := KeyName;
  OldInstalls[N].Name := DisplayName;
  OldInstalls[N].UninstallExe := Exe;
  OldInstalls[N].Location := Location;
end;

procedure ScanRoot(RootKey: Integer);
var
  Names: TArrayOfString;
  I: Integer;
begin
  if RegGetSubkeyNames(RootKey, UninstKey, Names) then
    for I := 0 to GetArrayLength(Names) - 1 do
      if IsOldKey(RootKey, Names[I]) then
        AddOld(RootKey, Names[I]);
end;

procedure FindOldInstalls;
begin
  SetArrayLength(OldInstalls, 0);
  ScanRoot(HKCU);
  ScanRoot(HKLM32);
  if IsWin64 then
    ScanRoot(HKLM64);
end;

{ Builds before 1.39 kept telemetry_settings.json next to the .exe. Carry it
  over to %APPDATA% so the configured IPs survive the switch. }
procedure MigrateSettings(const Location: String);
var
  Src, DestDir, Dest: String;
begin
  if Location = '' then
    Exit;
  Src := AddBackslash(Location) + 'telemetry_settings.json';
  DestDir := ExpandConstant('{userappdata}\TaisyncTelemetry');
  Dest := DestDir + '\telemetry_settings.json';
  if FileExists(Src) and not FileExists(Dest) then
  begin
    ForceDirectories(DestDir);
    FileCopy(Src, Dest, True);
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  I, ResultCode: Integer;
  List: String;
begin
  Result := '';
  FindOldInstalls;
  if GetArrayLength(OldInstalls) = 0 then
    Exit;

  List := '';
  for I := 0 to GetArrayLength(OldInstalls) - 1 do
    List := List + #13#10 + '  • ' + OldInstalls[I].Name;

  if not WizardSilent then
    if MsgBox('Знайдено попередні копії програми, встановлені окремо:' + List + #13#10#13#10 +
              'Видалити їх, щоб залишилась одна Taisync Telemetry?' + #13#10 +
              '(Налаштування IP буде перенесено. Для копій, встановлених для всіх ' +
              'користувачів, Windows запитає дозвіл адміністратора.)',
              mbConfirmation, MB_YESNO) <> IDYES then
      Exit;

  for I := 0 to GetArrayLength(OldInstalls) - 1 do
  begin
    MigrateSettings(OldInstalls[I].Location);
    { ShellExec rather than Exec: an all-users uninstaller needs elevation,
      and ShellExec is what lets Windows show the UAC prompt for it. }
    if not ShellExec('', OldInstalls[I].UninstallExe,
                     '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART', '',
                     SW_HIDE, ewWaitUntilTerminated, ResultCode) then
      Log('Could not run ' + OldInstalls[I].UninstallExe + ': ' + SysErrorMessage(ResultCode))
    else
      Log('Removed old install ' + OldInstalls[I].Name + ', exit code ' + IntToStr(ResultCode));
  end;
end;
