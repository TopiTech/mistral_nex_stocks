[CmdletBinding(SupportsShouldProcess=$true)]
param(
  [Parameter(Mandatory=$true)]
  [string[]]$ExtensionIds,
  [ValidateSet('Chrome','Edge','Both')][string]$Browser = 'Chrome',
  [ValidateSet('CurrentUser','LocalMachine')][string]$Scope = 'CurrentUser',
  [string]$PythonPath,
  [switch]$Force,
  [switch]$NoUnblock
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# セキュリティ強化関数
function Test-ExtensionId {
  param([string]$Id)
  if (-not $Id) { return $false }
  $trimmed = $Id.Trim()
  # 拡張機能IDの形式検証（32文字の16進数）
  if ($trimmed -notmatch '^[a-z0-9]{32}$') {
    Write-Host "[ERROR] Invalid extension ID format: $trimmed" -ForegroundColor Red
    return $false
  }
  return $true
}

function Test-SafePath {
  param([string]$Path)
  if (-not $Path) { return $false }
  # パストラバーサル攻撃の検出
  if ($Path -match '\.\.|\|/') {
    Write-Host "[ERROR] Unsafe path detected: $Path" -ForegroundColor Red
    return $false
  }
  return $true
}

function Test-Admin { 
  $id=[Security.Principal.WindowsIdentity]::GetCurrent(); 
  $p=New-Object Security.Principal.WindowsPrincipal($id); 
  return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) 
}

function Protect-FilePermissions {
  param([string]$Path)
  if (-not (Test-Path $Path)) { return }
  try {
    $sidSystem = New-Object System.Security.Principal.SecurityIdentifier("S-1-5-18")
    $sidAdmins = New-Object System.Security.Principal.SecurityIdentifier("S-1-5-32-544")
    $sidUsers = New-Object System.Security.Principal.SecurityIdentifier("S-1-5-32-545")

    $identitySystem = $sidSystem.Translate([System.Security.Principal.NTAccount])
    $identityAdmins = $sidAdmins.Translate([System.Security.Principal.NTAccount])
    $identityUsers = $sidUsers.Translate([System.Security.Principal.NTAccount])

    $acl = New-Object System.Security.AccessControl.FileSecurity
    $acl.SetAccessRuleProtection($true, $false)

    $arSystem = New-Object System.Security.AccessControl.FileSystemAccessRule($identitySystem, "FullControl", "Allow")
    $acl.AddAccessRule($arSystem)

    $arAdmins = New-Object System.Security.AccessControl.FileSystemAccessRule($identityAdmins, "FullControl", "Allow")
    $acl.AddAccessRule($arAdmins)

    $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
    $identityCurrentUser = $currentUser.Translate([System.Security.Principal.NTAccount])

    if ($Scope -eq 'CurrentUser') {
      $arUser = New-Object System.Security.AccessControl.FileSystemAccessRule($identityCurrentUser, "Modify", "Allow")
      $acl.AddAccessRule($arUser)
    } else {
      $arUser = New-Object System.Security.AccessControl.FileSystemAccessRule($identityCurrentUser, "ReadAndExecute", "Allow")
      $acl.AddAccessRule($arUser)
      $arUsersGroup = New-Object System.Security.AccessControl.FileSystemAccessRule($identityUsers, "ReadAndExecute", "Allow")
      $acl.AddAccessRule($arUsersGroup)
    }

    Set-Acl -Path $Path -AclObject $acl
    Write-Host "[ OK ] Hardened file permissions (NTFS ACL): $Path" -ForegroundColor Green
  } catch {
    Write-Host "[WARN] Failed to harden permissions for ${Path}: $($_.Exception.Message)" -ForegroundColor Yellow
  }
}
function Resolve-PythonPath {
  param([string]$Requested,[string]$RootDir)
  $candidates = New-Object System.Collections.Generic.List[string]
  if ($Requested) { [void]$candidates.Add($Requested) }
  foreach ($p in @((Join-Path $RootDir '.venv\Scripts\python.exe'),(Join-Path $RootDir 'venv\Scripts\python.exe'),(Join-Path $RootDir 'Scripts\python.exe'))) { if ($p) { [void]$candidates.Add($p) } }
  foreach ($cmd in @('python.exe','python3.exe','py.exe')) { try { $g = Get-Command $cmd -ErrorAction Stop; if ($g.Source) { [void]$candidates.Add($g.Source) } } catch {} }
  foreach ($candidate in ($candidates | Select-Object -Unique)) { try { $resolved = (Resolve-Path $candidate -ErrorAction Stop).Path; if (Test-Path $resolved -PathType Leaf) { return $resolved } } catch {} }
  throw 'Python executable not found. Use -PythonPath C:\Path\To\python.exe'
}

function Write-FileNoBom {
  param([string]$Path, [string]$Content)
  $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
  [System.IO.File]::WriteAllText($Path, $Content, $Utf8NoBom)
}
function Open-RegistryRoot { param([string]$InstallScope) if ($InstallScope -eq 'LocalMachine') { return [Microsoft.Win32.Registry]::LocalMachine } return [Microsoft.Win32.Registry]::CurrentUser }
function Get-SubKeys { param([string]$BrowserName) $keys=New-Object System.Collections.Generic.List[string]; if ($BrowserName -in @('Chrome','Both')) { [void]$keys.Add('SOFTWARE\Google\Chrome\NativeMessagingHosts\com.mistral_nex_stocks.host') }; if ($BrowserName -in @('Edge','Both')) { [void]$keys.Add('SOFTWARE\Microsoft\Edge\NativeMessagingHosts\com.mistral_nex_stocks.host') }; return $keys }
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir = Split-Path -Parent $ScriptDir
$TemplateLauncher = Join-Path $ScriptDir 'host_launcher.cmd.template'
$LauncherCmd = Join-Path $ScriptDir 'native_host.cmd'
$ManifestJson = Join-Path $ScriptDir 'com.mistral_nex_stocks.host.json'
$UninstallPs1 = Join-Path $ScriptDir 'uninstall_host_windows.ps1'
foreach ($required in @($TemplateLauncher,(Join-Path $ScriptDir 'native_host.py'),(Join-Path $ScriptDir 'start_backend.py'))) { if (-not (Test-Path $required -PathType Leaf)) { throw "Required file not found: $required" } }
if (($Scope -eq 'LocalMachine') -and (-not (Test-Admin))) { throw 'Run PowerShell as Administrator when using -Scope LocalMachine.' }
if (-not $NoUnblock) { Get-ChildItem -Path $ScriptDir -File | Unblock-File -ErrorAction SilentlyContinue }
$cleanIds = @()
foreach ($id in $ExtensionIds) { 
  if (-not $id) { continue }
  $trim = $id.Trim()
  if (-not (Test-ExtensionId -Id $trim)) {
    Write-Host "[ERROR] Skipping invalid extension ID: $trim" -ForegroundColor Red
    continue
  }
  $cleanIds += $trim 
}
$cleanIds = @($cleanIds | Select-Object -Unique)
if ($cleanIds.Count -eq 0) { throw 'At least one valid extension id is required.' }
$pythonExe = Resolve-PythonPath -Requested $PythonPath -RootDir $RootDir
Write-Host "[INFO] Python: $pythonExe" -ForegroundColor Cyan
$launcher = Get-Content $TemplateLauncher -Raw -Encoding UTF8
$launcher = $launcher.Replace('__PYTHON_EXE__', $pythonExe)
Write-FileNoBom -Path $LauncherCmd -Content $launcher
Protect-FilePermissions -Path $LauncherCmd
Write-Host "[ OK ] Launcher: $LauncherCmd" -ForegroundColor Green
$launcherAbsPath = (Resolve-Path $LauncherCmd).Path
$allowedOrigins = @(); foreach ($id in $cleanIds) { $allowedOrigins += "chrome-extension://$id/" }
$manifestObject = [ordered]@{ name='com.mistral_nex_stocks.host'; description='Mistral NeX Stocks native host'; path=$launcherAbsPath; type='stdio'; allowed_origins=$allowedOrigins }
$manifestContent = $manifestObject | ConvertTo-Json -Depth 4
Write-FileNoBom -Path $ManifestJson -Content $manifestContent
Protect-FilePermissions -Path $ManifestJson
# Manifest integrity checks (encoding/path/required keys)
try {
  $manifestCheck = Get-Content -Path $ManifestJson -Raw -Encoding UTF8 | ConvertFrom-Json
  if (-not $manifestCheck.name -or -not $manifestCheck.path -or -not $manifestCheck.allowed_origins) {
    throw "Manifest validation failed: required fields are missing"
  }
  if (-not (Test-Path $manifestCheck.path -PathType Leaf)) {
    throw "Manifest validation failed: launcher path does not exist -> $($manifestCheck.path)"
  }
} catch {
  throw "Failed to validate generated manifest: $($_.Exception.Message)"
}
Write-Host "[ OK ] Manifest: $ManifestJson" -ForegroundColor Green
$rootKey = Open-RegistryRoot -InstallScope $Scope
foreach ($subKey in (Get-SubKeys -BrowserName $Browser)) {
  if ($PSCmdlet.ShouldProcess($subKey, 'Register native messaging host')) {
    $key = $rootKey.CreateSubKey($subKey)
    if (-not $key) { throw "Failed to create registry key: $subKey" }
    $manifestAbsPath = (Resolve-Path $ManifestJson).Path
    try { $key.SetValue('', $manifestAbsPath, [Microsoft.Win32.RegistryValueKind]::String); $actual = $key.GetValue('', $null); if (($actual -ne $manifestAbsPath) -and (-not $Force)) { throw "Registry verification failed: $subKey" } }
    finally { $key.Close() }
    Write-Host "[ OK ] Registry: $Scope\\$subKey" -ForegroundColor Green
  }
}
$uninstall = @"
param([ValidateSet('Chrome','Edge','Both')][string]`$Browser = '$Browser',[ValidateSet('CurrentUser','LocalMachine')][string]`$Scope = '$Scope',[switch]`$KeepFiles)
function Open-RegistryRoot { param([string]`$InstallScope) if (`$InstallScope -eq 'LocalMachine') { return [Microsoft.Win32.Registry]::LocalMachine } return [Microsoft.Win32.Registry]::CurrentUser }
function Get-SubKeys { param([string]`$BrowserName) `$keys = New-Object System.Collections.Generic.List[string]; if (`$BrowserName -in @('Chrome','Both')) { [void]`$keys.Add('SOFTWARE\Google\Chrome\NativeMessagingHosts\com.mistral_nex_stocks.host') }; if (`$BrowserName -in @('Edge','Both')) { [void]`$keys.Add('SOFTWARE\Microsoft\Edge\NativeMessagingHosts\com.mistral_nex_stocks.host') }; return `$keys }
`$rootKey = Open-RegistryRoot -InstallScope `$Scope
foreach (`$subKey in (Get-SubKeys -BrowserName `$Browser)) { try { `$rootKey.DeleteSubKeyTree(`$subKey, `$false) } catch {} }
if (-not `$KeepFiles) {
  `$scriptDir = Split-Path -Parent `$MyInvocation.MyCommand.Path
  foreach (`$f in @((Join-Path `$scriptDir 'native_host.cmd'), (Join-Path `$scriptDir 'com.mistral_nex_stocks.host.json'))) {
    if (Test-Path `$f) { Remove-Item `$f -Force }
  }
}
Write-Host 'Native host registration removed.' -ForegroundColor Green
"@
Set-Content -Path $UninstallPs1 -Value $uninstall -Encoding UTF8
Write-Host "[ OK ] Uninstaller: $UninstallPs1" -ForegroundColor Green
Write-Host ''
Write-Host '==== DONE ====' -ForegroundColor Green
Write-Host "Extension IDs : $($cleanIds -join ', ')"
Write-Host "Browser       : $Browser"
Write-Host "Scope         : $Scope"
Write-Host "Manifest      : $ManifestJson"
Write-Host "Launcher      : $LauncherCmd"
Write-Host ''
Write-Host 'Reload the browser extension, then click Start Backend in the popup.' -ForegroundColor Cyan
