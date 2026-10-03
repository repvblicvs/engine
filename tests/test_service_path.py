from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from repvblicvs_engine.service import plist_document


class ServicePathTests(unittest.TestCase):
    def test_launchd_path_uses_discovered_original_command_directories(self):
        binaries = {"claude": "/fixture/.local/bin/claude", "codex": "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex", "copilot": "/opt/homebrew/bin/copilot", "agy": None}
        with tempfile.TemporaryDirectory() as root, patch("repvblicvs_engine.service.shutil.which", side_effect=binaries.get):
            doc = plist_document(sys.executable, root)
        directories = doc["EnvironmentVariables"]["PATH"].split(":")
        self.assertIn("/fixture/.local/bin", directories)
        self.assertIn("/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS", directories)
        self.assertEqual(directories.count("/opt/homebrew/bin"), 1)
        self.assertIn("/usr/bin", directories)
        self.assertNotIn("agy", directories)

    def test_symlink_names_and_virtualenv_interpreter_are_not_resolved(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            tool_dir = base / "local-bin"
            real_dir = base / "versioned"
            venv_dir = base / "venv/bin"
            for directory in (tool_dir, real_dir, venv_dir):
                directory.mkdir(parents=True)
            real_cli = real_dir / "claude-version-123"
            real_cli.write_text("fixture")
            claude = tool_dir / "claude"
            claude.symlink_to(real_cli)
            python = venv_dir / "python"
            python.symlink_to(sys.executable)
            with patch("repvblicvs_engine.service.shutil.which", side_effect=lambda name: str(claude) if name == "claude" else None):
                doc = plist_document(str(python), root)
            self.assertEqual(doc["ProgramArguments"][0], str(python))
            self.assertIn(str(tool_dir), doc["EnvironmentVariables"]["PATH"].split(":"))
            self.assertNotIn(str(real_dir), doc["EnvironmentVariables"]["PATH"].split(":"))


if __name__ == "__main__":
    unittest.main()
