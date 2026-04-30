param([ValidateSet('Chrome','Edge','Both')][string]$Browser = 'Both',[ValidateSet('CurrentUser','LocalMachine')][string]$Scope = 'CurrentUser',[string]$HostName = 'com.mistral_nex_stocks.host')
$ErrorActionPreference = 'Stop'
function Open-RegistryRoot { param([string]$InstallScope) if ($InstallScope -eq 'LocalMachine') { return [Microsoft.Win32.Registry]::LocalMachine } return [Microsoft.Win32.Registry]::CurrentUser }
function Get-SubKeys { param([string]$BrowserName,[string]$Name) $keys=@(); if ($BrowserName -in @('Chrome','Both')) { $keys += 'SOFTWARE\Google\Chrome\NativeMessagingHosts\' + $Name }; if ($BrowserName -in @('Edge','Both')) { $keys += 'SOFTWARE\Microsoft\Edge\NativeMessagingHosts\' + $Name }; return $keys }
$root = Open-RegistryRoot -InstallScope $Scope
foreach ($subKey in (Get-SubKeys -BrowserName $Browser -Name $HostName)) {
  Write-Host "===== $Scope\$subKey =====" -ForegroundColor Cyan
  $key = $root.OpenSubKey($subKey)
  if (-not $key) { Write-Host 'Registry key: MISSING' -ForegroundColor Yellow; continue }
  $manifestPath = $key.GetValue('', $null)
  Write-Host "Manifest path : $manifestPath"
  if (-not $manifestPath -or -not (Test-Path $manifestPath -PathType Leaf)) { Write-Host 'Manifest file : MISSING' -ForegroundColor Yellow; continue }
  $raw = Get-Content $manifestPath -Raw
  try {
    $manifest = $raw | ConvertFrom-Json
    Write-Host "Name          : $($manifest.name)"
    Write-Host "Type          : $($manifest.type)"
    Write-Host "Path          : $($manifest.path)"
    Write-Host "Origins       : $($manifest.allowed_origins -join ', ')"
    $resolvedPath = if ([System.IO.Path]::IsPathRooted($manifest.path)) { $manifest.path } else { Join-Path (Split-Path $manifestPath -Parent) $manifest.path }
    Write-Host "Resolved path : $resolvedPath"
    Write-Host "Path exists   : $(Test-Path $resolvedPath -PathType Leaf)"
  } catch {
    Write-Host "JSON parse    : FAILED ($($_.Exception.Message))" -ForegroundColor Red
  }
  Write-Host ''
}
