[CmdletBinding()]
param(
    [switch]$Apply,
    [switch]$ApproveCharges
)

$ErrorActionPreference = "Stop"
$terraformDirectory = Join-Path $PSScriptRoot "..\deploy\terraform\alicloud-ack"
$terraformDirectory = (Resolve-Path $terraformDirectory).Path
$backendConfig = Join-Path $terraformDirectory "backend.hcl"
$variableFile = Join-Path $terraformDirectory "terraform.tfvars"
$planFile = Join-Path $terraformDirectory "ack-production.tfplan"

if (-not (Get-Command terraform -ErrorAction SilentlyContinue)) {
    throw "Terraform 1.6+ was not found. Install it or run this stack in Alibaba Cloud Shell."
}

if (-not (Test-Path -LiteralPath $backendConfig)) {
    throw "Missing $backendConfig. Copy backend.hcl.example and configure OSS plus TableStore locking."
}

if (-not (Test-Path -LiteralPath $variableFile)) {
    throw "Missing $variableFile. Copy terraform.tfvars.example and replace all placeholders."
}

$configurationText = (Get-Content -LiteralPath $backendConfig -Raw) + (Get-Content -LiteralPath $variableFile -Raw)
if ($configurationText -match "replace-with") {
    throw "Terraform configuration still contains replace-with placeholders."
}

if ($Apply -and -not $ApproveCharges) {
    throw "Cloud creation incurs charges. Re-run with both -Apply and -ApproveCharges after reviewing the plan."
}

& terraform "-chdir=$terraformDirectory" init "-backend-config=$backendConfig"
if ($LASTEXITCODE -ne 0) { throw "terraform init failed" }

& terraform "-chdir=$terraformDirectory" fmt -check -recursive
if ($LASTEXITCODE -ne 0) { throw "terraform fmt check failed" }

& terraform "-chdir=$terraformDirectory" validate
if ($LASTEXITCODE -ne 0) { throw "terraform validate failed" }

& terraform "-chdir=$terraformDirectory" plan "-var-file=$variableFile" "-out=$planFile"
if ($LASTEXITCODE -ne 0) { throw "terraform plan failed" }

if (-not $Apply) {
    Write-Host "Plan created at $planFile. No cloud resources were changed."
    Write-Host "Review it with: terraform -chdir=$terraformDirectory show $planFile"
    exit 0
}

& terraform "-chdir=$terraformDirectory" apply $planFile
if ($LASTEXITCODE -ne 0) { throw "terraform apply failed" }

Write-Host "ACK cluster creation completed. Retrieve a private kubeconfig from the ACK console."
& terraform "-chdir=$terraformDirectory" output
