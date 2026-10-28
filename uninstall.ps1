# Remove Flow Bulk Generator. Projects and generated images are kept unless
# -Purge is given, because they are the expensive part.
param([switch]$Purge)

$ErrorActionPreference = 'SilentlyContinue'

$Prefix    = Join-Path $env:LOCALAPPDATA 'FlowBulkGenerator'
$ConfigDir = Join-Path $env:APPDATA      'FlowBulkGenerator'
$AppName   = 'Flow Bulk Generator'

Remove-Item (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\$AppName.lnk") -Force
Remove-Item (Join-Path ([Environment]::GetFolderPath('Desktop')) "$AppName.lnk") -Force
Remove-Item (Join-Path $Prefix 'venv') -Recurse -Force
Write-Host "==> Removed the app." -ForegroundColor Green

if ($Purge) {
    Remove-Item $Prefix -Recurse -Force
    Remove-Item $ConfigDir -Recurse -Force
    Write-Host "==> Purged projects, generated images and settings." -ForegroundColor Green
} else {
    Write-Host "    Kept projects and images in $Prefix"
    Write-Host "    Kept settings in $ConfigDir"
    Write-Host "    Use -Purge to delete those too."
}
