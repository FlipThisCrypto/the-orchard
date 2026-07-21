# SPDX-License-Identifier: Apache-2.0
# Live activation preflight — run before posting or daily during Activation Week.
# Usage:  powershell -File tools/preflight_orchard.ps1
# Exit 0 = all required checks OK; 1 = one or more failed.

$ErrorActionPreference = 'Continue'
$checks = @(
    @{ Name = 'home';     Uri = 'https://theorchard.network/';                         RequireJson = $false },
    @{ Name = 'flash';    Uri = 'https://flash.theorchard.network/';                 RequireJson = $false },
    @{ Name = 'manifest'; Uri = 'https://flash.theorchard.network/manifest.json';      RequireJson = $true },
    @{ Name = 'claim';    Uri = 'https://oracle.theorchard.network/claim';              RequireJson = $false },
    @{ Name = 'health';   Uri = 'https://oracle.theorchard.network/health';             RequireJson = $true },
    @{ Name = 'view';     Uri = 'https://view.theorchard.network/';                   RequireJson = $false },
    @{ Name = 'nodes';    Uri = 'https://view.theorchard.network/api/nodes';            RequireJson = $true },
    @{ Name = 'stats';    Uri = 'https://view.theorchard.network/api/network/stats';    RequireJson = $true },
    @{ Name = 'worldview';Uri = 'https://worldview.theorchard.network/';              RequireJson = $false }
)

$failed = 0
Write-Host "Orchard preflight $(Get-Date -Format 'u')"
Write-Host ('-' * 60)

foreach ($c in $checks) {
    try {
        $r = Invoke-WebRequest -Uri $c.Uri -UseBasicParsing -TimeoutSec 25
        $ok = ($r.StatusCode -ge 200 -and $r.StatusCode -lt 300)
        $extra = ''
        if ($ok -and $c.RequireJson) {
            try {
                $j = $r.Content | ConvertFrom-Json
                if ($c.Name -eq 'health') {
                    if ($j.ok -ne $true) { $ok = $false; $extra = ' ok!=true' }
                    elseif ($null -ne $j.db -and $j.db -ne 'ok') {
                        $ok = $false; $extra = " db=$($j.db)"
                    }
                    elseif ($null -ne $j.flags) {
                        $extra = " db=$($j.db) require_seq=$($j.flags.require_seq) body_cap=$($j.flags.max_reading_body_bytes)"
                        if ($null -ne $j.metrics) {
                            $extra += " accepted=$($j.metrics.readings_accepted) replay_rej=$($j.metrics.readings_rejected_replay_seq)"
                        }
                    }
                }
                if ($c.Name -eq 'manifest') { $extra = " version=$($j.version)" }
                if ($c.Name -eq 'nodes') {
                    $n = @($j).Count
                    $extra = " trees=$n"
                }
                if ($c.Name -eq 'stats') {
                    $extra = " registered=$($j.trees_registered) active24h=$($j.trees_active_24h) readings24h=$($j.readings_last_24h)"
                }
            } catch {
                $ok = $false
                $extra = ' invalid JSON'
            }
        }
        if ($ok) {
            Write-Host ("OK  {0,-10} {1}{2}" -f $c.Name, $r.StatusCode, $extra)
        } else {
            Write-Host ("FAIL {0,-10} {1}{2}" -f $c.Name, $r.StatusCode, $extra)
            $failed++
        }
    } catch {
        Write-Host ("FAIL {0,-10} {1}" -f $c.Name, $_.Exception.Message)
        $failed++
    }
}

Write-Host ('-' * 60)
if ($failed -eq 0) {
    Write-Host 'PREFLIGHT PASSED'
    exit 0
}
Write-Host "PREFLIGHT FAILED ($failed check(s))"
exit 1
