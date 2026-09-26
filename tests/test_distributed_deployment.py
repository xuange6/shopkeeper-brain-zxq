from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
KUBERNETES = ROOT / "deploy" / "kubernetes"


class DistributedDeploymentTests(unittest.TestCase):
    def _all_manifests(self) -> str:
        return "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(KUBERNETES.rglob("*.yaml"))
        )

    def test_workloads_do_not_use_hostpath_or_privileged_containers(self) -> None:
        manifests = self._all_manifests()
        self.assertNotIn("hostPath:", manifests)
        self.assertNotIn("privileged: true", manifests)
        self.assertIn("runAsNonRoot: true", manifests)
        self.assertIn('drop: ["ALL"]', manifests)

    def test_production_overlay_contains_ha_and_scaling_controls(self) -> None:
        manifests = self._all_manifests()
        for kind in (
            "HorizontalPodAutoscaler",
            "PodDisruptionBudget",
            "NetworkPolicy",
            "ScaledObject",
            "TriggerAuthentication",
        ):
            self.assertIn(f"kind: {kind}", manifests)
        self.assertIn("topologySpreadConstraints:", manifests)
        self.assertIn("minReplicaCount: 2", manifests)
        self.assertIn("maxReplicaCount: 30", manifests)

    def test_runtime_secret_is_external_and_has_separated_database_roles(self) -> None:
        overlay = (
            KUBERNETES / "overlays" / "production" / "kustomization.yaml"
        ).read_text(encoding="utf-8")
        self.assertNotIn("runtime-secret.example.yaml", overlay)
        secret_contract = (
            KUBERNETES / "runtime-secret.example.yaml"
        ).read_text(encoding="utf-8")
        for key in (
            "LIFECYCLE_DATABASE_URL",
            "LIFECYCLE_SCHEDULER_DATABASE_URL",
            "LIFECYCLE_MIGRATION_DATABASE_URL",
            "KEDA_DATABASE_URL",
            "TASK_STATE_REDIS_URL",
        ):
            self.assertIn(f"  {key}:", secret_contract)

    def test_distributed_mode_uses_database_release_pointer(self) -> None:
        config = (KUBERNETES / "base" / "configmap.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn('DISTRIBUTED_RUNTIME: "true"', config)
        self.assertIn("LIFECYCLE_RELEASE_POINTER_MODE: database", config)
        self.assertIn('LIFECYCLE_AUTO_MIGRATE: "false"', config)


if __name__ == "__main__":
    unittest.main()
