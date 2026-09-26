import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class K3sVagrantDeploymentTests(unittest.TestCase):
    def test_vagrantfile_defines_three_sized_private_nodes(self):
        content = (ROOT / "deploy" / "k3s-vagrant" / "Vagrantfile").read_text(encoding="utf-8")
        self.assertEqual(3, len(re.findall(r'name: "k3s-[123]"', content)))
        self.assertEqual(3, len(re.findall(r'ip: "192\.168\.56\.2[123]"', content)))
        self.assertIn('"4096"', content)
        self.assertIn('"2"', content)
        self.assertIn('size: "30GB"', content)
        self.assertIn('bento/ubuntu-24.04', content)
        self.assertIn('linked_clone = true', content)
        self.assertIn('config.vm.boot_timeout = 900', content)

    def test_bootstrap_is_dry_run_by_default_and_pins_k3s(self):
        content = (ROOT / "scripts" / "bootstrap_k3s_vagrant.ps1").read_text(encoding="utf-8")
        self.assertIn("[switch]$Apply", content)
        self.assertIn("[switch]$PrepareArtifacts", content)
        self.assertIn("if (-not $Apply)", content)
        self.assertIn('"v1.35.8+k3s1"', content)
        self.assertIn('cluster-init: true', content)
        self.assertIn('kubectl wait --for=condition=Ready node --all', content)
        self.assertIn('test "$(sudo k3s kubectl get nodes --no-headers 2>/dev/null | wc -l)" -eq 3', content)
        self.assertIn('etcd-snapshot-schedule-cron', content)
        self.assertIn('@("up", "--no-provision")', content)
        self.assertIn("Get-VerifiedK3sBinary", content)
        self.assertIn("Get-FileHash", content)
        self.assertIn("INSTALL_K3S_SKIP_DOWNLOAD=true", content)
        self.assertIn("INSTALL_K3S_SKIP_START=true", content)
        self.assertIn("systemctl restart --no-block k3s", content)
        self.assertIn("node-ip:", content)
        self.assertIn("advertise-address:", content)
        self.assertIn("flannel-iface:", content)
        self.assertIn("--retry-all-errors", content)
        self.assertIn("--continue-at", content)
        self.assertIn("rancher-mirror.rancher.cn", content)
        self.assertIn("Get-VerifiedK3sAirgapArchive", content)
        self.assertIn("k3s-airgap-images-amd64.tar.gz", content)
        self.assertIn("Get-VerifiedKedaImageArchive", content)
        self.assertIn("cp --platform linux/amd64", content)
        self.assertIn("ghcr.io/kedacore/keda:$KedaVersion", content)
        self.assertIn("--base-name localhost/shopkeeper-keda", content)

    def test_keda_manifest_uses_local_cache_friendly_pull_policy(self):
        content = (ROOT / "deploy" / "k3s-ha" / "keda-helmchart.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn("pullPolicy: IfNotPresent", content)
        self.assertIn("replicaCount: 2", content)

    def test_bootstrap_has_resource_and_hypervisor_safety_gates(self):
        content = (ROOT / "scripts" / "bootstrap_k3s_vagrant.ps1").read_text(encoding="utf-8")
        self.assertIn("$HostMemoryHeadroomBytes = 2GB", content)
        self.assertIn("Get-RunningVagrantNodeCount", content)
        self.assertIn("$nodesStillToStart", content)
        self.assertIn("$RequiredFreeDiskBytes = 45GB", content)
        self.assertIn("$computerSystem.HypervisorPresent", content)
        self.assertIn("[switch]$AllowHypervisorPresent", content)
        self.assertNotIn("bcdedit", content.lower())
        self.assertNotIn("Disable-WindowsOptionalFeature", content)

    def test_cluster_token_is_not_in_vagrantfile_or_command_arguments(self):
        vagrantfile = (ROOT / "deploy" / "k3s-vagrant" / "Vagrantfile").read_text(encoding="utf-8")
        script = (ROOT / "scripts" / "bootstrap_k3s_vagrant.ps1").read_text(encoding="utf-8")
        self.assertNotIn("SHOPKEEPER_K3S_TOKEN", vagrantfile)
        self.assertIn("$env:SHOPKEEPER_K3S_TOKEN", script)
        self.assertNotIn("Write-Host $token", script)
        self.assertNotIn("token.txt", script)
        self.assertIn("-InputText $firstConfig", script)

    def test_readme_is_explicit_about_single_host_failure_domain(self):
        content = (ROOT / "deploy" / "k3s-vagrant" / "README.md").read_text(encoding="utf-8")
        self.assertIn("它不是生产高可用", content)
        self.assertIn("共享同一台物理主机", content)
        self.assertIn("不会自动关闭 Hyper-V", content)
        self.assertIn("ORASProject.ORAS", content)
        self.assertIn("官方 air-gap 系统镜像包", content)
        self.assertIn("SHOPKEEPER_KEDA_REGISTRY", content)
        self.assertIn("vagrant destroy", content)

    def test_ha_validator_restores_faulted_node_and_writes_evidence(self):
        content = (ROOT / "scripts" / "validate_k3s_vagrant_ha.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("[switch]$ApplyFault", content)
        self.assertIn("systemctl stop k3s", content)
        self.assertIn("kubectl get --raw=/readyz", content)
        self.assertIn("ready_nodes_during_fault", content)
        self.assertIn("finally", content)
        self.assertIn("systemctl start k3s", content)
        self.assertIn("physical_failure_domains = 1", content)


if __name__ == "__main__":
    unittest.main()
