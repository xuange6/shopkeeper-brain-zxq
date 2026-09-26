import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STACK = ROOT / "deploy" / "terraform" / "alicloud-ack"


class AckTerraformTests(unittest.TestCase):
    def test_stack_uses_ack_pro_and_three_zone_networks(self):
        main = (STACK / "main.tf").read_text(encoding="utf-8")
        variables = (STACK / "variables.tf").read_text(encoding="utf-8")
        self.assertIn('cluster_spec                 = "ack.pro.small"', main)
        self.assertIn("length(var.availability_zones) == 3", variables)
        self.assertIn("pod_vswitch_ids", main)
        self.assertIn('multi_az_policy       = "BALANCE"', main)

    def test_stack_is_private_managed_and_destroy_protected(self):
        main = (STACK / "main.tf").read_text(encoding="utf-8")
        self.assertIn("slb_internet_enabled         = false", main)
        self.assertIn("deletion_protection          = true", main)
        self.assertIn("audit_log_config", main)
        self.assertGreaterEqual(main.count("prevent_destroy = true"), 3)
        self.assertIn("system_disk_encrypted = true", main)
        self.assertIn("security_hardening_os = true", main)
        self.assertIn("auto_repair     = true", main)
        self.assertIn("auto_upgrade    = true", main)

    def test_state_is_remote_locked_and_credentials_are_not_declared(self):
        versions = (STACK / "versions.tf").read_text(encoding="utf-8")
        backend = (STACK / "backend.hcl.example").read_text(encoding="utf-8")
        all_tf = "\n".join(path.read_text(encoding="utf-8") for path in STACK.glob("*.tf"))
        self.assertIn('backend "oss" {}', versions)
        self.assertIn("tablestore_table", backend)
        self.assertNotIn("access_key", all_tf.lower())
        self.assertNotIn("secret_key", all_tf.lower())

    def test_creation_wrapper_requires_explicit_charge_approval(self):
        script = (ROOT / "scripts" / "provision_ack_cluster.ps1").read_text(encoding="utf-8")
        self.assertIn("[switch]$Apply", script)
        self.assertIn("[switch]$ApproveCharges", script)
        self.assertIn("$Apply -and -not $ApproveCharges", script)
        self.assertNotIn("terraform destroy", script.lower())


if __name__ == "__main__":
    unittest.main()
