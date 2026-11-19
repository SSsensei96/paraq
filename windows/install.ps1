# Paraq installer for Windows 10/11 (64-bit).
#
# Started by "Install Paraq (Windows).cmd". Everything goes into the current
# user's profile and needs no administrator rights, except Tesseract OCR, whose
# official installer asks for permission once.
#
#   -Model nllb-600M | nllb-1.3B | nllb-3.3B | none   fast translation model (default nllb-1.3B)
#   -Ai gemma-4b | gemma-12b | none                   also install Ollama and the smarter TranslateGemma
#                                                     model, and make it the default (default none)
#   -FastOcr                                          smaller, faster OCR language data
#   -NoShortcuts                                      no Start menu / desktop shortcuts
#
# Run it again at any time to update Paraq or repair an installation.
# This file is deliberately plain ASCII: Windows PowerShell 5.1 misreads UTF-8 without a BOM.

param(
    [ValidateSet('nllb-600M', 'nllb-1.3B', 'nllb-3.3B', 'none')]
    [string]$Model = 'nllb-1.3B',
    [ValidateSet('gemma-4b', 'gemma-12b', 'none')]
    [string]$Ai = 'none',
    [switch]$FastOcr,
    [switch]$NoShortcuts
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is many times faster without its progress bar
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$Source     = Split-Path -Parent $PSScriptRoot                     # the unpacked download
$InstallDir = Join-Path $env:LOCALAPPDATA 'Programs\Paraq'         # program files
$DataDir    = Join-Path $env:LOCALAPPDATA 'Paraq'                  # settings, models, OCR languages (same as the app uses)
$AppDir     = Join-Path $InstallDir 'app'
$VenvDir    = Join-Path $InstallDir 'venv'
$Uv         = Join-Path $InstallDir 'tools\uv.exe'
$Py         = Join-Path $VenvDir 'Scripts\python.exe'
$PyW        = Join-Path $VenvDir 'Scripts\pythonw.exe'
$TessData   = Join-Path $DataDir 'tessdata'

New-Item -ItemType Directory -Force -Path $InstallDir, $DataDir, $TessData | Out-Null
try { Start-Transcript -Path (Join-Path $InstallDir 'install.log') -Force | Out-Null } catch {}

function Step([string]$text) {
    Write-Host ''
    Write-Host "== $text" -ForegroundColor Cyan
}

function Info([string]$text) { Write-Host "   $text" }

function Fail([string]$text) {
    Write-Host ''
    Write-Host "Installation stopped: $text" -ForegroundColor Red
    Write-Host "A log of this run is in $InstallDir\install.log"
    try { Stop-Transcript | Out-Null } catch {}
    exit 1
}

function Download([string]$url, [string]$dest) {
    $part = "$dest.part"
    for ($i = 1; $i -le 3; $i++) {
        try {
            Invoke-WebRequest -Uri $url -OutFile $part -UseBasicParsing
            Move-Item -Force $part $dest
            return
        } catch {
            if ($i -eq 3) { Fail "could not download $url`n   $($_.Exception.Message)" }
            Start-Sleep -Seconds 3
        }
    }
}

function Check-Exit([string]$what) {
    if ($LASTEXITCODE -ne 0) { Fail "$what failed (exit code $LASTEXITCODE)." }
}

function Find-Tesseract {
    $keys = 'HKLM:\SOFTWARE\Tesseract-OCR', 'HKLM:\SOFTWARE\WOW6432Node\Tesseract-OCR', 'HKCU:\SOFTWARE\Tesseract-OCR'
    foreach ($key in $keys) {
        try {
            $dir = (Get-ItemProperty -Path $key -ErrorAction Stop).InstallDir
            if ($dir -and (Test-Path (Join-Path $dir 'tesseract.exe'))) { return (Join-Path $dir 'tesseract.exe') }
        } catch {}
    }
    $cmd = Get-Command 'tesseract.exe' -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $candidates = @(
        (Join-Path $env:ProgramFiles 'Tesseract-OCR\tesseract.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Tesseract-OCR\tesseract.exe'),
        (Join-Path $env:LOCALAPPDATA 'Tesseract-OCR\tesseract.exe')
    )
    if (${env:ProgramFiles(x86)}) { $candidates += (Join-Path ${env:ProgramFiles(x86)} 'Tesseract-OCR\tesseract.exe') }
    foreach ($p in $candidates) { if (Test-Path $p) { return $p } }
    return $null
}

Write-Host ''
Write-Host 'Paraq - OCR and offline translation for PDFs and scans' -ForegroundColor White
Write-Host "Installing into $InstallDir"

if (-not [Environment]::Is64BitOperatingSystem) { Fail 'Paraq needs 64-bit Windows.' }
if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { Info 'Windows on ARM: Paraq will run through x64 emulation.' }
if (-not (Test-Path (Join-Path $Source 'paraq\app.py'))) { Fail "the Paraq files were not found next to this script ($Source). Unpack the whole zip first." }

$running = Get-Process -Name pythonw, python -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path.StartsWith($InstallDir, [StringComparison]::OrdinalIgnoreCase) }
if ($running) { Fail 'Paraq is running. Close it and start the installer again.' }

# ---------------------------------------------------------------- 1. program files
Step 'Copying Paraq'
if ((Resolve-Path $Source).Path -ne (Resolve-Path $AppDir -ErrorAction SilentlyContinue).Path) {
    if (Test-Path $AppDir) { Remove-Item -Recurse -Force $AppDir }
    New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
    foreach ($item in 'paraq', 'windows', 'run_paraq.py', 'requirements.txt', 'README.md') {
        $from = Join-Path $Source $item
        if (Test-Path $from) { Copy-Item -Recurse -Force $from $AppDir }
    }
    Get-ChildItem -Path $AppDir -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force
    Get-ChildItem -Path $AppDir -Recurse -File | Unblock-File -ErrorAction SilentlyContinue
}
Info 'done'

# ---------------------------------------------------------------- 2. Python
Step 'Python and libraries (about 250 MB, first time only)'
if (-not (Test-Path $Uv)) {
    Info 'getting the uv package manager'
    $zip = Join-Path $env:TEMP 'paraq-uv.zip'
    $unz = Join-Path $env:TEMP 'paraq-uv'
    Download 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip' $zip
    if (Test-Path $unz) { Remove-Item -Recurse -Force $unz }
    Expand-Archive -Path $zip -DestinationPath $unz -Force
    $exe = Get-ChildItem -Path $unz -Recurse -Filter 'uv.exe' | Select-Object -First 1
    if (-not $exe) { Fail 'uv.exe was not in the downloaded archive.' }
    New-Item -ItemType Directory -Force -Path (Split-Path $Uv) | Out-Null
    Copy-Item -Force $exe.FullName $Uv
    Remove-Item -Recurse -Force $zip, $unz -ErrorAction SilentlyContinue
}
# Keep uv's Python and cache inside Paraq's folder; never touch another Python on this PC.
$env:UV_PYTHON_INSTALL_DIR = Join-Path $InstallDir 'python'
$env:UV_CACHE_DIR          = Join-Path $InstallDir 'cache'
$env:UV_LINK_MODE          = 'copy'
$env:UV_PYTHON_PREFERENCE  = 'only-managed'
$env:PYTHONUTF8            = '1'   # Kazakh and Russian text in console output
if (-not (Test-Path $Py)) {
    Info 'installing Python 3.12'
    & $Uv venv --clear --python 3.12 $VenvDir
    Check-Exit 'Creating the Python environment'
}
Info 'installing libraries'
& $Uv pip install --python $Py --upgrade -r (Join-Path $AppDir 'requirements.txt')
Check-Exit 'Installing the Python libraries'
& $Uv cache prune --quiet | Out-Null

# ---------------------------------------------------------------- 3. Tesseract OCR
Step 'Tesseract OCR engine'
$tess = Find-Tesseract
if ($tess) {
    Info "already installed: $tess"
} else {
    $winget = Get-Command 'winget.exe' -ErrorAction SilentlyContinue
    if ($winget) {
        Info 'installing with winget - Windows will ask for permission, please click Yes'
        & winget install --id UB-Mannheim.TesseractOCR -e --silent --accept-package-agreements --accept-source-agreements
        $tess = Find-Tesseract
    }
    if (-not $tess) {
        Info 'downloading the installer from the University of Mannheim'
        $index = (Invoke-WebRequest -Uri 'https://digi.bib.uni-mannheim.de/tesseract/' -UseBasicParsing).Content
        $names = [regex]::Matches($index, 'tesseract-ocr-w64-setup-[0-9][0-9A-Za-z.\-]*\.exe') | ForEach-Object { $_.Value } | Sort-Object -Unique
        $stable = $names | Where-Object { $_ -notmatch 'alpha|beta|rc|dev' }
        if ($stable) { $names = $stable }
        $latest = $names | Sort-Object { [regex]::Replace($_, '\d+', { param($m) $m.Value.PadLeft(10, '0') }) } | Select-Object -Last 1
        if (-not $latest) { Fail 'could not find the Tesseract installer. Install it from https://github.com/UB-Mannheim/tesseract/wiki and run this again.' }
        $setup = Join-Path $env:TEMP $latest
        Download "https://digi.bib.uni-mannheim.de/tesseract/$latest" $setup
        Info "running $latest - Windows will ask for permission, please click Yes"
        try {
            Start-Process -FilePath $setup -ArgumentList '/S' -Verb RunAs -Wait
        } catch {
            Fail 'the Tesseract installer was not allowed to run. Run this again and click Yes when Windows asks.'
        }
        Remove-Item -Force $setup -ErrorAction SilentlyContinue
        $tess = Find-Tesseract
    }
    if (-not $tess) { Fail 'Tesseract was not installed. Install it from https://github.com/UB-Mannheim/tesseract/wiki and run this again.' }
    Info "installed: $tess"
}
Push-Location $AppDir
& $Py -m paraq.config --set tesseract_path $tess | Out-Null
Pop-Location

# ---------------------------------------------------------------- 4. OCR languages
$quality = 'best'
if ($FastOcr) { $quality = 'fast' }
Step "OCR language data: Russian, Kazakh, English, Turkish ($quality)"
$marker = Join-Path $TessData '.quality'
if ((Test-Path $marker) -and ((Get-Content $marker -Raw).Trim() -ne $quality)) {
    Get-ChildItem -Path $TessData -Filter '*.traineddata' | Where-Object { $_.BaseName -in 'rus', 'kaz', 'eng', 'tur' } | Remove-Item -Force
}
foreach ($lang in 'rus', 'kaz', 'eng', 'tur') {
    $file = Join-Path $TessData "$lang.traineddata"
    if ((Test-Path $file) -and ((Get-Item $file).Length -gt 500000)) { Info "$lang ok"; continue }
    Info "downloading $lang"
    Download "https://github.com/tesseract-ocr/tessdata_$quality/raw/main/$lang.traineddata" $file
}
Set-Content -Path $marker -Value $quality -Encoding ASCII

# ---------------------------------------------------------------- 5. translation model
if ($Model -ne 'none') {
    Step "Translation model $Model (downloaded once; resumes if interrupted)"
    Push-Location $AppDir
    & $Py -m paraq.translate --download $Model
    $modelOk = ($LASTEXITCODE -eq 0)
    Pop-Location
    if (-not $modelOk) {
        Write-Host '   The model could not be downloaded now. Paraq will offer to download it the first time you translate.' -ForegroundColor Yellow
    }
}

# ---------------------------------------------------------------- 5b. TranslateGemma through Ollama
function Find-Ollama {
    $cmd = Get-Command 'ollama.exe' -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($p in (Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'), (Join-Path $env:ProgramFiles 'Ollama\ollama.exe')) {
        if (Test-Path $p) { return $p }
    }
    return $null
}
if ($Ai -ne 'none') {
    Step "Smarter translator: TranslateGemma ($Ai) in the Ollama app"
    $ollama = Find-Ollama
    if ($ollama) {
        Info "Ollama already installed: $ollama"
    } else {
        $winget = Get-Command 'winget.exe' -ErrorAction SilentlyContinue
        if ($winget) {
            Info 'installing Ollama with winget'
            & winget install --id Ollama.Ollama -e --silent --accept-package-agreements --accept-source-agreements
            $ollama = Find-Ollama
        }
        if (-not $ollama) {
            Info 'downloading the Ollama installer'
            $setup = Join-Path $env:TEMP 'OllamaSetup.exe'
            Download 'https://ollama.com/download/OllamaSetup.exe' $setup
            Start-Process -FilePath $setup -ArgumentList '/VERYSILENT', '/NORESTART', '/SUPPRESSMSGBOXES' -Wait
            Remove-Item -Force $setup -ErrorAction SilentlyContinue
            $ollama = Find-Ollama
        }
    }
    if (-not $ollama) {
        Write-Host '   Ollama could not be installed. Get it from https://ollama.com/download and run this again.' -ForegroundColor Yellow
    } else {
        Info 'downloading the model through Ollama (it uses the NVIDIA graphics card if there is one)'
        Push-Location $AppDir
        & $Py -m paraq.translate --download $Ai
        $aiOk = ($LASTEXITCODE -eq 0)
        Pop-Location
        if (-not $aiOk) {
            Write-Host '   The model could not be downloaded now; choose it later in Tools > Translation Model.' -ForegroundColor Yellow
        }
    }
}

# ---------------------------------------------------------------- 6. shortcuts and Apps list
Step 'Shortcuts'
$icon = Join-Path $AppDir 'paraq\resources\icon.ico'
$run  = Join-Path $AppDir 'run_paraq.py'
if (-not $NoShortcuts) {
    $shell = New-Object -ComObject WScript.Shell
    $places = @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop'))
    foreach ($folder in $places) {
        $lnk = $shell.CreateShortcut((Join-Path $folder 'Paraq.lnk'))
        $lnk.TargetPath = $PyW
        $lnk.Arguments = "`"$run`""
        $lnk.WorkingDirectory = $AppDir
        $lnk.IconLocation = "$icon,0"
        $lnk.Description = 'OCR and offline translation for PDFs and scans'
        $lnk.Save()
    }
    Info 'Start menu and desktop'
}
# Entry in Settings > Apps, so Paraq can be removed like any other program.
Copy-Item -Force (Join-Path $AppDir 'windows\uninstall.ps1') (Join-Path $InstallDir 'uninstall.ps1')
$uninstall = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $InstallDir 'uninstall.ps1') + '"'
$reg = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Paraq'
New-Item -Path $reg -Force | Out-Null
$version = (Select-String -Path (Join-Path $AppDir 'paraq\__init__.py') -Pattern '__version__ = "(.+)"').Matches[0].Groups[1].Value
$values = @{
    DisplayName = 'Paraq'; DisplayVersion = $version; Publisher = 'Paraq'; DisplayIcon = $icon;
    InstallLocation = $InstallDir; UninstallString = $uninstall; NoModify = 1; NoRepair = 1
}
foreach ($k in $values.Keys) {
    $type = 'String'
    if ($values[$k] -is [int]) { $type = 'DWord' }
    New-ItemProperty -Path $reg -Name $k -Value $values[$k] -PropertyType $type -Force | Out-Null
}
Info 'Settings > Apps'

# ---------------------------------------------------------------- 7. check
Step 'Checking the installation'
Push-Location $AppDir
& $Py run_paraq.py --selftest
if (($Model -ne 'none') -or ($Ai -ne 'none')) {
    Write-Host ''
    Info 'Test translation (the first run loads the model, which can take a while):'
    & $Py run_paraq.py --test-translation
}
Pop-Location

Write-Host ''
Write-Host 'Paraq is installed. Start it from the Start menu or the desktop icon.' -ForegroundColor Green
Write-Host "Settings, models and the log file (paraq.log) are in $DataDir"
try { Stop-Transcript | Out-Null } catch {}
Start-Process -FilePath $PyW -ArgumentList "`"$run`"" -WorkingDirectory $AppDir
