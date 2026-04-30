param([ValidateSet('Chrome','Edge','Both')][string]$Browser = 'Chrome',[ValidateSet('CurrentUser','LocalMachine')][string]$Scope = 'CurrentUser',[switch]$KeepFiles)
function Open-RegistryRoot { param([string]$InstallScope) if ($InstallScope -eq 'LocalMachine') { return [Microsoft.Win32.Registry]::LocalMachine } return [Microsoft.Win32.Registry]::CurrentUser }
function Get-SubKeys { param([string]$BrowserName) $keys = New-Object System.Collections.Generic.List[string]; if ($BrowserName -in @('Chrome','Both')) { [void]$keys.Add('SOFTWARE\Google\Chrome\NativeMessagingHosts\com.mistral_nex_stocks.host') }; if ($BrowserName -in @('Edge','Both')) { [void]$keys.Add('SOFTWARE\Microsoft\Edge\NativeMessagingHosts\com.mistral_nex_stocks.host') }; return $keys }
$rootKey = Open-RegistryRoot -InstallScope $Scope
foreach ($subKey in (Get-SubKeys -BrowserName $Browser)) { try { $rootKey.DeleteSubKeyTree($subKey, $false) } catch {} }
if (-not $KeepFiles) {
  $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
  foreach ($f in @((Join-Path $scriptDir 'native_host.cmd'), (Join-Path $scriptDir 'com.mistral_nex_stocks.host.json'))) {
    if (Test-Path $f) { Remove-Item $f -Force }
  }
}
Write-Host 'Native host registration removed.' -ForegroundColor Green
