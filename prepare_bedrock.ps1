#Requires -Version 5.1
<#
Stage pinned ViaProxy and Geyser files for the private AI-AM server.
This script never starts Java, changes network policy, opens a port, or joins a game.
The loopback NetherNet configuration is staging only; client connectivity is untested.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$DataDir,
    [string]$JavaExe = '',
    [switch]$Plan
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Resolve-BedrockPath {
    param([string]$Value, [string]$Label)
    if ([string]::IsNullOrWhiteSpace($Value) -or $Value -notmatch '^[A-Za-z]:\\') {
        throw "$Label must be an absolute local path on a non-C drive."
    }
    $full = [IO.Path]::GetFullPath($Value)
    if ($full -match '^[Cc]:\\' -or $full.Substring(3) -match '[<>:"|?*\x00-\x1F]') {
        throw "$Label must be a normal local path on a non-C drive."
    }
    $drive = [IO.Path]::GetPathRoot($full)
    if (-not (Test-Path -LiteralPath $drive -PathType Container)) { throw "$Label drive is unavailable: $drive" }
    $cursor = $full
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            if (((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "$Label traverses a junction or symbolic link: $cursor"
            }
        }
        if ($cursor.TrimEnd('\') -eq $drive.TrimEnd('\')) { break }
        $cursor = [IO.Path]::GetDirectoryName($cursor.TrimEnd('\'))
    }
    return $full
}
function Write-NewText {
    param([string]$Path, [string]$Text)
    $stream = [IO.File]::Open($Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try {
        $bytes = (New-Object Text.UTF8Encoding($false)).GetBytes($Text)
        $stream.Write($bytes, 0, $bytes.Length)
    } finally { $stream.Dispose() }
}
function Get-VerifiedJar {
    param([string]$Url, [string]$Path, [string]$Sha256)
    if (Test-Path -LiteralPath $Path) { throw "Refusing to overwrite $Path" }
    Invoke-WebRequest -Uri $Url -OutFile $Path -UseBasicParsing -TimeoutSec 600
    if ((Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash -ne $Sha256) {
        throw "SHA256 verification failed for $Path. The JAR has not been executed."
    }
}

$DataDir = Resolve-BedrockPath $DataDir 'DataDir'
if ($DataDir.TrimEnd('\') -eq [IO.Path]::GetPathRoot($DataDir).TrimEnd('\')) {
    throw 'Choose a new data directory, not a drive root.'
}
if (Test-Path -LiteralPath $DataDir) {
    if (-not (Test-Path -LiteralPath $DataDir -PathType Container) -or @(Get-ChildItem -LiteralPath $DataDir -Force | Select-Object -First 1).Count) {
        throw 'DataDir must be new or empty. Existing or partial installations are never overwritten.'
    }
}
if ($JavaExe) {
    $JavaExe = Resolve-BedrockPath $JavaExe 'Java executable'
    if (-not (Test-Path -LiteralPath $JavaExe -PathType Leaf) -or [IO.Path]::GetExtension($JavaExe) -ne '.exe') {
        throw 'JavaExe must name an existing non-C Java executable.'
    }
}

$ViaProxyVersion = '3.4.14'
$GeyserVersion = '2.12.1'
$GeyserBuild = 1295
$ViaProxyUrl = 'https://github.com/ViaVersion/ViaProxy/releases/download/v3.4.14/ViaProxy-3.4.14.jar'
$GeyserUrl = 'https://download.geysermc.org/v2/projects/geyser/versions/2.12.1/builds/1295/downloads/viaproxy'
$ViaProxyHash = '2894bbfb2f4342f8fde887af6c689313efd5b0e46a8006775eea0e21e2948ab5'
$GeyserHash = '3145fa5bcd55bc1e87be1e909ffe93d80bb11415e6bf8ebb13d887e24a497654'
$PluginsDir = Join-Path $DataDir 'plugins'
$GeyserDir = Join-Path $PluginsDir 'Geyser'
$TempDir = Join-Path $DataDir 'temp'
$ViaProxyJar = Join-Path $DataDir "ViaProxy-$ViaProxyVersion.jar"
$ViaProxyConfig = Join-Path $DataDir 'viaproxy.yml'
$GeyserJar = Join-Path $PluginsDir 'Geyser-ViaProxy.jar'
$GeyserConfig = Join-Path $GeyserDir 'config.yml'
$ManifestPath = Join-Path $DataDir 'prepared-bedrock.json'
$manifest = [ordered]@{
    schema=1; data_dir=$DataDir; java_exe=$JavaExe; temp_dir=$TempDir
    viaproxy_jar=$ViaProxyJar; viaproxy_config=$ViaProxyConfig
    geyser_jar=$GeyserJar; geyser_config=$GeyserConfig
    viaproxy_version=$ViaProxyVersion; geyser_version=$GeyserVersion; geyser_build=$GeyserBuild
    viaproxy_sha256=$ViaProxyHash; geyser_sha256=$GeyserHash
    viaproxy_source=$ViaProxyUrl; geyser_source=$GeyserUrl
    java_server_host='127.0.0.1'; java_server_port=25565; target_java_version='1.21.4'
    proxy_host='127.0.0.1'; proxy_port=25568; bedrock_host='127.0.0.1'; bedrock_port=19132
    transport='nethernet'; bind_mode='loopback_staging_only'; launch_tested=$false
}
if ($Plan) {
    [pscustomobject]@{
        mode='plan'; manifest=$manifest
        writes=@((Join-Path $DataDir '.preparation-started'), $ViaProxyJar, $GeyserJar, $ViaProxyConfig, $GeyserConfig, $TempDir, $ManifestPath)
        downloads=$false; services_started=$false; network_policy_changes=$false
    } | ConvertTo-Json -Depth 5
    return
}

$viaYaml = @'
# Private staging configuration; nothing is started by prepare_bedrock.ps1.
bind-address: '127.0.0.1:25568'
target-address: '127.0.0.1:25565'
target-version: '1.21.4'
proxy-online-mode: false
auth-method: NONE
'@
$geyserYaml = @'
# Pinned Geyser 2.12.1 build 1295, configuration version 12.
# Loopback is a staging default, not a verified NetherNet connection route.
config-version: 12
bedrock:
  address: '127.0.0.1'
  port: 19132
  webrtc-port: 0
  clone-remote-port: false
  transport: nethernet
  signaling:
    mode: builtin
    stun-discovery: false
java:
  auth-type: offline
'@
$OldEnvironment = @{}
$OldProgress = $ProgressPreference
$OldTls = [Net.ServicePointManager]::SecurityProtocol
try {
    foreach ($key in @('TEMP','TMP','TMPDIR')) {
        $OldEnvironment[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
        [Environment]::SetEnvironmentVariable($key, $TempDir, 'Process')
    }
    $ProgressPreference = 'SilentlyContinue'
    [Net.ServicePointManager]::SecurityProtocol = $OldTls -bor [Net.SecurityProtocolType]::Tls12
    $null = New-Item -ItemType Directory -Path $DataDir -Force
    Write-NewText (Join-Path $DataDir '.preparation-started') ''
    foreach ($folder in @($PluginsDir, $GeyserDir, $TempDir)) { $null = New-Item -ItemType Directory -Path $folder }
    Write-Host "Downloading and verifying ViaProxy $ViaProxyVersion..."
    Get-VerifiedJar $ViaProxyUrl $ViaProxyJar $ViaProxyHash
    Write-Host "Downloading and verifying Geyser $GeyserVersion build $GeyserBuild..."
    Get-VerifiedJar $GeyserUrl $GeyserJar $GeyserHash
    Write-NewText $ViaProxyConfig $viaYaml
    Write-NewText $GeyserConfig $geyserYaml
    Write-NewText $ManifestPath ($manifest | ConvertTo-Json -Depth 5)
    Write-Host "Files prepared: $ManifestPath"
    Write-Host 'No Java process or network-policy change was made. NetherNet connectivity remains untested.'
} catch {
    Write-Warning "Preparation stopped. Partial files remain in $DataDir; nothing was deleted or executed."
    throw
} finally {
    foreach ($key in $OldEnvironment.Keys) { [Environment]::SetEnvironmentVariable($key, $OldEnvironment[$key], 'Process') }
    $ProgressPreference = $OldProgress
    [Net.ServicePointManager]::SecurityProtocol = $OldTls
}
