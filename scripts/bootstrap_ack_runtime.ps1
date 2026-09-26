[CmdletBinding()]
param(
    [string]$KedaChartVersion = "2.21.0",
    [switch]$AllowUnsupportedKubernetesVersion
)

$ErrorActionPreference = "Stop"
$kedaValues = Join-Path $PSScriptRoot "..\deploy\kubernetes\addons\keda-values.yaml"
$kedaValues = (Resolve-Path $kedaValues).Path

foreach ($command in @("kubectl", "helm")) {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
        throw "$command was not found. Install it before bootstrapping the ACK cluster."
    }
}

$currentContext = (& kubectl config current-context).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($currentContext)) {
    throw "kubectl has no current cluster context. Retrieve the private ACK kubeconfig first."
}

$versionDocument = & kubectl version -o json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw "Unable to query the Kubernetes server version." }
$minor = [int](($versionDocument.serverVersion.minor -replace '[^0-9]', ''))
if (($minor -lt 34 -or $minor -gt 36) -and -not $AllowUnsupportedKubernetesVersion) {
    throw "KEDA $KedaChartVersion is tested with Kubernetes 1.34-1.36, but this cluster is 1.$minor. Select a compatible KEDA chart or explicitly override after review."
}

Write-Host "Target context: $currentContext (Kubernetes 1.$minor)"
& helm repo add kedacore https://kedacore.github.io/charts --force-update
if ($LASTEXITCODE -ne 0) { throw "Unable to configure the official KEDA Helm repository." }
& helm repo update kedacore
if ($LASTEXITCODE -ne 0) { throw "Unable to update the KEDA Helm repository." }

& helm upgrade --install keda kedacore/keda --namespace keda --create-namespace --version $KedaChartVersion --values $kedaValues --atomic --timeout 10m
if ($LASTEXITCODE -ne 0) { throw "KEDA installation failed and Helm rolled it back." }

& kubectl -n keda rollout status deployment/keda-operator --timeout=5m
if ($LASTEXITCODE -ne 0) { throw "KEDA operator did not become ready." }
& kubectl -n keda rollout status deployment/keda-operator-metrics-apiserver --timeout=5m
if ($LASTEXITCODE -ne 0) { throw "KEDA metrics API server did not become ready." }
& kubectl -n keda rollout status deployment/keda-admission-webhooks --timeout=5m
if ($LASTEXITCODE -ne 0) { throw "KEDA admission webhooks did not become ready." }

& kubectl get customresourcedefinition scaledobjects.keda.sh triggerauthentications.keda.sh
if ($LASTEXITCODE -ne 0) { throw "KEDA API extensions are missing." }
Write-Host "ACK runtime bootstrap passed. KEDA $KedaChartVersion is highly available."
