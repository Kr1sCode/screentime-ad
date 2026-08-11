; screentime-ad agent — instalator Windows (usluga NSSM + tray per-user).
; Wzorowany na dojrzalym pipeline adminreminder (stop-przed-kopiowaniem,
; idempotentny remove-then-install NSSM, wersja wstrzykiwana z CI).
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0-dev"
#endif

[Setup]
AppId={{FC8D780D-D719-4631-AFCC-416C2D2F79C6}
AppName=screentime-ad agent
AppVersion={#MyAppVersion}
AppPublisher=KrzysztofGawkowski.pl
DefaultDirName={autopf}\screentime-ad
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
OutputDir=Output
OutputBaseFilename=screentime-ad-agent-setup
Compression=lzma
SolidCompression=yes
Uninstallable=yes

[Files]
Source: "..\dist\screentime-ad-agent\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
Source: "..\dist\screentime-ad-tray\*"; DestDir: "{app}\tray"; Flags: recursesubdirs ignoreversion
Source: "nssm.exe"; DestDir: "{app}"; Flags: ignoreversion

[Code]
var
  ServerPage: TInputQueryWizardPage;

const
  SvcName = 'screentime-ad-agent';
  TaskName = 'screentime-ad-tray';

procedure NssmExec(Params: String);
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{app}\nssm.exe'), Params, ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure ShellExec(Cmd, Params: String);
var
  ResultCode: Integer;
begin
  Exec(Cmd, Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure InitializeWizard;
begin
  ServerPage := CreateInputQueryPage(wpSelectDir,
    'Konfiguracja agenta', 'Dane potrzebne do polaczenia z serwerem screentime-ad',
    'Znajdziesz je w panelu serwera, w sekcji "Konfiguracja konta" (token agenta).');
  ServerPage.Add('Adres serwera (np. http://192.168.1.50):', False);
  ServerPage.Add('Token agenta:', False);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = ServerPage.ID then
  begin
    if (ServerPage.Values[0] = '') or (ServerPage.Values[1] = '') then
    begin
      MsgBox('Uzupelnij adres serwera i token agenta.', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ConfigPath, DataDir: String;
begin
  if CurStep = ssInstall then
  begin
    // Usluga trzyma otwarty plik .exe — kopiowanie [Files] wisi na
    // abort/retry jesli nie zatrzymamy jej PRZED nadpisaniem.
    if FileExists(ExpandConstant('{app}\nssm.exe')) then
      NssmExec('stop ' + SvcName);
  end;

  if CurStep = ssPostInstall then
  begin
    SaveStringToFile(ExpandConstant('{app}\version.txt'), '{#MyAppVersion}', False);

    // Reinstalacja/upgrade musi byc idempotentna — remove przed install,
    // ignorujac blad "usluga nie istnieje" przy pierwszej instalacji.
    NssmExec('remove ' + SvcName + ' confirm');
    NssmExec('install ' + SvcName + ' "' + ExpandConstant('{app}\screentime-ad-agent.exe') + '"');
    NssmExec('set ' + SvcName + ' AppDirectory "' + ExpandConstant('{app}') + '"');
    NssmExec('set ' + SvcName + ' DisplayName "screentime-ad agent"');
    NssmExec('set ' + SvcName + ' Start SERVICE_AUTO_START');
    NssmExec('set ' + SvcName + ' AppStdout "' + ExpandConstant('{app}\service.log') + '"');
    NssmExec('set ' + SvcName + ' AppStderr "' + ExpandConstant('{app}\service.log') + '"');
    NssmExec('set ' + SvcName + ' AppRotateFiles 1');
    NssmExec('set ' + SvcName + ' AppRotateBytes 10485760');
    NssmExec('set ' + SvcName + ' AppExit Default Restart');

    // Config agenta — TYLKO jesli jeszcze nie istnieje (upgrade nie ma
    // nadpisywac juz dzialajacej konfiguracji).
    DataDir := ExpandConstant('{commonappdata}\screentime-ad');
    ConfigPath := DataDir + '\agent.json';
    if not FileExists(ConfigPath) then
    begin
      ForceDirectories(DataDir);
      SaveStringToFile(ConfigPath,
        '{"server_url": "' + ServerPage.Values[0] + '", "token": "' + ServerPage.Values[1] + '"}',
        False);
    end;

    NssmExec('start ' + SvcName);

    // Tray per-user: trigger "at logon" dla grupy Users, zeby dzialalo
    // niezaleznie od tego kto sie akurat zaloguje (nie tylko instalujacy admin).
    ShellExec(ExpandConstant('{sys}\schtasks.exe'),
      '/Create /TN "' + TaskName + '" /TR "\"' + ExpandConstant('{app}\tray\screentime-ad-tray.exe') + '\"" ' +
      '/SC ONLOGON /RL LIMITED /RU "BUILTIN\Users" /F');
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    if FileExists(ExpandConstant('{app}\nssm.exe')) then
    begin
      NssmExec('stop ' + SvcName);
      NssmExec('remove ' + SvcName + ' confirm');
    end;
    ShellExec(ExpandConstant('{sys}\schtasks.exe'), '/Delete /TN "' + TaskName + '" /F');
    ShellExec(ExpandConstant('{sys}\taskkill.exe'), '/IM screentime-ad-tray.exe /F');
  end;
end;
