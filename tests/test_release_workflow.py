"""Check that release automation attaches the script matching the release tag."""

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release-assets.yml"


class ReleaseScriptSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        start_marker = "          python3 - <<'PY'\n"
        end_marker = "\n          PY\n"
        start = workflow.index(start_marker) + len(start_marker)
        end = workflow.index(end_marker, start)
        cls.selector = textwrap.dedent(workflow[start:end])
        if 'for file in "$SCRIPT_FILE"' not in workflow:
            raise AssertionError("Release upload must use the selected versioned script")

    def select_script(self, tag, cwd=ROOT):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "github-output"
            env = dict(os.environ, RELEASE_TAG=tag, GITHUB_OUTPUT=str(output))
            result = subprocess.run(
                [sys.executable, "-c", self.selector],
                cwd=cwd, env=env, capture_output=True, text=True, check=False,
            )
            return result, output.read_text(encoding="utf-8") if output.exists() else ""

    def test_v25_release_selects_the_new_script(self):
        result, output = self.select_script("v2.5")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output, "script=BP_SurgicalPatcher_v2.5.py\n")

    def test_older_release_still_selects_the_v2_script(self):
        result, output = self.select_script("v2.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output, "script=bp_surgicalpatcher_v2.py\n")

    def test_unknown_version_fails_instead_of_uploading_the_wrong_script(self):
        result, output = self.select_script("v99.0")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output, "")
        self.assertIn("Expected one script", result.stderr)

    def test_duplicate_version_fails_instead_of_selecting_arbitrarily(self):
        with tempfile.TemporaryDirectory() as temp:
            for filename in ("first.py", "second.py"):
                (Path(temp) / filename).write_text('APP_VERSION = "v2.5"\n', encoding="utf-8")
            result, output = self.select_script("v2.5", cwd=temp)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output, "")
        self.assertIn("Expected one script", result.stderr)


if __name__ == "__main__":
    unittest.main()
