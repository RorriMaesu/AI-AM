#Requires -Version 5.1
<#
Stage the verified AI-AM Minecraft fishbowl prerequisites on a non-C drive.
Use -Plan to preview the checkout and install paths without creating files.
#>
[CmdletBinding()]
param(
    [string]$Root = 'T:\AIAM-Fishbowl',
    [ValidatePattern('^[0-9a-f]{40}$')][string]$Revision = '0c7e5179dc08bb0009c9ce6be8535a4025574a79',
    [switch]$Plan
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Repository = 'https://github.com/RorriMaesu/AI-AM.git'
if ($Root -notmatch '^[A-Za-z]:[\\/]' -or $Root -match '^[Cc]:' -or $Root.Substring(3) -match '[<>:"|?*\x00-\x1F]') {
    throw 'Use an absolute path on a local drive other than C:, such as T:\AIAM-Fishbowl.'
}
$Root = [IO.Path]::GetFullPath($Root).TrimEnd('\')
if ($Root -match '^[A-Za-z]:$') { throw 'Choose a new folder below the drive root.' }
$drive = [IO.DriveInfo]::new([IO.Path]::GetPathRoot($Root))
if (!$drive.IsReady -or $drive.DriveType -notin @('Fixed', 'Removable')) {
    throw 'The destination must be on an available local disk.'
}
for ($path = $Root; $path; $path = [IO.Path]::GetDirectoryName($path)) {
    $item = Get-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    if ($item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "The destination traverses a junction or symbolic link: $path"
    }
}
if (Test-Path -LiteralPath $Root) { throw "Choose a new folder. This destination already exists: $Root" }

$checkout = Join-Path $Root 'AI-AM'
$data = Join-Path $Root 'data'
$temp = Join-Path $Root 'temp'
$manifest = Join-Path $data 'prepared.json'
if ($Plan) {
    [pscustomobject]@{
        Repository = $Repository
        Revision = $Revision
        Checkout = $checkout
        RuntimeData = $data
        BootstrapTemp = $temp
        Manifest = $manifest
    }
    return
}
$git = (Get-Command git.exe -CommandType Application | Select-Object -First 1).Source
$overrides = @{
    TEMP = $temp
    TMP = $temp
    TMPDIR = $temp
    GIT_CONFIG_NOSYSTEM = '1'
    GIT_CONFIG_GLOBAL = (Join-Path $Root 'gitconfig')
    GIT_TERMINAL_PROMPT = '0'
    GIT_DIR = $null
    GIT_WORK_TREE = $null
    GIT_INDEX_FILE = $null
    GIT_OBJECT_DIRECTORY = $null
    GIT_ALTERNATE_OBJECT_DIRECTORIES = $null
}
$previous = @{}
$null = New-Item -Path $Root -ItemType Directory
try {
    $null = New-Item -Path $checkout, $temp -ItemType Directory
    foreach ($name in $overrides.Keys) {
        $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        [Environment]::SetEnvironmentVariable($name, $overrides[$name], 'Process')
    }
    & $git -C $checkout init --quiet
    if ($LASTEXITCODE -ne 0) { throw 'Git could not initialize the checkout.' }
    & $git -C $checkout fetch --quiet --depth 1 --no-tags $Repository $Revision
    if ($LASTEXITCODE -ne 0) { throw 'Git could not fetch the tested AI-AM revision.' }
    & $git -C $checkout checkout --quiet --detach FETCH_HEAD
    if ($LASTEXITCODE -ne 0) { throw 'Git could not check out the tested AI-AM revision.' }
    $head = & $git -C $checkout rev-parse HEAD
    if ($LASTEXITCODE -ne 0 -or $head -ne $Revision) { throw 'The checkout did not match the tested revision.' }

    & (Join-Path $checkout 'prepare_minecraft.ps1') -DataDir $data
    if (!(Test-Path -LiteralPath $manifest -PathType Leaf)) { throw 'The runtime setup did not produce its manifest.' }
    Write-Host "Setup complete. Manifest: $manifest"
    Write-Host "Server setup guide: $(Join-Path $checkout 'docs\minecraft.md')"
} catch {
    Write-Warning "Setup stopped. Review any partial files under $Root before retrying with a new folder."
    throw
} finally {
    foreach ($name in $previous.Keys) {
        [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process')
    }
}
