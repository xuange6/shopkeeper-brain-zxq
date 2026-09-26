param(
    [switch]$AllowPlaceholders
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$overlay = Join-Path $projectRoot "deploy\kubernetes\overlays\production"

if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) {
    throw "kubectl is required to render the Kustomize production overlay."
}

$rendered = & kubectl kustomize $overlay 2>&1 | Out-String
if ($LASTEXITCODE -ne 0) {
    throw "Kustomize rendering failed:`n$rendered"
}

$requiredKinds = @(
    "Deployment",
    "Service",
    "HorizontalPodAutoscaler",
    "PodDisruptionBudget",
    "NetworkPolicy",
    "ScaledObject",
    "TriggerAuthentication",
    "Ingress",
    "Namespace"
)
foreach ($kind in $requiredKinds) {
    if ($rendered -notmatch "(?m)^kind:\s+$([regex]::Escape($kind))\s*$") {
        throw "Rendered deployment is missing required kind: $kind"
    }
}

foreach ($workload in @(
    "shopkeeper-api",
    "shopkeeper-lifecycle-worker",
    "shopkeeper-lifecycle-scheduler"
)) {
    if ($rendered -notmatch "(?m)^\s+name:\s+$([regex]::Escape($workload))\s*$") {
        throw "Rendered deployment is missing workload: $workload"
    }
}

if ($rendered -match "(?m)^\s+hostPath:") {
    throw "Production workloads must not use node-local hostPath volumes."
}
if ($rendered -match "(?m)^\s+privileged:\s+true") {
    throw "Production workloads must not run privileged containers."
}
if ($rendered -notmatch "DISTRIBUTED_RUNTIME") {
    throw "Distributed runtime is not enabled."
}
if ($rendered -notmatch "LIFECYCLE_RELEASE_POINTER_MODE") {
    throw "Database release pointer mode is not configured."
}
if (-not $AllowPlaceholders -and $rendered -match "replace-with|example\.(com|internal)") {
    throw "Production overlay still contains example endpoints, hostnames, models, or image tags."
}

$secretExample = Get-Content -LiteralPath (
    Join-Path $projectRoot "deploy\kubernetes\runtime-secret.example.yaml"
) -Raw
foreach ($key in @(
    "LIFECYCLE_DATABASE_URL",
    "LIFECYCLE_SCHEDULER_DATABASE_URL",
    "LIFECYCLE_MIGRATION_DATABASE_URL",
    "KEDA_DATABASE_URL",
    "LIFECYCLE_ADMIN_TOKEN",
    "ACCESS_CONTEXT_HMAC_SECRET",
    "TASK_STATE_REDIS_URL",
    "OPENAI_API_KEY",
    "NEO4J_PASSWORD",
    "MINIO_ACCESS_KEY",
    "MINIO_SECRET_KEY",
    "MONGO_URL"
)) {
    if ($secretExample -notmatch "(?m)^\s+$([regex]::Escape($key)):") {
        throw "Secret contract is missing key: $key"
    }
}

$objectCount = ([regex]::Matches($rendered, "(?m)^apiVersion:")).Count
Write-Output "Kubernetes production overlay is structurally valid ($objectCount objects)."
if ($AllowPlaceholders) {
    Write-Warning "Placeholder check was skipped; replace every example endpoint and immutable image tag before deployment."
}
