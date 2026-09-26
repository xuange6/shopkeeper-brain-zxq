locals {
  node_vswitch_cidrs_by_zone = zipmap(var.availability_zones, var.node_vswitch_cidrs)
  pod_vswitch_cidrs_by_zone  = zipmap(var.availability_zones, var.pod_vswitch_cidrs)
  node_vswitch_ids           = [for zone in var.availability_zones : alicloud_vswitch.nodes[zone].id]
  pod_vswitch_ids            = [for zone in var.availability_zones : alicloud_vswitch.pods[zone].id]
}

resource "alicloud_vpc" "production" {
  vpc_name   = "${var.cluster_name}-vpc"
  cidr_block = var.vpc_cidr
  tags       = var.tags

  lifecycle {
    prevent_destroy = true
  }
}

resource "alicloud_vswitch" "nodes" {
  for_each = local.node_vswitch_cidrs_by_zone

  vpc_id       = alicloud_vpc.production.id
  zone_id      = each.key
  cidr_block   = each.value
  vswitch_name = "${var.cluster_name}-${each.key}-nodes"
  tags         = var.tags
}

resource "alicloud_vswitch" "pods" {
  for_each = local.pod_vswitch_cidrs_by_zone

  vpc_id       = alicloud_vpc.production.id
  zone_id      = each.key
  cidr_block   = each.value
  vswitch_name = "${var.cluster_name}-${each.key}-pods"
  tags         = var.tags
}

resource "alicloud_cs_managed_kubernetes" "production" {
  name                         = var.cluster_name
  cluster_spec                 = "ack.pro.small"
  vswitch_ids                  = local.node_vswitch_ids
  pod_vswitch_ids              = local.pod_vswitch_ids
  new_nat_gateway              = true
  service_cidr                 = var.service_cidr
  slb_internet_enabled         = false
  enable_rrsa                  = true
  is_enterprise_security_group = true
  deletion_protection          = true

  control_plane_log_components = ["apiserver", "kcm", "scheduler", "ccm"]
  control_plane_log_ttl        = "30"

  audit_log_config {
    enabled = true
  }

  addons {
    name   = "terway-eniip"
    config = ""
  }

  addons {
    name   = "metrics-server"
    config = ""
  }

  addons {
    name   = "csi-plugin"
    config = ""
  }

  addons {
    name   = "csi-provisioner"
    config = ""
  }

  addons {
    name   = "nginx-ingress-controller"
    config = jsonencode({ IngressSlbNetworkType = "internet" })
  }

  addons {
    name   = "arms-prometheus"
    config = ""
  }

  addons {
    name   = "ack-node-problem-detector"
    config = jsonencode({ sls_project_name = "" })
  }

  addons {
    name   = "loongcollector"
    config = jsonencode({ IngressDashboardEnabled = "true" })
  }

  lifecycle {
    prevent_destroy = true

    precondition {
      condition     = var.node_pool_max_size >= var.node_pool_min_size
      error_message = "node_pool_max_size must be greater than or equal to node_pool_min_size."
    }
  }

  timeouts {
    create = "60m"
    delete = "30m"
    update = "60m"
  }
}

resource "alicloud_cs_kubernetes_node_pool" "application" {
  cluster_id            = alicloud_cs_managed_kubernetes.production.id
  node_pool_name        = "production-application"
  vswitch_ids           = local.node_vswitch_ids
  instance_types        = var.worker_instance_types
  instance_charge_type  = "PostPaid"
  install_cloud_monitor = true
  key_name              = var.ecs_key_pair_name
  image_type            = "AliyunLinux3"
  runtime_name          = "containerd"
  system_disk_category  = "cloud_essd"
  system_disk_size      = var.system_disk_size_gib
  system_disk_encrypted = true
  security_hardening_os = true
  login_as_non_root     = true
  multi_az_policy       = "BALANCE"

  scaling_config {
    min_size = var.node_pool_min_size
    max_size = var.node_pool_max_size
    type     = "cpu"
  }

  management {
    enable          = true
    auto_repair     = true
    auto_upgrade    = true
    auto_vul_fix    = true
    max_unavailable = 1

    auto_repair_policy {
      restart_node = true
    }

    auto_upgrade_policy {
      auto_upgrade_kubelet = true
    }

    auto_vul_fix_policy {
      vul_level    = "asap"
      restart_node = true
    }
  }

  labels {
    key   = "shopkeeper-brain/workload"
    value = "application"
  }

  tags = var.tags

  lifecycle {
    prevent_destroy = true
  }

  timeouts {
    create = "90m"
    delete = "60m"
    update = "90m"
  }
}
