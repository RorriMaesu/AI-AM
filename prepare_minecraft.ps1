#Requires -Version 5.1
<#
Prepare portable prerequisites for an existing AI-AM checkout on a non-C drive.
-Plan performs read-only path/source checks. Installation needs an empty DataDir.
No server JAR, EULA change, registry edit, system PATH change, or service startup.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$DataDir,
    [string]$ProjectDir = $PSScriptRoot,
    [switch]$Plan
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Resolve-PreparationPath {
    param([string]$Value, [string]$Label, [switch]$ExistingDirectory)
    if ([string]::IsNullOrWhiteSpace($Value)) { throw "$Label is required." }
    if (-not [IO.Path]::IsPathRooted($Value)) { $Value = Join-Path $PSScriptRoot $Value }
    $full = [IO.Path]::GetFullPath($Value)
    if ($full -notmatch '^[A-Za-z]:\\' -or $full -match '^[Cc]:\\' -or $full.Substring(3) -match '[<>:"|?*\x00-\x1F]') {
        throw "$Label must be a normal local path on a drive other than C:."
    }
    $root = [IO.Path]::GetPathRoot($full)
    if (-not (Test-Path -LiteralPath $root -PathType Container)) { throw "$Label drive is unavailable: $root" }
    $cursor = $full
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            if (((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "$Label traverses a junction or symbolic link: $cursor"
            }
        }
        if ($cursor.TrimEnd('\') -eq $root.TrimEnd('\')) { break }
        $cursor = [IO.Path]::GetDirectoryName($cursor.TrimEnd('\'))
    }
    if ($ExistingDirectory -and -not (Test-Path -LiteralPath $full -PathType Container)) { throw "$Label directory is missing: $full" }
    return $full.TrimEnd('\')
}
function Save-VerifiedDownload {
    param([string]$Url, [string]$Path, [string]$Algorithm, [string]$ExpectedHash)
    if (([Uri]$Url).Scheme -ne 'https') { throw 'Downloads must use HTTPS.' }
    if (Test-Path -LiteralPath $Path) { throw "Refusing to overwrite $Path" }
    Invoke-WebRequest -Uri $Url -OutFile $Path -UseBasicParsing -TimeoutSec 600
    if ((Get-FileHash -LiteralPath $Path -Algorithm $Algorithm).Hash -ne $ExpectedHash) {
        throw "Checksum verification failed: $Path. Nothing from this archive will be executed."
    }
}
function Expand-VerifiedArchive {
    param([string]$Archive, [string]$Destination)
    if (Test-Path -LiteralPath $Destination) { throw "Refusing to overwrite $Destination" }
    $null = New-Item -ItemType Directory -Path $Destination
    $prefix = [IO.Path]::GetFullPath($Destination).TrimEnd('\') + '\'
    $zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        foreach ($entry in $zip.Entries) {
            if ($entry.FullName -match '(^[\\/]|:)' -or (($entry.ExternalAttributes -shr 16) -band 61440) -eq 40960) {
                throw "Archive contains an unsupported absolute path or symbolic link: $($entry.FullName)"
            }
            $target = [IO.Path]::GetFullPath((Join-Path $Destination $entry.FullName))
            if (-not $target.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Archive path escapes its destination.' }
            if (-not $entry.Name) {
                $null = New-Item -ItemType Directory -Path $target -Force
            } else {
                $null = New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($target)) -Force
                [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $false)
            }
        }
    } finally { $zip.Dispose() }
}
function Run-PreparationCommand {
    param([string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed with exit code $LASTEXITCODE : $Executable" }
}

$ProjectDir = Resolve-PreparationPath $ProjectDir 'Checkout' -ExistingDirectory
$DataDir = Resolve-PreparationPath $DataDir 'Preparation data'
if ($DataDir -eq [IO.Path]::GetPathRoot($DataDir).TrimEnd('\')) { throw 'Choose a new data directory, not the drive root.' }
if ($DataDir -eq $ProjectDir -or $DataDir.StartsWith($ProjectDir + '\', [StringComparison]::OrdinalIgnoreCase) -or $ProjectDir.StartsWith($DataDir + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Keep the preparation data directory separate from the existing checkout.'
}
if (Test-Path -LiteralPath $DataDir) {
    if (-not (Test-Path -LiteralPath $DataDir -PathType Container) -or @(Get-ChildItem -LiteralPath $DataDir -Force | Select-Object -First 1).Count) {
        throw 'DataDir must be new or empty. A previous or partial installation is never overwritten.'
    }
}
$MinecraftDir = Resolve-PreparationPath (Join-Path $ProjectDir 'minecraft') 'Minecraft source' -ExistingDirectory
foreach ($relative in @('pyproject.toml', 'minecraft\package.json', 'minecraft\bridge.cjs', 'core\minecraft_supervisor.py', 'start_minecraft.ps1')) {
    $path = Resolve-PreparationPath (Join-Path $ProjectDir $relative) 'Required source'
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Checkout is missing $relative. Use the fishbowl branch first." }
}
$NodeModules = Join-Path $MinecraftDir 'node_modules'
if (Test-Path -LiteralPath $NodeModules) { throw 'minecraft\node_modules already exists; this installer does not overwrite dependencies.' }

$NodeVersion = 'v22.23.4'
$PythonVersion = '3.12.10'
$NodeFilename = "node-$NodeVersion-win-x64.zip"
$NodeBaseUrl = "https://nodejs.org/dist/$NodeVersion"
$JavaMetadataUrl = 'https://api.adoptium.net/v3/assets/latest/21/hotspot?architecture=x64&image_type=jdk&os=windows&vendor=eclipse'
$PythonRegistrationUrl = "https://api.nuget.org/v3/registration5-semver1/python/$PythonVersion.json"
$PythonPackageUrl = "https://api.nuget.org/v3-flatcontainer/python/$PythonVersion/python.$PythonVersion.nupkg"
if ($Plan) {
    [pscustomobject]@{
        mode='plan'; project=$ProjectDir; data=$DataDir
        java='Adoptium Temurin 21 x64 JDK; official API SHA256'
        node="$NodeVersion x64; official SHASUMS256.txt"
        python="$PythonVersion x64 NuGet; official NuGet catalog SHA512"
        writes=@("$DataDir\.preparation-started", "$DataDir\downloads", "$DataDir\runtimes", "$DataDir\venv", "$DataDir\cache", "$DataDir\temp", $NodeModules, "$DataDir\prepared.json")
        sources=@($JavaMetadataUrl, "$NodeBaseUrl/SHASUMS256.txt", $PythonRegistrationUrl, $PythonPackageUrl, 'https://pypi.org/simple', 'https://registry.npmjs.org')
        downloads=$false; services_started=$false
    } | ConvertTo-Json -Depth 4
    return
}

$TempDir = Join-Path $DataDir 'temp'
$CacheDir = Join-Path $DataDir 'cache'
$DownloadsDir = Join-Path $DataDir 'downloads'
$RuntimeDir = Join-Path $DataDir 'runtimes'
$EnvironmentOverrides = @{
    TEMP=$TempDir; TMP=$TempDir; TMPDIR=$TempDir
    PYTHONDONTWRITEBYTECODE='1'; PYTHONNOUSERSITE='1'
    PYTHONUSERBASE=(Join-Path $DataDir 'python-user')
    PYTHONHOME=$null; PYTHONPATH=$null; PYTHONPYCACHEPREFIX=(Join-Path $CacheDir 'pycache')
    PIP_CACHE_DIR=(Join-Path $CacheDir 'pip'); PIP_CONFIG_FILE='NUL'
    PIP_TARGET=$null; PIP_PREFIX=$null; PIP_USER=$null; PIP_LOG=$null; PIP_BUILD_TRACKER=$null; PIP_FIND_LINKS=$null
    PIP_INDEX_URL='https://pypi.org/simple'; PIP_EXTRA_INDEX_URL=$null
    NPM_CONFIG_CACHE=(Join-Path $CacheDir 'npm')
    NPM_CONFIG_USERCONFIG=(Join-Path $DataDir 'npmrc')
    NPM_CONFIG_GLOBALCONFIG=(Join-Path $DataDir 'npm-globalrc')
    NPM_CONFIG_PREFIX=$null
    NPM_CONFIG_DEVDIR=(Join-Path $CacheDir 'node-gyp')
    NPM_CONFIG_LOGS_DIR=(Join-Path $CacheDir 'npm-logs'); NPM_CONFIG_GLOBAL='false'
    NODE_OPTIONS=$null; NODE_PATH=$null
    HF_HOME=(Join-Path $CacheDir 'huggingface'); TORCH_HOME=(Join-Path $CacheDir 'torch')
    XDG_CACHE_HOME=$CacheDir; NUGET_PACKAGES=(Join-Path $CacheDir 'nuget')
}
$OldEnvironment = @{}
$OldProgress = $ProgressPreference
$OldTls = [Net.ServicePointManager]::SecurityProtocol
$LocationPushed = $false
try {
    foreach ($key in $EnvironmentOverrides.Keys) {
        $OldEnvironment[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
        [Environment]::SetEnvironmentVariable($key, $EnvironmentOverrides[$key], 'Process')
    }
    $ProgressPreference = 'SilentlyContinue'
    [Net.ServicePointManager]::SecurityProtocol = $OldTls -bor [Net.SecurityProtocolType]::Tls12
    $null = New-Item -ItemType Directory -Path $DataDir -Force
    $reservation = [IO.File]::Open((Join-Path $DataDir '.preparation-started'), [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    $reservation.Dispose()
    foreach ($path in @($TempDir, $CacheDir, $DownloadsDir, $RuntimeDir)) { $null = New-Item -ItemType Directory -Path $path }
    [IO.File]::WriteAllText((Join-Path $DataDir 'npmrc'), '')
    [IO.File]::WriteAllText((Join-Path $DataDir 'npm-globalrc'), '')
    Push-Location -LiteralPath $MinecraftDir
    $LocationPushed = $true
    $null = [Reflection.Assembly]::LoadWithPartialName('System.IO.Compression.FileSystem')

    Write-Host 'Verifying and preparing Node.js 22...'
    $sumText = (Invoke-WebRequest -Uri "$NodeBaseUrl/SHASUMS256.txt" -UseBasicParsing -TimeoutSec 60).Content
    $nodeHash = $null
    foreach ($line in ($sumText -split "\r?\n")) {
        if ($line -match ('^([a-fA-F0-9]{64})\s+\*?' + [regex]::Escape($NodeFilename) + '$')) { $nodeHash = $matches[1]; break }
    }
    if (-not $nodeHash) { throw 'Official Node checksum list did not contain the expected archive.' }
    $nodeArchive = Join-Path $DownloadsDir $NodeFilename
    Save-VerifiedDownload "$NodeBaseUrl/$NodeFilename" $nodeArchive 'SHA256' $nodeHash
    $nodeRoot = Join-Path $RuntimeDir 'node'
    Expand-VerifiedArchive $nodeArchive $nodeRoot
    $NodeExe = Join-Path $nodeRoot "node-$NodeVersion-win-x64\node.exe"
    $NpmCli = Join-Path $nodeRoot "node-$NodeVersion-win-x64\node_modules\npm\bin\npm-cli.js"
    if ((& $NodeExe --version) -ne $NodeVersion -or $LASTEXITCODE -ne 0) { throw 'Unexpected Node runtime version.' }

    Write-Host 'Verifying and preparing Temurin Java 21...'
    $javaAssets = @(Invoke-RestMethod -Uri $JavaMetadataUrl -UseBasicParsing -TimeoutSec 60 | ForEach-Object { $_ })
    if ($javaAssets.Count -ne 1) { throw 'Expected one official Java 21 Windows x64 JDK asset.' }
    $javaPackage = $javaAssets[0].binary.package
    $javaUri = [Uri]$javaPackage.link
    if ($javaUri.Scheme -ne 'https' -or $javaUri.Host -ne 'github.com' -or -not $javaUri.AbsolutePath.StartsWith('/adoptium/temurin21-binaries/releases/download/')) { throw 'Unexpected Java download origin.' }
    if ($javaPackage.checksum -notmatch '^[a-fA-F0-9]{64}$' -or $javaPackage.name -notmatch '\.zip$') { throw 'Invalid Java archive metadata.' }
    $javaArchive = Join-Path $DownloadsDir 'temurin-21.zip'
    Save-VerifiedDownload $javaPackage.link $javaArchive 'SHA256' $javaPackage.checksum
    $javaRoot = Join-Path $RuntimeDir 'java'
    Expand-VerifiedArchive $javaArchive $javaRoot
    $javaHomes = @(Get-ChildItem -LiteralPath $javaRoot -Directory | Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'bin\java.exe') -PathType Leaf })
    if ($javaHomes.Count -ne 1) { throw 'Expected one extracted Java installation.' }
    $JavaExe = Join-Path $javaHomes[0].FullName 'bin\java.exe'
    $javaVersion = @(& $JavaExe "-Djava.io.tmpdir=$TempDir" --version)
    if ($LASTEXITCODE -ne 0 -or $javaVersion[0] -notmatch '^(openjdk|java) 21[.\s]') { throw 'Unexpected Java runtime version.' }

    Write-Host 'Verifying and preparing Python 3.12.10...'
    $registration = Invoke-RestMethod -Uri $PythonRegistrationUrl -UseBasicParsing -TimeoutSec 60
    $catalogUri = [Uri]$registration.catalogEntry
    if ($catalogUri.Scheme -ne 'https' -or $catalogUri.Host -ne 'api.nuget.org' -or -not $catalogUri.AbsolutePath.StartsWith('/v3/catalog0/')) { throw 'Unexpected Python checksum catalog origin.' }
    $catalog = Invoke-RestMethod -Uri $catalogUri.AbsoluteUri -UseBasicParsing -TimeoutSec 60
    if ($catalog.id -ne 'python' -or $catalog.version -ne $PythonVersion -or $catalog.packageHashAlgorithm -ne 'SHA512') { throw 'Unexpected Python package identity or checksum algorithm.' }
    $pythonHashBytes = [Convert]::FromBase64String($catalog.packageHash)
    if ($pythonHashBytes.Length -ne 64) { throw 'Invalid Python package SHA512.' }
    $pythonHash = [BitConverter]::ToString($pythonHashBytes).Replace('-', '')
    $pythonArchive = Join-Path $DownloadsDir "python.$PythonVersion.nupkg"
    Save-VerifiedDownload $PythonPackageUrl $pythonArchive 'SHA512' $pythonHash
    $pythonRoot = Join-Path $RuntimeDir 'python'
    Expand-VerifiedArchive $pythonArchive $pythonRoot
    $BasePython = Join-Path $pythonRoot 'tools\python.exe'
    Run-PreparationCommand $BasePython @('-B', '-c', "import sys; assert sys.version_info[:3] == (3,12,10), sys.version")
    $VenvDir = Join-Path $DataDir 'venv'
    Run-PreparationCommand $BasePython @('-B', '-m', 'venv', $VenvDir)
    $PythonExe = Join-Path $VenvDir 'Scripts\python.exe'
    # Install the checkout's runtime dependencies without creating build metadata in it.
    $dependencyCode = "import sys,json,tomllib; print(json.dumps(tomllib.load(open(sys.argv[1],'rb'))['project']['dependencies']))"
    $dependencyJson = & $PythonExe -B -c $dependencyCode (Join-Path $ProjectDir 'pyproject.toml')
    if ($LASTEXITCODE -ne 0) { throw 'Could not read Python dependency requirements.' }
    [string[]]$pythonDependencies = ConvertFrom-Json -InputObject $dependencyJson
    if (-not $pythonDependencies.Count) { throw 'The checkout has no Python dependency requirements.' }
    Run-PreparationCommand $PythonExe (@('-B', '-m', 'pip', 'install', '--disable-pip-version-check', '--only-binary=:all:') + $pythonDependencies)

    Write-Host 'Installing Minecraft bridge dependencies with install scripts disabled...'
    Run-PreparationCommand $NodeExe @($NpmCli, 'install', '--ignore-scripts', '--global=false', '--package-lock=false', '--no-audit', '--no-fund', '--registry=https://registry.npmjs.org')
    $manifest = [ordered]@{
        schema=1; project_dir=$ProjectDir; preparation_dir=$DataDir
        python_exe=$PythonExe; python_base=$BasePython; node_exe=$NodeExe; java_exe=$JavaExe
        mind_data_dir=(Join-Path $DataDir 'mind-data'); temp_dir=$TempDir
        node_version=$NodeVersion; python_version=$PythonVersion
        node_sha256=$nodeHash; java_sha256=$javaPackage.checksum; python_sha512=$pythonHash
        java_source=$javaPackage.link; python_source=$PythonPackageUrl
    }
    $manifestPath = Join-Path $DataDir 'prepared.json'
    [IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 4), (New-Object Text.UTF8Encoding($false)))
    Write-Host "Prepared prerequisites. Runtime paths: $manifestPath"
    Write-Host 'Next: prepare and personally accept the private Minecraft server, then use start_minecraft.ps1.'
    Write-Host 'No server, model, bridge, or autonomous goal has been started.'
} catch {
    Write-Warning "Preparation stopped. Partial files remain in $DataDir and possibly $NodeModules; nothing was deleted. Use a fresh empty directory after reviewing them."
    throw
} finally {
    if ($LocationPushed) { Pop-Location }
    foreach ($key in $OldEnvironment.Keys) { [Environment]::SetEnvironmentVariable($key, $OldEnvironment[$key], 'Process') }
    $ProgressPreference = $OldProgress
    [Net.ServicePointManager]::SecurityProtocol = $OldTls
}
