#Requires -Version 5.1
<#
Start the local Minecraft fishbowl against an already-running private Java server.
Nothing is installed or downloaded. All project, application, runtime and data paths
must be on a local drive other than C:. Stop this launcher with Ctrl+C.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$DataDir,
    [Parameter(Mandatory=$true)][string]$ServerDir,
    [string]$PythonExe = '',
    [string]$NodeExe = '',
    [string]$MindConfig = '',
    [ValidateRange(1024,65535)][int]$Port = 8765,
    [ValidateRange(1024,65535)][int]$BridgePort = 8766,
    [ValidateRange(1024,65535)][int]$MinecraftPort = 25565,
    [ValidateRange(1024,65534)][int]$ViewerPort = 8767,
    [ValidateRange(1,2)][int]$Agents = 2,
    [ValidateRange(10,180)][int]$StartupTimeout = 60,
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Resolve-NonCPath {
    param([string]$Value, [string]$Label, [switch]$MustExist, [switch]$Directory)
    if ([string]::IsNullOrWhiteSpace($Value)) { throw "$Label is required." }
    if (-not [IO.Path]::IsPathRooted($Value)) { $Value = Join-Path $PSScriptRoot $Value }
    $full = [IO.Path]::GetFullPath($Value)
    if ($full -notmatch '^[A-Za-z]:\\' -or $full -match '^[Cc]:\\') {
        throw "$Label must be on a local drive other than C: ($full)."
    }
    if ($full.Substring(3) -match '[<>:"|?*\x00-\x1F]') { throw "$Label contains invalid path characters." }
    $drive = [IO.Path]::GetPathRoot($full)
    if (-not (Test-Path -LiteralPath $drive -PathType Container)) { throw "$Label drive is unavailable: $drive" }
    $cursor = $full
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "$Label traverses a junction or symbolic link: $cursor"
            }
        }
        if ($cursor.TrimEnd('\') -eq $drive.TrimEnd('\')) { break }
        $cursor = [IO.Path]::GetDirectoryName($cursor.TrimEnd('\'))
    }
    if ($MustExist) {
        $kind = if ($Directory) { 'Container' } else { 'Leaf' }
        if (-not (Test-Path -LiteralPath $full -PathType $kind)) { throw "$Label does not exist: $full" }
    }
    return $full
}
function Test-NoDataLinks {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { return }
    $pending = New-Object 'System.Collections.Generic.Stack[string]'
    $pending.Push($Path)
    while ($pending.Count) {
        foreach ($item in Get-ChildItem -LiteralPath $pending.Pop() -Force) {
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Existing application data contains a junction or symbolic link: $($item.FullName)"
            }
            if ($item.PSIsContainer) { $pending.Push($item.FullName) }
        }
    }
}
function Resolve-Runtime {
    param([string]$Value, [string]$Name)
    if (-not $Value) { $Value = (Get-Command $Name -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source }
    $resolved = Resolve-NonCPath $Value "$Name runtime" -MustExist
    if ([IO.Path]::GetExtension($resolved) -ne '.exe') { throw "$Name must name an actual .exe runtime." }
    return $resolved
}
function Read-ServerProperties {
    param([string]$Path)
    $properties = @{}
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^\s*([^#!][^=:]*?)\s*[=:]\s*(.*?)\s*$') { $properties[$matches[1].Trim()] = $matches[2].Trim() }
    }
    return $properties
}
function Test-LocalPort {
    param([int]$Number)
    $client = New-Object Net.Sockets.TcpClient
    try {
        $pending = $client.BeginConnect('127.0.0.1', $Number, $null, $null)
        if (-not $pending.AsyncWaitHandle.WaitOne(400)) { return $false }
        $client.EndConnect($pending)
        return $true
    } catch { return $false } finally { $client.Dispose() }
}
function Quote-NativeArgument {
    param([string]$Value)
    return '"' + ($Value -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
}
function Start-OwnedService {
    param([string]$Name, [string]$Exe, [string[]]$Arguments)
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $outLog = Join-Path $LogRoot "$Name-$stamp.out.log"
    $errLog = Join-Path $LogRoot "$Name-$stamp.err.log"
    $argumentLine = ($Arguments | ForEach-Object { Quote-NativeArgument $_ }) -join ' '
    $child = Start-Process -FilePath $Exe -ArgumentList $argumentLine -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $outLog -RedirectStandardError $errLog
    $Owned.Add($child)
    Write-Host "$Name started (PID $($child.Id)); logs: $LogRoot"
    return $child
}
function Wait-Service {
    param([Diagnostics.Process]$Process, [string]$Url, [hashtable]$Headers = @{})
    $deadline = [DateTime]::UtcNow.AddSeconds($StartupTimeout)
    do {
        if ($Process.HasExited) { throw "Service exited with code $($Process.ExitCode). Inspect $LogRoot." }
        try {
            $reply = Invoke-RestMethod -Uri $Url -Headers $Headers -Method Get -TimeoutSec 2 -UseBasicParsing
            if ($null -ne $reply) { return }
        } catch { }
        Start-Sleep -Milliseconds 400
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Service did not become ready at $Url. Inspect $LogRoot."
}

$ProjectRoot = Resolve-NonCPath $PSScriptRoot 'Project directory' -MustExist -Directory
$DataDir = Resolve-NonCPath $DataDir 'Application data'
Test-NoDataLinks $DataDir
$ServerDir = Resolve-NonCPath $ServerDir 'Existing Java server directory' -MustExist -Directory
$BridgeScript = Resolve-NonCPath (Join-Path $ProjectRoot 'minecraft\bridge.cjs') 'Bridge script' -MustExist
$null = Resolve-NonCPath (Join-Path $ProjectRoot 'core\minecraft_supervisor.py') 'Supervisor module' -MustExist
$NodePackage = Resolve-NonCPath (Join-Path $ProjectRoot 'minecraft') 'Node package' -MustExist -Directory
$NodeModules = Resolve-NonCPath (Join-Path $NodePackage 'node_modules') 'Installed Node packages' -MustExist -Directory
if (-not $PythonExe) {
    $venvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) { $PythonExe = $venvPython }
}
$PythonExe = Resolve-Runtime $PythonExe 'python'
$NodeExe = Resolve-Runtime $NodeExe 'node'
if ($MindConfig) { $MindConfig = Resolve-NonCPath $MindConfig 'Mind configuration' -MustExist }

$PropertiesFile = Resolve-NonCPath (Join-Path $ServerDir 'server.properties') 'Server properties' -MustExist
$EulaFile = Resolve-NonCPath (Join-Path $ServerDir 'eula.txt') 'Existing EULA decision' -MustExist
$settings = Read-ServerProperties $PropertiesFile
$eula = Read-ServerProperties $EulaFile
if ($eula['eula'] -ne 'true') { throw 'The server owner must personally review and accept the Minecraft EULA before starting the server.' }
if ($settings['server-ip'] -ne '127.0.0.1' -or $settings['server-port'] -ne [string]$MinecraftPort -or $settings['online-mode'] -ne 'false') {
    throw 'Existing server.properties must explicitly set server-ip=127.0.0.1, the requested server-port, and online-mode=false.'
}
if ($settings['gamemode'] -ne 'survival' -or $settings['force-gamemode'] -ne 'true') {
    throw 'Existing server.properties must set gamemode=survival and force-gamemode=true.'
}
$worldName = $settings['level-name']
if (-not $worldName) { $worldName = 'world' }
if ($worldName -notmatch '^[A-Za-z0-9_-]{1,64}$') { throw 'Use a simple level-name without directory separators, spaces, or traversal.' }
foreach ($name in @($worldName, 'logs', 'crash-reports', 'temp')) {
    $null = Resolve-NonCPath (Join-Path $ServerDir $name) "Server $name path"
}
$RuntimeRoot = Resolve-NonCPath (Join-Path $DataDir 'runtime') 'Runtime state'
$LogRoot = Resolve-NonCPath (Join-Path $DataDir 'logs') 'Application logs'
$CacheRoot = Resolve-NonCPath (Join-Path $DataDir 'cache') 'Caches'
$TempRoot = Resolve-NonCPath (Join-Path $DataDir 'temp') 'Temporary files'
$ConfigPath = Resolve-NonCPath (Join-Path $RuntimeRoot 'minecraft.json') 'Generated bridge configuration'
$ports = @($Port, $BridgePort, $MinecraftPort, $ViewerPort)
if ($Agents -eq 2) { $ports += ($ViewerPort + 1) }
if (@($ports | Select-Object -Unique).Count -ne $ports.Count) { throw 'Dashboard, bridge, server, and viewer ports must be different.' }
foreach ($number in $ports) {
    if ($number -ne $MinecraftPort -and (Test-LocalPort $number)) { throw "Local port $number is already in use." }
}
if (-not (Test-LocalPort $MinecraftPort)) { throw "Start the private Java server on 127.0.0.1:$MinecraftPort first." }

$EnvironmentOverrides = @{
    AIAM_DATA_DIR = $DataDir
    PYTHONDONTWRITEBYTECODE = '1'
    PYTHONNOUSERSITE = '1'
    PYTHONHOME = $null
    PYTHONPATH = $null
    PYTHONPYCACHEPREFIX = (Join-Path $CacheRoot 'pycache')
    TEMP = $TempRoot
    TMP = $TempRoot
    TMPDIR = $TempRoot
    PIP_CACHE_DIR = (Join-Path $CacheRoot 'pip')
    HF_HOME = (Join-Path $CacheRoot 'huggingface')
    TORCH_HOME = (Join-Path $CacheRoot 'torch')
    XDG_CACHE_HOME = $CacheRoot
    NPM_CONFIG_CACHE = (Join-Path $CacheRoot 'npm')
    NPM_CONFIG_USERCONFIG = (Join-Path $RuntimeRoot 'npmrc')
    NODE_REPL_HISTORY = (Join-Path $RuntimeRoot 'node_history')
    NODE_PATH = $NodeModules
    NODE_OPTIONS = $null
    AIAM_MINECRAFT_TOKEN = $null
}
$OldEnvironment = @{}
$Owned = New-Object 'System.Collections.Generic.List[System.Diagnostics.Process]'
$LocationPushed = $false
try {
    Push-Location -LiteralPath $ProjectRoot
    $LocationPushed = $true
    foreach ($key in $EnvironmentOverrides.Keys) {
        $value = $EnvironmentOverrides[$key]
        if ($value -and $value -match '^[A-Za-z]:') { $null = Resolve-NonCPath $value "$key path" }
        $OldEnvironment[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
        [Environment]::SetEnvironmentVariable($key, $value, 'Process')
    }
    $pythonCheck = "import sys,json,importlib; names=('fastapi','uvicorn','httpx','pydantic','anyio'); print(json.dumps({'version':list(sys.version_info[:2]),'prefix':sys.prefix,'base_prefix':sys.base_prefix,'paths':sys.path,'modules':[importlib.import_module(n).__file__ for n in names]}))"
    $pythonOutput = & $PythonExe -B -c $pythonCheck
    if ($LASTEXITCODE -ne 0) { throw 'Python dependencies are missing. Install the project manually on the non-C drive.' }
    $pythonInfo = ($pythonOutput -join [Environment]::NewLine) | ConvertFrom-Json
    if ($pythonInfo.version[0] -lt 3 -or ($pythonInfo.version[0] -eq 3 -and $pythonInfo.version[1] -lt 10)) { throw 'Python 3.10 or newer is required.' }
    foreach ($path in @($pythonInfo.prefix, $pythonInfo.base_prefix) + @($pythonInfo.modules) + @($pythonInfo.paths)) {
        if ($path) { $null = Resolve-NonCPath $path 'Python runtime/dependency path' }
    }
    $nodeCheck = "const p=process.argv[1];const names=['mineflayer','mineflayer-pathfinder','prismarine-viewer','express','socket.io','vec3'];process.stdout.write(JSON.stringify({major:Number(process.versions.node.split('.')[0]),paths:names.map(n=>require.resolve(n,{paths:[p]}))}));"
    $nodeOutput = & $NodeExe -e $nodeCheck $NodePackage
    if ($LASTEXITCODE -ne 0) { throw 'Node dependencies are missing. Install minecraft/package.json manually on the non-C drive.' }
    $nodeInfo = ($nodeOutput -join [Environment]::NewLine) | ConvertFrom-Json
    if ($nodeInfo.major -lt 22) { throw 'Node.js 22 or newer is required.' }
    foreach ($path in $nodeInfo.paths) { $null = Resolve-NonCPath $path 'Node dependency' -MustExist }
    if ($CheckOnly) {
        Write-Host 'Preflight passed. No services started or launcher files written.'
        return
    }
    foreach ($folder in @($DataDir, $RuntimeRoot, $LogRoot, $CacheRoot, $TempRoot)) {
        $null = New-Item -ItemType Directory -Path $folder -Force
        $null = Resolve-NonCPath $folder 'Created application directory' -MustExist -Directory
    }
    $characters = @(@{ id='ai-am'; username='AI_AM'; viewer_port=$ViewerPort })
    if ($Agents -eq 2) { $characters += @{ id='mira'; username='Mira'; viewer_port=($ViewerPort + 1) } }
    $configuration = @{
        server_host='127.0.0.1'; server_port=$MinecraftPort; version='1.21.4'
        bridge_port=$BridgePort; agents=$characters
    } | ConvertTo-Json -Depth 5
    [IO.File]::WriteAllText($ConfigPath, $configuration, (New-Object Text.UTF8Encoding($false)))
    $bytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    $SessionToken = [Convert]::ToBase64String($bytes)
    [Environment]::SetEnvironmentVariable('AIAM_MINECRAFT_TOKEN', $SessionToken, 'Process')
    $bridge = Start-OwnedService 'bridge' $NodeExe @($BridgeScript, '--config', $ConfigPath)
    Wait-Service $bridge "http://127.0.0.1:$BridgePort/health" @{ Authorization="Bearer $SessionToken" }
    $supervisorArguments = @('-B', '-m', 'core.minecraft_supervisor', '--config', $ConfigPath, '--data-dir', $DataDir, '--port', [string]$Port)
    if ($MindConfig) { $supervisorArguments += @('--mind-config', $MindConfig) }
    $supervisor = Start-OwnedService 'supervisor' $PythonExe $supervisorArguments
    Wait-Service $supervisor "http://127.0.0.1:$Port/api/fishbowl"
    Write-Host "Fishbowl: http://127.0.0.1:$Port"
    Write-Host 'Choose a bounded goal in the dashboard to begin. HTTP readiness does not imply Minecraft agents have spawned.'
    Write-Host 'Ctrl+C stops only this bridge and supervisor. Use stop in the Java server console to save and stop the world.'
    while ($true) {
        foreach ($child in $Owned) {
            if ($child.HasExited) { throw "Owned service PID $($child.Id) exited. Inspect $LogRoot." }
        }
        Start-Sleep -Seconds 1
    }
} finally {
    for ($index = $Owned.Count - 1; $index -ge 0; $index--) {
        $child = $Owned[$index]
        try {
            if (-not $child.HasExited) { Stop-Process -InputObject $child -ErrorAction Stop }
        } catch { Write-Warning "Could not stop owned PID $($child.Id): $($_.Exception.Message)" }
        finally { $child.Dispose() }
    }
    foreach ($key in $OldEnvironment.Keys) { [Environment]::SetEnvironmentVariable($key, $OldEnvironment[$key], 'Process') }
    $SessionToken = $null
    if ($LocationPushed) { Pop-Location }
}
