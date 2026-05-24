# start_mind.ps1
# Script to verify Ollama status and launch the Project Antahkarana python orchestrator cleanly.

# 1. Verify if Ollama is already running on port 11434
$PortBound = Get-NetTCPConnection -LocalPort 11434 -ErrorAction SilentlyContinue
if ($PortBound) {
    Write-Host "[Start] Ollama service detected running on port 11434. Backend connection ready." -ForegroundColor Green
} else {
    Write-Host "[Warning] Ollama does not seem to be running on port 11434!" -ForegroundColor Red
    Write-Host "[Warning] Please start Ollama before interacting with the cognitive engine." -ForegroundColor Yellow
}

# 2. Run the python orchestrator
Write-Host "[Start] Launching Antahkarana Orchestrator loop..." -ForegroundColor Cyan
python core\orchestrator.py
