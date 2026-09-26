from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from knowledge.app import create_app
from knowledge.core.app_config import AppConfig
from knowledge.core.deps import get_lifecycle_store
from knowledge.lifecycle.store import LifecycleStore


class AppSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(create_app())

    def test_home_redirects_to_chat(self) -> None:
        response = self.client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "/chat.html")

    def test_frontend_and_health_are_available(self) -> None:
        self.assertEqual(self.client.get("/chat.html").status_code, 200)
        self.assertEqual(self.client.get("/import.html").status_code, 200)

        payload = self.client.get("/health").json()
        self.assertEqual(payload["status"], "ok")
        self.assertIn("version", payload)
        self.assertEqual(self.client.get("/ready").status_code, 200)

    def test_production_readiness_fails_closed_without_external_services(self) -> None:
        with patch.dict("os.environ", {"APP_ENV": "production"}, clear=True):
            app = create_app(AppConfig(environment="production"))
            client = TestClient(app)
            response = client.get("/ready")
            self.assertEqual(response.status_code, 503)
            failures = response.json()["detail"]["failures"]
            self.assertIn("postgres_control_plane_not_configured", failures)

    def test_system_endpoint_never_returns_credentials(self) -> None:
        response = self.client.get("/api/system")
        self.assertEqual(response.status_code, 200)
        body = response.text.lower()
        self.assertNotIn("api_key", body)
        self.assertNotIn("password", body)
        self.assertIsInstance(response.json()["integrations"]["milvus"], bool)

    def test_lifecycle_metrics_endpoint_is_prometheus_text(self) -> None:
        response = self.client.get("/metrics")
        self.assertEqual(response.status_code, 200)
        self.assertIn("source_sync_total", response.text)
        self.assertNotIn("password", response.text.lower())

    def test_unknown_task_is_404(self) -> None:
        response = self.client.get("/status/not-a-real-task")
        self.assertEqual(response.status_code, 404)

    def test_query_validation_rejects_blank_input(self) -> None:
        response = self.client.post("/query", json={"query": "", "is_stream": False})
        self.assertEqual(response.status_code, 422)

    def test_upload_rejects_unsupported_file_type(self) -> None:
        response = self.client.post(
            "/upload",
            files={"file": ("payload.exe", b"not-a-document", "application/octet-stream")},
        )
        self.assertEqual(response.status_code, 400)

    def test_distributed_runtime_disables_non_durable_direct_upload(self) -> None:
        with patch.dict("os.environ", {"DISTRIBUTED_RUNTIME": "true"}, clear=False):
            response = self.client.post(
                "/upload",
                files={"file": ("guide.md", b"hello", "text/markdown")},
            )
        self.assertEqual(response.status_code, 503)
        self.assertIn("durable lifecycle Source", response.text)

    def test_lifecycle_admin_api_is_fail_closed_and_never_echoes_secret_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LifecycleStore(Path(directory) / "lifecycle.sqlite3")
            app = create_app()
            app.dependency_overrides[get_lifecycle_store] = lambda: store
            client = TestClient(app)
            with patch.dict("os.environ", {"LIFECYCLE_ADMIN_TOKEN": ""}, clear=False):
                self.assertEqual(client.get("/api/lifecycle/admin/sources").status_code, 503)
            with patch.dict("os.environ", {"LIFECYCLE_ADMIN_TOKEN": "test-admin-token"}):
                self.assertEqual(client.get("/api/lifecycle/admin/sources").status_code, 401)
                response = client.put(
                    "/api/lifecycle/admin/sources/source-a",
                    headers={"X-Lifecycle-Admin-Token": "test-admin-token"},
                    json={
                        "tenant_id": "tenant-a",
                        "connector_type": "local_directory",
                        "configuration_version": "v1",
                        "configuration": {"root": "fixtures"},
                        "credential_reference": "vault://source-a",
                    },
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertNotIn("credential_reference", response.json())


if __name__ == "__main__":
    unittest.main()
