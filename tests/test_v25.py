"""Version 2.5 patch-format, file-creation, and safety regression tests."""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMessageBox

MODULE_PATH = Path(__file__).resolve().parents[1] / "BP_SurgicalPatcher_v2.5.py"
SPEC = importlib.util.spec_from_file_location("surgical_patcher_v25", MODULE_PATH)
patcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(patcher)


def patch_block(file_name, operation, search, replacement, target="Code Block"):
    return f"""@@FILE: {file_name}@@
@@OPERATION: {operation}@@
@@TARGET: {target}@@
@@SEARCH@@
{search}
@@REPLACE@@
{replacement}
@@END@@"""


class PatchFormatTests(unittest.TestCase):
    def test_version_and_prompt_describe_creation(self):
        self.assertEqual(patcher.APP_VERSION, "v2.5")
        self.assertIn("@@OPERATION: CREATE@@", patcher.AI_PROMPT_INSTRUCTIONS)
        self.assertIn("brand new script", patcher.AI_PROMPT_INSTRUCTIONS)

    def test_parser_accepts_at_tags_and_multiple_files(self):
        text = "```text\n" + "\n\n".join([
            patch_block("old.py", "REPLACE", "value = 1", "value = 2"),
            patch_block("new/hello.py", "CREATE", "Create new file", "print('hello')"),
        ]) + "\n```"
        operations = patcher.parse_multi_file_ai_response(text)
        self.assertEqual([op.file_name for op in operations], ["old.py", "new/hello.py"])
        self.assertEqual([op.op_type for op in operations], ["REPLACE", "CREATE"])
        self.assertEqual(operations[1].replace_block, "print('hello')")

    def test_parser_remains_compatible_with_v2_angle_tags(self):
        text = """<<<FILE: old.py>>>
<<<OPERATION: DELETE>>>
<<<TARGET: Code Block>>>
<<<SEARCH>>>
obsolete = True
<<<REPLACE>>>
<<<END>>>"""
        operations = patcher.parse_multi_file_ai_response(text)
        self.assertEqual(len(operations), 1)
        self.assertEqual(operations[0].op_type, "DELETE")
        self.assertEqual(operations[0].replace_block, "")

    def test_whitespace_normalization_matches_extra_blank_lines(self):
        op = patcher.PatchOperation("test.py", "REPLACE", "Code Block", "a = 1\nb = 2", "a = 3")
        valid, result, _ = patcher.simulate_file_operations("a = 1\n\nb = 2\n", [op], "test.py")
        self.assertTrue(valid)
        self.assertEqual(result, "a = 3\n")
        self.assertIn("whitespace-normalized", op.message)

    def test_duplicate_anchor_is_ambiguous(self):
        op = patcher.PatchOperation("test.py", "REPLACE", "Code Block", "a = 1", "a = 2")
        valid, result, _ = patcher.simulate_file_operations("a = 1\na = 1\n", [op], "test.py")
        self.assertFalse(valid)
        self.assertEqual(op.status, "AMBIGUOUS")
        self.assertEqual(result, "a = 1\na = 1\n")

    def test_insert_and_delete_operations_run_in_order(self):
        response = "\n".join([
            patch_block("example.txt", "INSERT_BEFORE", "two", "before"),
            patch_block("example.txt", "INSERT_AFTER", "two", "after"),
            patch_block("example.txt", "DELETE", "three", ""),
        ])
        ops = patcher.parse_multi_file_ai_response(response)
        ok, result, diff = patcher.simulate_file_operations("one\ntwo\nthree\n", ops, "example.txt")
        self.assertTrue(ok)
        self.assertEqual(result, "one\nbefore\ntwo\nafter\n")
        self.assertEqual([op.status for op in ops], ["OK", "OK", "OK"])
        self.assertIn("+before", diff)
        self.assertIn("-three", diff)

    def test_unknown_operation_is_rejected(self):
        ops = patcher.parse_multi_file_ai_response(patch_block("example.txt", "MOVE", "old", "new"))
        ok, _, _ = patcher.simulate_file_operations("old\n", ops, "example.txt")
        self.assertFalse(ok)
        self.assertEqual(ops[0].status, "ERROR")


class Version25DesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt_app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.temp_root = Path(self.temp.name)
        self.root = self.temp_root / "project"
        self.root.mkdir()
        self.original_settings_file = patcher.SETTINGS_FILE
        patcher.SETTINGS_FILE = str(self.temp_root / "settings.json")
        self.window = patcher.SurgicalPatcherV2()
        self.window.txt_project_dir.setText(str(self.root))

    def tearDown(self):
        self.window.close()
        patcher.SETTINGS_FILE = self.original_settings_file
        self.temp.cleanup()

    def analyze(self, patch):
        self.window.scan_project_folder(str(self.root))
        self.window.txt_ai_patch.setPlainText(patch)
        self.qt_app.processEvents()

    def apply(self):
        with mock.patch.object(QMessageBox, "information") as info, \
                mock.patch.object(QMessageBox, "critical") as critical:
            self.window.apply_all_patches()
        critical.assert_not_called()
        return info

    def test_creates_nested_file_then_applies_followup_operation_and_reverts_it(self):
        existing = self.root / "examples" / "new.py"
        existing.parent.mkdir()
        existing.write_text("existing = True\n", encoding="utf-8")
        self.analyze("\n\n".join([
            patch_block("generated/new.py", "CREATE", "Create new file", "def answer():\n    return 1"),
            patch_block("generated/new.py", "REPLACE", "    return 1", "    return 2"),
        ]))
        created = self.root / "generated" / "new.py"
        self.assertTrue(self.window.btn_apply_all.isEnabled())
        self.assertFalse(created.exists(), "Preview should not write files")
        self.assertIn("+    return 2", self.window.txt_diff_preview.toPlainText())
        self.apply()
        self.assertEqual(created.read_text(encoding="utf-8"), "def answer():\n    return 2")
        self.assertEqual(existing.read_text(encoding="utf-8"), "existing = True\n")

        backup_root = self.root / ".patch_backups"
        entries = json.loads((backup_root / "backup_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]["was_created"])
        self.assertTrue(Path(entries[0]["backup_path"]).is_file())

        dialog = patcher.BackupManagerDialog(self.window, str(backup_root))
        try:
            dialog.table.selectRow(0)
            self.qt_app.processEvents()
            self.assertTrue(dialog.btn_restore.isEnabled())
            with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes), \
                    mock.patch.object(QMessageBox, "information"):
                dialog.restore_selected()
            self.assertFalse(created.exists())
            self.assertEqual(existing.read_text(encoding="utf-8"), "existing = True\n")
        finally:
            dialog.close()

    def test_create_never_overwrites_existing_file(self):
        target = self.root / "already.py"
        target.write_text("keep = True\n", encoding="utf-8")
        self.analyze(patch_block("already.py", "CREATE", "Create new file", "destroyed = True"))
        self.assertFalse(self.window.btn_apply_all.isEnabled())
        self.assertNotEqual(self.window.parsed_ops[0].status, "OK")
        self.window.apply_all_patches()
        self.assertEqual(target.read_text(encoding="utf-8"), "keep = True\n")

    def test_create_rejects_existing_unindexed_and_empty_files(self):
        for filename in ("notes.md", "empty.py"):
            with self.subTest(filename=filename):
                target = self.root / filename
                target.write_text("", encoding="utf-8")
                self.analyze(patch_block(filename, "CREATE", "Create new file", "new content"))
                self.assertFalse(self.window.btn_apply_all.isEnabled())
                self.assertNotEqual(self.window.parsed_ops[0].status, "OK")
                self.assertEqual(target.read_text(encoding="utf-8"), "")

    def test_manual_only_create_uses_the_first_selected_file_folder(self):
        selected = self.root / "selected.py"
        selected.write_text("existing = True\n", encoding="utf-8")
        self.window.manual_files = [str(selected)]
        self.window.chk_auto_backup.setChecked(False)
        self.window.txt_ai_patch.setPlainText(
            patch_block("nested/new.py", "CREATE", "Create new file", "created = True")
        )
        self.assertTrue(self.window.btn_apply_all.isEnabled())
        self.apply()
        self.assertEqual((self.root / "nested" / "new.py").read_text(encoding="utf-8"), "created = True")
        self.assertEqual(selected.read_text(encoding="utf-8"), "existing = True\n")

    def test_create_rejects_multiple_creates_for_same_path(self):
        self.analyze("\n\n".join([
            patch_block("new.py", "CREATE", "Create new file", "first = 1"),
            patch_block("new.py", "CREATE", "Create new file", "second = 2"),
        ]))
        self.assertFalse(self.window.btn_apply_all.isEnabled())
        self.assertNotEqual(self.window.parsed_ops[1].status, "OK")
        self.assertFalse((self.root / "new.py").exists())

    def test_create_rejects_path_traversal_and_absolute_paths(self):
        for path in ("../escape.py", "/tmp/escape.py", "C:\\escape.py"):
            with self.subTest(path=path):
                self.analyze(patch_block(path, "CREATE", "Create new file", "bad = True"))
                self.assertFalse(self.window.btn_apply_all.isEnabled())
                self.assertNotEqual(self.window.parsed_ops[0].status, "OK")
        self.assertFalse((self.temp_root / "escape.py").exists())

    def test_create_rejects_symlink_escape(self):
        outside = self.temp_root / "outside"
        outside.mkdir()
        link = self.root / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Directory symlinks unavailable")
        self.analyze(patch_block("linked/escape.py", "CREATE", "Create new file", "bad = True"))
        self.assertFalse(self.window.btn_apply_all.isEnabled())
        self.assertFalse((outside / "escape.py").exists())

    def test_create_rechecks_symlink_changes_after_preview(self):
        outside = self.temp_root / "outside"
        outside.mkdir()
        self.analyze(patch_block("linked/new.py", "CREATE", "Create new file", "bad = True"))
        self.assertTrue(self.window.btn_apply_all.isEnabled())
        try:
            (self.root / "linked").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Directory symlinks unavailable")
        with mock.patch.object(QMessageBox, "warning") as warning, mock.patch.object(QMessageBox, "information"):
            self.window.apply_all_patches()
        warning.assert_called_once()
        self.assertFalse((outside / "new.py").exists())

    def test_create_uses_exclusive_open_if_file_appears_after_preflight(self):
        target = self.root / "new.py"
        self.analyze(patch_block("new.py", "CREATE", "Create new file", "created = True"))
        self.assertTrue(self.window.btn_apply_all.isEnabled())
        real_open = open

        def raced_open(path, mode="r", *args, **kwargs):
            if Path(path) == target and mode == "x":
                target.write_text("external = True\n", encoding="utf-8")
            return real_open(path, mode, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=raced_open), \
                mock.patch.object(QMessageBox, "critical") as critical, \
                mock.patch.object(QMessageBox, "information"):
            self.window.apply_all_patches()
        critical.assert_called_once()
        self.assertEqual(target.read_text(encoding="utf-8"), "external = True\n")
        self.assertFalse((self.root / ".patch_backups").exists())

    def test_create_aborts_if_file_appears_after_preview(self):
        target = self.root / "new.py"
        self.analyze(patch_block("new.py", "CREATE", "Create new file", "created = True"))
        self.assertTrue(self.window.btn_apply_all.isEnabled())
        target.write_text("somebody_else = True\n", encoding="utf-8")
        with mock.patch.object(QMessageBox, "warning") as warning, mock.patch.object(QMessageBox, "information"):
            self.window.apply_all_patches()
        warning.assert_called_once()
        self.assertEqual(target.read_text(encoding="utf-8"), "somebody_else = True\n")
        self.assertFalse((self.root / ".patch_backups").exists())

    def test_external_edit_aborts_the_whole_batch_before_any_write(self):
        existing = self.root / "old.py"
        existing.write_text("value = 1\n", encoding="utf-8")
        self.analyze("\n\n".join([
            patch_block("new.py", "CREATE", "Create new file", "hello = True"),
            patch_block("old.py", "REPLACE", "value = 1", "value = 2"),
        ]))
        self.assertTrue(self.window.btn_apply_all.isEnabled())
        existing.write_text("value = 999\n", encoding="utf-8")
        with mock.patch.object(QMessageBox, "warning") as warning, mock.patch.object(QMessageBox, "information"):
            self.window.apply_all_patches()
        warning.assert_called_once()
        self.assertEqual(existing.read_text(encoding="utf-8"), "value = 999\n")
        self.assertFalse((self.root / "new.py").exists())
        self.assertFalse((self.root / ".patch_backups").exists())

    def test_non_utf8_target_is_rejected_without_data_loss(self):
        target = self.root / "old.py"
        target.write_bytes(b'value = "\xff"\n')
        self.analyze(patch_block("old.py", "REPLACE", 'value = "\ufffd"', 'value = "new"'))
        self.assertFalse(self.window.btn_apply_all.isEnabled())
        self.assertEqual(self.window.parsed_ops[0].status, "READ_ERROR")
        self.assertEqual(target.read_bytes(), b'value = "\xff"\n')

    def test_apply_preserves_crlf_line_endings(self):
        target = self.root / "old.py"
        target.write_bytes(b"value = 1\r\nnext = 2\r\n")
        self.analyze(patch_block("old.py", "REPLACE", "value = 1", "value = 3"))
        self.assertTrue(self.window.btn_apply_all.isEnabled())
        self.apply()
        self.assertEqual(target.read_bytes(), b"value = 3\r\nnext = 2\r\n")

    def test_same_named_files_get_distinct_backups(self):
        first = self.root / "one" / "same.py"
        second = self.root / "two" / "same.py"
        first.parent.mkdir()
        second.parent.mkdir()
        first.write_text("value = 1\n", encoding="utf-8")
        second.write_text("value = 10\n", encoding="utf-8")
        self.analyze("\n\n".join([
            patch_block("one/same.py", "REPLACE", "value = 1", "value = 2"),
            patch_block("two/same.py", "REPLACE", "value = 10", "value = 20"),
        ]))
        self.assertTrue(self.window.btn_apply_all.isEnabled())
        self.apply()
        entries = json.loads((self.root / ".patch_backups" / "backup_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(entries), 2)
        self.assertNotEqual(entries[0]["backup_path"], entries[1]["backup_path"])
        by_original = {entry["original_path"]: Path(entry["backup_path"]).read_text(encoding="utf-8") for entry in entries}
        self.assertEqual(by_original[str(first)], "value = 1\n")
        self.assertEqual(by_original[str(second)], "value = 10\n")

    def test_existing_file_backup_can_be_previewed_and_restored(self):
        source = self.root / "old.py"
        source.write_text("value = 1\n", encoding="utf-8")
        self.analyze(patch_block("old.py", "REPLACE", "value = 1", "value = 2"))
        self.apply()
        backup_root = self.root / ".patch_backups"
        dialog = patcher.BackupManagerDialog(self.window, str(backup_root))
        try:
            dialog.table.selectRow(0)
            self.qt_app.processEvents()
            self.assertIn("-value = 2", dialog.txt_diff.toPlainText())
            self.assertIn("+value = 1", dialog.txt_diff.toPlainText())
            with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes), \
                    mock.patch.object(QMessageBox, "information"):
                dialog.restore_selected()
            self.assertEqual(source.read_text(encoding="utf-8"), "value = 1\n")
        finally:
            dialog.close()

    def test_corrupt_manifest_aborts_create_before_writing(self):
        backup_root = self.root / ".patch_backups"
        backup_root.mkdir()
        manifest = backup_root / "backup_manifest.json"
        manifest.write_text("not valid JSON", encoding="utf-8")
        self.analyze(patch_block("new.py", "CREATE", "Create new file", "new = True"))
        self.assertTrue(self.window.btn_apply_all.isEnabled())
        with mock.patch.object(QMessageBox, "critical") as critical, mock.patch.object(QMessageBox, "information"):
            self.window.apply_all_patches()
        critical.assert_called_once()
        self.assertFalse((self.root / "new.py").exists())
        self.assertEqual(manifest.read_text(encoding="utf-8"), "not valid JSON")

    def test_failed_manifest_replace_preserves_existing_history_and_source(self):
        source = self.root / "old.py"
        source.write_text("value = 1\n", encoding="utf-8")
        backup_root = self.root / ".patch_backups"
        backup_root.mkdir()
        manifest = backup_root / "backup_manifest.json"
        manifest.write_text("[]", encoding="utf-8")
        self.analyze(patch_block("old.py", "REPLACE", "value = 1", "value = 2"))
        with mock.patch.object(patcher.os, "replace", side_effect=OSError("disk error")), \
                mock.patch.object(QMessageBox, "critical") as critical, \
                mock.patch.object(QMessageBox, "information"):
            self.window.apply_all_patches()
        critical.assert_called_once()
        self.assertEqual(source.read_text(encoding="utf-8"), "value = 1\n")
        self.assertEqual(manifest.read_text(encoding="utf-8"), "[]")
        self.assertEqual(list(backup_root.glob(".backup_manifest_*.tmp")), [])
        self.assertEqual(list(backup_root.glob("*.bak")), [])

    def test_corrupt_backup_manifest_is_not_overwritten(self):
        source = self.root / "old.py"
        source.write_text("value = 1\n", encoding="utf-8")
        backup_root = self.root / ".patch_backups"
        backup_root.mkdir()
        manifest = backup_root / "backup_manifest.json"
        manifest.write_text("not valid JSON", encoding="utf-8")
        self.analyze(patch_block("old.py", "REPLACE", "value = 1", "value = 2"))
        self.assertTrue(self.window.btn_apply_all.isEnabled())
        with mock.patch.object(QMessageBox, "critical") as critical, mock.patch.object(QMessageBox, "information"):
            self.window.apply_all_patches()
        critical.assert_called_once()
        self.assertEqual(source.read_text(encoding="utf-8"), "value = 1\n")
        self.assertEqual(manifest.read_text(encoding="utf-8"), "not valid JSON")


if __name__ == "__main__":
    unittest.main()
