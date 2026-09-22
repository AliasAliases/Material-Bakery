# Build the two installable zips.
#
# NOTE: Compress-Archive must NOT be used here: it writes entry names with
# backslashes (material_bakery\ui\ops.py), and Blender's addon_install treats
# the whole thing as one giant filename, so the installed tree is broken.
# This script uses .NET ZipArchive and writes '/' separators by hand.
#
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads .ps1 files as
# ANSI unless they carry a BOM, so non-ASCII comments would corrupt parsing.
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File tools\build_zips.ps1

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$ws = Split-Path -Parent $PSScriptRoot
if (-not $ws) { $ws = (Get-Location).Path }
$packages = @(
    @{ Name = "material_bakery"; Source = Join-Path $ws "MaterialBakery\material_bakery" }
)

function New-PackageZip {
    param([string]$Name, [string]$Source)

    if (-not (Test-Path $Source)) { throw "source not found: $Source" }
    $zipPath = Join-Path $ws ("MaterialBakery\{0}_install.zip" -f $Name)
    if (Test-Path $zipPath) { Remove-Item $zipPath -Force }

    $sourceFull = (Resolve-Path $Source).Path.TrimEnd('\')
    $files = Get-ChildItem $sourceFull -Recurse -File | Where-Object {
        $_.FullName -notmatch '__pycache__' -and $_.Extension -ne '.pyc'
    }

    $mode = [System.IO.Compression.ZipArchiveMode]::Create
    $level = [System.IO.Compression.CompressionLevel]::Optimal
    $stream = [System.IO.File]::Open($zipPath, [System.IO.FileMode]::CreateNew)
    $written = 0
    try {
        $archive = New-Object -TypeName System.IO.Compression.ZipArchive -ArgumentList $stream, $mode
        try {
            foreach ($file in $files) {
                $relative = $file.FullName.Substring($sourceFull.Length + 1).Replace('\', '/')
                $entryName = "{0}/{1}" -f $Name, $relative
                $entry = $archive.CreateEntry($entryName, $level)
                $entry.LastWriteTime = $file.LastWriteTime
                $entryStream = $entry.Open()
                $input = [System.IO.File]::OpenRead($file.FullName)
                try {
                    $input.CopyTo($entryStream)
                } finally {
                    $input.Dispose()
                    $entryStream.Dispose()
                }
                $written++
            }
        } finally {
            $archive.Dispose()
        }
    } finally {
        $stream.Dispose()
    }

    $size = (Get-Item $zipPath).Length
    "{0,-18} {1,6} files  {2,10:N2} MB  {3}" -f $Name, $written, ($size / 1MB), $zipPath
}

foreach ($package in $packages) {
    New-PackageZip -Name $package.Name -Source $package.Source
}

# Self-check: entry names must use forward slashes and live under the plugin root.
foreach ($package in $packages) {
    $zipPath = Join-Path $ws ("MaterialBakery\{0}_install.zip" -f $package.Name)
    $archive = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
    try {
        $names = @($archive.Entries | ForEach-Object { $_.FullName })
        $bad = @($names | Where-Object { $_ -like '*\*' })
        $rootPrefix = "$($package.Name)/"
        $wrongRoot = @($names | Where-Object { -not $_.StartsWith($rootPrefix) })
        if ($bad.Count -gt 0)       { throw "$($package.Name): entry with backslash: $($bad[0])" }
        if ($wrongRoot.Count -gt 0) { throw "$($package.Name): entry outside root: $($wrongRoot[0])" }
        $needsInit = $package.Name -eq "material_bakery"
        if ($needsInit -and -not ($names -contains "material_bakery/__init__.py")) {
            throw "material_bakery/__init__.py is missing from the zip"
        }
        "  ok  {0}: {1} entries, forward slashes, root = {2}" -f $package.Name, $names.Count, $rootPrefix
    } finally {
        $archive.Dispose()
    }
}
