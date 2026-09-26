param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$deployRoot = Join-Path $projectRoot "deploy"
$templatePath = Join-Path $deployRoot ".env.production.example"
$environmentPath = Join-Path $deployRoot ".env.production"
$secretsRoot = Join-Path $deployRoot "secrets"

if ((Test-Path -LiteralPath $environmentPath) -and -not $Force) {
    throw "deploy/.env.production already exists. Use -Force only when rotating every generated secret."
}

New-Item -ItemType Directory -Force -Path $secretsRoot | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $deployRoot "data\sources") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $deployRoot "data\staging") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $deployRoot "data\state") | Out-Null

function New-SafeSecret([int]$ByteCount = 48) {
    $bytes = New-Object byte[] $ByteCount
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function Write-Utf8NoBom([string]$Path, [string]$Value) {
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($Path, $Value, $encoding)
}

$postgresPassword = New-SafeSecret 36
$neo4jPassword = New-SafeSecret 36
$minioPassword = New-SafeSecret 36
$mongoPassword = New-SafeSecret 36
$adminToken = New-SafeSecret 48
$accessSecret = New-SafeSecret 48
$databaseUrl = "postgresql://shopkeeper:$postgresPassword@postgres:5432/shopkeeper"

$environment = Get-Content -LiteralPath $templatePath -Raw
$environment = $environment.Replace("__POSTGRES_PASSWORD__", $postgresPassword)
$environment = $environment.Replace("__NEO4J_PASSWORD__", $neo4jPassword)
$environment = $environment.Replace("__MINIO_ROOT_PASSWORD__", $minioPassword)

# Reuse only application/model settings from the existing developer env. Storage,
# lifecycle and credential-reference settings are deliberately rebuilt above.
$applicationEnvPath = Join-Path $projectRoot "knowledge\.env"
if (Test-Path -LiteralPath $applicationEnvPath) {
    $sourceValues = @{}
    foreach ($line in Get-Content -LiteralPath $applicationEnvPath) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            $sourceValues[$matches[1]] = $matches[2].Trim()
        }
    }

    $importNames = @(
        "OPENAI_API_BASE",
        "OPENAI_API_KEY",
        "MODEL",
        "LLM_DEFAULT_MODEL",
        "ITEM_MODEL",
        "KG_MODEL",
        "VL_MODEL",
        "MCP_DASHSCOPE_BASE_URL"
    )
    $environmentLines = $environment -split "`r?`n"
    for ($index = 0; $index -lt $environmentLines.Count; $index++) {
        if ($environmentLines[$index] -notmatch '^([A-Za-z_][A-Za-z0-9_]*)=') {
            continue
        }
        $name = $matches[1]
        if (($importNames -contains $name) -and $sourceValues.ContainsKey($name) -and $sourceValues[$name]) {
            $environmentLines[$index] = "$name=$($sourceValues[$name])"
        }
    }

    if (-not $sourceValues["MODEL"] -and $sourceValues["ITEM_MODEL"]) {
        for ($index = 0; $index -lt $environmentLines.Count; $index++) {
            if ($environmentLines[$index] -eq "MODEL=") {
                $environmentLines[$index] = "MODEL=$($sourceValues['ITEM_MODEL'])"
            }
        }
    }

    $modelPaths = @("BGE_M3_PATH", "BGE_RERANKER_LARGE") | ForEach-Object {
        if ($sourceValues.ContainsKey($_)) {
            $sourceValues[$_].Trim('"').Trim("'")
        }
    } | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Container) }
    if ($modelPaths.Count -eq 2) {
        $modelRoot = Split-Path -Parent $modelPaths[0]
        if ((Split-Path -Parent $modelPaths[1]) -eq $modelRoot) {
            $modelRoot = $modelRoot.Replace('\', '/')
            for ($index = 0; $index -lt $environmentLines.Count; $index++) {
                if ($environmentLines[$index] -eq "MODEL_HOST_DIR=./models") {
                    $environmentLines[$index] = "MODEL_HOST_DIR=$modelRoot"
                }
            }
        }
    }
    $environment = [string]::Join([Environment]::NewLine, $environmentLines)
}
Write-Utf8NoBom $environmentPath $environment

Write-Utf8NoBom (Join-Path $secretsRoot "lifecycle_database_url.txt") $databaseUrl
Write-Utf8NoBom (Join-Path $secretsRoot "lifecycle_admin_token.txt") $adminToken
Write-Utf8NoBom (Join-Path $secretsRoot "access_context_hmac_secret.txt") $accessSecret
Write-Utf8NoBom (Join-Path $secretsRoot "neo4j_password.txt") $neo4jPassword
Write-Utf8NoBom (Join-Path $secretsRoot "minio_secret_key.txt") $minioPassword
Write-Utf8NoBom (Join-Path $secretsRoot "mongo_password.txt") $mongoPassword

Write-Output "Created deploy/.env.production and six local secret files."
Write-Output "Existing model/provider settings were reused when available; review CORS and deployment paths before launch."
Write-Output "Validate with scripts/validate_production_deployment.ps1."
