[CmdletBinding()]
param(
    [string]$VagrantDirectory = (Join-Path $PSScriptRoot "..\deploy\k3s-vagrant"),
    [ValidateSet("k3s-1", "k3s-2", "k3s-3")]
    [string]$FaultNode = "k3s-1",
    [switch]$ApplyFault,
    [string]$OutputPath = ""
)

$ErrorActionPreference = "Stop"
$nodes = @("k3s-1", "k3s-2", "k3s-3")
$observerNode = @($nodes | Where-Object { $_ -ne $FaultNode })[0]
$vagrant = (Get-Command "vagrant" -ErrorAction SilentlyContinue).Source
if ([string]::IsNullOrWhiteSpace($vagrant)) {
    $vagrant = "C:\Program Files\Vagrant\bin\vagrant.exe"
}
if (-not (Test-Path -LiteralPath $vagrant)) {
    throw "vagrant was not found."
}
if (-not (Test-Path -LiteralPath (Join-Path $VagrantDirectory "Vagrantfile"))) {
    throw "Vagrantfile not found under $VagrantDirectory."
}

function Invoke-NodeCommand {
    param(
        [Parameter(Mandatory)][string]$Node,
        [Parameter(Mandatory)][string]$Command
    )

    Push-Location $VagrantDirectory
    try {
        $output = & $script:vagrant ssh $Node -c $Command 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "Remote command failed on ${Node}: $($output -join [Environment]::NewLine)"
        }
        return ($output -join [Environment]::NewLine).Trim()
    } finally {
        Pop-Location
    }
}

function Get-DeploymentAvailability {
    param([Parameter(Mandatory)][string]$Node)

    $json = Invoke-NodeCommand -Node $Node -Command 'sudo k3s kubectl -n keda get deployment -o json'
    $items = @(($json | ConvertFrom-Json).items)
    if ($items.Count -ne 3) {
        throw "Expected three KEDA deployments, found $($items.Count)."
    }
    return @($items | ForEach-Object {
        [ordered]@{
            name = $_.metadata.name
            desired_replicas = [int]$_.spec.replicas
            ready_replicas = [int]$_.status.readyReplicas
            available_replicas = [int]$_.status.availableReplicas
        }
    })
}

$initialNodeJson = Invoke-NodeCommand -Node $observerNode -Command 'sudo k3s kubectl get nodes -o json'
$initialNodes = @(($initialNodeJson | ConvertFrom-Json).items)
if ($initialNodes.Count -ne 3) {
    throw "Expected three Kubernetes nodes, found $($initialNodes.Count)."
}
$initialAvailability = Get-DeploymentAvailability -Node $observerNode
if (@($initialAvailability | Where-Object { $_.available_replicas -lt 1 }).Count -gt 0) {
    throw "Every KEDA deployment must have at least one available replica before the drill."
}

if (-not $ApplyFault) {
    Write-Host "Preflight passed. Re-run with -ApplyFault to stop $FaultNode, verify quorum from $observerNode, and restore it."
    exit 0
}

$startedAt = (Get-Date).ToUniversalTime()
$faultApplied = $false
try {
    Invoke-NodeCommand -Node $FaultNode -Command 'sudo systemctl stop k3s; test "$(systemctl is-active k3s)" = "inactive"' | Out-Null
    $faultApplied = $true

    $waitForNotReady = 'for i in $(seq 1 24); do sudo k3s kubectl get nodes --no-headers | grep "^' + $FaultNode + ' " | grep -Eq "[[:space:]]NotReady[[:space:]]" && exit 0; sleep 5; done; sudo k3s kubectl get nodes; exit 1'
    Invoke-NodeCommand -Node $observerNode -Command $waitForNotReady | Out-Null
    $apiReady = Invoke-NodeCommand -Node $observerNode -Command 'sudo k3s kubectl get --raw=/readyz'
    if ($apiReady -ne "ok") {
        throw "Kubernetes API readiness failed during the fault: $apiReady"
    }
    $readyNodeCountText = Invoke-NodeCommand -Node $observerNode -Command 'sudo k3s kubectl get nodes --no-headers | grep -Ec "[[:space:]]Ready[[:space:]]"'
    $readyNodeCount = [int]$readyNodeCountText
    if ($readyNodeCount -ne 2) {
        throw "Expected exactly two Ready nodes during the fault, found $readyNodeCount."
    }
    $degradedAvailability = Get-DeploymentAvailability -Node $observerNode
    if (@($degradedAvailability | Where-Object { $_.available_replicas -lt 1 }).Count -gt 0) {
        throw "At least one KEDA deployment lost all available replicas during the fault."
    }
} finally {
    if ($faultApplied) {
        Invoke-NodeCommand -Node $FaultNode -Command 'sudo systemctl start k3s' | Out-Null
        $waitForReady = 'sudo k3s kubectl wait node/' + $FaultNode + ' --for=condition=Ready --timeout=180s'
        Invoke-NodeCommand -Node $observerNode -Command $waitForReady | Out-Null
        Invoke-NodeCommand -Node $observerNode -Command 'sudo k3s kubectl -n keda rollout status deployment --timeout=300s' | Out-Null
    }
}

$finalNodeJson = Invoke-NodeCommand -Node $observerNode -Command 'sudo k3s kubectl get nodes -o json'
$finalNodes = @(($finalNodeJson | ConvertFrom-Json).items)
$finalReadyNodes = @($finalNodes | Where-Object {
    @($_.status.conditions | Where-Object { $_.type -eq "Ready" -and $_.status -eq "True" }).Count -eq 1
}).Count
$finalAvailability = Get-DeploymentAvailability -Node $observerNode
if ($finalReadyNodes -ne 3 -or @($finalAvailability | Where-Object { $_.available_replicas -ne $_.desired_replicas }).Count -gt 0) {
    throw "The cluster did not fully recover after the fault drill."
}

if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
    $OutputPath = Join-Path $PSScriptRoot "..\output\stage3-k3s-vagrant-ha.$stamp.json"
}
$resolvedOutputPath = [System.IO.Path]::GetFullPath($OutputPath)
New-Item -ItemType Directory -Path (Split-Path -Parent $resolvedOutputPath) -Force | Out-Null
$evidence = [ordered]@{
    schema_version = 1
    generated_at = (Get-Date).ToUniversalTime().ToString("o")
    cluster = [ordered]@{
        distribution = "k3s"
        topology = "three-server embedded-etcd"
        physical_failure_domains = 1
        limitation = "All three VMs share one Windows host; this validates Kubernetes behavior, not physical-host HA."
    }
    drill = [ordered]@{
        started_at = $startedAt.ToString("o")
        fault = "k3s service stopped"
        fault_node = $FaultNode
        observer_node = $observerNode
        api_ready_during_fault = $apiReady
        ready_nodes_during_fault = $readyNodeCount
        keda_during_fault = $degradedAvailability
    }
    recovery = [ordered]@{
        ready_nodes = $finalReadyNodes
        keda = $finalAvailability
    }
}
$evidence | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $resolvedOutputPath -Encoding utf8
Write-Host "K3s single-control-plane fault drill passed. Evidence: $resolvedOutputPath"
