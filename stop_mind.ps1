# stop_mind.ps1
# Fail-safe PowerShell macro that terminates the active python loop and unloads gemma4:latest from Ollama VRAM to release GPU memory.

Write-Host "[Stop] Reclaiming system resources..." -ForegroundColor Cyan

# 1. Kill project-specific python processes
$PythonProcs = Get-CimInstance Win32_Process -Filter "name = 'python.exe'"
foreach ($Proc in $PythonProcs) {
    if ($Proc.CommandLine -like "*core\orchestrator.py*" -or $Proc.CommandLine -like "*core/orchestrator.py*") {
        Write-Host "[Stop] Killing python orchestrator process (PID: $($Proc.ProcessId))..." -ForegroundColor Yellow
        Stop-Process -Id $Proc.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

# 2. Call Ollama model unload API to free VRAM on the GPU
$OllamaPort = Get-NetTCPConnection -LocalPort 11434 -ErrorAction SilentlyContinue
if ($OllamaPort) {
    $ModelName = "gemma4:latest"
    $ConfigPath = "config/engine_config.json"
    if (Test-Path $ConfigPath) {
        try {
            $Config = Get-Content $ConfigPath -Raw | ConvertFrom-Json
            if ($Config.llm_parameters -and $Config.llm_parameters.model_name) {
                $ModelName = $Config.llm_parameters.model_name
            }
        } catch {
            # Fallback to default
        }
    }

    Write-Host "[Stop] Sending unload request to Ollama for model '$ModelName' to free VRAM..." -ForegroundColor Yellow
    try {
        $Body = @{ model = $ModelName; keep_alive = 0 } | ConvertTo-Json
        $UnloadUrl = "http://localhost:11434/api/generate"
        $Response = Invoke-RestMethod -Method Post -Uri $UnloadUrl -Body $Body -ContentType "application/json" -TimeoutSec 5
        Write-Host "[Stop] Ollama model '$ModelName' successfully unloaded from VRAM." -ForegroundColor Green
    } catch {
        Write-Host "[Warning] Failed to request Ollama to unload model: $_" -ForegroundColor Red
    }
}

Write-Host "[Stop] Resource reclamation complete. VRAM released." -ForegroundColor Green
