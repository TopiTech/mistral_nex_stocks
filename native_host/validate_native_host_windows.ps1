[CmdletBinding()]
param(
  [string]$ManifestPath = '',
  [switch]$RequireLauncher
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if (-not $ManifestPath) {
  $baseDir = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
  $ManifestPath = Join-Path $baseDir 'com.mistral_nex_stocks.host.json'
}

# Read-only validation. This intentionally does not access or modify the registry.
$scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
$templatePath = Join-Path $scriptDir 'com.mistral_nex_stocks.host.json.template'
if (-not (Test-Path -LiteralPath $templatePath -PathType Leaf)) {
  throw "Manifest template not found: $templatePath"
}
$templatePath = (Resolve-Path -LiteralPath $templatePath).Path
if (-not (Test-Path $ManifestPath -PathType Leaf)) {
  $ManifestPath = $templatePath
  Write-Host "[INFO] Generated manifest not found; validating template." -ForegroundColor Yellow
}

$resolvedManifestPath = (Resolve-Path -LiteralPath $ManifestPath).Path
$isTemplate = [string]::Equals(
  $resolvedManifestPath,
  $templatePath,
  [System.StringComparison]::OrdinalIgnoreCase
)
$manifest = Get-Content -LiteralPath $resolvedManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($manifest.name -ne 'com.mistral_nex_stocks.host') { throw 'Invalid native host name.' }
if ($manifest.type -ne 'stdio') { throw 'Native host type must be stdio.' }
if (-not $manifest.path) { throw 'Native host launcher path is missing.' }
if (-not $isTemplate -and ($null -eq $manifest.allowed_origins -or @($manifest.allowed_origins).Count -eq 0)) {
  throw 'Manifest must contain at least one allowed origin.'
}
foreach ($origin in @($manifest.allowed_origins)) {
  if ($origin -notmatch '^chrome-extension://[a-z0-9]{32}/$') { throw "Invalid allowed origin: $origin" }
}

$resolvedLauncher = if ([IO.Path]::IsPathRooted($manifest.path)) { $manifest.path } else { Join-Path (Split-Path -Parent $resolvedManifestPath) $manifest.path }
if (Test-Path $resolvedLauncher -PathType Leaf) {
  Write-Host "[ OK ] Launcher exists: $resolvedLauncher" -ForegroundColor Green
} elseif ($RequireLauncher) {
  throw "Launcher does not exist: $resolvedLauncher"
} else {
  Write-Host "[WARN] Machine-specific launcher is missing: $resolvedLauncher" -ForegroundColor Yellow
}
foreach ($required in @('native_host.py', 'start_backend.py')) {
  if (-not (Test-Path (Join-Path $scriptDir $required) -PathType Leaf)) { throw "Required source file missing: $required" }
}
Write-Host "[ OK ] Native host manifest is structurally valid (read-only check)." -ForegroundColor Green
