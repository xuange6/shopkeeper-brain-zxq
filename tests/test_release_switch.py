from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.switch_rag_release import (
    RELEASE_KEYS,
    current_release_values,
    load_manifest,
    render_env,
    switch_release,
)


class ReleaseSwitchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.manifest = self.root / "release.json"
        self.payload = {
            "schema_version": "rag-release-v1",
            "release_id": "test-release",
            "candidate": {
                "CHUNKS_COLLECTION": "chunks-v2",
                "INDEX_VERSION": "index-v2",
                "ENTITY_NAME_COLLECTION": "entities-v2",
                "KG_GRAPH_VERSION": "graph-v2",
            },
            "rollback": {
                "CHUNKS_COLLECTION": "chunks-control",
                "INDEX_VERSION": "index-control",
                "ENTITY_NAME_COLLECTION": "entities-v2",
                "KG_GRAPH_VERSION": "graph-v2",
            },
        }
        self.manifest.write_text(json.dumps(self.payload), encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_manifest_requires_safe_complete_targets(self) -> None:
        loaded = load_manifest(self.manifest)
        self.assertEqual(loaded["release_id"], "test-release")
        self.payload["candidate"]["INDEX_VERSION"] = "bad\nINJECTED=true"
        self.manifest.write_text(json.dumps(self.payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unsafe values"):
            load_manifest(self.manifest)

    def test_render_updates_only_release_keys_and_preserves_secrets(self) -> None:
        original = (
            "OPENAI_API_KEY=keep-this-secret\n"
            "CHUNKS_COLLECTION=chunks-control\n"
            "INDEX_VERSION=index-control\n"
            "ENTITY_NAME_COLLECTION=entities-v2\n"
            "KG_GRAPH_VERSION=graph-v2\n"
        )
        rendered = render_env(original, self.payload["candidate"])
        self.assertIn("OPENAI_API_KEY=keep-this-secret", rendered)
        self.assertEqual(
            current_release_values(rendered),
            self.payload["candidate"],
        )

    def test_duplicate_release_keys_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate release keys"):
            current_release_values(
                "CHUNKS_COLLECTION=one\nCHUNKS_COLLECTION=two\n"
            )

    def test_dry_run_does_not_change_environment(self) -> None:
        env_path = self.root / ".env"
        original = "SECRET=value\n" + "\n".join(
            f"{key}={self.payload['rollback'][key]}" for key in RELEASE_KEYS
        ) + "\n"
        env_path.write_text(original, encoding="utf-8")
        report = switch_release(
            env_path,
            self.manifest,
            "candidate",
            apply=False,
        )
        self.assertEqual(env_path.read_text(encoding="utf-8"), original)
        self.assertEqual(report["mode"], "dry-run")
        self.assertEqual(set(report["changed_keys"]), {"CHUNKS_COLLECTION", "INDEX_VERSION"})

    def test_apply_is_atomic_and_creates_recoverable_backup(self) -> None:
        env_path = self.root / ".env"
        original = "SECRET=value\n" + "\n".join(
            f"{key}={self.payload['rollback'][key]}" for key in RELEASE_KEYS
        ) + "\n"
        env_path.write_text(original, encoding="utf-8")
        report = switch_release(
            env_path,
            self.manifest,
            "candidate",
            apply=True,
        )
        backup = Path(report["backup"])
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_text(encoding="utf-8"), original)
        self.assertEqual(
            current_release_values(env_path.read_text(encoding="utf-8")),
            self.payload["candidate"],
        )
        self.assertIn("SECRET=value", env_path.read_text(encoding="utf-8"))

    def test_unknown_current_release_requires_force(self) -> None:
        env_path = self.root / ".env"
        env_path.write_text(
            "\n".join(f"{key}=unknown" for key in RELEASE_KEYS) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "do not match"):
            switch_release(
                env_path,
                self.manifest,
                "candidate",
                apply=False,
            )


if __name__ == "__main__":
    unittest.main()
