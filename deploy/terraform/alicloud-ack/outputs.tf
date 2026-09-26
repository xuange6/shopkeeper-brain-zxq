output "cluster_id" {
  description = "ACK cluster ID used when retrieving kubeconfig."
  value       = alicloud_cs_managed_kubernetes.production.id
}

output "cluster_name" {
  description = "ACK cluster name."
  value       = alicloud_cs_managed_kubernetes.production.name
}

output "region_id" {
  description = "Alibaba Cloud region."
  value       = var.region_id
}

output "vpc_id" {
  description = "Production VPC ID for managed data-service private endpoints."
  value       = alicloud_vpc.production.id
}

output "node_vswitch_ids" {
  description = "Worker vSwitch IDs across the three availability zones."
  value       = local.node_vswitch_ids
}

output "pod_vswitch_ids" {
  description = "Terway pod vSwitch IDs across the three availability zones."
  value       = local.pod_vswitch_ids
}

output "node_pool_id" {
  description = "Managed auto-scaling application node pool ID."
  value       = alicloud_cs_kubernetes_node_pool.application.node_pool_id
}

output "api_server_public_access" {
  description = "The Kubernetes API server is intentionally private."
  value       = false
}
