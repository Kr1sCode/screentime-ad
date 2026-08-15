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
; restartreplace: jesli mimo taskkill jakis plik nadal jest zablokowany
; (np. Defender akurat go skanuje), instalator NIE wywala sie na twardo —
; podmiana tego pliku zostaje odlozona do najblizszego restartu zamiast
; przerywac cala instalacje.
Source: "..\dist\screentime-ad-agent\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion restartreplace
Source: "..\dist\screentime-ad-tray\*"; DestDir: "{app}\tray"; Flags: recursesubdirs ignoreversion restartreplace
Source: "nssm.exe"; DestDir: "{app}"; Flags: ignoreversion restartreplace

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

// Osobna wersja bez czekania — tray.exe zyje az do wylogowania, ewWaitUntilTerminated
// zawiesiloby instalator na dobre.
procedure ShellExecNoWait(Cmd, Params: String);
var
  ResultCode: Integer;
begin
  Exec(Cmd, Params, '', SW_SHOWNORMAL, ewNoWait, ResultCode);
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
    // abort/retry jesli nie zatrzymamy jej PRZED nadpisaniem. NssmExec('stop')
    // czasem nie zdąża/nie potrafi zabić procesu (np. utknął w sieciowym
    // I/O) — taskkill /F to twardy fallback, żeby reinstalacja nie wisiała.
    if FileExists(ExpandConstant('{app}\nssm.exe')) then
    begin
      NssmExec('stop ' + SvcName);
      ShellExec(ExpandConstant('{sys}\taskkill.exe'), '/T /F /IM screentime-ad-agent.exe');
    end;
    // Tray dziala caly czas w sesji uzytkownika (to caly jego sens) — bez
    // zabicia go tutaj, [Files] wisi na "plik w uzyciu" przy kazdym
    // upgrade, bo trzyma otwarte wlasne .exe/.dll w {app}\tray.
    ShellExec(ExpandConstant('{sys}\taskkill.exe'), '/T /F /IM screentime-ad-tray.exe');
    // Krotki oddech — nawet po /F proces bywa formalnie martwy, a uchwyty
    // plikow (zwlaszcza gdy Defender akurat skanuje) zwalniaja sie z
    // niewielkim opoznieniem. [Files] ma i tak restartreplace jako siatke
    // bezpieczenstwa, ale to zmniejsza szanse ze w ogole trzeba z niej korzystac.
    Sleep(1500);
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

    // Trigger ONLOGON odpali sie dopiero przy nastepnym zalogowaniu — zeby
    // instalujacy zobaczyl ikone od razu (bez wylogowania), startujemy ja
    // tez teraz, w biezacej sesji. Najpierw dobijamy ewentualny stary
    // proces po starej instalacji (inna sciezka pliku = stary uchwyt zombie).
    ShellExec(ExpandConstant('{sys}\taskkill.exe'), '/IM screentime-ad-tray.exe /F');
    ShellExecNoWait(ExpandConstant('{app}\tray\screentime-ad-tray.exe'), '');
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
