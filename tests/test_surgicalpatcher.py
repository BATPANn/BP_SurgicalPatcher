"""Regression tests for the patch engine and the desktop backup workflow."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# The UI tests do not need a visible desktop or window manager.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMessageBox

import bp_surgicalpatcher_v2 as patcher


class PatchEngineTests(unittest.TestCase):
    def test_parser_extracts_multiple_files_and_operations_from_an_answer(self):
        answer = """Here are the requested edits:
```text
<<<FILE: one.py>>>
<<<OPERATION: REPLACE>>>
<<<TARGET: Code Block>>>
<<<SEARCH>>>
a = 1
<<<REPLACE>>>
a = 2
<<<END>>>

<<<FILE: nested/two.py>>>
<<<OPERATION: DELETE>>>
<<<SEARCH>>>
old = True
<<<REPLACE>>>
<<<END>>>
```
"""
        operations = patcher.parse_ai_response(answer)

        self.assertEqual([op.file_hint for op in operations], ["one.py", "nested/two.py"])
        self.assertEqual([op.op_type for op in operations], ["REPLACE", "DELETE"])
        self.assertEqual(operations[0].search_block, "a = 1")
        self.assertEqual(operations[1].replace_block, "")

    def test_operations_are_simulated_sequentially(self):
        original = "def total(a, b):\n    return a - b\n"
        operations = [
            patcher.PatchOperation("REPLACE", "Code Block", "    return a - b", "    return a + b"),
            patcher.PatchOperation("INSERT_BEFORE", "Code Block", "    return a + b", "    print(a, b)"),
            patcher.PatchOperation("INSERT_AFTER", "Code Block", "    return a + b", "    # done"),
            patcher.PatchOperation("DELETE", "Code Block", "    # done", ""),
        ]

        valid, result, diff = patcher.simulate_patches(original, operations, "calculator.py")

        self.assertTrue(valid)
        self.assertEqual(result, "def total(a, b):\n    print(a, b)\n    return a + b\n")
        self.assertTrue(all(op.status == "OK" for op in operations))
        self.assertIn("-    return a - b", diff)
        self.assertIn("+    return a + b", diff)

    def test_duplicate_search_block_is_not_silently_applied(self):
        original = "value = 1\nvalue = 1\n"
        operation = patcher.PatchOperation("REPLACE", "Code Block", "value = 1", "value = 2")

        valid, result, _ = patcher.simulate_patches(original, [operation])

        self.assertFalse(valid)
        self.assertEqual(operation.status, "AMBIGUOUS")
        self.assertEqual(result, original)

    def test_indentation_insensitive_match_adjusts_replacement(self):
        operation = patcher.PatchOperation("REPLACE", "Code Block", "return 1", "return 2")

        valid, result, _ = patcher.simulate_patches("def f():\n    return 1\n", [operation])

        self.assertTrue(valid)
        self.assertEqual(result, "def f():\n    return 2\n")
        self.assertIn("indentation-adjusted", operation.message)

    def test_brace_scanner_can_find_a_method_when_search_does_not_match(self):
        source = 'void Start() {\n    Log("hello { world }");\n}\n'
        operation = patcher.PatchOperation(
            "REPLACE", "Start", "missing anchor", "void Start() {\n    Log(\"new\");\n}"
        )

        valid, result, _ = patcher.simulate_patches(source, [operation])

        self.assertTrue(valid)
        self.assertEqual(result, 'void Start() {\n    Log("new");\n}\n')
        self.assertIn("Brace-Scanner", operation.message)

    def test_unknown_operation_is_rejected(self):
        operation = patcher.PatchOperation("MOVE", "Code Block", "value = 1", "value = 2")

        valid, _, _ = patcher.simulate_patches("value = 1\n", [operation])

        self.assertFalse(valid)
        self.assertEqual(operation.status, "ERROR")

    def test_simulation_preserves_uniform_crlf_endings(self):
        operation = patcher.PatchOperation("REPLACE", "Code Block", "value = 1", "value = 2")

        valid, result, _ = patcher.simulate_patches("value = 1\r\nnext = 3\r\n", [operation])

        self.assertTrue(valid)
        self.assertEqual(result, "value = 2\r\nnext = 3\r\n")


class DesktopWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt_app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.original_settings_file = patcher.SETTINGS_FILE
        patcher.SETTINGS_FILE = str(self.root / "settings.json")
        self.window = patcher.SurgicalPatcherApp()
        self.window.txt_project_dir.setText(str(self.root))

    def tearDown(self):
        self.window.close()
        patcher.SETTINGS_FILE = self.original_settings_file
        self.temp.cleanup()

    def analyze(self, patch_text):
        self.window.scan_project_folder(str(self.root))
        self.window.txt_ai_patch.setPlainText(patch_text)
        self.qt_app.processEvents()

    @staticmethod
    def replace_patch(file_name, old, new):
        return f"""<<<FILE: {file_name}>>>
<<<OPERATION: REPLACE>>>
<<<TARGET: Code Block>>>
<<<SEARCH>>>
{old}
<<<REPLACE>>>
{new}
<<<END>>>"""

    def test_copy_context_contains_only_checked_files_and_the_prompt(self):
        checked = self.root / "checked.py"
        unchecked = self.root / "unchecked.py"
        checked.write_text("selected = True\n", encoding="utf-8")
        unchecked.write_text("not_selected = True\n", encoding="utf-8")
        self.window.scan_project_folder(str(self.root))
        self.window.checked_files = {str(checked)}
        self.window.refresh_script_checklist()

        self.window.copy_checked_scripts_with_prompt()
        copied = self.qt_app.clipboard().text()

        self.assertIn(patcher.AI_PROMPT_INSTRUCTIONS.strip(), copied)
        self.assertIn("<<<FILE: checked.py>>>", copied)
        self.assertIn("selected = True", copied)
        self.assertNotIn("unchecked.py", copied)
        self.assertNotIn("not_selected = True", copied)

    def test_backup_preview_renders_a_real_diff(self):
        live = self.root / "sample.py"
        backup_root = self.root / ".patch_backups"
        backup_root.mkdir()
        backup = backup_root / "sample.py.saved.bak"
        live.write_text("value = 2\n", encoding="utf-8")
        backup.write_text("value = 1\n", encoding="utf-8")
        (backup_root / "backup_index.json").write_text(json.dumps([{
            "timestamp": "20260101_120000_000000",
            "display_time": "2026-01-01 12:00:00",
            "file_name": "sample.py",
            "original_path": str(live),
            "backup_path": str(backup),
        }]), encoding="utf-8")

        restored = mock.Mock()
        dialog = patcher.BackupManagerDialog(self.window, str(backup_root), restored)
        try:
            item = dialog.list_backups.item(0)
            self.assertIsNotNone(item)
            dialog.list_backups.setCurrentRow(0)
            self.assertIn("-value = 2", dialog.txt_preview.toPlainText())
            self.assertIn("+value = 1", dialog.txt_preview.toPlainText())
            with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes), \
                    mock.patch.object(QMessageBox, "information"):
                dialog.revert_selected_backup()
            self.assertEqual(live.read_text(encoding="utf-8"), "value = 1\n")
            restored.assert_called_once()
        finally:
            dialog.close()

    def test_same_named_files_get_separate_backups_in_one_batch(self):
        first = self.root / "first" / "common.py"
        second = self.root / "second" / "common.py"
        first.parent.mkdir()
        second.parent.mkdir()
        first.write_text("value = 1\n", encoding="utf-8")
        second.write_text("value = 10\n", encoding="utf-8")
        self.analyze("\n\n".join([
            self.replace_patch("first/common.py", "value = 1", "value = 2"),
            self.replace_patch("second/common.py", "value = 10", "value = 20"),
        ]))
        self.assertTrue(self.window.btn_apply.isEnabled())

        with mock.patch.object(QMessageBox, "information"), mock.patch.object(QMessageBox, "critical") as critical:
            self.window.apply_all_patches()
        critical.assert_not_called()

        records = json.loads((self.root / ".patch_backups" / "backup_index.json").read_text(encoding="utf-8"))
        self.assertEqual(len(records), 2)
        self.assertNotEqual(records[0]["backup_path"], records[1]["backup_path"])
        backup_by_original = {r["original_path"]: Path(r["backup_path"]).read_text(encoding="utf-8") for r in records}
        self.assertEqual(backup_by_original[str(first)], "value = 1\n")
        self.assertEqual(backup_by_original[str(second)], "value = 10\n")
        self.assertEqual(first.read_text(encoding="utf-8"), "value = 2\n")
        self.assertEqual(second.read_text(encoding="utf-8"), "value = 20\n")

    def test_apply_refuses_to_overwrite_a_file_changed_after_preview(self):
        source = self.root / "sample.py"
        source.write_text("value = 1\n", encoding="utf-8")
        self.analyze(self.replace_patch("sample.py", "value = 1", "value = 2"))
        self.assertTrue(self.window.btn_apply.isEnabled())
        source.write_text("value = 999\n", encoding="utf-8")

        with mock.patch.object(QMessageBox, "warning") as warning, mock.patch.object(QMessageBox, "information"):
            self.window.apply_all_patches()

        warning.assert_called_once()
        self.assertEqual(source.read_text(encoding="utf-8"), "value = 999\n")
        self.assertFalse((self.root / ".patch_backups").exists())

    def test_stale_file_aborts_the_whole_batch_before_any_writes(self):
        first = self.root / "first.py"
        second = self.root / "second.py"
        first.write_text("value = 1\n", encoding="utf-8")
        second.write_text("value = 10\n", encoding="utf-8")
        self.analyze("\n\n".join([
            self.replace_patch("first.py", "value = 1", "value = 2"),
            self.replace_patch("second.py", "value = 10", "value = 20"),
        ]))
        self.assertTrue(self.window.btn_apply.isEnabled())
        second.write_text("value = 999\n", encoding="utf-8")

        with mock.patch.object(QMessageBox, "warning") as warning, mock.patch.object(QMessageBox, "information"):
            self.window.apply_all_patches()

        warning.assert_called_once()
        self.assertEqual(first.read_text(encoding="utf-8"), "value = 1\n")
        self.assertEqual(second.read_text(encoding="utf-8"), "value = 999\n")
        self.assertFalse((self.root / ".patch_backups").exists())

    def test_corrupt_backup_index_does_not_get_overwritten_or_patch_the_file(self):
        source = self.root / "sample.py"
        source.write_text("value = 1\n", encoding="utf-8")
        backup_root = self.root / ".patch_backups"
        backup_root.mkdir()
        index = backup_root / "backup_index.json"
        index.write_text("not valid JSON", encoding="utf-8")
        self.analyze(self.replace_patch("sample.py", "value = 1", "value = 2"))
        self.assertTrue(self.window.btn_apply.isEnabled())

        with mock.patch.object(QMessageBox, "critical") as critical, mock.patch.object(QMessageBox, "information"):
            self.window.apply_all_patches()

        critical.assert_called_once()
        self.assertEqual(source.read_text(encoding="utf-8"), "value = 1\n")
        self.assertEqual(index.read_text(encoding="utf-8"), "not valid JSON")

    def test_failed_index_update_keeps_existing_history_and_does_not_patch(self):
        source = self.root / "sample.py"
        source.write_text("value = 1\n", encoding="utf-8")
        backup_root = self.root / ".patch_backups"
        backup_root.mkdir()
        index = backup_root / "backup_index.json"
        index.write_text("[]", encoding="utf-8")
        self.analyze(self.replace_patch("sample.py", "value = 1", "value = 2"))
        self.assertTrue(self.window.btn_apply.isEnabled())

        with mock.patch.object(patcher.os, "replace", side_effect=OSError("disk error")), \
                mock.patch.object(QMessageBox, "critical") as critical, \
                mock.patch.object(QMessageBox, "information"):
            self.window.apply_all_patches()

        critical.assert_called_once()
        self.assertEqual(source.read_text(encoding="utf-8"), "value = 1\n")
        self.assertEqual(index.read_text(encoding="utf-8"), "[]")
        self.assertEqual(list(backup_root.glob(".backup_index_*.tmp")), [])

    def test_non_utf8_file_is_rejected_instead_of_being_silently_rewritten(self):
        source = self.root / "sample.py"
        source.write_bytes(b'value = "\xff"\n')
        self.analyze(self.replace_patch("sample.py", 'value = "\ufffd"', 'value = "fixed"'))

        self.assertFalse(self.window.btn_apply.isEnabled())
        self.assertIn("READ ERROR", self.window.list_ops.item(0).text())
        self.assertEqual(source.read_bytes(), b'value = "\xff"\n')

    def test_apply_preserves_uniform_crlf_line_endings(self):
        source = self.root / "sample.py"
        source.write_bytes(b"value = 1\r\nnext = 3\r\n")
        self.analyze(self.replace_patch("sample.py", "value = 1", "value = 2"))
        self.assertTrue(self.window.btn_apply.isEnabled())

        with mock.patch.object(QMessageBox, "information"), mock.patch.object(QMessageBox, "critical") as critical:
            self.window.apply_all_patches()

        critical.assert_not_called()
        self.assertEqual(source.read_bytes(), b"value = 2\r\nnext = 3\r\n")


if __name__ == "__main__":
    unittest.main()
