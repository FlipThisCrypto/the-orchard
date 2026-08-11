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
#   Orchard Sync       daily 00:30  post chain-sealed attestations the oracle
#                                   is missing (reads chain, writes only the
#                                   oracle's own DB — no fee, no chain write)
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

# The Chia CLI that ships inside the GUI install. Every job below is a no-op
# without the daemons it starts, so this path is load-bearing, not incidental.
$ChiaExe = "C:\Program Files\Chia\resources\app.asar.unpacked\daemon\chia.exe"

$Tasks = @(
    # FIRST, because nothing else works without it. Chia was installed as a GUI
    # application with NO autostart entry of any kind — not a Run key, not a
    # Startup shortcut. It had simply been left running for six weeks, so the
    # dependency was invisible until the machine needed a reboot.
    #
    # A restart without this leaves every timer below firing into a closed
    # port, and takes the DataLayer HTTP server (8575) down with it. That
    # server is currently the ONLY source of the store's data — the store has
    # zero mirrors — so while it is down nobody on earth can retrieve the
    # readings the on-chain root commits to.
    #
    # TWO groups, and the names are not the ones you would guess. `data`
    # brings up the daemon, the wallet it depends on, and the data_layer
    # service on 8562 (`data_layer` is NOT a valid group name — it fails with
    # a usage error). `data_layer_http` is separate and serves the store's
    # contents on 8575; without it the root hash on chain still commits to
    # data nobody can fetch. Both are idempotent — already-running services
    # are reported and left alone.
    @{ Name = "Orchard Chia Daemon"
       Exe  = $ChiaExe
       Args = "start data data_layer_http"
       # 90s after logon: the network stack and any VPN want a moment, and a
       # daemon that fails to bind because the interface was not ready yet is
       # the same outage this task exists to prevent.
       # -User is required. Without it the trigger means "at logon of ANY
       # user", which Task Scheduler treats as an administrative registration
       # and refuses with Access Denied for a non-elevated caller. Scoped to
       # this account it registers without elevation — and this account is the
       # one that holds the wallet session anyway.
       Trigger = { $t = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
                   $t.Delay = "PT90S"
                   $t } }
    @{ Name = "Orchard Publish"
       Args = "-m orchard_chia.datalayer publish"
       Trigger = { New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(10) `
                     -RepetitionInterval (New-TimeSpan -Hours 1) `
                     -RepetitionDuration (New-TimeSpan -Days 3650) } }
    @{ Name = "Orchard Attest"
       Args = "-m orchard_chia.datalayer attest"
       Trigger = { New-ScheduledTaskTrigger -Daily -At "00:25" } }
    @{ Name = "Orchard Sync"
       Args = "-m orchard_chia.datalayer sync-oracle"
       Trigger = { New-ScheduledTaskTrigger -Daily -At "00:30" } }
    @{ Name = "Orchard Settle"
       Args = "-m orchard_chia.economics settle --all --yes"
       Trigger = { New-ScheduledTaskTrigger -Daily -At "00:40" } }
    @{ Name = "Orchard Status"
       Args = "-m orchard_chia.economics status"
       Trigger = { New-ScheduledTaskTrigger -Daily -At "08:00" } }
    @{ Name = "Orchard Verify"
       Args = "-m orchard_chia.datalayer verify-latest"
       Trigger = { New-ScheduledTaskTrigger -Daily -At "01:00" } }
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
    # Most jobs are python modules; the daemon starter is its own executable.
    $exe = if ($t.ContainsKey("Exe")) { "`"$($t.Exe)`"" } else { $Python }
    $cmdLine = "/c cd /d `"$RepoRoot`" && echo ---- %DATE% %TIME% ---- >> `"$log`" && $exe $($t.Args) >> `"$log`" 2>&1"
    $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $cmdLine -WorkingDirectory $RepoRoot
    $trigger = & $t.Trigger
    # AllowStartIfOnBatteries: the default REFUSES to start on battery and
    # stops a running task when battery begins — which would silently halt
    # publishing during exactly the power event most likely to matter.
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
        -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
        -MultipleInstances IgnoreNew `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $t.Name -Action $action -Trigger $trigger `
        -Settings $settings -Force | Out-Null
    Write-Host "registered: $($t.Name)  ->  $log"
}

Write-Host ""
Write-Host "Scheduled. Publish :10 hourly; attest 00:25; sync 00:30; settle 00:40; verify 01:00; status 08:00."
Write-Host "Paying stays manual by design:  python -m orchard_chia.economics pay"
Write-Host "Remove everything with:  tools\schedule_windows.ps1 -Unregister"
