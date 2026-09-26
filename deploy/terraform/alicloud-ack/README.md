# Alibaba Cloud ACK production cluster

This stack creates the Kubernetes substrate for `shopkeeper_brain`:

- ACK Pro managed control plane;
- a dedicated VPC with node and Terway pod vSwitches in three availability zones;
- a private Kubernetes API server and managed Internet ingress;
- a managed, encrypted, security-hardened node pool with three-node minimum, balanced multi-zone placement, auto repair, controlled rolling maintenance, and scaling to 12 nodes;
- Metrics Server, CSI, NGINX Ingress, ARMS Prometheus, node diagnostics, and logging add-ons;
- OSS remote Terraform state with TableStore locking.

The Alibaba Cloud provider is pinned to 1.293.0. Upgrade it only through a reviewed pull request and a saved plan; do not silently consume an untested future provider release.

It intentionally does **not** create PostgreSQL, Redis, Milvus, Neo4j, MongoDB, object storage, DNS, TLS certificates, or application secrets. Those services have independent backup, security, sizing, and lifecycle requirements and must use private endpoints in the emitted VPC.

## Why ACK instead of self-managed Kubernetes

The ACK Pro control plane removes the need to maintain etcd and control-plane nodes ourselves. Worker capacity remains explicit and inspectable, while the application manifests stay portable Kubernetes/Kustomize resources.

## Prerequisites

1. An Alibaba Cloud account with ACK activated and a dedicated least-privilege RAM identity for Terraform. Prefer STS or a named profile; never write an AccessKey into `.tf`, `backend.hcl`, or `terraform.tfvars`.
2. ACK, Auto Scaling, NAT Gateway, SLB, ARMS, SLS, OSS, and TableStore activated. These products can incur charges.
3. A private versioned OSS bucket and TableStore lock table. The table's primary key must be named `LockID` and use String type.
4. An existing ECS key pair whose private key is held outside Terraform.
5. Terraform 1.6+ locally, or Alibaba Cloud Shell/ROS Terraform.

ACK Pro itself is billed hourly, and ECS workers, load balancers, NAT/EIP, storage, logs, and monitoring are billed separately. Treat the console price estimate as authoritative and configure budget alerts before applying.

## Configure safely

Copy the two templates; both destination files are ignored by Git:

```powershell
Copy-Item deploy/terraform/alicloud-ack/backend.hcl.example deploy/terraform/alicloud-ack/backend.hcl
Copy-Item deploy/terraform/alicloud-ack/terraform.tfvars.example deploy/terraform/alicloud-ack/terraform.tfvars
```

In the ACK console, start the cluster creation wizard and use **Console-to-Code** to identify three zones and ECS types actually available to your account. Do not finish the console wizard. Put those values into `terraform.tfvars`.

Configure credentials using an Alibaba Cloud profile or short-lived environment variables. Do not paste credentials into this repository.

## Plan first

The wrapper only generates a plan by default:

```powershell
.\scripts\provision_ack_cluster.ps1
```

Review the resource count, regions, CIDRs, instance types, and estimated cloud charges. Creation happens only with both switches:

```powershell
.\scripts\provision_ack_cluster.ps1 -Apply -ApproveCharges
```

The Kubernetes API is private. Retrieve the private kubeconfig from the ACK console and administer it from an ECS bastion, VPN-connected workstation, or another trusted host in the VPC. Do not commit kubeconfig.

## Install cluster runtime add-ons

After `kubectl` points at the new cluster, install KEDA in high-availability mode:

```powershell
.\scripts\bootstrap_ack_runtime.ps1
```

The script checks Kubernetes/KEDA compatibility, installs a pinned chart, waits for all KEDA components, and verifies the API extensions. Then replace every placeholder in `deploy/kubernetes/overlays/production`, create `shopkeeper-runtime` through a secret manager, and deploy following `docs/DISTRIBUTED_PRODUCTION_DEPLOYMENT.md`.

## Destruction protection

The VPC, ACK cluster, and node pool use `prevent_destroy`. There is deliberately no automated destroy wrapper. Retirement requires a reviewed backup/retention plan, removal of protection in code, a new reviewed Terraform plan, and an explicit manual apply.
