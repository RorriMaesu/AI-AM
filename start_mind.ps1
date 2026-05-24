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

# 2. Auto-recover dashboard port if stale runtime is already bound.
try {
    $DashboardListener = Get-NetTCPConnection -LocalPort 8002 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($DashboardListener) {
        $OwningProc = Get-CimInstance Win32_Process -Filter "ProcessId = $($DashboardListener.OwningProcess)"
        $CmdLine = if ($OwningProc) { $OwningProc.CommandLine } else { "" }
        $ProcName = if ($OwningProc) { $OwningProc.Name } else { "unknown" }
        $OwnerPid = $DashboardListener.OwningProcess

        Write-Host "[Start] Port 8002 currently owned by PID $OwnerPid ($ProcName). Attempting smart recovery..." -ForegroundColor Yellow

        # Attempt graceful shutdown first. If this is an existing Antahkarana runtime,
        # it should release the port without force-kill.
        try {
            $StopResponse = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8002/api/stop" -ContentType "application/json" -TimeoutSec 3
            Write-Host "[Start] Graceful stop request sent to existing runtime." -ForegroundColor Yellow
        } catch {
            Write-Host "[Start] No graceful stop endpoint response on port 8002. Evaluating process ownership..." -ForegroundColor DarkYellow
        }

        $Released = $false
        for ($i = 0; $i -lt 10; $i++) {
            Start-Sleep -Milliseconds 300
            $ListenerCheck = Get-NetTCPConnection -LocalPort 8002 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
            if (-not $ListenerCheck) {
                $Released = $true
                break
            }
        }

        if (-not $Released) {
            $LooksLikeAntahkarana = $false
            if ($CmdLine) {
                $LooksLikeAntahkarana = (
                    $CmdLine -like "*core\\orchestrator.py*" -or
                    $CmdLine -like "*core/orchestrator.py*" -or
                    $CmdLine -like "*AIYogicMind*" -or
                    $CmdLine -like "*Antahkarana*"
                )
            }

            # If command line metadata is unavailable, still allow auto-recovery for stale python workers.
            if (-not $LooksLikeAntahkarana -and $ProcName -ieq "python.exe") {
                $LooksLikeAntahkarana = $true
            }

            if ($LooksLikeAntahkarana) {
                Write-Host "[Start] Force-closing stale runtime on port 8002 (PID: $OwnerPid)..." -ForegroundColor Yellow
                Stop-Process -Id $OwnerPid -Force -ErrorAction SilentlyContinue
                Start-Sleep -Milliseconds 700

                $ListenerCheck = Get-NetTCPConnection -LocalPort 8002 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
                if ($ListenerCheck) {
                    Write-Host "[Error] Port 8002 is still busy after force-close attempt." -ForegroundColor Red
                    Write-Host "[Error] Stop PID $($ListenerCheck.OwningProcess) manually, then retry ./start_mind.ps1" -ForegroundColor Red
                    return
                }

                $Released = $true
            }
        }

        if (-not $Released) {
            Write-Host "[Error] Port 8002 is in use by PID $OwnerPid ($ProcName)." -ForegroundColor Red
            if ($CmdLine) {
                Write-Host "[Error] Owner command: $CmdLine" -ForegroundColor Red
            }
            Write-Host "[Error] Auto-close skipped because owner does not look like Antahkarana runtime." -ForegroundColor Red
            Write-Host "[Error] Free port 8002 or run: Stop-Process -Id $OwnerPid -Force" -ForegroundColor Red
            return
        }

        Write-Host "[Start] Port 8002 released. Continuing startup..." -ForegroundColor Green
    }
} catch {
    Write-Host "[Warning] Could not verify port 8002 availability: $_" -ForegroundColor Yellow
}

Write-Host "[Start] Launching Antahkarana Orchestrator loop..." -ForegroundColor Cyan
python core\orchestrator.py
