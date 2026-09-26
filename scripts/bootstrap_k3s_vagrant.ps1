[CmdletBinding()]
param(
    [string]$VagrantDirectory = (Join-Path $PSScriptRoot "..\deploy\k3s-vagrant"),
    [switch]$Apply,
    [switch]$PrepareArtifacts,
    [switch]$AllowHypervisorPresent,
    [string]$KedaRegistry = $env:SHOPKEEPER_KEDA_REGISTRY
)

$ErrorActionPreference = "Stop"
$K3sVersion = "v1.35.8+k3s1"
$KedaVersion = "2.21.0"
if ([string]::IsNullOrWhiteSpace($KedaRegistry)) {
    $KedaRegistry = "ghcr.linkos.org"
}
$VmMemoryMb = if ($env:K3S_LAB_VM_MEMORY_MB) { [int]$env:K3S_LAB_VM_MEMORY_MB } else { 4096 }
$HostMemoryHeadroomBytes = 2GB
$RequiredFreeDiskBytes = 45GB

function Resolve-Executable {
    param(
        [Parameter(Mandatory)][string]$Name,
        [string[]]$FallbackPaths = @()
    )

    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return $command.Source
    }
    foreach ($path in $FallbackPaths) {
        if (Test-Path -LiteralPath $path) {
            return $path
        }
    }
    throw "$Name was not found. Follow deploy/k3s-vagrant/README.md to install the free prerequisites."
}

function Invoke-Vagrant {
    param([Parameter(Mandatory)][string[]]$Arguments)

    Push-Location $VagrantDirectory
    try {
        & $script:Vagrant @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "Vagrant failed: $($Arguments -join ' ')"
        }
    } finally {
        Pop-Location
    }
}

function Invoke-VagrantSsh {
    param(
        [Parameter(Mandatory)][string]$Node,
        [Parameter(Mandatory)][string]$Command,
        [string]$InputText = ""
    )

    Push-Location $VagrantDirectory
    try {
        if ([string]::IsNullOrEmpty($InputText)) {
            & $script:Vagrant ssh $Node -c $Command
        } else {
            $InputText | & $script:Vagrant ssh $Node -c $Command
        }
        if ($LASTEXITCODE -ne 0) {
            throw "Remote command failed on $Node."
        }
    } finally {
        Pop-Location
    }
}

function Copy-ToVagrant {
    param(
        [Parameter(Mandatory)][string]$Node,
        [Parameter(Mandatory)][string]$SourcePath,
        [Parameter(Mandatory)][string]$DestinationPath
    )

    Push-Location $VagrantDirectory
    try {
        & $script:Vagrant upload $SourcePath $DestinationPath $Node
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to upload $SourcePath to $Node."
        }
    } finally {
        Pop-Location
    }
}

function Get-VerifiedK3sBinary {
    $cacheDirectory = Join-Path $VagrantDirectory ".cache"
    $binaryPath = Join-Path $cacheDirectory "k3s-$K3sVersion-amd64"
    $checksumPath = Join-Path $cacheDirectory "sha256sum-$K3sVersion-amd64.txt"
    $escapedVersion = [Uri]::EscapeDataString($K3sVersion)
    $mirrorVersion = $K3sVersion -replace '\+', '-'
    $releaseBases = @(
        "https://rancher-mirror.rancher.cn/k3s/$mirrorVersion",
        "https://github.com/k3s-io/k3s/releases/download/$escapedVersion"
    )

    New-Item -ItemType Directory -Path $cacheDirectory -Force | Out-Null
    if (-not (Test-Path -LiteralPath $checksumPath)) {
        $checksumDownloaded = $false
        foreach ($releaseBase in $releaseBases) {
            try {
                Invoke-WebRequest -Uri "$releaseBase/sha256sum-amd64.txt" -OutFile $checksumPath -UseBasicParsing
                $checksumDownloaded = $true
                break
            } catch {
                Write-Warning "Checksum download failed from $releaseBase; trying the next official source."
            }
        }
        if (-not $checksumDownloaded) {
            throw "Unable to download the K3s checksum manifest from any official source."
        }
    }

    $checksumLine = Get-Content -LiteralPath $checksumPath | Where-Object { $_ -match '^[a-f0-9]{64}\s+k3s$' } | Select-Object -First 1
    if ($null -eq $checksumLine -or $checksumLine -notmatch '^([a-f0-9]{64})\s+k3s$') {
        throw "The official K3s checksum manifest does not contain an amd64 k3s entry."
    }
    $expectedHash = $Matches[1].ToUpperInvariant()

    $downloadRequired = -not (Test-Path -LiteralPath $binaryPath)
    if (-not $downloadRequired) {
        $downloadRequired = (Get-FileHash -LiteralPath $binaryPath -Algorithm SHA256).Hash -ne $expectedHash
    }
    if ($downloadRequired) {
        $partialPath = "$binaryPath.partial"
        $curl = Resolve-Executable -Name "curl.exe"
        $binaryDownloaded = $false
        foreach ($releaseBase in $releaseBases) {
            & $curl --fail --location --retry 4 --retry-delay 3 --retry-all-errors --continue-at - --output $partialPath "$releaseBase/k3s"
            if ($LASTEXITCODE -eq 0) {
                $binaryDownloaded = $true
                break
            }
            Write-Warning "K3s binary download failed from $releaseBase; trying the next official source."
        }
        if (-not $binaryDownloaded) {
            throw "K3s binary download failed after bounded retries; the partial file was retained for resume."
        }
        $actualHash = (Get-FileHash -LiteralPath $partialPath -Algorithm SHA256).Hash
        if ($actualHash -ne $expectedHash) {
            Remove-Item -LiteralPath $partialPath -Force
            throw "Downloaded K3s binary failed SHA-256 verification."
        }
        Move-Item -LiteralPath $partialPath -Destination $binaryPath -Force
    }
    return $binaryPath
}

function Get-VerifiedK3sAirgapArchive {
    $cacheDirectory = Join-Path $VagrantDirectory ".cache"
    $archiveName = "k3s-airgap-images-amd64.tar.gz"
    $archivePath = Join-Path $cacheDirectory "k3s-airgap-images-amd64-$K3sVersion.tar.gz"
    $checksumPath = Join-Path $cacheDirectory "sha256sum-$K3sVersion-amd64.txt"
    $escapedVersion = [Uri]::EscapeDataString($K3sVersion)
    $mirrorVersion = $K3sVersion -replace '\+', '-'
    $releaseBases = @(
        "https://rancher-mirror.rancher.cn/k3s/$mirrorVersion",
        "https://github.com/k3s-io/k3s/releases/download/$escapedVersion"
    )

    if (-not (Test-Path -LiteralPath $checksumPath)) {
        throw "The K3s checksum manifest is missing; resolve the verified K3s binary first."
    }
    $checksumLine = Get-Content -LiteralPath $checksumPath | Where-Object {
        $_ -match '^[a-f0-9]{64}\s+k3s-airgap-images-amd64\.tar\.gz$'
    } | Select-Object -First 1
    if ($null -eq $checksumLine -or $checksumLine -notmatch '^([a-f0-9]{64})\s+k3s-airgap-images-amd64\.tar\.gz$') {
        throw "The official K3s checksum manifest does not contain the amd64 air-gap archive."
    }
    $expectedHash = $Matches[1].ToUpperInvariant()

    $downloadRequired = -not (Test-Path -LiteralPath $archivePath)
    if (-not $downloadRequired) {
        $downloadRequired = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash -ne $expectedHash
    }
    if ($downloadRequired) {
        $partialPath = "$archivePath.partial"
        $curl = Resolve-Executable -Name "curl.exe"
        $downloaded = $false
        foreach ($releaseBase in $releaseBases) {
            & $curl --fail --location --retry 4 --retry-delay 3 --retry-all-errors --continue-at - --output $partialPath "$releaseBase/$archiveName"
            if ($LASTEXITCODE -eq 0) {
                $downloaded = $true
                break
            }
            Write-Warning "K3s air-gap archive download failed from $releaseBase; trying the next official source."
        }
        if (-not $downloaded) {
            throw "K3s air-gap archive download failed after bounded retries; the partial file was retained for resume."
        }
        $actualHash = (Get-FileHash -LiteralPath $partialPath -Algorithm SHA256).Hash
        if ($actualHash -ne $expectedHash) {
            Remove-Item -LiteralPath $partialPath -Force
            throw "Downloaded K3s air-gap archive failed SHA-256 verification."
        }
        Move-Item -LiteralPath $partialPath -Destination $archivePath -Force
    }
    return $archivePath
}

function Resolve-Oras {
    $command = Get-Command "oras.exe" -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return $command.Source
    }
    $wingetPackages = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
    if (Test-Path -LiteralPath $wingetPackages) {
        $candidate = Get-ChildItem -LiteralPath $wingetPackages -Filter "oras.exe" -File -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match 'ORASProject\.ORAS' } |
            Select-Object -First 1
        if ($null -ne $candidate) {
            return $candidate.FullName
        }
    }
    throw "oras.exe was not found. Install the free ORAS CLI described in deploy/k3s-vagrant/README.md."
}

function Get-VerifiedKedaImageArchive {
    $cacheDirectory = Join-Path $VagrantDirectory ".cache"
    $layoutPath = Join-Path $cacheDirectory "keda-oci"
    $archivePath = Join-Path $cacheDirectory "keda-images-$KedaVersion-linux-amd64.tar"
    $archiveHashPath = "$archivePath.sha256"
    $images = @(
        [pscustomobject]@{ Name = "keda"; Tag = "keda-$KedaVersion"; RootDigest = "sha256:81fe6547ce8d1cc29273f887b76507e9b853e5bc1f875ec23fe60061a38ed809" },
        [pscustomobject]@{ Name = "keda-metrics-apiserver"; Tag = "keda-metrics-apiserver-$KedaVersion"; RootDigest = "sha256:255375037fe592732eeb554743aae360c2da88cf0b9b1258d0a987ef885f0881" },
        [pscustomobject]@{ Name = "keda-admission-webhooks"; Tag = "keda-admission-webhooks-$KedaVersion"; RootDigest = "sha256:e1969628cca6123e32eea13c47c75817091af2657fa68d9d0d58a8620d1b75b1" }
    )

    New-Item -ItemType Directory -Path $cacheDirectory -Force | Out-Null
    if ((Test-Path -LiteralPath $archivePath) -and (Test-Path -LiteralPath $archiveHashPath)) {
        $expectedArchiveHash = (Get-Content -LiteralPath $archiveHashPath -Raw).Trim().ToUpperInvariant()
        $actualArchiveHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash
        if ($expectedArchiveHash -eq $actualArchiveHash) {
            return $archivePath
        }
        Write-Warning "The cached KEDA archive checksum changed; rebuilding it from verified image digests."
    }

    $oras = Resolve-Oras
    New-Item -ItemType Directory -Path $layoutPath -Force | Out-Null
    foreach ($image in $images) {
        $source = "$KedaRegistry/kedacore/$($image.Name):$KedaVersion"
        $resolveOutput = & $oras resolve $source
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to resolve $source."
        }
        $resolvedDigest = @($resolveOutput | Where-Object { $_ -match '^sha256:[a-f0-9]{64}$' } | Select-Object -Last 1)
        if ($resolvedDigest.Count -ne 1 -or $resolvedDigest[0] -ne $image.RootDigest) {
            throw "Digest verification failed for $source. Expected $($image.RootDigest), received $($resolvedDigest -join ', ')."
        }
        $sourceByDigest = "$KedaRegistry/kedacore/$($image.Name)@$($image.RootDigest)"
        $layoutTarget = "${layoutPath}:$($image.Tag)"
        $copyOutput = & $oras cp --platform linux/amd64 $sourceByDigest --to-oci-layout $layoutTarget --concurrency 1 --no-tty
        $copyExitCode = $LASTEXITCODE
        $copyOutput | ForEach-Object { Write-Host $_ }
        if ($copyExitCode -ne 0) {
            throw "Unable to copy verified KEDA image $sourceByDigest into the local OCI layout."
        }
    }

    $ingestPath = Join-Path $layoutPath "ingest"
    if (Test-Path -LiteralPath $ingestPath) {
        Remove-Item -LiteralPath $ingestPath -Recurse -Force
    }
    $tar = Resolve-Executable -Name "tar.exe"
    $partialArchive = "$archivePath.partial"
    if (Test-Path -LiteralPath $partialArchive) {
        Remove-Item -LiteralPath $partialArchive -Force
    }
    & $tar -cf $partialArchive -C $layoutPath .
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to create the KEDA OCI archive."
    }
    Move-Item -LiteralPath $partialArchive -Destination $archivePath -Force
    $archiveHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash
    Set-Content -LiteralPath $archiveHashPath -Value $archiveHash -Encoding ascii
    return $archivePath
}

function Get-RunningVagrantNodeCount {
    Push-Location $VagrantDirectory
    try {
        $statusOutput = & $script:Vagrant status --machine-readable
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to read Vagrant machine state."
        }
        return @($statusOutput | Where-Object { $_ -match ',state,running$' }).Count
    } finally {
        Pop-Location
    }
}

if (-not (Test-Path -LiteralPath (Join-Path $VagrantDirectory "Vagrantfile"))) {
    throw "Vagrantfile not found under $VagrantDirectory."
}

$script:Vagrant = Resolve-Executable -Name "vagrant" -FallbackPaths @(
    "C:\Program Files\Vagrant\bin\vagrant.exe",
    "C:\HashiCorp\Vagrant\bin\vagrant.exe"
)
$null = Resolve-Executable -Name "VBoxManage" -FallbackPaths @(
    "C:\Program Files\Oracle\VirtualBox\VBoxManage.exe"
)

$operatingSystem = Get-CimInstance Win32_OperatingSystem
$computerSystem = Get-CimInstance Win32_ComputerSystem
$systemDrive = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$($env:SystemDrive)'"
$totalMemoryBytes = [int64]$computerSystem.TotalPhysicalMemory
$freeMemoryBytes = [int64]$operatingSystem.FreePhysicalMemory * 1KB
$freeDiskBytes = [int64]$systemDrive.FreeSpace

if ($totalMemoryBytes -lt 24GB) {
    throw "At least 24 GB total host memory is required for this three-VM profile."
}
if ($freeDiskBytes -lt $RequiredFreeDiskBytes) {
    throw "At least 45 GB free on $($env:SystemDrive) is required before creating the dynamic VM disks."
}

Write-Host ("Host resources: {0:N1} GB total RAM, {1:N1} GB free RAM, {2:N1} GB free disk." -f `
    ($totalMemoryBytes / 1GB), ($freeMemoryBytes / 1GB), ($freeDiskBytes / 1GB))
Invoke-Vagrant -Arguments @("validate")

if ($PrepareArtifacts) {
    if ($Apply) {
        throw "Use either -PrepareArtifacts or -Apply, not both."
    }
    $preparedK3sBinary = Get-VerifiedK3sBinary
    $preparedK3sAirgap = Get-VerifiedK3sAirgapArchive
    $preparedKedaArchive = Get-VerifiedKedaImageArchive
    Write-Host "Verified offline artifacts are ready:"
    Write-Host "  K3s binary: $preparedK3sBinary"
    Write-Host "  K3s air-gap images: $preparedK3sAirgap"
    Write-Host "  KEDA OCI archive: $preparedKedaArchive"
    exit 0
}

if (-not $Apply) {
    Write-Host "Vagrant configuration and host capacity preflight passed. No VM was created or changed."
    if ($computerSystem.HypervisorPresent) {
        Write-Warning "Windows hypervisor is running. Read deploy/k3s-vagrant/README.md before applying."
    }
    exit 0
}

$runningNodeCount = Get-RunningVagrantNodeCount
$nodesStillToStart = [math]::Max(0, 3 - $runningNodeCount)
$requiredFreeMemoryBytes = ([int64]$nodesStillToStart * $VmMemoryMb * 1MB) + $HostMemoryHeadroomBytes
if ($freeMemoryBytes -lt $requiredFreeMemoryBytes) {
    throw ("Only {0:N1} GB RAM is free. {1} node(s) still need to start; free at least {2:N1} GB, then retry." -f `
        ($freeMemoryBytes / 1GB), $nodesStillToStart, ($requiredFreeMemoryBytes / 1GB))
}
if ($computerSystem.HypervisorPresent -and -not $AllowHypervisorPresent) {
    throw "Windows hypervisor is running. To preserve Docker/WSL, first try again with -AllowHypervisorPresent after reading the compatibility warning."
}

$token = $env:SHOPKEEPER_K3S_TOKEN
if ([string]::IsNullOrWhiteSpace($token) -or $token -notmatch '^[A-Za-z0-9._~+/=-]{32,128}$') {
    throw "Set SHOPKEEPER_K3S_TOKEN to a 32-128 character random base64/base64url-style value before -Apply."
}

Invoke-Vagrant -Arguments @("up", "--no-provision")

$nodes = @(
    [pscustomobject]@{ Name = "k3s-1"; Address = "192.168.56.21"; Zone = "lab-a" },
    [pscustomobject]@{ Name = "k3s-2"; Address = "192.168.56.22"; Zone = "lab-b" },
    [pscustomobject]@{ Name = "k3s-3"; Address = "192.168.56.23"; Zone = "lab-c" }
)

foreach ($node in $nodes) {
    Invoke-VagrantSsh -Node $node.Name -Command 'test "$(nproc)" -ge 2 && test "$(awk ''/MemTotal/ {print $2}'' /proc/meminfo)" -ge 3500000 && command -v curl >/dev/null'
}

$k3sBinaryPath = Get-VerifiedK3sBinary
$k3sAirgapArchivePath = Get-VerifiedK3sAirgapArchive
$kedaImageArchivePath = Get-VerifiedKedaImageArchive
foreach ($node in $nodes) {
    Copy-ToVagrant -Node $node.Name -SourcePath $k3sBinaryPath -DestinationPath "/tmp/shopkeeper-k3s"
    Copy-ToVagrant -Node $node.Name -SourcePath $k3sAirgapArchivePath -DestinationPath "/tmp/k3s-airgap-images-amd64.tar.gz"
    Invoke-VagrantSsh -Node $node.Name -Command 'sudo install -m 755 /tmp/shopkeeper-k3s /usr/local/bin/k3s && sudo install -d -m 755 /var/lib/rancher/k3s/agent/images && sudo install -m 644 /tmp/k3s-airgap-images-amd64.tar.gz /var/lib/rancher/k3s/agent/images/k3s-airgap-images-amd64.tar.gz && rm -f /tmp/shopkeeper-k3s /tmp/k3s-airgap-images-amd64.tar.gz'
}

$tlsYaml = ($nodes | ForEach-Object { "  - `"$($_.Address)`"" }) -join "`n"
$commonConfig = @"
token: "$token"
tls-san:
$tlsYaml
secrets-encryption: true
write-kubeconfig-mode: "0600"
etcd-snapshot-schedule-cron: "0 */6 * * *"
etcd-snapshot-retention: 20
"@

$first = $nodes[0]
$firstConfig = "$commonConfig`ncluster-init: true`nnode-name: `"$($first.Name)`"`nnode-ip: `"$($first.Address)`"`nadvertise-address: `"$($first.Address)`"`nflannel-iface: `"eth1`"`nnode-label:`n  - `"topology.kubernetes.io/zone=$($first.Zone)`"`n"
Invoke-VagrantSsh -Node $first.Name -Command 'sudo install -d -m 700 /etc/rancher/k3s'
Invoke-VagrantSsh -Node $first.Name -Command 'sudo tee /etc/rancher/k3s/config.yaml >/dev/null && sudo chmod 600 /etc/rancher/k3s/config.yaml' -InputText $firstConfig
$installCommand = "curl -sfL https://get.k3s.io | INSTALL_K3S_SKIP_DOWNLOAD=true INSTALL_K3S_SKIP_START=true INSTALL_K3S_VERSION='$K3sVersion' sh -"
Invoke-VagrantSsh -Node $first.Name -Command $installCommand
Invoke-VagrantSsh -Node $first.Name -Command 'sudo systemctl enable k3s >/dev/null && sudo systemctl restart --no-block k3s'
Invoke-VagrantSsh -Node $first.Name -Command 'for i in $(seq 1 120); do sudo k3s kubectl get --raw=/readyz >/dev/null 2>&1 && exit 0; sleep 5; done; sudo systemctl status k3s --no-pager --full; exit 1'

foreach ($node in $nodes[1..2]) {
    $joinConfig = "$commonConfig`nserver: `"https://$($first.Address):6443`"`nnode-name: `"$($node.Name)`"`nnode-ip: `"$($node.Address)`"`nadvertise-address: `"$($node.Address)`"`nflannel-iface: `"eth1`"`nnode-label:`n  - `"topology.kubernetes.io/zone=$($node.Zone)`"`n"
    Invoke-VagrantSsh -Node $node.Name -Command 'sudo install -d -m 700 /etc/rancher/k3s'
    Invoke-VagrantSsh -Node $node.Name -Command 'sudo tee /etc/rancher/k3s/config.yaml >/dev/null && sudo chmod 600 /etc/rancher/k3s/config.yaml' -InputText $joinConfig
    Invoke-VagrantSsh -Node $node.Name -Command $installCommand
    Invoke-VagrantSsh -Node $node.Name -Command 'sudo systemctl enable k3s >/dev/null && sudo systemctl restart --no-block k3s'
}

$nodeReadinessCommand = 'for i in $(seq 1 120); do test "$(sudo k3s kubectl get nodes --no-headers 2>/dev/null | wc -l)" -eq 3 && sudo k3s kubectl wait --for=condition=Ready node --all --timeout=30s && exit 0; sleep 5; done; sudo k3s kubectl get nodes -o wide; exit 1'
Invoke-VagrantSsh -Node $first.Name -Command $nodeReadinessCommand

foreach ($node in $nodes) {
    Copy-ToVagrant -Node $node.Name -SourcePath $kedaImageArchivePath -DestinationPath "/tmp/shopkeeper-keda-images.tar"
    $importCommand = "sudo k3s ctr -n k8s.io images import --base-name localhost/shopkeeper-keda /tmp/shopkeeper-keda-images.tar && " +
        "sudo k3s ctr -n k8s.io images tag --force localhost/shopkeeper-keda:keda-$KedaVersion ghcr.io/kedacore/keda:$KedaVersion && " +
        "sudo k3s ctr -n k8s.io images tag --force localhost/shopkeeper-keda:keda-metrics-apiserver-$KedaVersion ghcr.io/kedacore/keda-metrics-apiserver:$KedaVersion && " +
        "sudo k3s ctr -n k8s.io images tag --force localhost/shopkeeper-keda:keda-admission-webhooks-$KedaVersion ghcr.io/kedacore/keda-admission-webhooks:$KedaVersion && " +
        "sudo k3s crictl inspecti ghcr.io/kedacore/keda:$KedaVersion >/dev/null && " +
        "sudo k3s crictl inspecti ghcr.io/kedacore/keda-metrics-apiserver:$KedaVersion >/dev/null && " +
        "sudo k3s crictl inspecti ghcr.io/kedacore/keda-admission-webhooks:$KedaVersion >/dev/null && " +
        "rm -f /tmp/shopkeeper-keda-images.tar"
    Invoke-VagrantSsh -Node $node.Name -Command $importCommand
}

$kedaManifestPath = Join-Path $PSScriptRoot "..\deploy\k3s-ha\keda-helmchart.yaml"
$kedaManifest = Get-Content -LiteralPath $kedaManifestPath -Raw
Invoke-VagrantSsh -Node $first.Name -Command 'sudo tee /var/lib/rancher/k3s/server/manifests/shopkeeper-keda.yaml >/dev/null' -InputText $kedaManifest
Invoke-VagrantSsh -Node $first.Name -Command 'sudo k3s kubectl -n keda wait --for=create deployment/keda-operator --timeout=10m'
Invoke-VagrantSsh -Node $first.Name -Command 'sudo k3s kubectl -n keda wait --for=create deployment/keda-operator-metrics-apiserver --timeout=10m'
Invoke-VagrantSsh -Node $first.Name -Command 'sudo k3s kubectl -n keda wait --for=create deployment/keda-admission-webhooks --timeout=10m'
Invoke-VagrantSsh -Node $first.Name -Command 'sudo k3s kubectl -n keda rollout status deployment/keda-operator --timeout=10m'
Invoke-VagrantSsh -Node $first.Name -Command 'sudo k3s kubectl -n keda rollout status deployment/keda-operator-metrics-apiserver --timeout=10m'
Invoke-VagrantSsh -Node $first.Name -Command 'sudo k3s kubectl -n keda rollout status deployment/keda-admission-webhooks --timeout=10m'
Invoke-VagrantSsh -Node $first.Name -Command 'sudo k3s kubectl get customresourcedefinition scaledobjects.keda.sh triggerauthentications.keda.sh >/dev/null'

Write-Host "Three-VM K3s acceptance cluster is Ready. The cluster token was not printed or saved by this script."
