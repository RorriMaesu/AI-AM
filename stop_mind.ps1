# Gracefully stop only a service that identifies itself as AI-AM.
[CmdletBinding()]
param([ValidateRange(1,65535)][int]$Port = 8002)
$ErrorActionPreference = 'Stop'
$ServiceUrl = "http://127.0.0.1:$Port"
try {
    $Health = Invoke-RestMethod -Method Get -Uri "$ServiceUrl/api/health" -TimeoutSec 3
} catch {
    throw "AI-AM is not responding on port $Port. No process was terminated."
}
if ($Health.identity -ne 'ai-am') {
    throw "The service on port $Port is not AI-AM. No stop request was sent."
}
$Result = Invoke-RestMethod -Method Post -Uri "$ServiceUrl/api/stop" -ContentType 'application/json' -Body '{}' -TimeoutSec 15
Write-Host 'AI-AM is stopping gracefully. Its memory has been retained.' -ForegroundColor Cyan
Write-Host 'The shared Ollama service and other Python processes remain available.'
