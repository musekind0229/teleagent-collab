[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$pythonCandidates = @(
    $env:COLLAB_PYTHON,
    (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'),
    (Join-Path $env:USERPROFILE '.local\share\TeleAgent\runtimes\python\python.exe')
)
$pythonExe = $pythonCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
if (-not $pythonExe) {
    $pythonExe = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
}
if (-not $pythonExe) { throw 'Python 3.12+ required; set COLLAB_PYTHON to python.exe.' }
Push-Location -LiteralPath $projectRoot
try {
    & $pythonExe -X utf8 -m win_collab @Arguments
    $collabExit = $LASTEXITCODE
} finally { Pop-Location }
exit $collabExit
