# Configure the GitHub remote and push the baseline (main + the v1.0 tag).
#
# Why this script exists: the first attempt failed with
#   "The requested URL returned error: 400"
# because the copy-pasted command still contained the literal placeholders
# (<your-user>/<repo>). Angle brackets in a URL are invalid, so git never even
# reached GitHub's auth. This script takes the URL as input, REJECTS anything
# with < > in it, checks that the repo is actually reachable, and only then
# pushes -- so the next failure, if any, means something real.
#
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads .ps1 files as
# ANSI unless they carry a BOM, so non-ASCII comments would corrupt parsing.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\push_to_github.ps1
# or pass the URL directly:
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\push_to_github.ps1 -Url https://github.com/me/material-bakery.git

param(
    [string]$Url = "",
    [string]$Branch = "main",
    [string]$Tag = "v1.0"
)

# NOTE: deliberately NOT "Stop". git writes progress and errors to stderr, and
# PowerShell turns a native command's stderr into a *terminating* error when
# ErrorActionPreference is Stop -- so the reachability probe below would abort
# instead of printing the advice it is supposed to print. Exit codes are checked
# explicitly instead (same lesson as the comment in tools\run_all_tests.ps1).
$ErrorActionPreference = "Continue"
$ws = Split-Path -Parent $PSScriptRoot
if (-not $ws) { $ws = (Get-Location).Path }
Set-Location $ws

function Fail($message) {
    ""
    "XX  $message"
    exit 1
}

if (-not $Url) {
    ""
    "Open your private repo in a browser, copy its URL from the address bar"
    "(or the green 'Code' button -> HTTPS), and paste it here."
    "Example: https://github.com/someone/material-bakery.git"
    ""
    $Url = Read-Host "Repo URL"
}

$Url = $Url.Trim().Trim('"').Trim("'")

# The exact mistake that produced the HTTP 400: placeholders left in the URL.
if ($Url -match '[<>]') {
    Fail "That URL still contains <> (a placeholder). Replace it with your real user/repo name."
}
if ($Url -notmatch '^https://github\.com/[^/\s]+/[^/\s]+$') {
    Fail "Not a GitHub HTTPS repo URL: $Url"
}
$Url = $Url.TrimEnd('/')
if ($Url -notmatch '\.git$') { $Url = "$Url.git" }

""
"Repo : $Url"
"Local: $ws"
""

if (-not (Test-Path (Join-Path $ws ".git"))) { Fail "This folder is not a git repository." }

# Reachability first: a wrong repo name should fail here with a clear message,
# not halfway through a push.
"-> checking the repo is reachable (and that you are allowed to see it)"
$env:GIT_TERMINAL_PROMPT = "0"
$probe = & git ls-remote $Url 2>&1
$probeExit = $LASTEXITCODE
if ($probeExit -ne 0) {
    $text = ($probe | Out-String).Trim()
    # Unauthenticated git cannot tell "does not exist" from "private": both make
    # it ask for credentials. So both possibilities get listed, most likely first.
    Fail ("GitHub did not let us in. Two reasons, both common:`n" +
          "     (a) the private repo does not exist yet -> create it first:`n" +
          "         github.com -> New repository -> Private, and do NOT tick`n" +
          "         'Add a README' (this local repo already has its own history);`n" +
          "     (b) it exists but git could not log in -> run this script from your OWN`n" +
          "         terminal window (not a captured pipe) so the Git Credential Manager`n" +
          "         browser login can appear, or sign in once with: git push`n" +
          "     `n" +
          "     Also double-check the owner/repo spelling in the URL.`n" +
          "     git said: " + $text)
}
"   ok"

"-> setting remote origin"
$existing = & git remote
if ($existing -contains "origin") { & git remote remove origin | Out-Null }
& git remote add origin $Url
& git remote -v

"-> pushing $Branch"
& git push -u origin $Branch
if ($LASTEXITCODE -ne 0) {
    Fail ("push of $Branch failed. If it was an auth prompt, run this script from a real`n" +
          "     terminal window (not through a captured pipe) so the browser login can appear.")
}

if (& git tag -l $Tag) {
    "-> pushing tag $Tag"
    & git push origin $Tag
    if ($LASTEXITCODE -ne 0) { Fail "push of tag $Tag failed." }
}

""
"Done. Open $($Url -replace '\.git$','')"
""
