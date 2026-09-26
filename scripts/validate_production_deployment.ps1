$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$environmentPath = Join-Path $projectRoot "deploy\.env.production"

if (-not (Test-Path -LiteralPath $environmentPath)) {
    throw "Missing deploy/.env.production. Run scripts/bootstrap_production_env.ps1 first."
}

$environmentValues = @{}
foreach ($line in Get-Content -LiteralPath $environmentPath) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
        $environmentValues[$matches[1]] = $matches[2].Trim().Trim('"').Trim("'")
    }
}
foreach ($requiredName in @("OPENAI_API_BASE", "OPENAI_API_KEY", "ITEM_MODEL", "VL_MODEL", "MODEL_HOST_DIR")) {
    if (-not $environmentValues[$requiredName]) {
        throw "Missing required production setting: $requiredName"
    }
}
if (-not ($environmentValues["MODEL"] -or $environmentValues["LLM_DEFAULT_MODEL"])) {
    throw "Set MODEL or LLM_DEFAULT_MODEL for production queries."
}
if ((Get-Content -LiteralPath $environmentPath -Raw) -match '__[A-Z0-9_]+__') {
    throw "deploy/.env.production still contains an unresolved placeholder."
}
if (-not (Test-Path -LiteralPath $environmentValues["MODEL_HOST_DIR"] -PathType Container)) {
    throw "MODEL_HOST_DIR does not exist on this host."
}
foreach ($modelDirectory in @("bge-m3", "bge-reranker-large")) {
    $modelPath = Join-Path $environmentValues["MODEL_HOST_DIR"] $modelDirectory
    if (-not (Test-Path -LiteralPath $modelPath -PathType Container)) {
        throw "Missing model directory below MODEL_HOST_DIR: $modelDirectory"
    }
}
if ($environmentValues["APP_CORS_ORIGINS"] -match 'example\.com') {
    Write-Warning "APP_CORS_ORIGINS still uses the example domain; replace it before exposing the API."
}

$requiredSecretFiles = @(
    "lifecycle_database_url.txt",
    "lifecycle_admin_token.txt",
    "access_context_hmac_secret.txt",
    "neo4j_password.txt",
    "minio_secret_key.txt",
    "mongo_password.txt"
)
foreach ($name in $requiredSecretFiles) {
    $path = Join-Path $projectRoot "deploy\secrets\$name"
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Missing production secret file: $name"
    }
    if ((Get-Item -LiteralPath $path).Length -lt 32) {
        throw "Production secret file is unexpectedly short: $name"
    }
}

Push-Location $projectRoot
try {
    docker compose `
        --env-file deploy/.env.production `
        -f docker-compose.yml `
        -f docker-compose.production.yml `
        config --quiet
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Compose production configuration is invalid."
    }
}
finally {
    Pop-Location
}

Write-Output "Production deployment configuration is valid."
