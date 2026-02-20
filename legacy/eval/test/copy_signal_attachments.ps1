# Copy Signal Desktop attachment files referenced in an export JSON.
#
# Goal:
# - You already exported decrypted messages into `test/data/signal_messages.json`
#   (via `python test/read_signal_db.py`) and it contains attachment metadata with a `path`.
# - On the ORIGINAL Windows machine/user where Signal Desktop has the attachment files,
#   run this script to copy only the referenced files out of:
#     %APPDATA%\Signal\attachments.noindex\
#
# Output:
# - A folder with the same relative structure: <OutDir>\attachments.noindex\<relpath>
# - A manifest JSON with counts + missing paths
#
# Usage (PowerShell):
#   .\copy_signal_attachments.ps1
#   .\copy_signal_attachments.ps1 -MessagesJsonPath "C:\path\to\signal_messages.json" -Zip
#

[CmdletBinding()]
param(
    [string]$MessagesJsonPath,
    [string]$SignalDir = "$env:APPDATA\Signal",
    [string]$OutDir = (Join-Path $env:USERPROFILE "Desktop\signal_attachments_export"),
    [switch]$Zip,
    [string]$ZipPath = (Join-Path $env:USERPROFILE "Desktop\signal_attachments_export.zip")
)

$ErrorActionPreference = "Stop"

function Normalize-RelPath([string]$p) {
    if (-not $p) { return $null }
    $s = $p.Trim().Trim('"').Trim("'")
    if (-not $s) { return $null }
    $s = $s -replace '/', '\'

    # If path contains "...attachments.noindex\<rel>", strip prefix.
    $m = [regex]::Match($s, '(?i)attachments\.noindex[\\/](.+)$')
    if ($m.Success) { return $m.Groups[1].Value }

    # If it looks like an absolute Windows path, return as-is (caller may handle).
    if ($s -match '^[A-Za-z]:\\') { return $s }

    # Otherwise treat as relative path under attachments.noindex.
    return $s.TrimStart('\')
}

Write-Host "Signal Attachment Copier" -ForegroundColor Cyan
Write-Host "========================" -ForegroundColor Cyan
Write-Host ""

if (-not $MessagesJsonPath) {
    # Default: repo-local export path if running from SupportBot\test\
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    $MessagesJsonPath = Join-Path $scriptDir "data\signal_messages.json"
}

$attachmentsRoot = Join-Path $SignalDir "attachments.noindex"

Write-Host "Messages JSON : $MessagesJsonPath" -ForegroundColor DarkGray
Write-Host "Signal dir    : $SignalDir" -ForegroundColor DarkGray
Write-Host "Attachments   : $attachmentsRoot" -ForegroundColor DarkGray
Write-Host "Output dir    : $OutDir" -ForegroundColor DarkGray
Write-Host ""

if (-not (Test-Path -LiteralPath $MessagesJsonPath)) {
    Write-Host "ERROR: messages JSON not found: $MessagesJsonPath" -ForegroundColor Red
    Write-Host "Tip: copy `test/data/signal_messages.json` from your dev machine to this PC." -ForegroundColor Yellow
    exit 1
}

if (-not (Test-Path -LiteralPath $attachmentsRoot)) {
    Write-Host "ERROR: attachments folder not found: $attachmentsRoot" -ForegroundColor Red
    Write-Host "Make sure Signal Desktop is installed for THIS Windows user and attachments are downloaded." -ForegroundColor Yellow
    Write-Host "Tip: open the group chat and scroll up / click media so Signal downloads it, then retry." -ForegroundColor Yellow
    exit 1
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$outAttachmentsRoot = Join-Path $OutDir "attachments.noindex"
New-Item -ItemType Directory -Force -Path $outAttachmentsRoot | Out-Null

# Parse JSON
$raw = Get-Content -LiteralPath $MessagesJsonPath -Raw -Encoding UTF8
$data = $raw | ConvertFrom-Json
$msgs = @($data.messages)

if (-not $msgs -or $msgs.Count -eq 0) {
    Write-Host "ERROR: No messages found in JSON." -ForegroundColor Red
    exit 1
}

# Collect unique attachment paths
$paths = New-Object System.Collections.Generic.List[string]
foreach ($m in $msgs) {
    $atts = @($m.attachments)
    foreach ($a in $atts) {
        if ($null -ne $a.path -and ($a.path.ToString().Trim())) {
            $paths.Add($a.path.ToString())
        }
    }
}

$unique = $paths | Sort-Object -Unique
Write-Host ("Found {0} unique attachment paths in JSON" -f $unique.Count) -ForegroundColor Green

$copied = 0
$missing = New-Object System.Collections.Generic.List[string]

foreach ($p in $unique) {
    $rel = Normalize-RelPath $p
    if (-not $rel) { continue }

    # Absolute path case
    if ($rel -match '^[A-Za-z]:\\') {
        $src = $rel
        $dst = Join-Path $outAttachmentsRoot ("abs\" + ($src -replace '[:\\]', '_'))
    } else {
        $src = Join-Path $attachmentsRoot $rel
        $dst = Join-Path $outAttachmentsRoot $rel
    }

    if (-not (Test-Path -LiteralPath $src)) {
        $missing.Add($p)
        continue
    }

    $dstDir = Split-Path -Parent $dst
    if ($dstDir -and -not (Test-Path -LiteralPath $dstDir)) {
        New-Item -ItemType Directory -Force -Path $dstDir | Out-Null
    }

    Copy-Item -LiteralPath $src -Destination $dst -Force
    $copied += 1
}

Write-Host ("Copied {0}/{1} files" -f $copied, $unique.Count) -ForegroundColor Cyan
if ($missing.Count -gt 0) {
    Write-Host ("Missing {0} files (not present on disk)" -f $missing.Count) -ForegroundColor Yellow
    Write-Host "Tip: Signal may not have downloaded older media. Open the chat/media gallery and scroll, then rerun." -ForegroundColor Yellow
}

$manifest = @{
    generated_at = (Get-Date).ToString("o")
    messages_json = $MessagesJsonPath
    signal_dir = $SignalDir
    attachments_root = $attachmentsRoot
    out_dir = $OutDir
    unique_paths = $unique.Count
    copied = $copied
    missing = @($missing)
}

$manifestPath = Join-Path $OutDir "manifest.json"
$manifest | ConvertTo-Json -Depth 6 | Out-File -FilePath $manifestPath -Encoding UTF8
Write-Host "Wrote manifest: $manifestPath" -ForegroundColor DarkGray

if ($Zip) {
    if (Test-Path -LiteralPath $ZipPath) { Remove-Item -LiteralPath $ZipPath -Force }
    Write-Host "Creating zip: $ZipPath" -ForegroundColor Cyan
    Compress-Archive -Path $outAttachmentsRoot -DestinationPath $ZipPath -Force
    Write-Host "Done: $ZipPath" -ForegroundColor Green
}

Write-Host ""
Write-Host "Next (on your dev machine):" -ForegroundColor Cyan
Write-Host "1) Copy the folder (or zip) back" -ForegroundColor White
Write-Host "2) Place it under: test/data/extracted/Signal1/attachments.noindex/" -ForegroundColor White
Write-Host ""
