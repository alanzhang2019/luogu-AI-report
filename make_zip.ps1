$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression.FileSystem

$projectRoot = $PSScriptRoot
if (-not $projectRoot) { $projectRoot = (Get-Location).Path }
Set-Location $projectRoot

$excludeDirs = @('.git', '__pycache__', 'node_modules', 'data', '.source_cache', 'reports', 'tasks.db*', '*.pdf', '.dbg', '.preimage_bak', '.claude')
$zipName = 'deploy-pkg.zip'
$zipPath = Join-Path $projectRoot $zipName

if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
$tmp = Join-Path $env:TEMP ("zip_" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tmp | Out-Null

Get-ChildItem -Path $projectRoot -Force | Where-Object {
    $name = $_.Name
    -not ($excludeDirs | Where-Object { $name -like $_ })
} | ForEach-Object {
    $dest = Join-Path $tmp $_.Name
    if ($_.PSIsContainer) {
        Copy-Item -Path $_.FullName -Destination $dest -Recurse -Force
    } else {
        Copy-Item -Path $_.FullName -Destination $dest -Force
    }
}
[System.IO.Compression.ZipFile]::CreateFromDirectory($tmp, $zipPath)
Remove-Item $tmp -Recurse -Force
Write-Host "OK zip: $zipPath"
