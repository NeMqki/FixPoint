# fixpoint installer (publish.py が fixpoint-setup.cmd に埋め込んで使う)
# 環境変数でのオプション (テスト用):
#   FIXPOINT_INSTALL_DIR   インストール先 (既定: %LOCALAPPDATA%\Programs\fixpoint)
#   FIXPOINT_SETUP_SOURCE  ペイロードの代わりにこのフォルダのファイルを使う
#   FIXPOINT_SETUP_FLAGS   カンマ区切り: nolaunch,noshortcut,nopath,noregistry,nopause
$ErrorActionPreference = 'Stop'
$AppVersion   = '__VERSION__'
$UpdateSource = '__UPDATE_SOURCE__'
if ($UpdateSource -like '__UPDATE*') { $UpdateSource = '' }

$flags = @(); if ($env:FIXPOINT_SETUP_FLAGS) { $flags = $env:FIXPOINT_SETUP_FLAGS.Split(',') | ForEach-Object { $_.Trim() } }
function Has($f) { return $flags -contains $f }
function Say($m, $c = 'Gray') { Write-Host $m -ForegroundColor $c }
function Pause-End { if (-not (Has 'nopause')) { Read-Host 'Enter で閉じます' | Out-Null } }
$utf8 = New-Object System.Text.UTF8Encoding $false

function Find-Python {
    foreach ($c in @('py -3', 'python', 'python3')) {
        $parts = $c.Split(' ')
        if (-not (Get-Command $parts[0] -ErrorAction SilentlyContinue)) { continue }
        try {
            $a = @()
            if ($parts.Count -gt 1) { $a += $parts[1] }
            $a += @('-c', 'import sys;assert sys.version_info>=(3,9);print(sys.executable)')
            $out = & $parts[0] @a 2>$null
            if ($LASTEXITCODE -eq 0 -and $out) {
                $p = ($out | Select-Object -Last 1).Trim()
                if (Test-Path $p) { return $p }
            }
        } catch {}
    }
    return $null
}

try {
    Say ''
    Say "  FixPoint $AppVersion セットアップ" Cyan
    Say '  複数のAIが合意するまで一緒に作業するツール' DarkGray
    Say ''

    $dest = if ($env:FIXPOINT_INSTALL_DIR) { $env:FIXPOINT_INSTALL_DIR } else { Join-Path $env:LOCALAPPDATA 'Programs\fixpoint' }

    # 1. Python
    $py = Find-Python
    if (-not $py) {
        Say 'Python 3.9 以上が見つかりませんでした。' Red
        Say '先に Python を入れてから、もう一度このセットアップを実行してください:' Yellow
        Say '  winget install Python.Python.3.12' White
        Say '  (または https://www.python.org/downloads/ から。インストール時に "Add python.exe to PATH" にチェック)' DarkGray
        Pause-End; exit 1
    }
    $pyw = Join-Path (Split-Path $py) 'pythonw.exe'
    if (-not (Test-Path $pyw)) { $pyw = $py }
    Say "[1/6] Python: $py" Green

    # 2. 起動中なら止める
    $cfgPath = Join-Path $dest 'config.json'
    $port = 8765
    if (Test-Path $cfgPath) { try { $port = [int]((Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json).port) } catch {} ; if (-not $port) { $port = 8765 } }
    try {
        Invoke-WebRequest -UseBasicParsing -Method Post -Uri "http://127.0.0.1:$port/api/shutdown" -Headers @{ 'X-Fixpoint' = '1' } -Body '{}' -ContentType 'application/json' -TimeoutSec 2 | Out-Null
        Say '[2/6] 起動中の FixPoint を終了しました' Green
        Start-Sleep -Milliseconds 800
    } catch { Say '[2/6] 起動中の FixPoint はありません' Green }

    # 3. ファイルを展開してコピー
    $tmpRoot = Join-Path ([IO.Path]::GetTempPath()) ('fixpoint-setup-' + [guid]::NewGuid().ToString('N'))
    $tmp = $tmpRoot
    New-Item -ItemType Directory -Force $tmp | Out-Null
    if ($env:FIXPOINT_SETUP_SOURCE) {
        Copy-Item (Join-Path $env:FIXPOINT_SETUP_SOURCE '*') $tmp -Recurse -Force
    } else {
        $t = [IO.File]::ReadAllText($env:FIXPOINT_SETUP_SELF, [Text.Encoding]::UTF8)
        $marker = '#__PAY' + 'LOAD__'
        $i = $t.IndexOf($marker)
        if ($i -lt 0) { throw 'インストーラーが壊れています (ペイロードが見つかりません)' }
        $b64 = ($t.Substring($i + $marker.Length) -replace '[^A-Za-z0-9+/=]', '')
        $zip = Join-Path $tmp 'payload.zip'
        [IO.File]::WriteAllBytes($zip, [Convert]::FromBase64String($b64))
        Expand-Archive -Path $zip -DestinationPath (Join-Path $tmp 'app') -Force
        Remove-Item $zip
        $tmp = Join-Path $tmp 'app'
    }
    New-Item -ItemType Directory -Force $dest | Out-Null
    Get-ChildItem $tmp | Where-Object { $_.Name -ne 'config.json' } | ForEach-Object { Copy-Item $_.FullName $dest -Recurse -Force }
    Say "[3/6] インストール先: $dest" Green

    # 4. 設定 (既存の設定は残し、更新元だけ最新に)
    $cfg = [ordered]@{ update_source = ''; auto_update = $true; check_interval_hours = 6; port = 8765; last_project = '' }
    if (Test-Path $cfgPath) {
        try { (Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $cfg[$_.Name] = $_.Value } } catch {}
    }
    if ($UpdateSource) { $cfg['update_source'] = $UpdateSource }
    [IO.File]::WriteAllText($cfgPath, ($cfg | ConvertTo-Json), $utf8)
    if ($cfg['update_source']) { Say "[4/6] 自動更新: オン（更新元 $($cfg['update_source'])）" Green }
    else { Say '[4/6] 自動更新: 更新元が未設定のためオフ（config.json の update_source で設定できます）' Yellow }

    # 5. コマンド・ショートカット
    $bin = Join-Path $dest 'bin'
    New-Item -ItemType Directory -Force $bin | Out-Null
    [IO.File]::WriteAllText((Join-Path $bin 'fixpoint.cmd'), "@echo off`r`n`"$py`" `"$dest\launcher.py`" %*`r`n", $utf8)
    if (-not (Has 'nopath')) {
        $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
        if (-not $userPath) { $userPath = '' }
        if (($userPath.Split(';') | Where-Object { $_ -eq $bin }).Count -eq 0) {
            [Environment]::SetEnvironmentVariable('Path', ($userPath.TrimEnd(';') + ';' + $bin).TrimStart(';'), 'User')
        }
    }
    if (-not (Has 'noshortcut')) {
        $ws = New-Object -ComObject WScript.Shell
        $places = @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop'))
        foreach ($dir in $places) {
            $lnk = $ws.CreateShortcut((Join-Path $dir 'FixPoint.lnk'))
            $lnk.TargetPath = $pyw
            $lnk.Arguments = "`"$dest\launcher.py`""
            $lnk.WorkingDirectory = $env:USERPROFILE
            $lnk.Description = '複数のAIが合意するまで一緒に作業する'
            $lnk.Save()
        }
    }
    Say '[5/6] スタートメニュー・デスクトップのショートカットと fixpoint コマンドを作成しました' Green

    # 6. 「アプリと機能」に登録 (アンインストール用)
    if (-not (Has 'noregistry')) {
        $key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\fixpoint'
        New-Item -Path $key -Force | Out-Null
        Set-ItemProperty $key DisplayName 'FixPoint'
        Set-ItemProperty $key DisplayVersion $AppVersion
        Set-ItemProperty $key Publisher 'NeMqki'
        Set-ItemProperty $key InstallLocation $dest
        Set-ItemProperty $key UninstallString "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$dest\uninstall.ps1`""
        Set-ItemProperty $key NoModify 1 -Type DWord
        Set-ItemProperty $key NoRepair 1 -Type DWord
    }
    Say '[6/6] Windows の「インストールされているアプリ」に登録しました' Green
    if ($tmpRoot -like '*fixpoint-setup-*') { Remove-Item $tmpRoot -Recurse -Force -ErrorAction SilentlyContinue }

    Say ''
    Say '  インストールが完了しました。' Cyan
    Say '  ・スタートメニュー / デスクトップの「FixPoint」から起動' Gray
    Say '  ・ターミナルなら、プロジェクトのフォルダで fixpoint（新しいターミナルから有効）' Gray
    Say '  ・AIのログインは各CLIで: claude → /login、grok login など' Gray
    Say ''
    if (-not (Has 'nolaunch')) {
        Start-Process -FilePath $pyw -ArgumentList "`"$dest\launcher.py`"" -WorkingDirectory $env:USERPROFILE
        Say '  FixPoint を起動しました（ブラウザが開きます）' Cyan
        if (-not (Has 'nopause')) { Start-Sleep -Seconds 4 }
    }
    exit 0
} catch {
    Say ''
    Say ('セットアップに失敗しました: ' + $_.Exception.Message) Red
    Pause-End
    exit 1
}
