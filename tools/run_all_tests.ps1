# Run every regression suite and print a summary table.
#
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads .ps1 files as
# ANSI unless they carry a BOM, so non-ASCII comments would corrupt parsing.
#
# Two suites need preparation BEFORE Blender starts (the addon search path is
# scanned at startup), so this script does that setup for them:
#   mb_test_install.py      -> copy MaterialBakery\material_bakery into the probe
#   mb_test_zip_install.py  -> just point BLENDER_USER_SCRIPTS at a clean probe
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File tools\run_all_tests.ps1

param(
    [string]$Blender = "C:\Program Files\Blender Foundation\Blender 4.5\blender.exe",
    [string]$Filter = "",
    [switch]$KeepProbe = $false
)

$ErrorActionPreference = "Continue"
$ws = Split-Path -Parent $PSScriptRoot
if (-not $ws) { $ws = (Get-Location).Path }

if (-not (Test-Path $Blender)) { throw "blender not found: $Blender" }

$suites = Get-ChildItem (Join-Path $ws "tests") -Filter "*.py" -File | Sort-Object Name
if ($Filter) { $suites = $suites | Where-Object { $_.Name -like $Filter } }

$rows = @()
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$grand = [System.Diagnostics.Stopwatch]::StartNew()
foreach ($suite in $suites) {
    $env:BLENDER_USER_CONFIG = $null
    $env:BLENDER_USER_SCRIPTS = $null

    if ($suite.Name -eq "mb_test_install.py") {
        $probe = Join-Path $ws "_probe\install"
        if (Test-Path $probe) { Remove-Item -Recurse -Force $probe }
        New-Item -ItemType Directory -Force -Path (Join-Path $probe "scr\addons") | Out-Null
        Copy-Item -Recurse (Join-Path $ws "MaterialBakery\material_bakery") `
            (Join-Path $probe "scr\addons\material_bakery")
        $env:BLENDER_USER_CONFIG = Join-Path $probe "cfg"
        $env:BLENDER_USER_SCRIPTS = Join-Path $probe "scr"
    }
    elseif ($suite.Name -eq "mb_test_zip_install.py") {
        # The zip is a build artifact and something outside the test suite can
        # remove it (it happened once, and the suite then died in 0.5 s with a
        # confusing "zip 存在 FAIL"). Rebuild it here -- loudly, so it is never a
        # mystery which zip a given run actually verified.
        $zipPath = Join-Path $ws "MaterialBakery\material_bakery_install.zip"
        if (-not (Test-Path $zipPath)) {
            "  !! material_bakery_install.zip is missing -- building it now"
            try { & (Join-Path $ws "tools\build_zips.ps1") | Out-Null }
            catch { "  !! build_zips.ps1 failed: $($_.Exception.Message)" }
        }
        $probe = Join-Path $ws "_probe\zipinstall"
        if (Test-Path $probe) { Remove-Item -Recurse -Force $probe }
        New-Item -ItemType Directory -Force -Path (Join-Path $probe "scr\addons") | Out-Null
        $env:BLENDER_USER_CONFIG = Join-Path $probe "cfg"
        $env:BLENDER_USER_SCRIPTS = Join-Path $probe "scr"
    }

    # NOTE: do not use $ErrorActionPreference = "Stop" here. Some suites write
    # tracebacks to stderr on purpose (mb_test_panel_draw.py feeds broken
    # draw() bodies in), and Stop would turn that into a terminating error.
    $output = & $Blender --background --factory-startup --python $suite.FullName 2>&1 | Out-String
    $seconds = $sw.Elapsed.TotalSeconds
    $sw.Restart()
    # NOTE: a suite may legitimately skip itself (no QuadRemesher engine, no
    # license, ...) and say so with "SKIPPED (reason)". That is not a failure --
    # reporting it as FAIL trains everyone to ignore a red matrix. It gets its
    # own status and is listed separately at the end, so a skip stays visible.
    $status = "FAIL"
    $note = ""
    if ($output -match "ALL CHECKS PASSED") { $status = "PASS" }
    elseif ($output -match "SKIPPED \(([^)]*)\)") {
        $status = "SKIP"
        $note = $Matches[1]
    }
    $checks = ([regex]::Matches($output, "\[PASS\]")).Count
    $rows += [pscustomobject]@{ Suite = $suite.Name; Status = $status; Checks = $checks;
                                Note = $note; Seconds = $seconds }
    "=== {0}: {1} ({2} checks, {3:N1}s){4}" -f $suite.Name, $status, $checks, $seconds,
        $(if ($note) { "  <- " + $note } else { "" })
    if ($status -eq "FAIL") {
        ($output -split "`r?`n" | Where-Object { $_ -match "FAILED \d+ check|^  - " }) |
            ForEach-Object { "    $_" }
    }
}

$env:BLENDER_USER_CONFIG = $null
$env:BLENDER_USER_SCRIPTS = $null

""
"===== SUMMARY ====="
$rows | Sort-Object Seconds -Descending |
    ForEach-Object { "{0,-40} {1,-5} {2,4}  {3,6:N1}s" -f $_.Suite, $_.Status, $_.Checks, $_.Seconds }
$passed = @($rows | Where-Object { $_.Status -eq "PASS" }).Count
$skipped = @($rows | Where-Object { $_.Status -eq "SKIP" })
$failed = @($rows | Where-Object { $_.Status -eq "FAIL" })
$total = ($rows | Measure-Object Checks -Sum).Sum
"PASS suites: {0} / {1}   (skipped: {2}, failed: {3})" -f $passed, $rows.Count, $skipped.Count, $failed.Count
"TOTAL checks: {0}" -f $total
"TOTAL time:   {0:N1}s  ({1:N1} min) for {2} suite(s)  -- slowest: {3:N1}s" -f `
    $grand.Elapsed.TotalSeconds, ($grand.Elapsed.TotalSeconds / 60), $rows.Count,
    (($rows | Measure-Object Seconds -Maximum).Maximum)
if ($skipped.Count -gt 0) {
    ""
    "Skipped (not a failure, but read the reason):"
    $skipped | ForEach-Object { "  {0}: {1}" -f $_.Suite, $_.Note }
}

# The install suites copy a whole plugin tree (and QuadRemesher, ~108 MB) into
# _probe before Blender starts. That is scratch, not evidence -- but when a suite
# FAILS the probe tree is exactly what you want to look at, so only clean up a
# fully green run.
if ($failed.Count -eq 0 -and -not $KeepProbe) {
    foreach ($name in @("install", "zipinstall")) {
        $probe = Join-Path $ws "_probe\$name"
        if (Test-Path $probe) {
            Remove-Item -Recurse -Force $probe -ErrorAction SilentlyContinue
        }
    }
    "cleaned _probe\install and _probe\zipinstall (~108 MB of scratch; use -KeepProbe to keep them)"
}

if ($failed.Count -gt 0) { exit 1 }
