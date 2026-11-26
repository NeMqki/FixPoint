; FixPoint のインストーラー (Inno Setup)。publish.py が ISCC で fixpoint-setup.exe にする。
; 直接ビルドするときは、publish.py が作る build/ フォルダを /D で渡す:
;   ISCC /DAppVer=0.6.0 /DAppDir=..\build\app /DPyDir=..\build\python /DAppExe=..\desktop\target\release\fixpoint-app.exe /DUpdateSource=https://... fixpoint.iss
;
; テスト用のオプション (コマンドライン):
;   /TEST=1  PATH・ショートカット・「インストールされているアプリ」への登録をしない

#ifndef AppVer
  #define AppVer "0.0.0"
#endif
#ifndef UpdateSource
  #define UpdateSource ""
#endif
#ifndef OutDir
  #define OutDir "..\dist"
#endif

[Setup]
AppId=FixPoint
AppName=FixPoint
AppVersion={#AppVer}
AppVerName=FixPoint {#AppVer}
AppPublisher=NeMqki
AppPublisherURL=https://github.com/NeMqki/FixPoint
AppSupportURL=https://github.com/NeMqki/FixPoint/issues
; 以前の .cmd 版と同じ場所。設定や履歴はそのまま引き継がれる
DefaultDirName={localappdata}\Programs\fixpoint
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutDir}
OutputBaseFilename=fixpoint-setup
SetupIconFile=fixpoint.ico
UninstallDisplayIcon={app}\FixPoint.exe
UninstallDisplayName=FixPoint
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ChangesEnvironment=yes
CloseApplications=no
CreateUninstallRegKey=not IsTest
VersionInfoVersion={#AppVer}
VersionInfoDescription=FixPoint セットアップ

[Languages]
Name: "ja"; MessagesFile: "compiler:Languages\Japanese.isl"

[Messages]
ja.WelcomeLabel2=複数のAIが「これでいいね」と言うまで、一緒に作業を続けてもらうツールです。%n%nPython も一緒に入るので、ほかに準備するものはありません。

[Tasks]
Name: "desktopicon"; Description: "デスクトップに FixPoint のアイコンを作る"; Check: not IsTest

[InstallDelete]
; 以前の .cmd 版のアンインストーラーと、古い Python のキャッシュ
Type: files; Name: "{app}\uninstall.ps1"
Type: filesandordirs; Name: "{app}\__pycache__"
; v0.5 で Edge の専用ウィンドウに使っていたプロファイル (今は FixPoint アプリの webview/ を使う)
Type: filesandordirs; Name: "{app}\window"

[Files]
Source: "{#AppDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs
Source: "{#PyDir}\*"; DestDir: "{app}\python"; Flags: ignoreversion recursesubdirs
Source: "fixpoint.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#AppExe}"; DestDir: "{app}"; DestName: "FixPoint.exe"; Flags: ignoreversion

[Icons]
Name: "{userprograms}\FixPoint"; Filename: "{app}\FixPoint.exe"; WorkingDir: "{app}"; Comment: "複数のAIが合意するまで一緒に作業する"; Check: not IsTest
Name: "{userdesktop}\FixPoint"; Filename: "{app}\FixPoint.exe"; WorkingDir: "{app}"; Comment: "複数のAIが合意するまで一緒に作業する"; Tasks: desktopicon

[Registry]
; fixpoint コマンド
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}\bin"; Check: NeedsAddPath
; 以前の .cmd 版の「インストールされているアプリ」の登録 (二重に出ないように消す)
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Uninstall\fixpoint"; ValueType: none; Flags: deletekey; Check: not IsTest

[Run]
Filename: "{app}\python\python.exe"; Parameters: "-I ""{app}\updater.py"" configure ""{#UpdateSource}"""; Flags: runhidden waituntilterminated
Filename: "{app}\FixPoint.exe"; WorkingDir: "{app}"; Description: "FixPoint を起動する"; Flags: postinstall nowait skipifsilent

[UninstallDelete]
; 自動更新で入ったファイル・バックアップ・設定もまとめて消す (各プロジェクトの .fixpoint/ は残る)
Type: filesandordirs; Name: "{app}"

[Code]
function IsTest: Boolean;
begin
  Result := ExpandConstant('{param:TEST|0}') = '1';
end;

function BinDir: String;
begin
  Result := ExpandConstant('{app}\bin');
end;

function NeedsAddPath: Boolean;
var
  Path: String;
begin
  if IsTest then begin
    Result := False;
    exit;
  end;
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', Path) then begin
    Result := True;
    exit;
  end;
  Result := Pos(';' + Uppercase(BinDir) + ';', ';' + Uppercase(Path) + ';') = 0;
end;

procedure RemoveFromPath;
var
  Path, Item, Rest, NewPath: String;
  P: Integer;
begin
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', Path) then exit;
  Rest := Path;
  NewPath := '';
  while Rest <> '' do begin
    P := Pos(';', Rest);
    if P = 0 then begin
      Item := Rest;
      Rest := '';
    end else begin
      Item := Copy(Rest, 1, P - 1);
      Rest := Copy(Rest, P + 1, Length(Rest));
    end;
    if (Item <> '') and (CompareText(Item, BinDir) <> 0) then begin
      if NewPath <> '' then NewPath := NewPath + ';';
      NewPath := NewPath + Item;
    end;
  end;
  if NewPath <> Path then
    RegWriteExpandStringValue(HKCU, 'Environment', 'Path', NewPath);
end;

{ 起動中の FixPoint に終了をお願いする (ファイルを置き換えられるように) }
procedure StopRunning(const Dir: String);
var
  Cmd, Cfg, DirQ: String;
  Code: Integer;
begin
  Cfg := Dir + '\config.json';
  StringChangeEx(Cfg, '''', '''''', True);
  DirQ := Dir + '\';
  StringChangeEx(DirQ, '''', '''''', True);
  Cmd := '$p=8765;try{$p=[int]((Get-Content -Raw -Encoding UTF8 -LiteralPath ''' + Cfg + ''' | ConvertFrom-Json).port)}catch{};' +
         'if(-not $p){$p=8765};' +
         'try{Invoke-WebRequest -UseBasicParsing -Method Post -Uri (''http://127.0.0.1:''+$p+''/api/shutdown'') ' +
         '-Headers @{''X-Fixpoint''=''1''} -Body ''{}'' -ContentType ''application/json'' -TimeoutSec 2 | Out-Null;' +
         'Start-Sleep -Milliseconds 1500}catch{};' +
         { FixPoint アプリ (FixPoint.exe) も閉じる。ほかの場所にある同じ名前のアプリには触らない }
         '$d=''' + DirQ + ''';Get-Process -Name FixPoint -ErrorAction SilentlyContinue | ' +
         'Where-Object {$_.Path -and $_.Path.StartsWith($d,[StringComparison]::OrdinalIgnoreCase)} | ' +
         'Stop-Process -Force -ErrorAction SilentlyContinue';
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
       '-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "' + Cmd + '"',
       '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopRunning(ExpandConstant('{app}'));
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    StopRunning(ExpandConstant('{app}'));
  if CurUninstallStep = usPostUninstall then
    RemoveFromPath;
end;
