variable "region_id" {
  description = "Alibaba Cloud region for the production ACK cluster."
  type        = string
  default     = "cn-shanghai"

  validation {
    condition     = can(regex("^[a-z]{2}-[a-z]+$", var.region_id))
    error_message = "region_id must look like cn-shanghai."
  }
}
variable "cluster_name" {
  description = "ACK cluster name."
  type        = string
  default     = "shopkeeper-brain-production"

  validation {
    condition     = length(var.cluster_name) >= 6 && length(var.cluster_name) <= 63
    error_message = "cluster_name must contain 6 to 63 characters."
  }
}

variable "availability_zones" {
  description = "Exactly three ACK-supported availability zones in region_id. Keep the order aligned with both CIDR lists."
  type        = list(string)

  validation {
    condition     = length(var.availability_zones) == 3 && length(distinct(var.availability_zones)) == 3
    error_message = "Production requires exactly three distinct availability zones."
  }
}

variable "vpc_cidr" {
  description = "Dedicated production VPC CIDR."
  type        = string
  default     = "10.20.0.0/16"
}

variable "node_vswitch_cidrs" {
  description = "One node vSwitch CIDR per availability zone."
  type        = list(string)
  default     = ["10.20.0.0/20", "10.20.16.0/20", "10.20.32.0/20"]

  validation {
    condition     = length(var.node_vswitch_cidrs) == 3 && length(distinct(var.node_vswitch_cidrs)) == 3
    error_message = "Provide exactly three distinct node vSwitch CIDRs."
  }
}

variable "pod_vswitch_cidrs" {
  description = "One Terway pod vSwitch CIDR per availability zone."
  type        = list(string)
  default     = ["10.20.64.0/19", "10.20.96.0/19", "10.20.128.0/19"]

  validation {
    condition     = length(var.pod_vswitch_cidrs) == 3 && length(distinct(var.pod_vswitch_cidrs)) == 3
    error_message = "Provide exactly three distinct pod vSwitch CIDRs."
  }
}

variable "service_cidr" {
  description = "Kubernetes Service CIDR. It must not overlap the VPC or other connected networks."
  type        = string
  default     = "172.21.0.0/20"
}

variable "worker_instance_types" {
  description = "At least two ECS worker types available in all three zones, ordered by preference."
  type        = list(string)

  validation {
    condition     = length(var.worker_instance_types) >= 2
    error_message = "Provide at least two instance types to tolerate zone inventory shortages."
  }
}

variable "ecs_key_pair_name" {
  description = "Existing ECS SSH key pair name. Private key material must not be created or stored in Terraform state."
  type        = string
  sensitive   = true

  validation {
    condition     = length(trimspace(var.ecs_key_pair_name)) > 0
    error_message = "ecs_key_pair_name cannot be empty."
  }
}

variable "node_pool_min_size" {
  description = "Minimum worker count. Keep at least one worker per availability zone."
  type        = number
  default     = 3

  validation {
    condition     = var.node_pool_min_size >= 3
    error_message = "node_pool_min_size must be at least 3."
  }
}

variable "node_pool_max_size" {
  description = "Maximum worker count for ACK node autoscaling."
  type        = number
  default     = 12

  validation {
    condition     = var.node_pool_max_size >= 6
    error_message = "node_pool_max_size must be at least 6."
  }
}

variable "system_disk_size_gib" {
  description = "Encrypted system disk size for worker nodes."
  type        = number
  default     = 120

  validation {
    condition     = var.system_disk_size_gib >= 100
    error_message = "Worker system disks must be at least 100 GiB."
  }
}

variable "tags" {
  description = "Tags applied to production resources."
  type        = map(string)
  default = {
    Environment = "production"
    ManagedBy   = "terraform"
    Project     = "shopkeeper-brain"
  }
}
