$ErrorActionPreference = 'Stop'

$projectDir = $PSScriptRoot
$venvDir = Join-Path $projectDir '.venv-v01'
$venvPython = Join-Path $venvDir 'Scripts\python.exe'
$requirements = Join-Path $projectDir 'requirements-v01.txt'

function Require-Winget {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw 'winget was not found. Install or update App Installer from Microsoft Store, then rerun this script.'
    }
}

function Ensure-Python312 {
    $candidates = @(
        (Get-Command py -ErrorAction SilentlyContinue),
        (Get-Command python -ErrorAction SilentlyContinue)
    ) | Where-Object { $_ -ne $null }

    foreach ($candidate in $candidates) {
        try {
            if ($candidate.Name -eq 'py.exe' -or $candidate.Name -eq 'py') {
                & $candidate.Source -3.12 -c "import sys; print(sys.executable)" *> $null
                if ($LASTEXITCODE -eq 0) { return @($candidate.Source, '-3.12') }
            } else {
                & $candidate.Source -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3,12) else 1)" *> $null
                if ($LASTEXITCODE -eq 0) { return @($candidate.Source) }
            }
        } catch {}
    }

    Require-Winget
    Write-Host 'Python 3.12 not found. Installing it with winget ...'
    winget install --id Python.Python.3.12 -e --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "Python installation failed (exit code $LASTEXITCODE)." }

    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($null -ne $py) { return @($py.Source, '-3.12') }

    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $python) { return @($python.Source) }

    throw 'Python was installed but is not visible in this PowerShell session. Close PowerShell, open it again, and rerun bootstrap-v01.ps1.'
}

function Ensure-FFmpeg {
    if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
        return
    }

    Require-Winget
    Write-Host 'FFmpeg not found. Installing it with winget ...'
    winget install --id Gyan.FFmpeg -e --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "FFmpeg installation failed (exit code $LASTEXITCODE)." }
}

$pythonLauncher = Ensure-Python312
Ensure-FFmpeg

if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    Write-Host 'Creating isolated Python environment ...'
    if ($pythonLauncher.Count -eq 2) {
        & $pythonLauncher[0] $pythonLauncher[1] -m venv $venvDir
    } else {
        & $pythonLauncher[0] -m venv $venvDir
    }
    if ($LASTEXITCODE -ne 0) { throw "Could not create virtual environment (exit code $LASTEXITCODE)." }
}

Write-Host 'Installing Journey Talk v0.1 Python packages ...'
& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed (exit code $LASTEXITCODE)." }

& $venvPython -m pip install -r $requirements
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed (exit code $LASTEXITCODE)." }

Write-Host ''
Write-Host 'Verification:'
& $venvPython -c "import feedparser, dotenv; import edge_tts; from google import genai; from faster_whisper import WhisperModel; print('Python packages: OK')"
if ($LASTEXITCODE -ne 0) { throw 'Python package verification failed.' }

$ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
if ($null -eq $ffmpeg) {
    Write-Warning 'FFmpeg was installed but is not yet visible in this PowerShell session. Close PowerShell, reopen it, then run: ffmpeg -version'
} else {
    & $ffmpeg.Source -version | Select-Object -First 1
}

Write-Host ''
Write-Host 'Bootstrap complete.'
Write-Host 'Next step: add GEMINI_API_KEY to a local .env file, then run the v0.1 generator.'
