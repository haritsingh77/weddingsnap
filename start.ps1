# WeddingSnap Dev Server Launcher for Windows PowerShell

Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "       WeddingSnap Development Launcher 🚀         " -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

# Ensure we are in the script's directory
$ScriptDir = $PSScriptRoot
if (-not $ScriptDir) {
    $ScriptDir = Get-Location
}
Set-Location $ScriptDir

# Check virtual env
if (-not (Test-Path ".venv\Scripts\uvicorn.exe")) {
    Write-Host "[Backend] Error: virtual environment '.venv' not found or uvicorn.exe is missing." -ForegroundColor Red
    exit 1
}

Write-Host "Starting servers in background jobs..." -ForegroundColor Green

# Start backend job with stderr merged into stdout
$BackendJob = Start-Job -ScriptBlock {
    Set-Location $using:ScriptDir\backend
    & ..\.venv\Scripts\uvicorn.exe app.main:app --reload --host 0.0.0.0 --port 8000 2>&1
}

# Start frontend job with stderr merged into stdout
$FrontendJob = Start-Job -ScriptBlock {
    Set-Location $using:ScriptDir\frontend
    npm run dev -- --host 2>&1
}

Write-Host "Servers started. Press Ctrl+C to stop." -ForegroundColor Green

try {
    while ($true) {
        # Fetch output from jobs without throwing on NativeCommandError
        $backendOut = Receive-Job -Job $BackendJob
        if ($backendOut) {
            $backendOut | ForEach-Object { Write-Host "[Backend] $_" }
        }
        
        $frontendOut = Receive-Job -Job $FrontendJob
        if ($frontendOut) {
            $frontendOut | ForEach-Object { Write-Host "[Frontend] $_" -ForegroundColor Gray }
        }
        
        Start-Sleep -Milliseconds 200
    }
}
finally {
    Write-Host "`nStopping servers..." -ForegroundColor Yellow
    Stop-Job $BackendJob -ErrorAction SilentlyContinue
    Stop-Job $FrontendJob -ErrorAction SilentlyContinue
    Remove-Job $BackendJob -ErrorAction SilentlyContinue
    Remove-Job $FrontendJob -ErrorAction SilentlyContinue
    Write-Host "Servers stopped. Goodbye! 👋" -ForegroundColor Green
}
