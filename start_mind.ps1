# AI-AM launcher. All writable runtime paths stay inside DataDir.
[CmdletBinding()]
param(
    [string]$DataDir = (Join-Path $PSScriptRoot 'data'),
    [switch]$Demo,
    [ValidateRange(1,65535)][int]$Port = 8002
)
$ErrorActionPreference = 'Stop'
if (-not [IO.Path]::IsPathRooted($DataDir)) {
    $DataDir = Join-Path $PSScriptRoot $DataDir
}
$DataDir = [IO.Path]::GetFullPath($DataDir)

# A busy port is an error, never permission to terminate its owner.
$Probe = [Net.Sockets.TcpClient]::new()
try {
    $Pending = $Probe.BeginConnect('127.0.0.1', $Port, $null, $null)
    $Busy = $Pending.AsyncWaitHandle.WaitOne(300) -and $Probe.Connected
} finally {
    $Probe.Dispose()
}
if ($Busy) {
    throw "Port $Port is already occupied. Use stop_mind.ps1 for an existing AI-AM runtime, or choose -Port."
}

$Python = (Get-Command python -ErrorAction Stop).Source
$CacheRoot = Join-Path $DataDir 'cache'
$TempRoot = Join-Path $CacheRoot 'temp'
New-Item -ItemType Directory -Path $DataDir, $CacheRoot, $TempRoot -Force | Out-Null
$Environment = @{
    'AIAM_DATA_DIR' = $DataDir
    'PYTHONDONTWRITEBYTECODE' = '1'
    'PYTHONPYCACHEPREFIX' = (Join-Path $CacheRoot 'pycache')
    'TEMP' = $TempRoot
    'TMP' = $TempRoot
    'PIP_CACHE_DIR' = (Join-Path $CacheRoot 'pip')
    'HF_HOME' = (Join-Path $CacheRoot 'huggingface')
    'TORCH_HOME' = (Join-Path $CacheRoot 'torch')
    'XDG_CACHE_HOME' = $CacheRoot
}
$Previous = @{}
$ProcessExit = 1
try {
    foreach ($Key in $Environment.Keys) {
        $Previous[$Key] = [Environment]::GetEnvironmentVariable($Key, 'Process')
        [Environment]::SetEnvironmentVariable($Key, $Environment[$Key], 'Process')
    }
    $Arguments = @('-B', (Join-Path $PSScriptRoot 'core\orchestrator.py'),
                   '--data-dir', $DataDir, '--port', "$Port")
    if ($Demo) { $Arguments += '--demo' }
    Write-Host "AI-AM dashboard: http://127.0.0.1:$Port" -ForegroundColor Cyan
    Write-Host "Memory and caches: $DataDir"
    if ($Demo) {
        Write-Host 'Demonstration mode: scripted examples; no LLM inference.' -ForegroundColor Yellow
    } else {
        Write-Host 'Ollama must already be running with an installed local model.'
    }
    & $Python @Arguments
    $ProcessExit = $LASTEXITCODE
} finally {
    foreach ($Key in $Previous.Keys) {
        [Environment]::SetEnvironmentVariable($Key, $Previous[$Key], 'Process')
    }
}
if ($ProcessExit -ne 0) {
    Write-Error "AI-AM stopped with exit code $ProcessExit. Check the error above."
}
