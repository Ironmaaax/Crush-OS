$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
Set-Location ..\..

$bundleRoot = Join-Path (Get-Location) "bundle"
$venvPath = Join-Path $bundleRoot ".venv"
$pythonDir = Join-Path $bundleRoot "python"
$modelsDir = Join-Path $bundleRoot "models"
$piperDir = Join-Path $modelsDir "piper"
$binDir = Join-Path $bundleRoot "bin"

Write-Host "Crush - build offline bundle (Windows)" -ForegroundColor Cyan
Write-Host "This script downloads Python, deps and models once." -ForegroundColor DarkGray
Write-Host ""

function Ensure-Command {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

if (-not (Ensure-Command "uv")) {
    Write-Host "Installing uv..." -ForegroundColor Yellow
    powershell -ExecutionPolicy ByPass -NoProfile -Command "irm https://astral.sh/uv/install.ps1 | iex"
    $uvBin = Join-Path $env:USERPROFILE ".local\bin"
    if ($env:PATH -notlike "*$uvBin*") {
        $env:PATH = "$uvBin;$env:PATH"
    }
}
if (-not (Ensure-Command "uv")) {
    throw "uv not found."
}

New-Item -ItemType Directory -Path $bundleRoot, $modelsDir, $piperDir, $binDir -Force | Out-Null

# A standalone Python is embedded and the venv is created with the std `venv`
# module (--copies). uv's own venv uses a trampoline with the base interpreter
# path baked into python.exe, which is NOT relocatable across machines. The std
# venv reads `home` from pyvenv.cfg, so it can be re-homed on the target machine
# (see scripts/release/rehome_bundle.ps1).
Write-Host "[1/6] Embed relocatable Python into bundle/python" -ForegroundColor Cyan
if (Test-Path $pythonDir) { Remove-Item -Recurse -Force $pythonDir }
$pyInstallCache = Join-Path $bundleRoot ".python-install"
if (Test-Path $pyInstallCache) { Remove-Item -Recurse -Force $pyInstallCache }
$env:UV_PYTHON_INSTALL_DIR = $pyInstallCache
uv python install 3.11
if ($LASTEXITCODE -ne 0) { throw "uv python install failed." }
$managedPython = Get-ChildItem $pyInstallCache -Recurse -Filter "python.exe" | Select-Object -First 1
if (-not $managedPython) { throw "managed python.exe not found after install." }
$managedRoot = Split-Path $managedPython.FullName -Parent
New-Item -ItemType Directory -Path $pythonDir -Force | Out-Null
Copy-Item (Join-Path $managedRoot "*") $pythonDir -Recurse -Force
Remove-Item -Recurse -Force $pyInstallCache
$bundleBasePython = Join-Path $pythonDir "python.exe"
if (-not (Test-Path $bundleBasePython)) { throw "bundle base python missing." }

Write-Host "[2/6] Create relocatable venv (std venv --copies)" -ForegroundColor Cyan
if (Test-Path $venvPath) { Remove-Item -Recurse -Force $venvPath }
& $bundleBasePython -m venv --copies $venvPath
if ($LASTEXITCODE -ne 0) { throw "venv creation failed." }
$bundlePython = Join-Path $venvPath "Scripts\python.exe"
if (-not (Test-Path $bundlePython)) { throw "bundle venv python missing." }

Write-Host "[3/6] Install deps + crush into venv" -ForegroundColor Cyan
# L'extra `vision` (ultralytics + opencv) est inclus : sans lui le bundle
# embarquait les poids yolov8n.pt SANS la bibliotheque capable de les lire, donc
# une detection d'objets morte a l'installation. C'est ce qui faisait tomber
# l'archive a 371 Mio contre ~700 pour les releases de reference.
# L'extra `face` reste dehors : dlib compile depuis les sources.
uv pip install --python $bundlePython -e ".[vision]"
if ($LASTEXITCODE -ne 0) { throw "crush package install failed." }
& $bundlePython -c "import crush.setup_app"
if ($LASTEXITCODE -ne 0) { throw "crush.setup_app not importable in bundle venv." }
& $bundlePython -c "import ultralytics, cv2"
if ($LASTEXITCODE -ne 0) { throw "vision extra not importable in bundle venv." }

Write-Host "[4/6] Copy uv binary" -ForegroundColor Cyan
$uvExe = (Get-Command uv).Source
Copy-Item $uvExe (Join-Path $binDir "uv.exe") -Force

Write-Host "[5/6] Download ML models" -ForegroundColor Cyan
# Poids YOLO telecharges DIRECTEMENT, comme les voix Piper juste en dessous.
# L'ancienne version instanciait `ultralytics.YOLO` pour declencher le
# telechargement en effet de bord. Or l'extra `vision` n'est PAS installe dans le
# bundle -- il tirerait torch, plusieurs Go dans une archive de 700 Mo -- donc
# l'import echouait, et comme l'appel n'etait pas suivi d'un controle de code
# retour (contrairement a toutes les autres etapes de ce script), l'echec
# ressortait en « Cannot find path yolov8n.pt », qui ne designe pas la cause.
$yoloTarget = Join-Path $modelsDir "yolov8n.pt"
if (Test-Path "yolov8n.pt") {
    # Cache local d'un poste de dev : on ne retelecharge pas.
    Copy-Item "yolov8n.pt" $yoloTarget -Force
} elseif (-not (Test-Path $yoloTarget)) {
    $yoloUrl = "https://github.com/ultralytics/assets/releases/download/v8.3.0/yolov8n.pt"
    curl.exe -fL --retry 3 -o $yoloTarget $yoloUrl
    if ($LASTEXITCODE -ne 0) { throw "yolov8n.pt download failed." }
    $yoloSize = (Get-Item $yoloTarget).Length
    if ($yoloSize -lt 3000000) {
        throw "yolov8n.pt too small ($yoloSize bytes), download likely corrupted."
    }
}
if (-not (Test-Path $yoloTarget)) { throw "yolov8n.pt missing after model step." }

$piperOnnx = Join-Path $piperDir "fr_FR-upmc-medium.onnx"
$piperJson = "$piperOnnx.json"
if (-not (Test-Path $piperOnnx)) {
    # `-f` et les controles de taille ne sont pas du zele : sans `-f`, curl ecrit
    # la page d'erreur HTML DANS le .onnx et rend 0. Le script continuerait, le
    # workflow verifie l'existence du chemin et la trouverait -- on publierait
    # donc un bundle de 700 Mo dont la synthese vocale est un fichier HTML.
    $baseUrl = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/fr/fr_FR/upmc/medium"
    curl.exe -fL --retry 3 --silent -o $piperOnnx "$baseUrl/fr_FR-upmc-medium.onnx"
    if ($LASTEXITCODE -ne 0) { throw "piper onnx download failed." }
    curl.exe -fL --retry 3 --silent -o $piperJson "$baseUrl/fr_FR-upmc-medium.onnx.json"
    if ($LASTEXITCODE -ne 0) { throw "piper json download failed." }
    $onnxSize = (Get-Item $piperOnnx).Length
    if ($onnxSize -lt 10000000) {
        throw "piper onnx too small ($onnxSize bytes), download likely corrupted."
    }
    $jsonSize = (Get-Item $piperJson).Length
    if ($jsonSize -lt 1000) {
        throw "piper json too small ($jsonSize bytes), download likely corrupted."
    }
}

Write-Host "[6/6] Download livekit-server" -ForegroundColor Cyan
$lkTarget = Join-Path $binDir "livekit-server.exe"
if (-not (Test-Path $lkTarget)) {
    # Authentifie l'appel API GitHub si un token est présent (CI) : les runners
    # partagent des IP très sollicitées et la limite anonyme (60/h) y est vite
    # atteinte. En local (pas de GITHUB_TOKEN) -> appel anonyme comme avant.
    $lkHeaders = @{ "User-Agent" = "crush-bundle" }
    if ($env:GITHUB_TOKEN) { $lkHeaders["Authorization"] = "Bearer $env:GITHUB_TOKEN" }
    $release = Invoke-RestMethod -Uri "https://api.github.com/repos/livekit/livekit/releases/latest" -Headers $lkHeaders
    $asset = $release.assets | Where-Object { $_.name -match '^livekit_.*_windows_amd64\.zip$' } | Select-Object -First 1
    if (-not $asset) {
        throw "livekit windows asset not found in latest release."
    }
    $zipPath = Join-Path $env:TEMP "livekit-server.zip"
    if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
    curl.exe -fL --retry 3 -o $zipPath $asset.browser_download_url
    if ($LASTEXITCODE -ne 0) { throw "livekit download failed." }
    $zipSize = (Get-Item $zipPath).Length
    if ($zipSize -lt 1000000) { throw "livekit zip too small ($zipSize bytes), download likely corrupted." }
    $extractDir = Join-Path $env:TEMP "livekit-extract"
    if (Test-Path $extractDir) { Remove-Item $extractDir -Recurse -Force }
    New-Item -ItemType Directory -Path $extractDir -Force | Out-Null
    tar -xf $zipPath -C $extractDir
    if ($LASTEXITCODE -ne 0) { throw "livekit zip extraction failed." }
    $extracted = Get-ChildItem $extractDir -Filter "livekit-server.exe" -Recurse | Select-Object -First 1
    if (-not $extracted) { throw "livekit-server.exe not found in archive." }
    Copy-Item $extracted.FullName $lkTarget -Force
    Remove-Item $zipPath -Force -ErrorAction SilentlyContinue
    Remove-Item $extractDir -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host "Write manifest" -ForegroundColor Cyan
$manifest = @{
    version = "2"
    platform = "windows"
    python = "3.11"
    venv = ".venv"
    python_home = "python"
    relocatable = $true
    models = @{
        yolo = "models/yolov8n.pt"
        piper_onnx = "models/piper/fr_FR-upmc-medium.onnx"
        piper_json = "models/piper/fr_FR-upmc-medium.onnx.json"
    }
    bin = @{
        uv = "bin/uv.exe"
        livekit = "bin/livekit-server.exe"
    }
} | ConvertTo-Json -Depth 5
$manifestPath = Join-Path $bundleRoot "manifest.json"
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText($manifestPath, $manifest, $utf8NoBom)

Write-Host ""
Write-Host "Bundle ready: $bundleRoot" -ForegroundColor Green
Write-Host 'Next: .\crush.ps1 setup' -ForegroundColor White
