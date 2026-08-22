[CmdletBinding(SupportsShouldProcess=$true)]
param(
  [ValidateSet('Chrome','Edge','Both')][string]$Browser = 'Chrome',
  [ValidateSet('CurrentUser','LocalMachine')][string]$Scope = 'CurrentUser',
  [switch]$KeepFiles
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# Remove every registration this install family can create (both hives x both
# browsers), not just the hive/browser named by parameters. A mismatched
# -Scope (e.g. LocalMachine install followed by a default CurrentUser
# uninstall) otherwise leaves machine-wide registrations pointing at files
# this script then deletes, so anyone able to recreate those files controls
# what every user's browser launches as the native host.
$hostKeyNames = @(
  'SOFTWARE\Google\Chrome\NativeMessagingHosts\com.mistral_nex_stocks.host',
  'SOFTWARE\Microsoft\Edge\NativeMessagingHosts\com.mistral_nex_stocks.host'
)
$roots = @(
  [Microsoft.Win32.Registry]::CurrentUser,
  [Microsoft.Win32.Registry]::LocalMachine
)
# Registry paths whose stored manifest still points into this directory.
$liveManifestPaths = New-Object System.Collections.Generic.List[string]

foreach ($root in $roots) {
  $rootName = if ($root -eq [Microsoft.Win32.Registry]::LocalMachine) { 'HKLM' } else { 'HKCU' }
  foreach ($subKey in $hostKeyNames) {
    # Read the stored manifest path first (read-only open works without
    # elevation, so a non-admin uninstall can still detect an HKLM entry that
    # references this directory before attempting deletion).
    try {
      $key = $root.OpenSubKey($subKey)
      if ($null -ne $key) {
        try {
          $manifestPath = [string]$key.GetValue('', '')
          if ((-not [string]::IsNullOrWhiteSpace($manifestPath)) -and
              ($manifestPath -ieq (Join-Path $scriptDir 'com.mistral_nex_stocks.host.json'))) {
            [void]$liveManifestPaths.Add($manifestPath)
          }
        } finally {
          $key.Close()
        }
      }
    } catch {}
    try {
      if ($PSCmdlet.ShouldProcess("$rootName\$subKey", "Remove NativeMessagingHosts registry key")) {
        $root.DeleteSubKeyTree($subKey, $false)
      }
    } catch {}
  }
}

if (-not $KeepFiles) {
  foreach ($f in @((Join-Path $scriptDir 'native_host.cmd'), (Join-Path $scriptDir 'com.mistral_nex_stocks.host.json'))) {
    if (Test-Path $f) {
      # Never delete a generated manifest that a surviving registration still
      # references (deleting it would leave a dangling entry pointing at a
      # recreatable path). This can only happen when registry deletion above
      # was declined (-WhatIf) or failed.
      $stillReferenced = $false
      foreach ($livePath in $liveManifestPaths) {
        if ($livePath -ieq $f) { $stillReferenced = $true; break }
      }
      if ($stillReferenced) {
        Write-Host "[WARN] Skipped $f : still referenced by a surviving registry entry." -ForegroundColor Yellow
        continue
      }
      if ($PSCmdlet.ShouldProcess($f, "Delete generated host file")) {
        Remove-Item $f -Force
      }
    }
  }
}

Write-Host 'Native host registration removed.' -ForegroundColor Green
