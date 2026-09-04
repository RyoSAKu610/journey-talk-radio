$ErrorActionPreference = 'Stop'

$projectDir = $PSScriptRoot
$venvDir = Join-Path $projectDir '.venv'
$venvPython = Join-Path $venvDir 'Scripts\python.exe'
$requirements = Join-Path $projectDir 'requirements.txt'

$venvReady = $false
if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
    & $venvPython -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
    $venvReady = ($LASTEXITCODE -eq 0)
}

if (-not $venvReady) {
    $uvCommand = Get-Command uv -ErrorAction SilentlyContinue
    if ($null -ne $uvCommand) {
        Write-Host 'Creating .venv with uv and Python 3.12 ...'
        & $uvCommand.Source venv --clear --seed --python 3.12 $venvDir
    }
    else {
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if ($null -eq $pythonCommand) {
            throw 'Python was not found on PATH. Install Python 3.11 or 3.12, then run this script again.'
        }

        & $pythonCommand.Source -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
        if ($LASTEXITCODE -ne 0) {
            throw 'Python 3.10 or newer is required. Python 3.11 or 3.12 is recommended.'
        }

        Write-Host 'Creating .venv with Python ...'
        & $pythonCommand.Source -m venv --clear $venvDir
    }

    if ($LASTEXITCODE -ne 0) {
        throw "Could not create virtual environment (exit code $LASTEXITCODE)."
    }

    & $venvPython -c "import sys; print(sys.version)"
    if ($LASTEXITCODE -ne 0) {
        throw 'The virtual environment was created but its Python cannot start.'
    }
}

Write-Host 'Installing Python dependencies ...'
& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    throw "Could not upgrade pip (exit code $LASTEXITCODE)."
}

& $venvPython -m pip install --requirement $requirements
if ($LASTEXITCODE -ne 0) {
    throw "Could not install dependencies (exit code $LASTEXITCODE)."
}

& $venvPython -m pip check
if ($LASTEXITCODE -ne 0) {
    throw "Dependency check failed (exit code $LASTEXITCODE)."
}

Write-Host ''
Write-Host 'Installation complete.'
Write-Host 'Next commands:'
Write-Host '  ollama pull qwen2.5:7b'
Write-Host '  .\run.ps1'
