# SPDX-License-Identifier: Apache-2.0
# Register (or update) the Orchard pipeline as Windows Scheduled Tasks.
#
#   powershell -ExecutionPolicy Bypass -File tools\schedule_windows.ps1 [-Unregister]
#
# WHAT IT SCHEDULES, AND WHAT IT DELIBERATELY DOES NOT
# ====================================================
#
#   Orchard Publish    hourly :10   publish device-signed readings to DataLayer
#   Orchard Attest     daily 00:25  seal closed seasons (skips no-evidence ones)
#   Orchard Settle     daily 00:40  economics settle --all --yes (ledger write,
#                                   NOT a spend)
#   Orchard Status     daily 08:00  status + audit into the ops log
#
# PAYING IS NOT SCHEDULED. `economics pay` stays a human act by design: it
# requires DRY_RUN=false, an explicit flag, external ceilings and a wallet id,
# and this script will not launder that decision through a timer. The daily
# Status task prints the unpaid backlog so the human knows when to act.
#
# Every scheduled job already carries its own safety rails: the shared writer
# lock (a timer tick overlapping a manual run refuses, exit 64), watermarks,
# the no-baseline refusal, provenance gates, and the placeholder skip. This
# script adds ONLY the timers.
#
# The tasks run as the current user, non-elevated, only when logged on —
# the wallet daemon runs in the user session anyway. Output is appended to
# orchard_chia\data\ops\scheduler-*.log so a silent failure leaves a trail.

[CmdletBinding()]
param(
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = "python"
$LogDir = Join-Path $RepoRoot "orchard_chia\data\ops"
New-Item -ItemType Directory -Force $LogDir | Out-Null

$Tasks = @(
    @{ Name = "Orchard Publish"
       Args = "-m orchard_chia.datalayer publish"
       Trigger = { New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
                     -RepetitionInterval (New-TimeSpan -Hours 1) `
                     -RepetitionDuration (New-TimeSpan -Days 3650) } }
    @{ Name = "Orchard Attest"
       Args = "-m orchard_chia.datalayer attest"
       Trigger = { New-ScheduledTaskTrigger -Daily -At "00:25" } }
    @{ Name = "Orchard Settle"
       Args = "-m orchard_chia.economics settle --all --yes"
       Trigger = { New-ScheduledTaskTrigger -Daily -At "00:40" } }
    @{ Name = "Orchard Status"
       Args = "-m orchard_chia.economics status"
       Trigger = { New-ScheduledTaskTrigger -Daily -At "08:00" } }
)

if ($Unregister) {
    foreach ($t in $Tasks) {
        try {
            Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false -ErrorAction Stop
            Write-Host "removed: $($t.Name)"
        } catch {
            Write-Host "not present: $($t.Name)"
        }
    }
    exit 0
}

foreach ($t in $Tasks) {
    $log = Join-Path $LogDir ("scheduler-" + ($t.Name -replace ' ', '-').ToLower() + ".log")
    # cmd wrapper so stdout+stderr land in the log with a timestamp header —
    # Task Scheduler itself keeps no output, and an invisible failure is the
    # failure mode this whole pipeline keeps designing against.
    $cmdLine = "/c cd /d `"$RepoRoot`" && echo ---- %DATE% %TIME% ---- >> `"$log`" && $Python $($t.Args) >> `"$log`" 2>&1"
    $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $cmdLine -WorkingDirectory $RepoRoot
    $trigger = & $t.Trigger
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
        -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
        -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $t.Name -Action $action -Trigger $trigger `
        -Settings $settings -Force | Out-Null
    Write-Host "registered: $($t.Name)  ->  $log"
}

Write-Host ""
Write-Host "Scheduled. Publish hourly at :10; attest 00:25; settle 00:40; status 08:00."
Write-Host "Paying stays manual by design:  python -m orchard_chia.economics pay"
Write-Host "Remove everything with:  tools\schedule_windows.ps1 -Unregister"
