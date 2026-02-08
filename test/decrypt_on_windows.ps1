# Signal Desktop Database Decryptor
# Run this on Windows PowerShell (not WSL)

$ErrorActionPreference = "Stop"

# Paths
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$configPath = Join-Path $scriptDir "data\extracted\Signal1\config.json"
$dbPath = Join-Path $scriptDir "data\extracted\Signal1\sql\db.sqlite"
$outputPath = Join-Path $scriptDir "data\signal_messages.json"

Write-Host "Signal Database Decryptor" -ForegroundColor Cyan
Write-Host "=========================" -ForegroundColor Cyan

# Check paths
if (-not (Test-Path $configPath)) {
    Write-Host "ERROR: config.json not found at $configPath" -ForegroundColor Red
    Write-Host "First extract: cd test/data; Expand-Archive Signal1*.zip -DestinationPath extracted"
    exit 1
}

if (-not (Test-Path $dbPath)) {
    Write-Host "ERROR: db.sqlite not found at $dbPath" -ForegroundColor Red
    exit 1
}

# Read encrypted key
$config = Get-Content $configPath | ConvertFrom-Json
$encryptedKeyHex = $config.encryptedKey
Write-Host "Encrypted key (first 40 chars): $($encryptedKeyHex.Substring(0,40))..." -ForegroundColor Yellow

# Convert hex to bytes
$encryptedKeyBytes = [byte[]]::new($encryptedKeyHex.Length / 2)
for ($i = 0; $i -lt $encryptedKeyHex.Length; $i += 2) {
    $encryptedKeyBytes[$i / 2] = [Convert]::ToByte($encryptedKeyHex.Substring($i, 2), 16)
}
Write-Host "Encrypted key length: $($encryptedKeyBytes.Length) bytes"

# Decrypt using DPAPI
Add-Type -AssemblyName System.Security
try {
    $decryptedKey = [System.Security.Cryptography.ProtectedData]::Unprotect(
        $encryptedKeyBytes,
        $null,
        [System.Security.Cryptography.DataProtectionScope]::CurrentUser
    )
    $keyHex = [BitConverter]::ToString($decryptedKey).Replace("-", "").ToLower()
    Write-Host "SUCCESS: Decrypted key length: $($decryptedKey.Length) bytes" -ForegroundColor Green
    Write-Host "Key (first 16 chars): $($keyHex.Substring(0,16))..." -ForegroundColor Green
    
    # Save key to file for use by Python
    $keyPath = Join-Path $scriptDir "data\signal_key.txt"
    $keyHex | Out-File -FilePath $keyPath -NoNewline -Encoding ASCII
    Write-Host "Key saved to: $keyPath" -ForegroundColor Green
    
} catch {
    Write-Host "ERROR: DPAPI decryption failed" -ForegroundColor Red
    Write-Host "This usually means:" -ForegroundColor Yellow
    Write-Host "  1. Different Windows user account" -ForegroundColor Yellow
    Write-Host "  2. Different computer" -ForegroundColor Yellow
    Write-Host "  3. Corrupted data" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Exception: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "1. Install Python sqlite3 with sqlcipher support"
Write-Host "2. Run: python open_signal_db.py"
