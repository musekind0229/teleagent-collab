<#
.SYNOPSIS
  One-command demo of the collab loop: goal -> Grok lead plans -> worker ->
  lead reviews (scope check, send back, rework) -> delivery report.

.DESCRIPTION
  -Mode rogue (default): the lead is the real Grok CLI; the worker is
    demo/rogue-agy.py, a scripted stand-in that oversteps on its first try
    (it edits an input file it was only asked to read) and behaves on the
    rework. Shows the lead catching it, collab restoring the file and sending
    the task back. About 1-2 minutes, a little Grok quota, no AGY quota.
  -Mode real: real Grok lead + real AGY worker from your account pool on a
    small coding goal. Several minutes, uses AGY and Grok quota.

  Everything runs in a fresh temp directory; the service is stopped at the
  end. Your own agy login is not touched (the pool no longer clears the
  system keyring). The delivery report is printed and saved as report.md.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File demo\agy-review-demo.ps1
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File demo\agy-review-demo.ps1 -Mode real -Pool G:\codex\projects\teleagent-collab\jobs\agy-account-pool.json
#>
param(
    [ValidateSet('rogue', 'real')][string]$Mode = 'rogue',
    [int]$Port = 8795,
    [string]$Pool = $env:COLLAB_AGY_ACCOUNT_POOL,
    [int]$TimeoutSec = 1200
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$demo = $PSScriptRoot
$work = Join-Path $env:TEMP ("collab-demo-" + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Force $work | Out-Null
$utf8 = New-Object System.Text.UTF8Encoding($false)

if ($Mode -eq 'real' -and -not $Pool) {
    throw "-Mode real needs -Pool <agy-account-pool.json> (or COLLAB_AGY_ACCOUNT_POOL)."
}

# Child-process environment. Saved and restored in finally.
$names = 'COLLAB_API_TOKEN', 'COLLAB_API_BASE', 'COLLAB_AGY_ACCOUNT_POOL', 'AGY_BIN', 'AGY_AUTO_APPROVE',
         'AGY_FAKE_STATE', 'AGY_FAKE_STEPS_FILE', 'PYTHONIOENCODING'
$saved = @{}
foreach ($n in $names) { $saved[$n] = [Environment]::GetEnvironmentVariable($n, 'Process') }

$token = -join ((48..57) + (97..122) | Get-Random -Count 32 | ForEach-Object { [char]$_ })
$service = $null
try {
    $env:COLLAB_API_TOKEN = $token
    $env:COLLAB_API_BASE = "http://127.0.0.1:$Port"
    $env:AGY_AUTO_APPROVE = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $svcArgs = @('-3.12', 'bin\collab-service.py', '--persist', (Join-Path $work 'persist'),
                 '--port', "$Port", '--planner', 'grok', '--backend', 'antigravity')
    if ($Mode -eq 'rogue') {
        $shim = Join-Path $work 'agy.cmd'
        [IO.File]::WriteAllText($shim, "@py -3.12 `"$demo\rogue-agy.py`" %*`r`n")
        $env:AGY_BIN = $shim
        $env:AGY_FAKE_STATE = Join-Path $work 'rogue-state.txt'
        $env:AGY_FAKE_STEPS_FILE = Join-Path $demo 'rogue-steps.json'
        $env:COLLAB_AGY_ACCOUNT_POOL = $null
    } else {
        $env:COLLAB_AGY_ACCOUNT_POOL = $Pool
        $env:AGY_BIN = $null
        $svcArgs += @('--agy-account-pool', $Pool)
    }

    Write-Host "[demo] mode=$Mode  work=$work"
    $service = Start-Process -FilePath 'py' -ArgumentList $svcArgs -WorkingDirectory $repo -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $work 'service.out.log') -RedirectStandardError (Join-Path $work 'service.err.log')
    $headers = @{ Authorization = "Bearer $token" }
    $up = $false
    for ($i = 0; $i -lt 30; $i++) {
        try { Invoke-RestMethod "$($env:COLLAB_API_BASE)/health" -TimeoutSec 2 | Out-Null; $up = $true; break } catch { Start-Sleep 1 }
    }
    if (-not $up) { throw "service did not come up; see $work\service.err.log" }

    $goal = Get-Content -Raw -Encoding UTF8 (Join-Path $demo "goal-$Mode.json") | ConvertFrom-Json
    $goal | Add-Member -NotePropertyName idempotency_key -NotePropertyValue ("demo-" + [guid]::NewGuid().ToString('N')) -Force
    if ($Mode -eq 'rogue') {
        $notes = Get-Item (Join-Path $demo 'inputs\notes.txt')
        $manifest = @{
            root = $notes.DirectoryName
            entries = @(@{ relative = 'notes.txt'; sha256 = (Get-FileHash $notes.FullName -Algorithm SHA256).Hash.ToLower(); size = $notes.Length })
            max_files = 4
            max_total_bytes = 65536
        }
        $goal | Add-Member -NotePropertyName input_manifest -NotePropertyValue $manifest -Force
    }
    $body = $utf8.GetBytes(($goal | ConvertTo-Json -Depth 8))
    $opened = Invoke-RestMethod -Method Post -Uri "$($env:COLLAB_API_BASE)/v1/requests" -Headers $headers `
        -ContentType 'application/json; charset=utf-8' -Body $body
    $id = $opened.request_id
    Write-Host "[demo] submitted $id"

    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    $last = ''
    do {
        Start-Sleep 5
        $s = Invoke-RestMethod "$($env:COLLAB_API_BASE)/v1/requests/$([uri]::EscapeDataString($id))" -Headers $headers
        $line = "state=$($s.state) " + (($s.tasks | ForEach-Object { "[$($_.status) rework=$([int]$_.rework_count)]" }) -join ' ')
        if ($line -ne $last) { Write-Host ("[demo] {0:HH:mm:ss} {1}" -f (Get-Date), $line); $last = $line }
    } while ($s.state -notin 'completed', 'failed', 'cancelled' -and (Get-Date) -lt $deadline)

    $report = Join-Path $work 'report.md'
    & py -3.12 (Join-Path $repo 'bin\hermes-collab-request.py') report $id --md --out $report | Out-Null
    Write-Host ""
    Write-Host ([IO.File]::ReadAllText($report, [Text.Encoding]::UTF8))
    Write-Host "[demo] report saved: $report"
    Write-Host "[demo] workspaces:   $work\persist\workspaces"
} finally {
    if ($service -and -not $service.HasExited) { & taskkill /PID $service.Id /T /F 2>&1 | Out-Null }
    foreach ($n in $names) { [Environment]::SetEnvironmentVariable($n, $saved[$n], 'Process') }
}
