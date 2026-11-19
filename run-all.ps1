# run-all.ps1  -  starts the whole Day2 BP Chat app for a demo.
#
# Run from the project root:
#     .\run-all.ps1
# If PowerShell blocks the script:
#     powershell -ExecutionPolicy Bypass -File .\run-all.ps1
#
# It starts, in this order (each one waits for the previous one):
#     1. Qdrant            (Docker, port 6333)
#     2. Retriever         (gRPC, port 50051)   - new window
#     3. Orchestrator      (FastAPI, port 8000) - new window
#     4. Frontend          (Vite, port 5173)    - new window
# Then it opens http://localhost:5173 in your browser.
#
# To stop: close the three windows (or press Ctrl+C in each), then run:
#     docker compose down

$ErrorActionPreference = "Stop"

$Root         = $PSScriptRoot
$Backend      = Join-Path $Root "backend"
$Retriever    = Join-Path $Backend "retriever"
$Orchestrator = Join-Path $Backend "orchestrator"
$Frontend     = Join-Path $Root "frontend"


function Write-Step($text) {
    Write-Host ""
    Write-Host "==> $text" -ForegroundColor Cyan
}

function Fail($text) {
    Write-Host ""
    Write-Host "ERROR: $text" -ForegroundColor Red
    exit 1
}

# Finds the Python of the service's own .venv, or the shared backend\.venv
function Get-VenvPython($serviceDir) {
    $candidates = @(
        (Join-Path $serviceDir ".venv\Scripts\python.exe"),
        (Join-Path $Backend ".venv\Scripts\python.exe")
    )

    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) { return $candidate }
    }

    Fail "No virtual environment found for '$serviceDir'. Create one and run: pip install -r requirements.txt"
}

# Waits until something is listening on a port
function Wait-ForPort($port, $name, $timeoutSeconds) {
    $deadline = (Get-Date).AddSeconds($timeoutSeconds)

    while ((Get-Date) -lt $deadline) {
        try {
            $client = New-Object System.Net.Sockets.TcpClient
            $client.Connect("localhost", $port)
            $client.Close()
            Write-Host "    $name is ready on port $port" -ForegroundColor Green
            return
        }
        catch {
            Start-Sleep -Seconds 2
        }
    }

    Fail "$name did not start on port $port within $timeoutSeconds seconds. Check its window for errors."
}

# Waits until the API health check answers
function Wait-ForApi($url, $timeoutSeconds) {
    $deadline = (Get-Date).AddSeconds($timeoutSeconds)

    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-RestMethod -Uri $url -TimeoutSec 3
            if ($response.status -eq "healthy") {
                Write-Host "    API is healthy ($url)" -ForegroundColor Green
                return
            }
        }
        catch {
            Start-Sleep -Seconds 2
        }
    }

    Fail "The API did not answer at $url within $timeoutSeconds seconds. Check the orchestrator window."
}

# Opens a new PowerShell window that runs one command in one folder
function Start-InWindow($title, $folder, $command) {
    $inner = "`$Host.UI.RawUI.WindowTitle = '$title'; Set-Location '$folder'; $command"
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $inner | Out-Null
}


# ---------- 0. Checks ----------
Write-Step "Checking requirements"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Fail "Docker is not installed or not in PATH. Install Docker Desktop."
}

docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Fail "Docker is installed but not running. Start Docker Desktop and try again."
}

if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    Fail "Node.js / npm is not installed or not in PATH."
}

if (-not (Test-Path (Join-Path $Orchestrator ".env"))) {
    Fail "Missing backend\orchestrator\.env (copy .env.example and set GROQ_API_KEY and SECRET_KEY)."
}

if (-not (Test-Path (Join-Path $Retriever ".env"))) {
    Fail "Missing backend\retriever\.env (it needs QDRANT_URL=http://localhost:6333)."
}

$RetrieverPython    = Get-VenvPython $Retriever
$OrchestratorPython = Get-VenvPython $Orchestrator
Write-Host "    All checks passed" -ForegroundColor Green


# ---------- 1. Qdrant ----------
Write-Step "Starting Qdrant (Docker)"
Push-Location $Root
docker compose up -d
$composeExit = $LASTEXITCODE
Pop-Location
if ($composeExit -ne 0) { Fail "docker compose failed. Check docker-compose.yml in the project root." }

Wait-ForPort 6333 "Qdrant" 60


# ---------- 2. Retriever ----------
Write-Step "Starting Retriever (gRPC) - the first start is slow, it loads the embedding models"
Start-InWindow "Retriever (gRPC :50051)" $Retriever "& '$RetrieverPython' main.py"

Wait-ForPort 50051 "Retriever" 300


# ---------- 3. Orchestrator ----------
Write-Step "Starting Orchestrator (FastAPI)"
Start-InWindow "Orchestrator (FastAPI :8000)" $Orchestrator "& '$OrchestratorPython' -m uvicorn main:app --reload --port 8000"

Wait-ForApi "http://localhost:8000/api/health" 90


# ---------- 4. Frontend ----------
Write-Step "Starting Frontend (Vite)"

if (-not (Test-Path (Join-Path $Frontend "node_modules"))) {
    Write-Host "    node_modules not found, running npm install first..." -ForegroundColor Yellow
    Push-Location $Frontend
    npm install
    $npmExit = $LASTEXITCODE
    Pop-Location
    if ($npmExit -ne 0) { Fail "npm install failed." }
}

# Port 5173 is fixed because the backend CORS setting only allows that address
Start-InWindow "Frontend (Vite :5173)" $Frontend "npm run dev -- --port 5173 --strictPort"

Wait-ForPort 5173 "Frontend" 60


# ---------- Done ----------
Write-Step "Everything is running"
Write-Host "    App:          http://localhost:5173"
Write-Host "    API docs:     http://localhost:8000/docs"
Write-Host "    API health:   http://localhost:8000/api/health"
Write-Host "    Qdrant:       http://localhost:6333/dashboard"
Write-Host ""
Write-Host "    To stop: close the 3 windows, then run: docker compose down"
Write-Host ""

Start-Process "http://localhost:5173"