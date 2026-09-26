[CmdletBinding()]
param(
    [string]$InventoryPath = (Join-Path $PSScriptRoot "..\deploy\k3s-ha\inventory.json"),
    [switch]$Apply
)

$ErrorActionPreference = "Stop"

function Invoke-Remote {
    param(
        [Parameter(Mandatory)]$Server,
        [Parameter(Mandatory)][string]$Command,
        [string]$InputText = ""
    )

    $arguments = @(
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new"
    )
    if (-not [string]::IsNullOrWhiteSpace($script:Inventory.sshKeyPath)) {
        $arguments += @("-i", $script:Inventory.sshKeyPath)
    }
    $arguments += "{0}@{1}" -f $Server.sshUser, $Server.address
    $arguments += $Command

    if ([string]::IsNullOrEmpty($InputText)) {
        & ssh @arguments
    } else {
        $InputText | & ssh @arguments
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Remote command failed on $($Server.name) ($($Server.address))."
    }
}

if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) {
    throw "OpenSSH client was not found. Enable the Windows OpenSSH Client feature first."
}
if (-not (Test-Path -LiteralPath $InventoryPath)) {
    throw "Missing inventory: $InventoryPath. Copy deploy/k3s-ha/inventory.example.json first."
}

$script:Inventory = Get-Content -LiteralPath $InventoryPath -Raw | ConvertFrom-Json
$servers = @($script:Inventory.servers)
if ($servers.Count -ne 3) {
    throw "Embedded-etcd HA requires exactly three server entries in this zero-cost profile."
}
if ((@($servers.name | Sort-Object -Unique)).Count -ne 3) {
    throw "Every K3s server must have a unique name."
}
if ((@($servers.address | Sort-Object -Unique)).Count -ne 3) {
    throw "Every K3s server must have a unique address. Three VMs on one host are not accepted."
}
if ($script:Inventory.k3sVersion -notmatch '^v1\.35\.[0-9]+\+k3s[0-9]+$') {
    throw "k3sVersion must be an explicit stable v1.35.x+k3sN release."
}
if (-not [string]::IsNullOrWhiteSpace($script:Inventory.sshKeyPath) -and -not (Test-Path -LiteralPath $script:Inventory.sshKeyPath)) {
    throw "sshKeyPath does not exist."
}

$preflight = 'set -eu; test "$(uname -s)" = "Linux"; command -v sudo >/dev/null; command -v curl >/dev/null; test "$(nproc)" -ge 2; test "$(awk ''/MemTotal/ {print $2}'' /proc/meminfo)" -ge 3500000; test -d /sys/fs/cgroup; printf "ready\n"'
foreach ($server in $servers) {
    Write-Host "Preflight $($server.name) at $($server.address)"
    Invoke-Remote -Server $server -Command $preflight
}

if (-not $Apply) {
    Write-Host "Three-node preflight passed. No remote state was changed. Re-run with -Apply to install K3s."
    exit 0
}

$token = $env:SHOPKEEPER_K3S_TOKEN
if ([string]::IsNullOrWhiteSpace($token) -or $token -notmatch '^[A-Za-z0-9._~+/=-]{32,128}$') {
    throw "Set SHOPKEEPER_K3S_TOKEN to a 32-128 character random base64/base64url-style value before -Apply."
}

$tlsSans = @($script:Inventory.tlsSans)
if ($tlsSans.Count -lt 3) {
    throw "tlsSans must include all three server addresses and any stable API DNS/VIP."
}
foreach ($server in $servers) {
    if ($tlsSans -notcontains [string]$server.address) {
        throw "tlsSans is missing server address $($server.address)."
    }
}

$tlsYaml = ($tlsSans | ForEach-Object { "  - `"$_`"" }) -join "`n"
$commonConfig = @"
token: "$token"
tls-san:
$tlsYaml
secrets-encryption: true
write-kubeconfig-mode: "0600"
etcd-snapshot-schedule-cron: "0 */6 * * *"
etcd-snapshot-retention: 20
"@

$first = $servers[0]
$firstConfig = "$commonConfig`ncluster-init: true`nnode-name: `"$($first.name)`"`nnode-label:`n  - `"topology.kubernetes.io/zone=$($first.zone)`"`n"
Invoke-Remote -Server $first -Command 'sudo install -d -m 700 /etc/rancher/k3s'
Invoke-Remote -Server $first -Command 'sudo tee /etc/rancher/k3s/config.yaml >/dev/null && sudo chmod 600 /etc/rancher/k3s/config.yaml' -InputText $firstConfig
$installCommand = "curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION='$($script:Inventory.k3sVersion)' sh -"
Invoke-Remote -Server $first -Command $installCommand
Invoke-Remote -Server $first -Command 'for i in $(seq 1 60); do sudo k3s kubectl get --raw=/readyz >/dev/null 2>&1 && exit 0; sleep 5; done; exit 1'

foreach ($server in $servers[1..2]) {
    $joinConfig = "$commonConfig`nserver: `"https://$($first.address):6443`"`nnode-name: `"$($server.name)`"`nnode-label:`n  - `"topology.kubernetes.io/zone=$($server.zone)`"`n"
    Invoke-Remote -Server $server -Command 'sudo install -d -m 700 /etc/rancher/k3s'
    Invoke-Remote -Server $server -Command 'sudo tee /etc/rancher/k3s/config.yaml >/dev/null && sudo chmod 600 /etc/rancher/k3s/config.yaml' -InputText $joinConfig
    Invoke-Remote -Server $server -Command $installCommand
}

Invoke-Remote -Server $first -Command 'sudo k3s kubectl wait --for=condition=Ready node --all --timeout=300s'
$nodeCountCommand = 'test "$(sudo k3s kubectl get nodes --no-headers | wc -l)" -eq 3'
Invoke-Remote -Server $first -Command $nodeCountCommand

$kedaManifestPath = Join-Path $PSScriptRoot "..\deploy\k3s-ha\keda-helmchart.yaml"
$kedaManifest = Get-Content -LiteralPath $kedaManifestPath -Raw
Invoke-Remote -Server $first -Command 'sudo tee /var/lib/rancher/k3s/server/manifests/shopkeeper-keda.yaml >/dev/null' -InputText $kedaManifest
Invoke-Remote -Server $first -Command 'sudo k3s kubectl -n keda wait --for=create deployment/keda-operator --timeout=10m'
Invoke-Remote -Server $first -Command 'sudo k3s kubectl -n keda wait --for=create deployment/keda-operator-metrics-apiserver --timeout=10m'
Invoke-Remote -Server $first -Command 'sudo k3s kubectl -n keda wait --for=create deployment/keda-admission-webhooks --timeout=10m'
Invoke-Remote -Server $first -Command 'sudo k3s kubectl -n keda rollout status deployment/keda-operator --timeout=10m'
Invoke-Remote -Server $first -Command 'sudo k3s kubectl -n keda rollout status deployment/keda-operator-metrics-apiserver --timeout=10m'
Invoke-Remote -Server $first -Command 'sudo k3s kubectl -n keda rollout status deployment/keda-admission-webhooks --timeout=10m'
Invoke-Remote -Server $first -Command 'sudo k3s kubectl get customresourcedefinition scaledobjects.keda.sh triggerauthentications.keda.sh >/dev/null'

Write-Host "Three-node K3s HA bootstrap passed. Retrieve kubeconfig manually as documented; the cluster token was not printed."
