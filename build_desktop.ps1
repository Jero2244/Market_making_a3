param([string]$Python = "python")
$ErrorActionPreference = "Stop"
$taskRoot = $PSScriptRoot
$taskBuildEnv = Join-Path $taskRoot ".venv-desktop-build"
$taskBuildPython = Join-Path $taskBuildEnv "Scripts/python.exe"
Push-Location -LiteralPath $taskRoot
try {
    if (-not (Test-Path -LiteralPath $taskBuildPython)) {
        & $Python -m venv $taskBuildEnv
        if ($LASTEXITCODE -ne 0) { throw "Could not create the build environment." }
    }
    & $taskBuildPython -m pip install -r requirements-desktop-build.txt
    if ($LASTEXITCODE -ne 0) { throw "Could not install packaging dependencies." }
    & $taskBuildPython -m PyInstaller --noconfirm --onefile --windowed --name GGALDesk --paths src --specpath build --distpath dist --workpath build/desktop --exclude-module market_making.execution --exclude-module market_making.validation --exclude-module market_making.market_data desktop_launcher.py
    if ($LASTEXITCODE -ne 0) { throw "Could not build GGALDesk.exe." }
    Write-Host "Ready: $taskRoot/dist/GGALDesk.exe"
} finally {
    Pop-Location
}
