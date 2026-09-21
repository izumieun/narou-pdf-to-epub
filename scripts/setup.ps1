param(
    [string]$Python = 'python'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvDirectory = Join-Path $projectRoot '.venv'
$venvPython = Join-Path $venvDirectory 'Scripts/python.exe'
$requirements = Join-Path $projectRoot 'requirements.txt'

if (-not (Test-Path -LiteralPath $venvDirectory)) {
    & $Python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else "Python 3.12 or newer is required")'
    if ($LASTEXITCODE -ne 0) { throw 'Python version check failed.' }
    & $Python -m venv $venvDirectory
    if ($LASTEXITCODE -ne 0) { throw 'Could not create .venv.' }
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw '.venv exists but has no Windows Python executable. Inspect it before retrying.'
}
& $venvPython -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else "Existing .venv requires Python 3.12 or newer")'
if ($LASTEXITCODE -ne 0) { throw 'Existing virtual environment version check failed.' }
& $venvPython -m pip install --disable-pip-version-check -r $requirements
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Rerun setup after resolving the error.' }
& $venvPython -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Dependency consistency check failed.' }
Write-Host "Ready: $venvPython"
Write-Host 'Activation is optional; invoke .venv/Scripts/python.exe directly.'
