terraform {
  required_version = ">= 1.6.0, < 2.0.0"

  required_providers {
    alicloud = {
      source  = "aliyun/alicloud"
      version = "= 1.293.0"
    }
  }

  # Production state must be stored in OSS and locked with TableStore.
  # Supply all values through: terraform init -backend-config=backend.hcl
  backend "oss" {}
}
