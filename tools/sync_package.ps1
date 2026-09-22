# Mirror the working tree into the shipping folder.
#
# Why this exists: tools\build_zips.ps1 packages MaterialBakery\material_bakery,
# NOT the working tree -- so the zip only ever contains what was copied there.
# Forgetting that copy is invisible: the install suites keep passing against the
# previous build, and the zip ships stale code. (It happened: v1.0.3 was built
# once while MaterialBakery\material_bakery still said 1.0.2.)
#
# robocopy /MIR also deletes files that no longer exist in the source, which is
# what we want for deleted modules.
#
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads .ps1 files as
# ANSI unless they carry a BOM, so non-ASCII comments would corrupt parsing.
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File tools\sync_package.ps1

$ErrorActionPreference = "Stop"
$ws = Split-Path -Parent $PSScriptRoot
if (-not $ws) { $ws = (Get-Location).Path }

$source = Join-Path $ws "material_bakery"
$target = Join-Path $ws "MaterialBakery\material_bakery"
if (-not (Test-Path $source)) { throw "working tree not found: $source" }
if (-not (Test-Path $target)) { New-Item -ItemType Directory -Force -Path $target | Out-Null }

& robocopy $source $target /MIR /XD __pycache__ /XF *.pyc /NFL /NDL /NJH /NJS /NP | Out-Null
$code = $LASTEXITCODE
# robocopy: 0 = nothing to do, 1..7 = copied/extra/deleted (all success), >= 8 = failure
if ($code -ge 8) { throw "robocopy failed with exit code $code" }

# Report what the shipping copy now claims to be -- the whole point of this step.
$init = Join-Path $target "__init__.py"
$version = (Select-String -Path $init -Pattern '"version": \(([0-9, ]+)\)').Matches[0].Groups[1].Value
$build = (Select-String -Path $init -Pattern '__build__ = "([^"]+)"').Matches[0].Groups[1].Value
$files = @(Get-ChildItem $target -Recurse -File -Filter *.py |
           Where-Object { $_.FullName -notmatch '__pycache__' })
$leftover = @(Get-ChildItem $target -Recurse -File -Include *.pyc |
              Where-Object { $_.FullName -match '__pycache__' })

"Synchronised {0} -> {1}" -f $source, $target
"Synchronised {0} python file(s)" -f $files.Count
"Shipping version: {0} (build {1})" -f $version.Replace(" ", ""), $build
if ($leftover.Count -gt 0) { "  note: {0} stale .pyc file(s) still there (ignored by the zip)" -f $leftover.Count }
if ($code -eq 0) { "  (nothing changed - already in sync)" }
