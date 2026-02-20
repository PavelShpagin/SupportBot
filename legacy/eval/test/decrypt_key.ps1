# Signal Desktop Key Decryptor (Windows)
#
# Why you got "The data is invalid.":
# - Signal Desktop stores config.json "encryptedKey" in Chromium/Electron "v10" AES-GCM format.
# - That value is NOT a DPAPI blob. DPAPI is used to protect a *master key* in "Local State".
# - So we must:
#   1) DPAPI-decrypt Local State os_crypt.encrypted_key -> master key
#   2) AES-GCM decrypt config.json encryptedKey (v10) -> SQLCipher key (usually 64 hex chars)
#
# Run on the SAME Windows user account where Signal Desktop is installed.
#
# Output:
# - Desktop\signal_key.txt (DO NOT commit/share publicly)

$ErrorActionPreference = "Stop"

$signalPath = "$env:APPDATA\Signal"
$configPath = "$signalPath\config.json"
$localStatePath = "$signalPath\Local State"

Write-Host "Signal Desktop Key Decryptor" -ForegroundColor Cyan
Write-Host "=============================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Signal dir: $signalPath" -ForegroundColor DarkGray
Write-Host "Config:     $configPath" -ForegroundColor DarkGray
Write-Host "LocalState: $localStatePath" -ForegroundColor DarkGray
Write-Host ""

if (-not (Test-Path $configPath)) {
    Write-Host "ERROR: Signal config not found at $configPath" -ForegroundColor Red
    Write-Host "Is Signal Desktop installed for this Windows user?" -ForegroundColor Yellow
    Write-Host ""
    pause
    exit 1
}

if (-not (Test-Path $localStatePath)) {
    Write-Host "ERROR: Signal Local State not found at $localStatePath" -ForegroundColor Red
    Write-Host "This file is required to decrypt the master key." -ForegroundColor Yellow
    Write-Host ""
    pause
    exit 1
}

function HexToBytes([string]$hex) {
    $hex = $hex.Trim()
    if ($hex.Length % 2 -ne 0) { throw "Hex length must be even, got $($hex.Length)" }
    $bytes = [byte[]]::new($hex.Length / 2)
    for ($i = 0; $i -lt $hex.Length; $i += 2) {
        $bytes[$i / 2] = [Convert]::ToByte($hex.Substring($i, 2), 16)
    }
    return $bytes
}

function BytesToHex([byte[]]$bytes) {
    return ([BitConverter]::ToString($bytes)).Replace("-", "").ToLower()
}

function IsAsciiHex([byte[]]$bytes) {
    foreach ($b in $bytes) {
        if (($b -ge 0x30 -and $b -le 0x39) -or ($b -ge 0x61 -and $b -le 0x66) -or ($b -ge 0x41 -and $b -le 0x46)) {
            continue
        }
        return $false
    }
    return $true
}

# Read Local State and DPAPI-decrypt master key
Write-Host "Step 1: Read Local State and decrypt master key (DPAPI)" -ForegroundColor Cyan

$localStateRaw = Get-Content $localStatePath -Raw -ErrorAction Stop
$localState = $localStateRaw | ConvertFrom-Json

$encKeyB64 = $localState.os_crypt.encrypted_key
if (-not $encKeyB64) { throw "Local State missing os_crypt.encrypted_key" }

$encKeyAll = [Convert]::FromBase64String($encKeyB64)
Write-Host ("Local State encrypted_key (base64) length: {0} bytes" -f $encKeyAll.Length) -ForegroundColor DarkGray

$prefix = [System.Text.Encoding]::ASCII.GetString($encKeyAll, 0, [Math]::Min(5, $encKeyAll.Length))
Write-Host ("Local State encrypted_key prefix: '{0}'" -f $prefix) -ForegroundColor DarkGray
if ($prefix -ne "DPAPI") { throw "Unexpected Local State encrypted_key prefix: $prefix (expected DPAPI)" }

$encKeyDpapi = $encKeyAll[5..($encKeyAll.Length - 1)]

Add-Type -AssemblyName System.Security
$masterKey = [System.Security.Cryptography.ProtectedData]::Unprotect(
    $encKeyDpapi,
    $null,
    [System.Security.Cryptography.DataProtectionScope]::CurrentUser
)

Write-Host ("Master key decrypted: {0} bytes, hex prefix: {1}..." -f $masterKey.Length, (BytesToHex($masterKey).Substring(0, 16))) -ForegroundColor Green
Write-Host ""

# Read config.json encryptedKey
Write-Host "Step 2: Read config.json and decrypt encryptedKey" -ForegroundColor Cyan

$configRaw = Get-Content $configPath -Raw -ErrorAction Stop
$config = $configRaw | ConvertFrom-Json
$encryptedKeyHex = $config.encryptedKey
if (-not $encryptedKeyHex) { throw "config.json missing encryptedKey" }

Write-Host ("config.json encryptedKey hex length: {0} chars ({1} bytes)" -f $encryptedKeyHex.Length, ($encryptedKeyHex.Length / 2)) -ForegroundColor DarkGray
$encryptedKeyBytes = HexToBytes $encryptedKeyHex

$vPrefix = [System.Text.Encoding]::ASCII.GetString($encryptedKeyBytes, 0, [Math]::Min(3, $encryptedKeyBytes.Length))
Write-Host ("encryptedKey prefix: '{0}' (expected 'v10' or 'v11')" -f $vPrefix) -ForegroundColor DarkGray

if ($encryptedKeyBytes.Length -lt 3 + 12 + 16) {
    throw "encryptedKey too short to be v10 AES-GCM (len=$($encryptedKeyBytes.Length))"
}

if ($vPrefix -ne "v10" -and $vPrefix -ne "v11") {
    Write-Host ""
    Write-Host "NOTE: encryptedKey is not v10/v11. The legacy DPAPI-only method might apply, but is uncommon." -ForegroundColor Yellow
    Write-Host "Try running: decrypt_key_legacy_dpapi_only.ps1" -ForegroundColor Yellow
    Write-Host ""
    pause
    exit 1
}

# Parse Chromium v10 format: "v10" + 12-byte nonce + ciphertext||tag(16)
# (Cast slices to byte[]; PowerShell slicing returns object[] by default.)
$nonce = [byte[]]($encryptedKeyBytes[3..14])
$cipherAndTag = [byte[]]($encryptedKeyBytes[15..($encryptedKeyBytes.Length - 1)])
$ciphertext = [byte[]]($cipherAndTag[0..($cipherAndTag.Length - 17)])
$tag = [byte[]]($cipherAndTag[($cipherAndTag.Length - 16)..($cipherAndTag.Length - 1)])

Write-Host ("Nonce length: {0}  Ciphertext length: {1}  Tag length: {2}" -f $nonce.Length, $ciphertext.Length, $tag.Length) -ForegroundColor DarkGray

# AES-GCM decrypt. This requires .NET's AesGcm (PowerShell 7 / modern .NET).
$AesGcmType = 'System.Security.Cryptography.AesGcm' -as [type]
if (-not $AesGcmType) {
    Write-Host ""
    Write-Host "ERROR: AES-GCM decryption requires PowerShell 7+ (AesGcm not available in Windows PowerShell 5.1)." -ForegroundColor Red
    Write-Host ""
    Write-Host "What to do:" -ForegroundColor Yellow
    Write-Host "1) Install PowerShell 7 (pwsh): https://aka.ms/powershell" -ForegroundColor Yellow
    Write-Host "2) Re-run this script using pwsh:" -ForegroundColor Yellow
    Write-Host "   pwsh -File .\\decrypt_key.ps1" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "OR: use the Python variant (I can provide if you prefer)." -ForegroundColor Yellow
    Write-Host ""
    pause
    exit 1
}

Write-Host "Decrypting AES-GCM..." -ForegroundColor Cyan
try {
    $aes = $AesGcmType::new($masterKey)
    $plaintext = New-Object byte[] ($ciphertext.Length)
    $aes.Decrypt($nonce, $ciphertext, $tag, $plaintext, $null)
} catch {
    Write-Host ""
    Write-Host "ERROR: AES-GCM decrypt failed." -ForegroundColor Red
    Write-Host "Message: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host ""
    Write-Host "Most likely causes:" -ForegroundColor Yellow
    Write-Host "  - Different Windows user account than the one that created this Signal data" -ForegroundColor Yellow
    Write-Host "  - Signal was reinstalled / Local State + config.json don't match" -ForegroundColor Yellow
    Write-Host "  - You're pointing at the wrong extracted folder (if using a backup)" -ForegroundColor Yellow
    Write-Host ""
    pause
    exit 1
}

Write-Host ("Decrypted plaintext length: {0} bytes" -f $plaintext.Length) -ForegroundColor Green

# Interpret output. Often plaintext is ASCII hex (64 chars) representing the SQLCipher key.
$outPath = "$env:USERPROFILE\Desktop\signal_key.txt"

if (IsAsciiHex $plaintext) {
    $keyHex = [System.Text.Encoding]::ASCII.GetString($plaintext)
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Green
    Write-Host "SUCCESS! SQLCipher key (ASCII hex):" -ForegroundColor Green
    Write-Host "========================================" -ForegroundColor Green
    Write-Host ""
    Write-Host $keyHex -ForegroundColor White
    $keyHex | Out-File -FilePath $outPath -NoNewline -Encoding ASCII
    Write-Host ""
    Write-Host "Saved to: $outPath" -ForegroundColor Green
} else {
    $keyHex = BytesToHex $plaintext
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Green
    Write-Host "SUCCESS! SQLCipher key (raw bytes as hex):" -ForegroundColor Green
    Write-Host "========================================" -ForegroundColor Green
    Write-Host ""
    Write-Host $keyHex -ForegroundColor White
    $keyHex | Out-File -FilePath $outPath -NoNewline -Encoding ASCII
    Write-Host ""
    Write-Host "Saved to: $outPath" -ForegroundColor Green
}

Write-Host ""
Write-Host "Done." -ForegroundColor Cyan
Write-Host ""
pause
