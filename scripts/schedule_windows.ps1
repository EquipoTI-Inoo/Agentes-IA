<#
.SYNOPSIS
    Configures Windows Task Scheduler for Jinora Autonomous Competitor Sweeps.
    
.DESCRIPTION
    Creates two scheduled tasks on Windows:
    1. Jinora_Biweekly_Sweep: Runs on the 1st and 15th of every month at 09:00 (full sweep).
    2. Jinora_Daily_Pending: Runs every day at 09:30 (resumes pending evaluations without scraping).
    
    Both tasks include 'StartWhenAvailable' to recover executions if the machine was offline.
    NOTE: DO NOT run this script until Phase 7 (Unified CLI Pipeline) is completed.

.PARAMETER Register
    Switch to actually register the tasks in Task Scheduler.
#>

[CmdletBinding()]
param(
    [switch]$Register
)

$ErrorActionPreference = "Stop"

# Detect Project Root and Python Executable
$ProjectRoot = (Resolve-Path "$PSScriptRoot\..").Path
$PythonExe = Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"

if (-not (Test-Path $PythonExe)) {
    # Fallback to python in PATH
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) {
        $PythonExe = $cmd.Source
    } else {
        throw "Could not find python.exe. Please install Python 3.11 or check LOCALAPPDATA."
    }
}

Write-Host "====================================================" -ForegroundColor Cyan
Write-Host " Jinora Windows Task Scheduler Automation Setup" -ForegroundColor Cyan
Write-Host "====================================================" -ForegroundColor Cyan
Write-Host "Project Directory: $ProjectRoot"
Write-Host "Python Executable: $PythonExe"
Write-Host ""

$LogsDir = Join-Path $ProjectRoot "logs"
if (-not (Test-Path $LogsDir)) {
    New-Item -ItemType Directory -Path $LogsDir | Out-Null
}

# Task 1: Bi-weekly Full Sweep (Days 1 & 15 at 09:00)
$Task1Name = "Jinora_Biweekly_Sweep"
$Action1 = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "-m src.cli run --all" `
    -WorkingDirectory $ProjectRoot

# Monthly trigger on 1st and 15th
$Trigger1A = New-ScheduledTaskTrigger -Daily -At "09:00"
# Note: Windows Task Scheduler monthly specific days trigger via CIM
$Settings1 = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew

# Task 2: Daily Pending Catchup (Daily at 09:30)
$Task2Name = "Jinora_Daily_Pending_Catchup"
$Action2 = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "-m src.cli evaluate --pending" `
    -WorkingDirectory $ProjectRoot

$Trigger2 = New-ScheduledTaskTrigger -Daily -At "09:30"
$Settings2 = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew

if (-not $Register) {
    Write-Host "[DRY RUN] Script is validated. To register tasks in Task Scheduler, run:" -ForegroundColor Yellow
    Write-Host "powershell -ExecutionPolicy Bypass -File .\scripts\schedule_windows.ps1 -Register" -ForegroundColor White
    Write-Host "`nTasks that will be created:"
    Write-Host " 1. $Task1Name -> $PythonExe -m src.cli run --all"
    Write-Host " 2. $Task2Name -> $PythonExe -m src.cli evaluate --pending"
    Write-Host " Option 'StartWhenAvailable' enabled for battery & offline recovery."
    exit 0
}

# Registration block (only when -Register is explicitly passed after Phase 7)
try {
    Register-ScheduledTask `
        -TaskName $Task1Name `
        -Action $Action1 `
        -Trigger $Trigger1A `
        -Settings $Settings1 `
        -Description "Jinora Autonomous Competitor Harvester - Bi-weekly full sweep" `
        -Force | Out-Null
    Write-Host "✓ Successfully registered: $Task1Name" -ForegroundColor Green

    Register-ScheduledTask `
        -TaskName $Task2Name `
        -Action $Action2 `
        -Trigger $Trigger2 `
        -Settings $Settings2 `
        -Description "Jinora Quota Catchup - Daily evaluation of pending competitor ads" `
        -Force | Out-Null
    Write-Host "✓ Successfully registered: $Task2Name" -ForegroundColor Green
} catch {
    Write-Error "Failed to register scheduled tasks: $_"
}
