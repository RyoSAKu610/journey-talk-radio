$ErrorActionPreference = 'Stop'

$projectDir = $PSScriptRoot
$venvPython = Join-Path $projectDir '.venv\Scripts\python.exe'
$mainScript = Join-Path $projectDir 'main.py'

if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    Write-Error 'Virtual environment not found. Run .\install.ps1 first.'
    exit 1
}

$previousPythonUtf8 = $env:PYTHONUTF8
$env:PYTHONUTF8 = '1'
$exitCode = 1

Push-Location $projectDir
try {
    & $venvPython $mainScript @args
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
    if ($null -eq $previousPythonUtf8) {
        Remove-Item Env:PYTHONUTF8 -ErrorAction SilentlyContinue
    }
    else {
        $env:PYTHONUTF8 = $previousPythonUtf8
    }
}

exit $exitCode
