[CmdletBinding(SupportsShouldProcess=$true)]
param(
  [ValidateSet('Chrome','Edge','Both')][string]$Browser = 'Both',
  [ValidateSet('CurrentUser','LocalMachine','All')][string]$Scope = 'All',
  [switch]$KeepFiles
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

$chromeKey = 'SOFTWARE\Google\Chrome\NativeMessagingHosts\com.mistral_nex_stocks.host'
$edgeKey = 'SOFTWARE\Microsoft\Edge\NativeMessagingHosts\com.mistral_nex_stocks.host'
$allHostKeyNames = @($chromeKey, $edgeKey)
$targetKeyNames = New-Object System.Collections.Generic.List[string]
if ($Browser -in @('Chrome','Both')) { [void]$targetKeyNames.Add($chromeKey) }
if ($Browser -in @('Edge','Both')) { [void]$targetKeyNames.Add($edgeKey) }

$allRoots = @(
  [Microsoft.Win32.Registry]::CurrentUser,
  [Microsoft.Win32.Registry]::LocalMachine
)
$targetRoots = New-Object System.Collections.Generic.List[Microsoft.Win32.RegistryKey]
if ($Scope -in @('CurrentUser','All')) { [void]$targetRoots.Add([Microsoft.Win32.Registry]::CurrentUser) }
if ($Scope -in @('LocalMachine','All')) { [void]$targetRoots.Add([Microsoft.Win32.Registry]::LocalMachine) }

$failedKeys = New-Object System.Collections.Generic.List[string]

foreach ($root in $targetRoots) {
  $rootName = if ($root -eq [Microsoft.Win32.Registry]::LocalMachine) { 'HKLM' } else { 'HKCU' }
  foreach ($subKey in $targetKeyNames) {
    $exists = $false
    try {
      $probe = $root.OpenSubKey($subKey, $false)
      if ($null -ne $probe) {
        $exists = $true
        $probe.Close()
      }
    } catch {
      [void]$failedKeys.Add("$rootName\${subKey}: $($_.Exception.Message)")
      continue
    }
    if ($exists) {
      try {
        if ($PSCmdlet.ShouldProcess("$rootName\$subKey", "Remove NativeMessagingHosts registry key")) {
          $root.DeleteSubKeyTree($subKey, $false)
        }
      } catch {
        [void]$failedKeys.Add("$rootName\${subKey}: $($_.Exception.Message)")
      }
    }
  }
}

# Scan ALL hives x BOTH browsers after the deletion pass so that any surviving
# registration (from another browser/scope, -WhatIf, or a failed removal) blocks
# deletion of shared launcher/manifest files.
$liveManifestPaths = New-Object System.Collections.Generic.List[string]
$expectedManifestPath = Join-Path $scriptDir 'com.mistral_nex_stocks.host.json'
foreach ($root in $allRoots) {
  foreach ($subKey in $allHostKeyNames) {
    try {
      $key = $root.OpenSubKey($subKey, $false)
      if ($null -ne $key) {
        try {
          $manifestPath = [string]$key.GetValue('', '')
          if ((-not [string]::IsNullOrWhiteSpace($manifestPath)) -and
              ($manifestPath -ieq $expectedManifestPath)) {
            [void]$liveManifestPaths.Add($manifestPath)
          }
        } finally {
          $key.Close()
        }
      }
    } catch {
      # Treat an unreadable surviving key conservatively as still present.
      [void]$liveManifestPaths.Add($expectedManifestPath)
    }
  }
}

if (-not $KeepFiles) {
  foreach ($f in @((Join-Path $scriptDir 'native_host.cmd'), $expectedManifestPath)) {
    if (Test-Path -LiteralPath $f) {
      if ($liveManifestPaths.Count -gt 0) {
        Write-Host "[WARN] Skipped $f : still referenced by a surviving registry entry." -ForegroundColor Yellow
        continue
      }
      if ($PSCmdlet.ShouldProcess($f, "Delete generated host file")) {
        Remove-Item -LiteralPath $f -Force
      }
    }
  }
}

if ($failedKeys.Count -gt 0) {
  foreach ($err in $failedKeys) {
    Write-Host "[ERROR] Failed to remove registry key $err" -ForegroundColor Red
  }
  throw "Failed to remove $($failedKeys.Count) native host registry key(s)."
}

Write-Host 'Native host registration removed.' -ForegroundColor Green
