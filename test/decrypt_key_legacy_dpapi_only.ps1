# Legacy script (DPAPI-only) kept as backup.
# This approach is NOT sufficient for current Signal Desktop,
# because config.json "encryptedKey" is typically in Chromium/Electron "v10" AES-GCM format.
#
# Prefer running decrypt_key.ps1 instead.

$signalPath = "$env:APPDATA\Signal"
$configPath = "$signalPath\config.json"

Write-Host "Signal Desktop Key Decryptor (LEGACY: DPAPI-only)" -ForegroundColor Yellow
Write-Host "=================================================" -ForegroundColor Yellow
Write-Host ""

if (-not (Test-Path $configPath)) {
    Write-Host "ERROR: Signal config not found at $configPath" -ForegroundColor Red
    pause
    exit 1
}

$config = Get-Content $configPath | ConvertFrom-Json
$encryptedKeyHex = $config.encryptedKey
if (-not $encryptedKeyHex) {
    Write-Host "ERROR: No encryptedKey found in config.json" -ForegroundColor Red
    pause
    exit 1
}

$encryptedKeyBytes = [byte[]]::new($encryptedKeyHex.Length / 2)
for ($i = 0; $i -lt $encryptedKeyHex.Length; $i += 2) {
    $encryptedKeyBytes[$i / 2] = [Convert]::ToByte($encryptedKeyHex.Substring($i, 2), 16)
}

Add-Type -AssemblyName System.Security
try {
    $decryptedKey = [System.Security.Cryptography.ProtectedData]::Unprotect(
        $encryptedKeyBytes,
        $null,
        [System.Security.Cryptography.DataProtectionScope]::CurrentUser
    )
    $keyHex = [BitConverter]::ToString($decryptedKey).Replace("-", "").ToLower()
    Write-Host "SUCCESS (legacy). Decrypted bytes: $($decryptedKey.Length)" -ForegroundColor Green
    Write-Host $keyHex
} catch {
    Write-Host "ERROR: DPAPI decryption failed (legacy)" -ForegroundColor Red
    Write-Host "Message: $($_.Exception.Message)" -ForegroundColor Red
}

Write-Host ""
pause

