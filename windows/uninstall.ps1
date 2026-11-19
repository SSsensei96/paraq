# Removes Paraq for the current user. Tesseract OCR is a separate program and stays
# installed (remove it in Settings > Apps if you no longer need it).
# Plain ASCII on purpose (Windows PowerShell 5.1).

$ErrorActionPreference = 'Continue'
$InstallDir = Join-Path $env:LOCALAPPDATA 'Programs\Paraq'
$DataDir    = Join-Path $env:LOCALAPPDATA 'Paraq'

Add-Type -AssemblyName System.Windows.Forms
$answer = [System.Windows.Forms.MessageBox]::Show(
    "Remove Paraq?`n`nYour PDFs and the searchable *_ocr.pdf files are not touched.",
    'Uninstall Paraq', 'YesNo', 'Question')
if ($answer -ne 'Yes') { exit 0 }
$deleteData = [System.Windows.Forms.MessageBox]::Show(
    "Also delete the downloaded translation model, the OCR language data, saved translations and settings?`n`n($DataDir)",
    'Uninstall Paraq', 'YesNo', 'Question')

Get-Process -Name pythonw, python -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path.StartsWith($InstallDir, [StringComparison]::OrdinalIgnoreCase) } |
    Stop-Process -Force -ErrorAction SilentlyContinue

Set-Location $env:TEMP
foreach ($folder in [Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop')) {
    Remove-Item -Force (Join-Path $folder 'Paraq.lnk') -ErrorAction SilentlyContinue
}
Remove-Item -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Paraq' -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force $InstallDir -ErrorAction SilentlyContinue
if ($deleteData -eq 'Yes') { Remove-Item -Recurse -Force $DataDir -ErrorAction SilentlyContinue }

[System.Windows.Forms.MessageBox]::Show('Paraq has been removed.', 'Uninstall Paraq') | Out-Null
