# Signal Desktop Key Decryptor
# 
# This script extracts the SQLCipher encryption key from Signal Desktop.
# Run on the SAME Windows user account where Signal Desktop is installed.
#
# Usage:
#   1. Open PowerShell
#   2. cd to this folder
#   3. Run: .\decrypt_key.ps1
#
# Output: Decrypted key saved to Desktop\signal_key.txt

$signalPath = "$env:APPDATA\Signal"
$configPath = "$signalPath\config.json"

Write-Host "Signal Desktop Key Decryptor" -ForegroundColor Cyan
Write-Host "=============================" -ForegroundColor Cyan
Write-Host ""

if (-not (Test-Path $configPath)) {
    Write-Host "ERROR: Signal config not found at $configPath" -ForegroundColor Red
    Write-Host "Is Signal Desktop installed on this machine?" -ForegroundColor Yellow
    Write-Host ""
    pause
    exit 1
}

Write-Host "Found Signal config: $configPath" -ForegroundColor Green

# Read encrypted key
$config = Get-Content $configPath | ConvertFrom-Json
$encryptedKeyHex = $config.encryptedKey

if (-not $encryptedKeyHex) {
    Write-Host "ERROR: No encryptedKey found in config.json" -ForegroundColor Red
    pause
    exit 1
}

Write-Host "Encrypted key: $($encryptedKeyHex.Substring(0,30))..." -ForegroundColor Yellow

# Convert hex to bytes
$encryptedKeyBytes = [byte[]]::new($encryptedKeyHex.Length / 2)
for ($i = 0; $i -lt $encryptedKeyHex.Length; $i += 2) {
    $encryptedKeyBytes[$i / 2] = [Convert]::ToByte($encryptedKeyHex.Substring($i, 2), 16)
}

Write-Host "Encrypted key length: $($encryptedKeyBytes.Length) bytes" -ForegroundColor Yellow

# Decrypt using DPAPI
Add-Type -AssemblyName System.Security
try {
    $decryptedKey = [System.Security.Cryptography.ProtectedData]::Unprotect(
        $encryptedKeyBytes,
        $null,
        [System.Security.Cryptography.DataProtectionScope]::CurrentUser
    )
    $keyHex = [BitConverter]::ToString($decryptedKey).Replace("-", "").ToLower()
    
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Green
    Write-Host "SUCCESS! Decrypted key ($($decryptedKey.Length) bytes):" -ForegroundColor Green
    Write-Host "========================================" -ForegroundColor Green
    Write-Host ""
    Write-Host $keyHex -ForegroundColor White
    Write-Host ""
    
    # Save to file
    $outputPath = "$env:USERPROFILE\Desktop\signal_key.txt"
    $keyHex | Out-File -FilePath $outputPath -NoNewline -Encoding ASCII
    Write-Host "Key saved to: $outputPath" -ForegroundColor Green
    Write-Host ""
    Write-Host "Next steps:" -ForegroundColor Cyan
    Write-Host "1. Copy signal_key.txt to your dev machine" -ForegroundColor White
    Write-Host "2. Place it in SupportBot/test/data/signal_key.txt" -ForegroundColor White
    Write-Host "3. Run: python test/read_signal_db.py" -ForegroundColor White
    
} catch {
    Write-Host ""
    Write-Host "ERROR: DPAPI decryption failed" -ForegroundColor Red
    Write-Host "Message: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host ""
    Write-Host "This usually means:" -ForegroundColor Yellow
    Write-Host "  - You're on a different Windows user account" -ForegroundColor Yellow
    Write-Host "  - You're on a different computer" -ForegroundColor Yellow
    Write-Host "  - Signal was reinstalled" -ForegroundColor Yellow
}

Write-Host ""
pause
