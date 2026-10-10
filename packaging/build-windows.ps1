# Builds dist\cable-outage-evidence-<version>-windows-x64.zip with PyInstaller.
# Usage (from the repository root): powershell -File packaging\build-windows.ps1 [python.exe]
param([string]$Python = "py")
$ErrorActionPreference = "Stop"
$version = & $Python -c "import cable_outage_evidence as c; print(c.__version__)"
& $Python -m PyInstaller --noconfirm --clean --onefile --console `
    --name cable-outage-evidence --distpath dist --workpath build\pyinstaller --specpath build `
    packaging\entry.py
$zip = "dist\cable-outage-evidence-$version-windows-x64.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path dist\cable-outage-evidence.exe, README.md, LICENSE -DestinationPath $zip
$hash = (Get-FileHash dist\cable-outage-evidence.exe -Algorithm SHA256).Hash.ToLower()
Write-Output "built $zip"
Write-Output "SHA256 (exe) $hash"
