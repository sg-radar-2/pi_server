# Deploy the Pi server to the Raspberry Pi (run on Windows from this folder)
#   powershell -ExecutionPolicy Bypass -File .\deploy.ps1
#   powershell -ExecutionPolicy Bypass -File .\deploy.ps1 -Pi radar@10.1.160.100
#   powershell -ExecutionPolicy Bypass -File .\deploy.ps1 -Pi radar@10.1.160.100 -WithRecordings
#
# Copies pi_server + tools/behavior/{situation_core.py, scenario_sim.py, models/*.pkl} to ~/doorradar
# on the Pi, then runs install.sh there. config.toml on the Pi is kept.
# -WithRecordings also copies recordings/*_tracks_raw.csv and labels*.csv to ~/doorradar/recordings
# (for judge_file.py and fake_radar.py --replay on the Pi).
param(
    [string]$Pi = 'radar@rad2pi.local',
    [string]$Dest = 'doorradar',
    [string]$Behavior = '',
    [switch]$WithRecordings
)
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot

# find tools/behavior (pi_server inside sg-radar-tools/tools, or next to sg-radar-tools)
if (-not $Behavior) {
    foreach ($cand in @("$here\..\behavior", "$here\..\sg-radar-tools\tools\behavior")) {
        if (Test-Path "$cand\situation_core.py") { $Behavior = (Resolve-Path $cand).Path; break }
    }
}
if (-not $Behavior -or -not (Test-Path "$Behavior\situation_core.py")) {
    throw 'tools\behavior not found. Pass it: -Behavior C:\...\sg-radar-tools\tools\behavior'
}
Write-Host "behavior: $Behavior" -ForegroundColor DarkGray

$stage = Join-Path $env:TEMP 'doorradar_deploy'
if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory -Force "$stage\behavior\models" | Out-Null

Copy-Item "$here\*.py", "$here\requirements.txt", "$here\install.sh", "$here\doorradar.service", "$here\config.example.toml" $stage
Copy-Item "$Behavior\situation_core.py", "$Behavior\scenario_sim.py" "$stage\behavior"
Copy-Item "$Behavior\models\*.pkl" "$stage\behavior\models"

if ($WithRecordings) {
    $rec = Join-Path $Behavior '..\..\recordings'
    if (-not (Test-Path $rec)) { throw "recordings folder not found: $rec" }
    New-Item -ItemType Directory -Force "$stage\recordings" | Out-Null
    Copy-Item "$rec\*_tracks_raw.csv", "$rec\labels*.csv" "$stage\recordings"
    Write-Host ("recordings: " + (Get-ChildItem "$stage\recordings").Count + " files") -ForegroundColor DarkGray
}

# install.sh must have LF line endings on the Pi
$sh = [IO.File]::ReadAllText("$stage\install.sh") -replace "`r`n", "`n"
[IO.File]::WriteAllText("$stage\install.sh", $sh)

$tgz = Join-Path $env:TEMP 'doorradar_deploy.tgz'
if (Test-Path $tgz) { Remove-Item -Force $tgz }
tar -czf $tgz -C $stage .
if ($LASTEXITCODE -ne 0) { throw 'tar failed' }

Write-Host "upload -> ${Pi}" -ForegroundColor Cyan
scp $tgz "${Pi}:/tmp/doorradar_deploy.tgz"
if ($LASTEXITCODE -ne 0) { throw 'scp failed' }

Write-Host "install on Pi" -ForegroundColor Cyan
ssh -t $Pi "mkdir -p ~/$Dest && tar -xzf /tmp/doorradar_deploy.tgz -C ~/$Dest && cd ~/$Dest && bash install.sh"
if ($LASTEXITCODE -ne 0) { throw 'install failed' }
Write-Host 'done' -ForegroundColor Green
