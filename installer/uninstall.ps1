# fixpoint uninstaller (インストール先に置かれ、「インストールされているアプリ」から呼ばれる)
# FIXPOINT_UNINSTALL_YES=1 で確認を省略 (テスト用)
$ErrorActionPreference = 'Continue'
$dest = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not (Test-Path (Join-Path $dest 'launcher.py'))) { Write-Host "FixPoint のフォルダではありません: $dest" -ForegroundColor Red; exit 1 }

if (-not $env:FIXPOINT_UNINSTALL_YES) {
    $ans = Read-Host "FixPoint をアンインストールします ($dest)。よろしいですか？ [y/N]"
    if ($ans -notmatch '^(y|yes)$') { Write-Host '中止しました'; exit 0 }
}

# 起動中なら終了
$port = 8765
try { $port = [int]((Get-Content (Join-Path $dest 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json).port) } catch {}
try { Invoke-WebRequest -UseBasicParsing -Method Post -Uri "http://127.0.0.1:$port/api/shutdown" -Headers @{ 'X-Fixpoint' = '1' } -Body '{}' -ContentType 'application/json' -TimeoutSec 2 | Out-Null; Start-Sleep -Milliseconds 800 } catch {}

# ショートカット
foreach ($dir in @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop'))) {
    $lnk = Join-Path $dir 'FixPoint.lnk'
    if (Test-Path $lnk) { Remove-Item $lnk -Force }
}
# PATH
$bin = Join-Path $dest 'bin'
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if ($userPath) {
    $new = ($userPath.Split(';') | Where-Object { $_ -and $_ -ne $bin }) -join ';'
    if ($new -ne $userPath) { [Environment]::SetEnvironmentVariable('Path', $new, 'User') }
}
# 「アプリ」登録
Remove-Item 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\fixpoint' -Recurse -Force -ErrorAction SilentlyContinue

# フォルダ本体 (このスクリプト自身が中にあるので、終了後に別プロセスで消す)
# 各プロジェクトの .fixpoint/ フォルダ (ログ) はそのまま残ります
Start-Process -FilePath 'cmd.exe' -ArgumentList "/c timeout /t 2 /nobreak >nul & rmdir /s /q `"$dest`"" -WindowStyle Hidden
Write-Host 'FixPoint をアンインストールしました。' -ForegroundColor Cyan
