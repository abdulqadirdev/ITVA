# Install Flow Bulk Generator for the current user on Windows.
# Usage:  powershell -ExecutionPolicy Bypass -File install.ps1 [-Yes]
#
# No admin rights are needed: everything goes under %LOCALAPPDATA%.

param([switch]$Yes, [switch]$Bootstrap)

$ErrorActionPreference = 'Stop'

$AppName    = 'Flow Bulk Generator'
$SourceDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$Prefix     = Join-Path $env:LOCALAPPDATA 'FlowBulkGenerator'
$Venv       = Join-Path $Prefix 'venv'
$ConfigDir  = Join-Path $env:APPDATA   'FlowBulkGenerator'

function Say  ($m) { Write-Host "==> $m" -ForegroundColor Green }
function Warn ($m) { Write-Host "!   $m" -ForegroundColor Yellow }
function Die  ($m) { Write-Host "X   $m" -ForegroundColor Red; exit 1 }

function Confirm($question) {
    if ($Yes) { return $true }
    $reply = Read-Host "    $question [Y/n]"
    return -not ($reply -match '^[Nn]')
}

# ---- Python -------------------------------------------------------------

# Probing a native command is hazardous under $ErrorActionPreference='Stop':
# anything it writes to stderr becomes a terminating error, and 2>$null does
# not prevent that in Windows PowerShell 5.1. Capture the stream instead.
function Invoke-Probe($exePath, $argList) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & $exePath @argList -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>&1
        return ($output | Out-String).Trim()
    } catch {
        return ''
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Test-RealPython($exePath, $argList) {
    # Windows puts stub launchers in WindowsApps that only advertise the
    # Microsoft Store. They answer to "python" on PATH and are not Python.
    if ($exePath -like '*\WindowsApps\*') { return $null }

    $answer = Invoke-Probe $exePath $argList
    # A real interpreter prints exactly a version; the stub prints a sentence.
    if ($answer -notmatch '^\d+\.\d+$') { return $null }
    try { $parsed = [version]$answer } catch { return $null }
    if ($parsed -lt [version]'3.10') { return $null }
    return $answer
}

# Wrapped in a function so it can run a second time after installing Python,
# instead of telling the user to close the window and start over.
function Find-Python {
    # 1. Whatever is on PATH, stubs excluded.
    #    -All matters: Get-Command returns only the FIRST match, and the Store
    #    stub routinely sits ahead of a real interpreter installed later.
    #    Without it, a machine with working Python reports having none.
    foreach ($candidate in @(@('py', @('-3')), @('python', @()), @('python3', @()))) {
        $onPath = @(Get-Command $candidate[0] -All -ErrorAction SilentlyContinue)
        foreach ($found in $onPath) {
            if (-not $found.Source) { continue }
            $version = Test-RealPython $found.Source $candidate[1]
            if ($version) {
                return @{ Exe = $found.Source; Args = $candidate[1]; Version = $version }
            }
        }
    }

    # 2. The usual install locations, in case PATH was never set up. This is
    #    also where winget puts it, which is what makes continuing possible.
    $roots = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python'),
        $env:ProgramFiles,
        ${env:ProgramFiles(x86)}
    ) | Where-Object { $_ -and (Test-Path $_) }

    foreach ($root in $roots) {
        $dirs = Get-ChildItem -Path $root -Filter 'Python3*' -Directory -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending
        foreach ($dir in $dirs) {
            $exePath = Join-Path $dir.FullName 'python.exe'
            if (-not (Test-Path $exePath)) { continue }
            $version = Test-RealPython $exePath @()
            if ($version) {
                return @{ Exe = $exePath; Args = @(); Version = $version }
            }
        }
    }

    return $null
}

# A process inherits PATH at launch, so an interpreter installed a moment ago
# is invisible until it is re-read from the registry.
function Update-PathFromRegistry {
    $parts = @(
        [Environment]::GetEnvironmentVariable('Path', 'Machine'),
        [Environment]::GetEnvironmentVariable('Path', 'User')
    ) | Where-Object { $_ }
    if ($parts) { $env:PATH = ($parts -join ';') }
}

$python = Find-Python

if (-not $python) {
    Write-Host ''
    Warn 'Python 3.10 or newer was not found on this machine.'
    if (Get-Command python -ErrorAction SilentlyContinue) {
        Warn 'The "python" on your PATH is the Microsoft Store placeholder,'
        Warn 'not an interpreter. Installing real Python below replaces it.'
    }
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($Bootstrap -and $winget -and (Confirm 'Install Python now using winget?')) {
        & winget install -e --id Python.Python.3.12 --scope user `
            --accept-source-agreements --accept-package-agreements
        Write-Host ''
        Say 'Python installed.'

        # Carry straight on rather than making the person start again: the
        # directory scan above covers where winget puts it, so this works even
        # when PATH has not caught up.
        Update-PathFromRegistry
        $python = Find-Python
        if ($python) {
            Say 'Continuing with the install.'
        } else {
            Write-Host ''
            Warn 'Python installed, but it is not visible from this window yet.'
            Warn 'Close this window, open a new one, and run the installer again.'
            exit 1
        }
    }
}

if (-not $python) {
    Write-Host ''
    Write-Host '  Install Python 3.10 or newer (64-bit) from:'
    Write-Host '      https://www.python.org/downloads/windows/'
    Write-Host '  On the first screen, tick "Add python.exe to PATH".'
    Write-Host '  Then run this installer again.'
    exit 1
}
Say "Python $($python.Version)"

# A quick environment check before touching anything, when launched by
# double-click - the person running it may not be at a terminal otherwise.
if ($Bootstrap -and (Test-Path (Join-Path $SourceDir 'windows_preflight.py'))) {
    Write-Host ''
    & $python.Exe @($python.Args) (Join-Path $SourceDir 'windows_preflight.py') --from-installer
    if ($LASTEXITCODE -ne 0) {
        Warn 'Fix the items marked [FAIL] above, then run this installer again.'
        exit 1
    }
}

# ---- install ------------------------------------------------------------

Say "Installing into $Prefix"
New-Item -ItemType Directory -Force -Path $Prefix | Out-Null

if (-not (Test-Path (Join-Path $Venv 'Scripts\python.exe'))) {
    & $python.Exe @($python.Args) -m venv $Venv
}
# Windows venvs put executables in Scripts\; everywhere else it is bin/.
# Resolving it rather than assuming means a half-created venv produces a
# clear message instead of "pip.exe is not recognized", and lets this script
# be exercised outside Windows.
$Scripts = Join-Path $Venv 'Scripts'
if (-not (Test-Path $Scripts)) { $Scripts = Join-Path $Venv 'bin' }
if (-not (Test-Path $Scripts)) {
    Die "The virtual environment at $Venv looks incomplete. Delete it and run this again."
}
$exe = if ($IsWindows -or $null -eq $IsWindows) { '.exe' } else { '' }

$VenvPip = Join-Path $Scripts "pip$exe"
if (-not (Test-Path $VenvPip)) { Die "pip was not created in $Scripts." }
& $VenvPip install --upgrade pip --quiet
# Non-editable, so the app uses the per-user config and data directories
# rather than writing beside the source checkout.
& $VenvPip install --upgrade "$SourceDir" --quiet
if ($LASTEXITCODE -ne 0) { Die "pip install failed." }

$GuiExe = Join-Path $Scripts "flow-bulk-generator$exe"
$CliExe = Join-Path $Scripts "imgen$exe"
if (-not (Test-Path $GuiExe)) { Die "The GUI entry point was not created - check the pip output above." }

# ---- shortcuts ----------------------------------------------------------

# Shortcut creation needs the Windows scripting host. If it is unavailable
# the install is still complete and usable, so this must not abort it.
$shell = $null
$shortcutMade = $false
try { $shell = New-Object -ComObject WScript.Shell } catch { }
if ($null -eq $shell) {
    Warn "Could not create shortcuts (Windows Script Host unavailable)."
    Warn "Run the app directly: $GuiExe"
} else {
$startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
$link = $shell.CreateShortcut((Join-Path $startMenu "$AppName.lnk"))
$link.TargetPath       = $GuiExe
$link.WorkingDirectory = $Prefix
$link.Description      = 'Bulk-generate images with Google Flow from a sheet of prompts'
$link.Save()
$shortcutMade = $true
Say "Added to the Start Menu"

if (Confirm 'Put a shortcut on the Desktop too?') {
    $desktop = $shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) "$AppName.lnk"))
    $desktop.TargetPath       = $GuiExe
    $desktop.WorkingDirectory = $Prefix
    $desktop.Save()
    Say "Added to the Desktop"
}
}

# ---- carry over existing work ------------------------------------------

$SourceProjects = Join-Path $SourceDir 'projects'
$DataProjects   = Join-Path $Prefix    'projects'
if ((Test-Path $SourceProjects) -and -not (Test-Path $DataProjects)) {
    Write-Host ''
    Warn "This checkout already has projects (queues and generated images)."
    Warn "An installed copy reads from $Prefix instead."
    if (Confirm 'Copy them across so nothing is regenerated?') {
        Copy-Item $SourceProjects $DataProjects -Recurse
        Say "Copied $(@(Get-ChildItem $DataProjects -Directory).Count) project(s)"
    }
}

$SourceSettings = Join-Path $SourceDir 'config\settings.toml'
if ((Test-Path $SourceSettings) -and -not (Test-Path (Join-Path $ConfigDir 'settings.toml'))) {
    New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
    Copy-Item $SourceSettings (Join-Path $ConfigDir 'settings.toml')
    Copy-Item (Join-Path $SourceDir 'config\selectors.toml') (Join-Path $ConfigDir 'selectors.toml')
    Say "Copied your settings to $ConfigDir"
}

# ---- done ---------------------------------------------------------------

Write-Host ''
Say "$AppName installed."
# Do not claim a Start Menu entry that could not be created.
if ($shortcutMade) {
    Write-Host "    GUI :  Start Menu -> $AppName"
} else {
    Write-Host "    GUI :  $GuiExe"
}
Write-Host "    CLI :  $CliExe doctor"
Write-Host "    Data:  $Prefix"
Write-Host "    Conf:  $ConfigDir"
Write-Host ''
Write-Host "Chrome is found automatically under Program Files or LocalAppData."
Write-Host "Set chrome.binary in settings.toml to force a specific browser."
