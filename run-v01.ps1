$ErrorActionPreference = 'Stop'

$projectDir = $PSScriptRoot
$python = Join-Path $projectDir '.venv-v01\Scripts\python.exe'
$script = Join-Path $projectDir 'main_v01.py'

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'v0.1 environment not found. Run .\bootstrap-v01.ps1 first.'
}

Push-Location $projectDir
try {
    & $python $script
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
