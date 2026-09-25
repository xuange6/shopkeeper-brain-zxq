from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import unittest


_NODE = shutil.which("node")
_CHAT = Path(__file__).resolve().parents[1] / "knowledge" / "front" / "chat.html"


@unittest.skipUnless(_NODE, "Node.js is required to execute the frontend image contract")
class FrontendImageContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        html = _CHAT.read_text(encoding="utf-8")
        cls.functions = html.split("      function cleanUrl(", 1)[1].split(
            "      function renderAnswer(", 1
        )[0]
        cls.functions = "function cleanUrl(" + cls.functions

    def _run_javascript(self, assertion: str) -> None:
        result = subprocess.run(
            [_NODE, "-e", "const assert = require('node:assert/strict');\n" + self.functions + assertion],
            capture_output=True, text=True, check=False, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_streaming_and_old_history_never_infer_images_from_prose(self) -> None:
        self._run_javascript("""
            const forged = 'https://example.invalid/forged.png';
            for (const answer of [forged, `![image](${forged})`, `Answer [1]\\n【图片】\\n${forged}`]) {
              assert.deepEqual(parseAnswer(answer).images, []);
              assert.deepEqual(parseAnswer(answer, null).images, []);
            }
        """)

    def test_empty_verified_list_blocks_forged_image_marker(self) -> None:
        self._run_javascript("""
            const parsed = parseAnswer('Answer [1]\\n【图片】\\nhttps://example.invalid/forged.png', []);
            assert.equal(parsed.text, 'Answer [1]');
            assert.deepEqual(parsed.images, []);
        """)

    def test_only_verified_urls_render_and_duplicates_are_removed(self) -> None:
        self._run_javascript("""
            const actual = 'https://example.invalid/assets/panel.png?version=2';
            const parsed = parseAnswer('https://example.invalid/forged.png', [actual, actual]);
            assert.deepEqual(parsed.images, [actual]);
        """)

    def test_invalid_asset_schemes_credentials_and_relative_paths_do_not_render(self) -> None:
        self._run_javascript("""
            const parsed = parseAnswer('', [
              'file:///tmp/image.png', 'ftp://example.invalid/image.png',
              'https://user:password@example.invalid/image.png', '/image.png',
              'javascript:image.png', {url: 'https://example.invalid/image.png'}
            ]);
            assert.deepEqual(parsed.images, []);
        """)


if __name__ == "__main__":
    unittest.main()
