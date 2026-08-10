# screentime-ad agent installer — uruchom jako Administrator w PowerShell.
#
#   irm https://raw.githubusercontent.com/Kr1sCode/screentime-ad/main/agent_windows/install.ps1 | iex
#
param(
  [string]$Server = $(Read-Host "Adres serwera (np. http://172.19.19.22)"),
  [string]$Token  = $(Read-Host "Token agenta (z panelu, sekcja Konfiguracja)")
)
$ErrorActionPreference = "Stop"
$Repo = "Kr1sCode/screentime-ad"
$Branch = "main"
$InstallDir = "$env:ProgramData\screentime-ad"

# Idempotentne dla GPO Computer Startup Script — usluga juz dziala i
# aktualizuje sie sama (agent.py sam sciaga nowe wersje z gita), wiec
# przy kolejnych bootach nic tu nie robimy.
$existing = Get-Service screentime-ad-agent -ErrorAction SilentlyContinue
if ($existing -and $existing.Status -eq "Running") {
    Write-Host "screentime-ad-agent juz dziala — pomijam instalacje."
    exit 0
}

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

$python = Get-Command python.exe -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Host "Python nie znaleziony — instaluję przez winget..."
    winget install --id Python.Python.3.12 -e --silent --accept-package-agreements --accept-source-agreements
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
}
if (-not $python) { throw "Nie udało się znaleźć/zainstalować Pythona 3" }

$tmp = New-Item -ItemType Directory -Path (Join-Path $env:TEMP ([guid]::NewGuid()))
try {
    $tarPath = Join-Path $tmp "src.tar.gz"
    Invoke-WebRequest -Uri "https://github.com/$Repo/archive/refs/heads/$Branch.tar.gz" -OutFile $tarPath
    tar -xzf $tarPath -C $tmp
    $src = Get-ChildItem $tmp -Directory | Select-Object -First 1
    Copy-Item "$($src.FullName)\agent_windows\agent.py" $InstallDir -Force

    $config = @{ server_url = $Server; token = $Token } | ConvertTo-Json
    Set-Content -Path (Join-Path $InstallDir "agent.json") -Value $config -Encoding UTF8

    $nssmExe = Join-Path $InstallDir "nssm.exe"
    if (-not (Test-Path $nssmExe)) {
        $nssmZip = Join-Path $tmp "nssm.zip"
        Invoke-WebRequest -Uri "https://nssm.cc/release/nssm-2.24.zip" -OutFile $nssmZip
        Expand-Archive $nssmZip -DestinationPath $tmp
        Copy-Item (Join-Path $tmp "nssm-2.24\win64\nssm.exe") $nssmExe -Force
    }

    if (Get-Service screentime-ad-agent -ErrorAction SilentlyContinue) {
        & $nssmExe stop screentime-ad-agent
        & $nssmExe remove screentime-ad-agent confirm
    }
    & $nssmExe install screentime-ad-agent $python.Source "`"$InstallDir\agent.py`""
    & $nssmExe set screentime-ad-agent AppDirectory $InstallDir
    & $nssmExe set screentime-ad-agent Start SERVICE_AUTO_START
    & $nssmExe set screentime-ad-agent AppRestartDelay 5000
    & $nssmExe start screentime-ad-agent

    Write-Host "==> agent screentime-ad zainstalowany i uruchomiony jako usluga Windows"
} finally {
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
