from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from knowledge.app import create_app


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

    def test_system_endpoint_never_returns_credentials(self) -> None:
        response = self.client.get("/api/system")
        self.assertEqual(response.status_code, 200)
        body = response.text.lower()
        self.assertNotIn("api_key", body)
        self.assertNotIn("password", body)
        self.assertIsInstance(response.json()["integrations"]["milvus"], bool)

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


if __name__ == "__main__":
    unittest.main()
