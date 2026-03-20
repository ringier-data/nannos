<#
PowerShell script to run A2A Inspector frontend and backend simultaneously.
Both processes will be monitored and killed when the script exits.
Mimics the behavior of run.sh with colored output and prefixes.
#>

$ErrorActionPreference = "Stop"
$OriginalLocation = Get-Location

# Process tracking
$script:FrontendJob = $null
$script:BackendJob = $null

function Write-Prefix {
    param(
        [string]$Prefix,
        [string]$Message,
        [string]$Color
    )
    Write-Host "[$Prefix] " -ForegroundColor $Color -NoNewline
    Write-Host $Message
}

function Cleanup {
    Write-Host "`nShutting down A2A Inspector..." -ForegroundColor Yellow

    # Stop jobs
    if ($script:BackendJob) {
        Stop-Job -Job $script:BackendJob -ErrorAction SilentlyContinue
        Remove-Job -Job $script:BackendJob -Force -ErrorAction SilentlyContinue
    }

    if ($script:FrontendJob) {
        Stop-Job -Job $script:FrontendJob -ErrorAction SilentlyContinue
        Remove-Job -Job $script:FrontendJob -Force -ErrorAction SilentlyContinue
    }

    Set-Location $OriginalLocation
    Write-Host "A2A Inspector stopped." -ForegroundColor Green
}

# Register cleanup
try {
    $null = Register-EngineEvent -SourceIdentifier PowerShell.Exiting -Action { Cleanup } -ErrorAction SilentlyContinue
}
catch {}

# Load .env file into process environment
# Build path using Join-Path stepwise to avoid incorrect positional args
$repoRoot = Join-Path $PSScriptRoot ".."
$envFile = Join-Path $repoRoot ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^([^#][^=]+)=(.*)$') {
            $name = $matches[1].Trim()
            $value = $matches[2].Trim()
            # Remove surrounding quotes (both single and double)
            $value = $value.Trim('"', "'")
            [Environment]::SetEnvironmentVariable($name, $value, 'Process')
        }
    }
}
else {
    Write-Warning ".env file not found"
}

# Set AWS profile for SSO
if (-not $env:AWS_PROFILE) {
    $env:AWS_PROFILE = "nannos-dev-developer"
    Write-Host "Using AWS profile: $env:AWS_PROFILE" -ForegroundColor Cyan
}

# Check directories
$frontendDir = Join-Path $repoRoot "frontend"
$backendDir = Join-Path $repoRoot "backend"

if (-not (Test-Path $frontendDir)) {
    Write-Host "Error: ./frontend directory not found!" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path $backendDir)) {
    Write-Host "Error: ./backend directory not found!" -ForegroundColor Red
    exit 1
}

Write-Host "Starting A2A Inspector..." -ForegroundColor Green

# Start frontend build in watch mode as a job
Write-Host "Starting frontend build (watch mode)..." -ForegroundColor Cyan
$script:FrontendJob = Start-Job -ScriptBlock {
    param($dir)
    Set-Location $dir
    npx esbuild src/script.ts --bundle --outfile=public/script.js --platform=browser --watch=forever 2>&1
} -ArgumentList $frontendDir

# Give frontend a moment to start
Start-Sleep -Seconds 2

# Start backend server as a job
Write-Host "Starting backend server..." -ForegroundColor Cyan
$script:BackendJob = Start-Job -ScriptBlock {
    param($repoRoot, $backendDir)

    # Helper to find executables
    function Find-Executable($name) {
        try {
            $cmd = Get-Command $name -ErrorAction SilentlyContinue
            if ($cmd) { return $cmd.Source }
        }
        catch {}

        # Fallback: search PATH entries but skip empty entries
        foreach ($p in $env:PATH.Split([System.IO.Path]::PathSeparator) | Where-Object { $_ -and $_.Trim() -ne '' }) {
            $candidate = Join-Path $p $name
            if (Test-Path $candidate) { return $candidate }
            if (Test-Path ($candidate + ".exe")) { return $candidate + ".exe" }
        }
        return $null
    }

    # Diagnostics
    Write-Host "[BACKEND] Repo root: $repoRoot" -ForegroundColor Magenta
    Write-Host "[BACKEND] Backend dir: $backendDir" -ForegroundColor Magenta

    # Activate .venv if present (PowerShell Activate script)
    $venvPath = (Join-Path $repoRoot ".venv")
    if ($venvPath -and (Test-Path $venvPath)) {
        $venvPath = (Get-Item $venvPath).FullName
        Write-Host "[BACKEND] Found venv at: $venvPath" -ForegroundColor Magenta
    }
    else {
        Write-Host "[BACKEND] No .venv found at: $venvPath" -ForegroundColor Yellow
        $venvPath = $null
    }

    if ($venvPath) {
        $activateScript = Join-Path $venvPath "Scripts\Activate.ps1"
        Write-Host "[BACKEND] Looking for activate script at: $activateScript" -ForegroundColor Magenta
        if (Test-Path $activateScript) {
            try {
                . $activateScript
                Write-Host "Activated virtual environment at: $venvPath" -ForegroundColor Cyan
            }
            catch {
                Write-Host "Warning: failed to activate virtual environment: $_" -ForegroundColor Yellow
            }
        }
        else {
            Write-Host "[BACKEND] Activate script not found: $activateScript" -ForegroundColor Yellow
        }
    }

    Set-Location $backendDir

    # Prefer 'uv' CLI, fallback to 'uvicorn' if available
    $uvPath = Find-Executable 'uv'
    if ($uvPath) {
        Write-Host "[BACKEND] Using uv at: $uvPath" -ForegroundColor Magenta
        try { & $uvPath run app.py 2>&1 }
        catch { Write-Host "[BACKEND] Error running uv: $_" -ForegroundColor Red; exit 1 }
    }
    else {
        $uvicornPath = Find-Executable 'uvicorn'
        if ($uvicornPath) {
            Write-Host "[BACKEND] Using uvicorn at: $uvicornPath" -ForegroundColor Magenta
            try { & $uvicornPath app:asgi_app --host 127.0.0.1 --port 5001 --reload --log-config log_conf.yml --access-log False 2>&1 }
            catch { Write-Host "[BACKEND] Error running uvicorn: $_" -ForegroundColor Red; exit 1 }
        }
        else {
            # Last resort: try 'python -m uvicorn' if python is available
            $python = Find-Executable 'python'
            if (-not $python) { $python = Find-Executable 'python.exe' }
            if ($python) {
                Write-Host "[BACKEND] Using python at: $python to run uvicorn" -ForegroundColor Magenta
                try { & $python -m uvicorn app:asgi_app --host 127.0.0.1 --port 5001 --reload --log-config log_conf.yml --access-log False 2>&1 }
                catch { Write-Host "[BACKEND] Error running 'python -m uvicorn': $_" -ForegroundColor Red; exit 1 }
            }
            else {
                Write-Host "Error: cannot find 'uv', 'uvicorn', or 'python' to start backend." -ForegroundColor Red
                exit 1
            }
        }
    }

} -ArgumentList $repoRoot, $backendDir

Write-Host "`nA2A Inspector is running!" -ForegroundColor Green
Write-Host "Frontend Job ID: $($script:FrontendJob.Id)" -ForegroundColor Yellow
Write-Host "Backend Job ID: $($script:BackendJob.Id)" -ForegroundColor Yellow
Write-Host "Press Ctrl+C to stop both services`n" -ForegroundColor Yellow

# Monitor both jobs and display output
try {
    while ($true) {
        # Check frontend
        if ($script:FrontendJob.State -ne 'Running') {
            Write-Host "Frontend process died unexpectedly!" -ForegroundColor Red
            $output = Receive-Job -Job $script:FrontendJob 2>&1
            $output | ForEach-Object { Write-Prefix "FRONTEND" $_ "Cyan" }
            break
        }

        # Check backend
        if ($script:BackendJob.State -ne 'Running') {
            Write-Host "Backend process died unexpectedly!" -ForegroundColor Red
            $output = Receive-Job -Job $script:BackendJob 2>&1
            $output | ForEach-Object { Write-Prefix "BACKEND" $_ "Magenta" }
            break
        }

        # Get and display frontend output
        $frontendOutput = Receive-Job -Job $script:FrontendJob 2>&1
        if ($frontendOutput) {
            $frontendOutput | ForEach-Object { Write-Prefix "FRONTEND" $_ "Cyan" }
        }

        # Get and display backend output
        $backendOutput = Receive-Job -Job $script:BackendJob 2>&1
        if ($backendOutput) {
            $backendOutput | ForEach-Object { Write-Prefix "BACKEND" $_ "Magenta" }
        }

        Start-Sleep -Milliseconds 500
    }
}
catch {
    Write-Host "Error: $_" -ForegroundColor Red
}
finally {
    Cleanup
}
