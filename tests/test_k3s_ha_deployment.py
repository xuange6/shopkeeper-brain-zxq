import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class K3sHaDeploymentTests(unittest.TestCase):
    def test_example_inventory_requires_three_distinct_servers(self):
        inventory = json.loads(
            (ROOT / "deploy" / "k3s-ha" / "inventory.example.json").read_text(encoding="utf-8")
        )
        servers = inventory["servers"]
        self.assertEqual(3, len(servers))
        self.assertEqual(3, len({server["address"] for server in servers}))
        self.assertEqual(3, len({server["name"] for server in servers}))
        self.assertRegex(inventory["k3sVersion"], r"^v1\.35\.\d+\+k3s\d+$")

    def test_installer_is_dry_run_by_default_and_uses_embedded_etcd(self):
        script = (ROOT / "scripts" / "bootstrap_k3s_ha.ps1").read_text(encoding="utf-8")
        self.assertIn("[switch]$Apply", script)
        self.assertIn("if (-not $Apply)", script)
        self.assertIn("cluster-init: true", script)
        self.assertIn("kubectl wait --for=condition=Ready node --all", script)
        self.assertIn("etcd-snapshot-schedule-cron", script)

    def test_installer_does_not_persist_or_print_cluster_token(self):
        script = (ROOT / "scripts" / "bootstrap_k3s_ha.ps1").read_text(encoding="utf-8")
        self.assertIn("$env:SHOPKEEPER_K3S_TOKEN", script)
        self.assertNotIn("Write-Host $token", script)
        self.assertNotIn("token.txt", script)
        self.assertIn("chmod 600", script)

    def test_keda_is_pinned_and_highly_available(self):
        manifest = (ROOT / "deploy" / "k3s-ha" / "keda-helmchart.yaml").read_text(encoding="utf-8")
        self.assertIn("version: 2.21.0", manifest)
        self.assertEqual(3, manifest.count("replicaCount: 2"))
        self.assertEqual(3, manifest.count("minAvailable: 1"))


if __name__ == "__main__":
    unittest.main()
