<#
.SYNOPSIS
    Configures Windows Task Scheduler for Jinora Autonomous Competitor Sweeps and Evaluation Queue.
    
.DESCRIPTION
    Creates scheduled tasks on Windows:
    1. Jinora-EvalQueue-Nocturna: Runs every day at 01:30 (America/Lima) evaluating pending ads in off-peak window.
    2. Jinora_Biweekly_Sweep: Runs on the 1st and 15th of every month at 09:00 (full sweep).
    3. Jinora_Daily_Pending_Catchup: Runs every day at 09:30 (resumes pending evaluations without scraping).
    
    All tasks include 'StartWhenAvailable' to recover executions if the machine was offline.
    Default mode is simulation (dry-run). Use -Register to actually register the tasks.

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

# Task 1: Nightly Evaluation Queue (Daily at 01:30 Lima time in off-peak window)
$TaskEvalName = "Jinora-EvalQueue-Nocturna"
$ActionEval = New-ScheduledTaskAction `
    -Execute "cmd.exe" `
    -Argument "/c python main.py queue --run --respect-hours >> logs\nightly_eval.log 2>&1" `
    -WorkingDirectory $ProjectRoot

$TriggerEval = New-ScheduledTaskTrigger -Daily -At "01:30"
$SettingsEval = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew

# Task 2: Bi-weekly Full Sweep (Days 1 & 15 at 09:00)
$Task1Name = "Jinora_Biweekly_Sweep"
$Action1 = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "-m src.cli run --all" `
    -WorkingDirectory $ProjectRoot

# New-ScheduledTaskTrigger has no monthly mode, so build the CIM monthly trigger: days 1 and 15 of every month.
# DaysOfMonth is a bitmask (bit 0 = day 1, bit 14 = day 15); MonthOfYear 4095 = all 12 months.
$MonthlyTriggerClass = Get-CimClass -ClassName MSFT_TaskMonthlyTrigger -Namespace Root/Microsoft/Windows/TaskScheduler
$SweepDaysMask = [uint16]((1 -shl 0) -bor (1 -shl 14))
$Trigger1A = New-CimInstance -CimClass $MonthlyTriggerClass -ClientOnly -Property @{
    Enabled       = $true
    DaysOfMonth   = $SweepDaysMask
    MonthOfYear   = [uint16]4095
    StartBoundary = (Get-Date -Hour 9 -Minute 0 -Second 0).ToString("yyyy-MM-dd'T'HH:mm:ss")
}
$SweepDays = (1..31 | Where-Object { $SweepDaysMask -band (1 -shl ($_ - 1)) }) -join " y "
if ($SweepDays -ne "1 y 15") { throw "Monthly trigger misconfigured: days = $SweepDays (expected 1 y 15)" }
$Settings1 = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew

# Task 3: Daily Pending Catchup (Daily at 09:30)
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

# Validate (in memory, nothing is registered) that Task Scheduler accepts the monthly trigger inside a task definition
$SweepDefinition = New-ScheduledTask -Action $Action1 -Trigger $Trigger1A -Settings $Settings1
if ($SweepDefinition.Triggers[0].CimClass.CimClassName -ne "MSFT_TaskMonthlyTrigger") { throw "Sweep task does not use a monthly trigger" }

if (-not $Register) {
    Write-Host "[DRY RUN] Script is validated. To register tasks in Task Scheduler, run:" -ForegroundColor Yellow
    Write-Host "powershell -ExecutionPolicy Bypass -File .\scripts\schedule_windows.ps1 -Register" -ForegroundColor White
    Write-Host "`nTasks that will be created:"
    Write-Host " 1. $TaskEvalName -> cmd.exe /c python main.py queue --run --respect-hours >> logs\nightly_eval.log 2>&1 (Diario 01:30 Lima)"
    Write-Host " 2. $Task1Name -> $PythonExe -m src.cli run --all (1 y 15 de cada mes a las 09:00)"
    Write-Host " 3. $Task2Name -> $PythonExe -m src.cli evaluate --pending (Diario 09:30)"
    Write-Host " Trigger de la tarea 2 validado: MONTHLY, dias $SweepDays de todos los meses (DaysOfMonth=$SweepDaysMask), 09:00."
    Write-Host " Option 'StartWhenAvailable' enabled for battery & offline recovery."
    exit 0
}

# Registration block (only when -Register is explicitly passed)
try {
    Register-ScheduledTask `
        -TaskName $TaskEvalName `
        -Action $ActionEval `
        -Trigger $TriggerEval `
        -Settings $SettingsEval `
        -Description "Jinora Evaluation Queue - Evaluacion nocturna en ventana de baja demanda (01:30 Lima)" `
        -Force | Out-Null
    Write-Host "[OK] Successfully registered: $TaskEvalName" -ForegroundColor Green

    Register-ScheduledTask `
        -TaskName $Task1Name `
        -Action $Action1 `
        -Trigger $Trigger1A `
        -Settings $Settings1 `
        -Description "Jinora Autonomous Competitor Harvester - Bi-weekly full sweep" `
        -Force | Out-Null
    Write-Host "[OK] Successfully registered: $Task1Name" -ForegroundColor Green

    Register-ScheduledTask `
        -TaskName $Task2Name `
        -Action $Action2 `
        -Trigger $Trigger2 `
        -Settings $Settings2 `
        -Description "Jinora Quota Catchup - Daily evaluation of pending competitor ads" `
        -Force | Out-Null
    Write-Host "[OK] Successfully registered: $Task2Name" -ForegroundColor Green
} catch {
    $err = $_.ToString()
    Write-Error "Failed to register scheduled tasks: $err"
}
