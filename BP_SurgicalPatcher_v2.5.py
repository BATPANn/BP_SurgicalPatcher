import os
import sys
import json
import re
import shutil
import difflib
import ntpath
import tempfile
from datetime import datetime
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QColor
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QTextEdit, QFileDialog, QMessageBox,
    QGroupBox, QSplitter, QListWidget, QListWidgetItem, QCheckBox,
    QDialog, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView
)

APP_VERSION = "v2.5"

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SETTINGS_FILE = os.path.join(BASE_DIR, "patcher_v2_settings.json")

AI_PROMPT_INSTRUCTIONS = "\n".join([
    "CRITICAL INSTRUCTION — MULTI-SCRIPT SURGICAL PATCH FORMAT:",
    "Do NOT output entire scripts unless creating a NEW file. Output ONLY the changed, added, or deleted sections.",
    "Wrap your ENTIRE patch response inside a single code fence and use @@ tags (NOT angle brackets) so the chat UI never strips tags.",
    "",
    "Rules:",
    "1. You can patch MULTIPLE scripts in one response. Start each script section with @@FILE: ScriptName.cs@@ (or a subpath like Folders/ScriptName.cs).",
    "2. You can include multiple @@OPERATION: ...@@ blocks under each @@FILE@@.",
    "3. In @@SEARCH@@, include the exact existing lines from the original script. Keep indentation accurate.",
    "",
    "Use this exact structure:",
    "",
    "@@FILE: ExistingScript.cs@@",
    "",
    "@@OPERATION: REPLACE@@",
    "@@TARGET: MethodNameOrDescription@@",
    "@@SEARCH@@",
    "// Exact existing code or full void/function from the original script",
    "@@REPLACE@@",
    "// New updated code to replace the SEARCH block",
    "@@END@@",
    "",
    "@@FILE: NewScript.cs@@",
    "",
    "@@OPERATION: CREATE@@",
    "@@TARGET: Create New File@@",
    "@@SEARCH@@",
    "// Create new file",
    "@@REPLACE@@",
    "// Full new code here...",
    "@@END@@",
    "",
    "Supported Operations:",
    "- @@OPERATION: REPLACE@@ -> Replaces @@SEARCH@@ with @@REPLACE@@.",
    "- @@OPERATION: INSERT_AFTER@@ -> Inserts @@REPLACE@@ immediately after @@SEARCH@@.",
    "- @@OPERATION: INSERT_BEFORE@@ -> Inserts @@REPLACE@@ immediately before @@SEARCH@@.",
    "- @@OPERATION: DELETE@@ -> Deletes the lines in @@SEARCH@@ (leave @@REPLACE@@ empty).",
    "- @@OPERATION: CREATE@@ -> Creates a brand new script. Put full file code in @@REPLACE@@."
])


class PatchOperation:
    def __init__(self, file_name: str, op_type: str, target: str, search_block: str, replace_block: str):
        self.file_name = file_name.strip()
        self.resolved_path = ""
        self.op_type = op_type.upper().strip()
        self.target = target.strip()
        self.search_block = search_block
        self.replace_block = replace_block
        self.status = "PENDING"
        self.message = ""
        self.match_start_line = -1
        self.match_end_line = -1


def strip_code_fences(text: str) -> str:
    lines = text.splitlines()
    while lines and lines[0].strip().startswith("```"):
        lines.pop(0)
    while lines and lines[-1].strip().startswith("```"):
        lines.pop()
    return "\n".join(lines)


def parse_multi_file_ai_response(raw_text: str) -> list[PatchOperation]:
    operations = []
    normalized = re.sub(
        r"<<<\s*(FILE|OPERATION|TARGET|SEARCH|REPLACE|END)(.*?)>>>",
        r"@@\1\2@@",
        raw_text,
        flags=re.IGNORECASE
    )

    file_tokens = re.split(r"@@FILE:\s*(.+?)@@", normalized, flags=re.IGNORECASE)

    op_pattern = re.compile(
        r"@@OPERATION:\s*([A-Z_]+)\s*@@\s*"
        r"(?:@@TARGET:\s*(.*?)\s*@@\s*)?"
        r"@@SEARCH@@\r?\n(.*?)\r?\n@@REPLACE@@(?:\r?\n(.*?))?\r?\n@@END@@",
        re.DOTALL | re.IGNORECASE
    )

    if len(file_tokens) == 1:
        for match in op_pattern.finditer(normalized):
            op_type = match.group(1)
            target = match.group(2) or "Code Block"
            search_blk = strip_code_fences(match.group(3))
            replace_blk = strip_code_fences(match.group(4) or "")
            operations.append(PatchOperation("", op_type, target, search_blk, replace_blk))
        return operations

    pre_text = file_tokens[0]
    for match in op_pattern.finditer(pre_text):
        operations.append(PatchOperation(
            "", match.group(1), match.group(2) or "Code Block",
            strip_code_fences(match.group(3)), strip_code_fences(match.group(4) or "")
        ))

    for i in range(1, len(file_tokens), 2):
        f_name = file_tokens[i].strip()
        section_body = file_tokens[i + 1] if i + 1 < len(file_tokens) else ""
        for match in op_pattern.finditer(section_body):
            op_type = match.group(1)
            target = match.group(2) or "Code Block"
            search_blk = strip_code_fences(match.group(3))
            replace_blk = strip_code_fences(match.group(4) or "")
            operations.append(PatchOperation(f_name, op_type, target, search_blk, replace_blk))

    return operations


def find_csharp_method_span(source_lines: list[str], method_signature: str) -> list[tuple[int, int]]:
    clean_sig = method_signature.strip()
    func_name_match = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(", clean_sig)
    func_name = func_name_match.group(1) if func_name_match else clean_sig
    results = []
    n = len(source_lines)
    i = 0
    while i < n:
        line = source_lines[i]
        if re.search(r"\b" + re.escape(func_name) + r"\s*\(", line):
            stripped = line.strip()
            if (
                not stripped.endswith(";")
                and not stripped.startswith("//")
                and not re.match(r"^(if|while|for|foreach|switch|catch)\b", stripped)
            ):
                start_idx = i
                while start_idx > 0 and source_lines[start_idx - 1].strip().startswith("["):
                    start_idx -= 1
                brace_depth = 0
                found_open_brace = False
                in_string = False
                string_char = ""
                in_block_comment = False
                for j in range(i, n):
                    cur = source_lines[j]
                    k = 0
                    line_len = len(cur)
                    while k < line_len:
                        ch = cur[k]
                        nxt = cur[k + 1] if k + 1 < line_len else ""
                        if in_block_comment:
                            if ch == "*" and nxt == "/":
                                in_block_comment = False
                                k += 2
                                continue
                            k += 1
                            continue
                        if in_string:
                            if ch == "\\" and k + 1 < line_len:
                                k += 2
                                continue
                            if ch == string_char:
                                in_string = False
                            k += 1
                            continue
                        if ch == "/" and nxt == "/":
                            break
                        if ch == "/" and nxt == "*":
                            in_block_comment = True
                            k += 2
                            continue
                        if ch in ('"', "'"):
                            in_string = True
                            string_char = ch
                            k += 1
                            continue
                        if ch == "{":
                            brace_depth += 1
                            found_open_brace = True
                        elif ch == "}":
                            brace_depth -= 1
                            if found_open_brace and brace_depth == 0:
                                results.append((start_idx, j))
                                i = j
                                break
                        k += 1
                    if found_open_brace and brace_depth == 0:
                        break
        i += 1
    return results


def locate_block_in_lines(source_lines: list[str], search_text: str, target_hint: str) -> tuple[str, int, int, str]:
    search_lines = search_text.splitlines()
    if not search_lines:
        return ("NOT_FOUND", -1, -1, "Search block is empty.")

    norm_source = [ln.rstrip() for ln in source_lines]
    norm_search = [ln.rstrip() for ln in search_lines]
    m_len = len(norm_search)
    matches = []
    for idx in range(len(norm_source) - m_len + 1):
        if norm_source[idx:idx + m_len] == norm_search:
            matches.append((idx, idx + m_len - 1))
    if len(matches) == 1:
        s, e = matches[0]
        return ("OK", s, e, f"Exact match L{s + 1}–L{e + 1}")
    elif len(matches) > 1:
        locs = ", ".join([f"L{s + 1}" for s, _ in matches])
        return ("AMBIGUOUS", -1, -1, f"Multiple matches ({locs}). Needs more context lines.")

    strip_source = [ln.strip() for ln in source_lines]
    strip_search = [ln.strip() for ln in search_lines]
    matches_pass2 = []
    for idx in range(len(strip_source) - m_len + 1):
        if strip_source[idx:idx + m_len] == strip_search:
            matches_pass2.append((idx, idx + m_len - 1))
    if len(matches_pass2) == 1:
        s, e = matches_pass2[0]
        return ("OK", s, e, f"Matched (indent-adjusted) L{s + 1}–L{e + 1}")
    elif len(matches_pass2) > 1:
        locs = ", ".join([f"L{s + 1}" for s, _ in matches_pass2])
        return ("AMBIGUOUS", -1, -1, f"Multiple matches ({locs}).")

    non_empty_search = [ln.strip() for ln in search_lines if ln.strip()]
    if non_empty_search:
        ne_matches = []
        s_count = len(non_empty_search)
        ne_source_indices = [(idx, ln.strip()) for idx, ln in enumerate(source_lines) if ln.strip()]
        for i in range(len(ne_source_indices) - s_count + 1):
            window = [item[1] for item in ne_source_indices[i:i + s_count]]
            if window == non_empty_search:
                start_line = ne_source_indices[i][0]
                end_line = ne_source_indices[i + s_count - 1][0]
                ne_matches.append((start_line, end_line))
        if len(ne_matches) == 1:
            s, e = ne_matches[0]
            return ("OK", s, e, f"Matched (whitespace-normalized) L{s + 1}–L{e + 1}")
        elif len(ne_matches) > 1:
            locs = ", ".join([f"L{s + 1}" for s, _ in ne_matches])
            return ("AMBIGUOUS", -1, -1, f"Multiple matches ({locs}).")

    if target_hint and target_hint.lower() != "code block":
        brace_matches = find_csharp_method_span(source_lines, target_hint)
        if len(brace_matches) == 1:
            s, e = brace_matches[0]
            return ("OK", s, e, f"Brace-Scanner matched '{target_hint}' L{s + 1}–L{e + 1}")
        elif len(brace_matches) > 1:
            locs = ", ".join([f"L{s + 1}" for s, _ in brace_matches])
            return ("AMBIGUOUS", -1, -1, f"Multiple overloaded methods for '{target_hint}' ({locs}).")

    return ("NOT_FOUND", -1, -1, "Target block not found in script.")


def simulate_file_operations(
    original_text: str, file_ops: list[PatchOperation], display_name: str, *, is_new_file: bool = False
) -> tuple[bool, str, str]:
    has_crlf = "\r\n" in original_text
    line_sep = "\r\n" if has_crlf else "\n"
    working_lines = original_text.splitlines()
    file_ok = True
    file_exists = not is_new_file

    for op in file_ops:
        if op.op_type == "CREATE":
            if file_exists:
                op.status = "ERROR"
                op.message = "CREATE requires a new file; this path already exists or was already created."
                file_ok = False
                continue
            file_exists = True
            op.status = "OK"
            op.message = "Will create new file with provided content."
            working_lines = op.replace_block.splitlines()
            op.match_start_line = 1
            op.match_end_line = len(working_lines)
            continue

        status, s_idx, e_idx, msg = locate_block_in_lines(working_lines, op.search_block, op.target)
        op.status = status
        op.message = msg
        op.match_start_line = s_idx + 1 if s_idx >= 0 else -1
        op.match_end_line = e_idx + 1 if e_idx >= 0 else -1

        if status != "OK":
            file_ok = False
            continue

        orig_first_line = working_lines[s_idx]
        orig_indent = len(orig_first_line) - len(orig_first_line.lstrip(" \t"))
        non_empty_search = [ln for ln in op.search_block.splitlines() if ln.strip()]
        search_first = non_empty_search[0] if non_empty_search else ""
        search_indent = len(search_first) - len(search_first.lstrip(" \t"))
        indent_delta = max(0, orig_indent - search_indent)
        indent_prefix = " " * indent_delta

        rep_lines_raw = op.replace_block.splitlines()
        rep_lines = [
            (indent_prefix + ln if ln.strip() else ln) if indent_delta > 0 else ln
            for ln in rep_lines_raw
        ]

        if op.op_type == "REPLACE":
            working_lines = working_lines[:s_idx] + rep_lines + working_lines[e_idx + 1:]
        elif op.op_type == "INSERT_AFTER":
            working_lines = working_lines[:e_idx + 1] + rep_lines + working_lines[e_idx + 1:]
        elif op.op_type == "INSERT_BEFORE":
            working_lines = working_lines[:s_idx] + rep_lines + working_lines[s_idx:]
        elif op.op_type == "DELETE":
            working_lines = working_lines[:s_idx] + working_lines[e_idx + 1:]
        else:
            op.status = "ERROR"
            op.message = f"Unknown operation: {op.op_type}"
            file_ok = False

    new_text = line_sep.join(working_lines)
    if original_text.endswith(("\n", "\r\n")) and not new_text.endswith(("\n", "\r\n")) and original_text:
        new_text += line_sep

    diff_lines = list(difflib.unified_diff(
        original_text.splitlines(),
        new_text.splitlines(),
        fromfile=f"a/{display_name}",
        tofile=f"b/{display_name}",
        lineterm=""
    ))
    diff_text = "\n".join(diff_lines)
    return file_ok, new_text, diff_text


class BackupManagerDialog(QDialog):
    def __init__(self, parent, backup_root_dir: str):
        super().__init__(parent)
        self.setWindowTitle("🗂️ Surgical Backup Manager — Selectively Revert Any Script")
        self.resize(980, 620)
        self.backup_root_dir = backup_root_dir
        self.manifest_path = os.path.join(self.backup_root_dir, "backup_manifest.json")
        self.entries = []
        self.setup_ui()
        self.load_backups()

    def setup_ui(self):
        layout = QVBoxLayout(self)
        info_lbl = QLabel("Select any backup snapshot below to inspect its code diff and restore that single script.")
        info_lbl.setStyleSheet("color: #a1a1aa; font-weight: bold;")
        layout.addWidget(info_lbl)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Timestamp", "Script Name", "Operations Applied", "Original Script Path"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self.on_selection_changed)
        splitter.addWidget(self.table)

        diff_box = QGroupBox("Diff Preview: Current Live File vs. Selected Backup Snapshot")
        diff_layout = QVBoxLayout(diff_box)
        self.txt_diff = QTextEdit()
        self.txt_diff.setReadOnly(True)
        self.txt_diff.setFont(QFont("Consolas", 10))
        diff_layout.addWidget(self.txt_diff)
        splitter.addWidget(diff_box)

        splitter.setSizes([260, 320])
        layout.addWidget(splitter)

        btn_row = QHBoxLayout()
        self.btn_restore = QPushButton("↩️ Revert Selected Script to This Backup")
        self.btn_restore.setObjectName("undo_btn")
        self.btn_restore.setEnabled(False)
        self.btn_restore.clicked.connect(self.restore_selected)

        self.btn_delete_bak = QPushButton("🗑️ Delete Selected Backup")
        self.btn_delete_bak.setEnabled(False)
        self.btn_delete_bak.clicked.connect(self.delete_selected)

        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)

        btn_row.addWidget(self.btn_restore)
        btn_row.addWidget(self.btn_delete_bak)
        btn_row.addStretch()
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)

    def load_backups(self):
        self.table.setRowCount(0)
        self.entries = []
        if not os.path.isfile(self.manifest_path):
            return
        try:
            with open(self.manifest_path, "r", encoding="utf-8") as f:
                raw_list = json.load(f)
            for item in reversed(raw_list):
                if os.path.isfile(item.get("backup_path", "")):
                    self.entries.append(item)

            for row_idx, entry in enumerate(self.entries):
                self.table.insertRow(row_idx)
                self.table.setItem(row_idx, 0, QTableWidgetItem(entry.get("timestamp", "")))
                self.table.setItem(row_idx, 1, QTableWidgetItem(entry.get("script_name", "")))
                self.table.setItem(row_idx, 2, QTableWidgetItem(str(entry.get("ops_summary", ""))))
                self.table.setItem(row_idx, 3, QTableWidgetItem(entry.get("original_path", "")))
        except Exception:
            pass

    def on_selection_changed(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            self.btn_restore.setEnabled(False)
            self.btn_delete_bak.setEnabled(False)
            self.txt_diff.clear()
            return

        idx = rows[0].row()
        entry = self.entries[idx]
        self.btn_restore.setEnabled(True)
        self.btn_delete_bak.setEnabled(True)

        orig_path = entry.get("original_path", "")
        bak_path = entry.get("backup_path", "")

        try:
            with open(bak_path, "r", encoding="utf-8", errors="replace") as f:
                bak_lines = f.read().splitlines()
            if os.path.isfile(orig_path):
                with open(orig_path, "r", encoding="utf-8", errors="replace") as f:
                    cur_lines = f.read().splitlines()
            else:
                cur_lines = []

            diff = list(difflib.unified_diff(
                cur_lines, bak_lines,
                fromfile="Current Live Script",
                tofile=f"Backup Snapshot ({entry.get('timestamp', '')})",
                lineterm=""
            ))
            if not diff:
                self.txt_diff.setHtml("<span style='color:#4ade80;'>Current file is identical to this backup snapshot.</span>")
            else:
                self.render_diff("\n".join(diff))
        except Exception as e:
            self.txt_diff.setPlainText(f"Error comparing files: {e}")

    def render_diff(self, diff_text: str):
        html_lines = ["<pre style='font-family: Consolas, monospace; font-size: 12px;'>"]
        for line in diff_text.splitlines():
            esc = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            if line.startswith("+") and not line.startswith("+++"):
                html_lines.append(f"<span style='color: #4ade80; background-color: #142e1f;'>{esc}</span>")
            elif line.startswith("-") and not line.startswith("---"):
                html_lines.append(f"<span style='color: #f87171; background-color: #361717;'>{esc}</span>")
            elif line.startswith("@@"):
                html_lines.append(f"<span style='color: #38bdf8; font-weight: bold;'>{esc}</span>")
            else:
                html_lines.append(f"<span style='color: #a1a1aa;'>{esc}</span>")
        html_lines.append("</pre>")
        self.txt_diff.setHtml("\n".join(html_lines))

    def restore_selected(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        entry = self.entries[rows[0].row()]
        orig_path = entry.get("original_path", "")
        bak_path = entry.get("backup_path", "")

        reply = QMessageBox.question(
            self, "Confirm Revert",
            f"Restore '{entry.get('script_name')}' to snapshot from {entry.get('timestamp')}?\n\nTarget: {orig_path}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            try:
                os.makedirs(os.path.dirname(orig_path), exist_ok=True)
                was_created = entry.get(
                    "was_created", entry.get("ops_summary", "").endswith(" [CREATED]")
                )
                if os.path.isfile(bak_path) and os.path.getsize(bak_path) == 0 and was_created:
                    if os.path.exists(orig_path):
                        os.remove(orig_path)
                    QMessageBox.information(self, "Reverted", f"Successfully deleted created script:\n{orig_path}")
                else:
                    shutil.copy2(bak_path, orig_path)
                    QMessageBox.information(self, "Reverted", f"Successfully restored:\n{orig_path}")
                self.on_selection_changed()
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Could not restore backup:\n{e}")

    def delete_selected(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        entry = self.entries[rows[0].row()]
        bak_path = entry.get("backup_path", "")
        try:
            if os.path.isfile(bak_path):
                os.remove(bak_path)
            if os.path.isfile(self.manifest_path):
                with open(self.manifest_path, "r", encoding="utf-8") as f:
                    raw_list = json.load(f)
                raw_list = [x for x in raw_list if x.get("backup_path") != bak_path]
                with open(self.manifest_path, "w", encoding="utf-8") as f:
                    json.dump(raw_list, f, indent=2)
            self.load_backups()
            self.txt_diff.clear()
        except Exception as e:
            QMessageBox.warning(self, "Delete Error", f"Failed to delete backup:\n{e}")


class SurgicalPatcherV2(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"🦇 BATPAN SurgicalPatcher {APP_VERSION} — Multi-Script Batch AI Code Patcher")
        self.resize(1220, 880)

        self.project_folder = ""
        self.indexed_files = []
        self.manual_files = []
        self.parsed_ops = []
        self.simulated_files = {}
        # None means the file did not exist when the preview was generated.
        self.original_files: dict[str, str | None] = {}
        self.file_diffs = {}

        self.setup_ui()
        self.apply_theme()
        self.load_settings()

    def apply_theme(self):
        self.setStyleSheet("""
            QMainWindow, QWidget, QDialog { background-color: #121215; color: #e4e4e7; font-family: 'Segoe UI', sans-serif; font-size: 13px; }
            QGroupBox { border: 1px solid #27272a; border-radius: 6px; margin-top: 10px; font-weight: bold; padding: 10px; }
            QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; padding: 0 6px; color: #a1a1aa; }
            QLineEdit, QTextEdit, QListWidget, QTableWidget { background-color: #18181c; border: 1px solid #27272a; border-radius: 5px; color: #fafafa; padding: 6px; }
            QHeaderView::section { background-color: #27272a; color: #e4e4e7; padding: 5px; border: 1px solid #3f3f46; font-weight: bold; }
            QLineEdit:focus, QTextEdit:focus, QListWidget:focus { border: 1px solid #6366f1; }
            QPushButton { background-color: #27272a; border: 1px solid #3f3f46; border-radius: 5px; padding: 7px 14px; font-weight: bold; color: #fafafa; }
            QPushButton:hover { background-color: #3f3f46; }
            QPushButton#prompt_btn { background-color: #1e1b4b; border: 1px solid #6366f1; color: #c7d2fe; font-size: 13px; }
            QPushButton#prompt_btn:hover { background-color: #312e81; color: #ffffff; }
            QPushButton#apply_btn { background-color: #15803d; border: 1px solid #22c55e; color: #ffffff; font-size: 14px; padding: 8px 18px; }
            QPushButton#apply_btn:hover { background-color: #16a34a; }
            QPushButton#apply_btn:disabled { background-color: #1f2937; border-color: #374151; color: #6b7280; }
            QPushButton#undo_btn { background-color: #7f1d1d; border: 1px solid #ef4444; color: #fecaca; }
            QPushButton#undo_btn:hover { background-color: #991b1b; color: #ffffff; }
        """)

    def setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(10)

        top_bar = QHBoxLayout()
        lbl_title = QLabel(f"⚡ Multi-Script Surgical Code Patcher ({APP_VERSION})")
        lbl_title.setStyleSheet("font-size: 15px; font-weight: bold; color: #38bdf8;")

        self.btn_copy_prompt = QPushButton("📋 Copy AI System Prompt (Supports New Files & HTML-Proof)")
        self.btn_copy_prompt.setObjectName("prompt_btn")
        self.btn_copy_prompt.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_copy_prompt.clicked.connect(self.copy_ai_prompt)

        top_bar.addWidget(lbl_title)
        top_bar.addStretch()
        top_bar.addWidget(self.btn_copy_prompt)
        main_layout.addLayout(top_bar)

        file_box = QGroupBox("1. Project Folder Auto-Search & Multi-Script Selection")
        file_layout = QVBoxLayout(file_box)
        proj_row = QHBoxLayout()
        proj_row.addWidget(QLabel("Search Folder:"))
        self.txt_project_dir = QLineEdit()
        self.txt_project_dir.setPlaceholderText("Select your Unity / Game project folder (App will automatically find or CREATE scripts inside)...")
        self.txt_project_dir.editingFinished.connect(self.on_folder_text_edited)
        btn_browse_proj = QPushButton("📁 Browse Project Folder")
        btn_browse_proj.clicked.connect(self.browse_project_folder)
        btn_rescan = QPushButton("🔄 Rescan")
        btn_rescan.clicked.connect(lambda: self.scan_project_folder(self.txt_project_dir.text().strip()))
        proj_row.addWidget(self.txt_project_dir, stretch=1)
        proj_row.addWidget(btn_browse_proj)
        proj_row.addWidget(btn_rescan)
        file_layout.addLayout(proj_row)

        pick_row = QHBoxLayout()
        btn_pick_multi = QPushButton("📄 Select Specific Script(s)...")
        btn_pick_multi.clicked.connect(self.pick_multiple_scripts)
        btn_clear_manual = QPushButton("Clear Selected Scripts")
        btn_clear_manual.clicked.connect(self.clear_manual_scripts)
        self.lbl_index_status = QLabel("No folder or scripts loaded yet.")
        self.lbl_index_status.setStyleSheet("color: #4ade80; font-weight: bold;")
        pick_row.addWidget(btn_pick_multi)
        pick_row.addWidget(btn_clear_manual)
        pick_row.addWidget(self.lbl_index_status, stretch=1)
        file_layout.addLayout(pick_row)
        main_layout.addWidget(file_box)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        left_box = QGroupBox("2. Paste Full AI Answer (Supports 1 or Many Scripts at Once)")
        left_layout = QVBoxLayout(left_box)
        paste_btn_row = QHBoxLayout()
        btn_paste_verify = QPushButton("📋 Paste from Clipboard & Auto-Locate Scripts")
        btn_paste_verify.setObjectName("prompt_btn")
        btn_paste_verify.clicked.connect(self.paste_and_verify)
        btn_clear_patch = QPushButton("Clear")
        btn_clear_patch.clicked.connect(self.clear_patch_input)
        paste_btn_row.addWidget(btn_paste_verify, stretch=1)
        paste_btn_row.addWidget(btn_clear_patch)
        left_layout.addLayout(paste_btn_row)

        self.txt_ai_patch = QTextEdit()
        self.txt_ai_patch.setFont(QFont("Consolas", 10))
        self.txt_ai_patch.setPlaceholderText(
            "Paste the AI's full multi-script patch response here.\n\n"
            "The app will automatically read every @@FILE: ScriptName.cs@@ tag, search your project folder, "
            "create NEW files if requested, verify operations, and prepare all files for 1-click patching."
        )
        self.txt_ai_patch.textChanged.connect(self.analyze_current_patch)
        left_layout.addWidget(self.txt_ai_patch)
        splitter.addWidget(left_box)

        right_box = QGroupBox("3. Multi-Script Match Status & Live Diff Preview")
        right_layout = QVBoxLayout(right_box)
        right_layout.addWidget(QLabel("Detected Operations Across Scripts (Click an item to filter diff, or view all):"))
        self.list_ops = QListWidget()
        self.list_ops.setFixedHeight(165)
        self.list_ops.setFont(QFont("Consolas", 10))
        self.list_ops.itemClicked.connect(self.on_op_item_clicked)
        right_layout.addWidget(self.list_ops)

        diff_hdr = QHBoxLayout()
        diff_hdr.addWidget(QLabel("Live Code Diff Preview (- Removed / + Added):"))
        btn_show_all_diffs = QPushButton("Show All Scripts Diff")
        btn_show_all_diffs.clicked.connect(self.show_all_diffs)
        diff_hdr.addStretch()
        diff_hdr.addWidget(btn_show_all_diffs)
        right_layout.addLayout(diff_hdr)

        self.txt_diff_preview = QTextEdit()
        self.txt_diff_preview.setReadOnly(True)
        self.txt_diff_preview.setFont(QFont("Consolas", 10))
        right_layout.addWidget(self.txt_diff_preview)

        splitter.addWidget(right_box)
        splitter.setSizes([520, 700])
        main_layout.addWidget(splitter, stretch=1)

        bottom_bar = QHBoxLayout()
        self.chk_auto_backup = QCheckBox("Save automatic per-script backups before patching")
        self.chk_auto_backup.setChecked(True)
        self.chk_auto_backup.setStyleSheet("color: #4ade80; font-weight: bold;")
        self.chk_auto_backup.stateChanged.connect(self.save_settings)
        self.btn_backup_mgr = QPushButton("🗂️ Backup Manager (Revert Any Script)")
        self.btn_backup_mgr.setObjectName("undo_btn")
        self.btn_backup_mgr.clicked.connect(self.open_backup_manager)
        self.btn_apply_all = QPushButton("✅ Apply All Verified Changes (0 Scripts)")
        self.btn_apply_all.setObjectName("apply_btn")
        self.btn_apply_all.setEnabled(False)
        self.btn_apply_all.clicked.connect(self.apply_all_patches)

        bottom_bar.addWidget(self.chk_auto_backup)
        bottom_bar.addStretch()
        bottom_bar.addWidget(self.btn_backup_mgr)
        bottom_bar.addWidget(self.btn_apply_all)
        main_layout.addLayout(bottom_bar)

    def copy_ai_prompt(self):
        QApplication.clipboard().setText(AI_PROMPT_INSTRUCTIONS)
        self.btn_copy_prompt.setText("✅ Copied Prompt! Paste it to Gemini / ChatGPT")
        QTimer.singleShot(3000, lambda: self.btn_copy_prompt.setText("📋 Copy AI System Prompt (Supports New Files & HTML-Proof)"))

    def on_folder_text_edited(self):
        folder = self.txt_project_dir.text().strip()
        self.scan_project_folder(folder)
        self.save_settings()

    def browse_project_folder(self):
        start_dir = self.txt_project_dir.text().strip()
        if not start_dir or not os.path.isdir(start_dir):
            start_dir = os.path.expanduser("~/Desktop")
        folder = QFileDialog.getExistingDirectory(self, "Select Project Root Folder", start_dir)
        if folder:
            self.txt_project_dir.setText(folder)
            self.scan_project_folder(folder)
            self.save_settings()

    def scan_project_folder(self, folder: str):
        self.indexed_files.clear()
        if not folder or not os.path.isdir(folder):
            self.project_folder = ""
            self.update_index_label()
            self.analyze_current_patch()
            return
        self.project_folder = os.path.abspath(folder)
        exts = {".cs", ".py", ".pyw", ".js", ".ts", ".cpp", ".h", ".gd", ".json", ".shader", ".txt", ".xml"}
        ignore_dirs = {".git", "__pycache__", "Library", "Temp", "Obj", "Build", ".patch_backups", "node_modules", ".vs"}
        for root, dirs, files in os.walk(self.project_folder):
            dirs[:] = [d for d in dirs if d not in ignore_dirs]
            for fn in files:
                if os.path.splitext(fn)[1].lower() in exts:
                    self.indexed_files.append(os.path.join(root, fn))
        self.update_index_label()
        self.analyze_current_patch()

    def pick_multiple_scripts(self):
        start_dir = self.project_folder if os.path.isdir(self.project_folder) else os.path.expanduser("~/Desktop")
        files, _ = QFileDialog.getOpenFileNames(
            self, "Select One or Multiple Scripts", start_dir,
            "Code Files (*.cs *.py *.pyw *.js *.ts *.cpp *.h *.gd *.shader);;All Files (*.*)"
        )
        if files:
            for f in files:
                norm_f = os.path.abspath(f)
                if norm_f not in self.manual_files:
                    self.manual_files.append(norm_f)
            self.update_index_label()
            self.save_settings()
            self.analyze_current_patch()

    def clear_manual_scripts(self):
        self.manual_files.clear()
        self.update_index_label()
        self.save_settings()
        self.analyze_current_patch()

    def update_index_label(self):
        parts = []
        if self.indexed_files:
            parts.append(f"📁 Auto-Search Folder: {len(self.indexed_files)} scripts indexed")
        if self.manual_files:
            names = ", ".join([os.path.basename(p) for p in self.manual_files[:4]])
            if len(self.manual_files) > 4:
                names += f" (+{len(self.manual_files) - 4} more)"
            parts.append(f"📄 Manually Selected: {len(self.manual_files)} ({names})")
        if parts:
            self.lbl_index_status.setText(" | ".join(parts))
            self.lbl_index_status.setStyleSheet("color: #4ade80; font-weight: bold;")
        else:
            self.lbl_index_status.setText("No folder or scripts selected. Browse a project folder or pick scripts.")
            self.lbl_index_status.setStyleSheet("color: #facc15; font-weight: bold;")

    def resolve_new_script_path(self, file_hint: str) -> tuple[str, str]:
        """Resolve a new file beneath the selected project (or first selected file's folder)."""
        hint = file_hint.strip().replace("\\", "/")
        parts = hint.split("/")
        if (
            not hint or hint.endswith("/") or "\x00" in hint or hint.startswith("/")
            or ntpath.splitdrive(hint)[0] or ".." in parts
        ):
            return ("ERROR", "CREATE needs a project-relative file path without '..' or an absolute path.")

        if self.project_folder and os.path.isdir(self.project_folder):
            base_dir = self.project_folder
        elif self.manual_files and os.path.isdir(os.path.dirname(self.manual_files[0])):
            base_dir = os.path.dirname(self.manual_files[0])
        else:
            return ("FILE_NOT_FOUND", "Select a project folder or a script before creating a file.")

        base_dir = os.path.realpath(base_dir)
        candidate = os.path.realpath(os.path.join(base_dir, hint))
        try:
            within_base = os.path.normcase(os.path.commonpath([base_dir, candidate])) == os.path.normcase(base_dir)
        except ValueError:
            within_base = False
        if not within_base or os.path.normcase(candidate) == os.path.normcase(base_dir):
            return ("ERROR", "The new file must remain inside the selected base folder, including through symlinks.")
        return ("NEW_FILE", candidate)

    def resolve_script_path(self, file_hint: str, *, for_create: bool = False) -> tuple[str, str]:
        if for_create:
            return self.resolve_new_script_path(file_hint)

        pool = list(dict.fromkeys(self.manual_files + self.indexed_files))
        if not file_hint:
            if len(self.manual_files) == 1:
                return ("OK", self.manual_files[0])
            if len(pool) == 1:
                return ("OK", pool[0])
            return ("ERROR", "No @@FILE: ScriptName@@ tag in patch, and multiple/zero scripts are loaded.")

        clean_hint = file_hint.replace("\\", "/").strip("/").lower()
        if not clean_hint:
            return ("ERROR", "The file path is empty.")

        # Prefer manually selected files, but never silently choose between
        # duplicates or fall back from a subpath to an unrelated basename.
        for candidates in (self.manual_files, pool):
            matches = [
                path for path in candidates
                if path.replace("\\", "/").lower() == clean_hint
                or path.replace("\\", "/").lower().endswith("/" + clean_hint)
            ]
            if len(matches) == 1:
                return ("OK", matches[0])
            if len(matches) > 1:
                return ("AMBIGUOUS_FILE", f"Multiple matches for '{file_hint}'. Specify a subpath or select manually.")

        return self.resolve_new_script_path(file_hint)

    def paste_and_verify(self):
        clip_text = QApplication.clipboard().text()
        if not clip_text.strip():
            QMessageBox.warning(self, "Clipboard Empty", "Clipboard is empty! Copy the AI's response first.")
            return
        self.txt_ai_patch.setPlainText(clip_text)

    def clear_patch_input(self):
        self.txt_ai_patch.clear()
        self.list_ops.clear()
        self.txt_diff_preview.clear()
        self.parsed_ops.clear()
        self.simulated_files.clear()
        self.original_files.clear()
        self.file_diffs.clear()
        self.btn_apply_all.setEnabled(False)
        self.btn_apply_all.setText("✅ Apply All Verified Changes (0 Scripts)")

    def analyze_current_patch(self):
        raw_patch = self.txt_ai_patch.toPlainText().strip()
        self.list_ops.clear()
        self.txt_diff_preview.clear()
        self.simulated_files.clear()
        self.original_files.clear()
        self.file_diffs.clear()
        self.btn_apply_all.setEnabled(False)
        self.btn_apply_all.setText("✅ Apply All Verified Changes (0 Scripts)")

        if not raw_patch:
            return

        if "<<<>>>" in raw_patch:
            warn_item = QListWidgetItem(
                "⚠️ Detected '<<<>>>' (Browser stripped angle-bracket tags!). Click 'Copy AI System Prompt' to use @@ tags."
            )
            warn_item.setForeground(QColor("#facc15"))
            self.list_ops.addItem(warn_item)

        self.parsed_ops = parse_multi_file_ai_response(raw_patch)
        if not self.parsed_ops:
            item = QListWidgetItem("❌ No valid @@OPERATION: ...@@ or <<<OPERATION: ...>>> blocks found.")
            item.setForeground(QColor("#f87171"))
            self.list_ops.addItem(item)
            return

        ops_by_file: dict[str, list[PatchOperation]] = {}
        all_valid = True

        def hint_key(value: str) -> str:
            return os.path.normcase(value.strip().replace("\\", "/"))

        # A CREATE section targets the requested relative path, even if a file
        # with the same basename exists elsewhere in the project.
        create_hints = {hint_key(op.file_name) for op in self.parsed_ops if op.op_type == "CREATE"}
        for op in self.parsed_ops:
            f_status, resolved = self.resolve_script_path(
                op.file_name, for_create=hint_key(op.file_name) in create_hints
            )
            if f_status not in ("OK", "NEW_FILE"):
                op.status = f_status
                op.message = resolved
                all_valid = False
            else:
                op.resolved_path = resolved
                ops_by_file.setdefault(resolved, []).append(op)

        for file_path, file_ops in ops_by_file.items():
            is_new_file = not os.path.lexists(file_path)
            if is_new_file:
                orig_text = ""
            else:
                try:
                    # Reject non-UTF-8 rather than silently replacing bytes, and
                    # retain uniform LF/CRLF endings for the preview and write.
                    with open(file_path, "r", encoding="utf-8", newline="") as f:
                        orig_text = f.read()
                except (OSError, UnicodeError) as e:
                    for op in file_ops:
                        op.status = "READ_ERROR"
                        op.message = f"Cannot read file: {e}"
                    all_valid = False
                    continue

            disp_name = os.path.basename(file_path)
            f_ok, new_text, diff_text = simulate_file_operations(
                orig_text, file_ops, disp_name, is_new_file=is_new_file
            )
            if not f_ok:
                all_valid = False
            self.original_files[file_path] = None if is_new_file else orig_text
            self.simulated_files[file_path] = new_text
            self.file_diffs[file_path] = diff_text

        for idx, op in enumerate(self.parsed_ops, 1):
            script_label = os.path.basename(op.resolved_path) if op.resolved_path else (op.file_name or "Unknown Script")
            if op.resolved_path and not os.path.exists(op.resolved_path) and op.status == "OK":
                script_label += " [NEW FILE]"

            if op.status == "OK":
                icon = "✅"
                color = QColor("#4ade80")
            elif "AMBIGUOUS" in op.status:
                icon = "⚠️"
                color = QColor("#facc15")
            else:
                icon = "❌"
                color = QColor("#f87171")

            item = QListWidgetItem(f"{icon} [{idx}] {script_label} :: {op.op_type} ({op.target}) — {op.message}")
            item.setData(Qt.ItemDataRole.UserRole, op.resolved_path)
            item.setForeground(color)
            self.list_ops.addItem(item)

        self.show_all_diffs()

        if all_valid and self.simulated_files:
            num_files = len(self.simulated_files)
            num_ops = len(self.parsed_ops)
            self.btn_apply_all.setEnabled(True)
            self.btn_apply_all.setText(f"✅ Apply All Verified Changes ({num_ops} Ops across {num_files} Script{'s' if num_files > 1 else ''})")

    def on_op_item_clicked(self, item: QListWidgetItem):
        file_path = item.data(Qt.ItemDataRole.UserRole)
        if file_path and file_path in self.file_diffs:
            diff_txt = self.file_diffs[file_path]
            self.render_colored_diff(diff_txt if diff_txt else f"No line changes in {os.path.basename(file_path)}.")

    def show_all_diffs(self):
        if not self.file_diffs:
            return
        combined = []
        for f_path, d_text in self.file_diffs.items():
            header = f"==================== {os.path.basename(f_path)} ({f_path}) ===================="
            combined.append(header)
            combined.append(d_text if d_text else "(No changes)")
            combined.append("")
        self.render_colored_diff("\n".join(combined))

    def render_colored_diff(self, diff_text: str):
        html_lines = ["<pre style='font-family: Consolas, monospace; font-size: 12px;'>"]
        for line in diff_text.splitlines():
            escaped = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            if line.startswith("===================="):
                html_lines.append(f"<span style='color: #facc15; background-color: #27272a; font-weight: bold;'>{escaped}</span>")
            elif line.startswith("+") and not line.startswith("+++"):
                html_lines.append(f"<span style='color: #4ade80; background-color: #142e1f;'>{escaped}</span>")
            elif line.startswith("-") and not line.startswith("---"):
                html_lines.append(f"<span style='color: #f87171; background-color: #361717;'>{escaped}</span>")
            elif line.startswith("@@"):
                html_lines.append(f"<span style='color: #38bdf8; font-weight: bold;'>{escaped}</span>")
            else:
                html_lines.append(f"<span style='color: #a1a1aa;'>{escaped}</span>")
        html_lines.append("</pre>")
        self.txt_diff_preview.setHtml("\n".join(html_lines))

    def get_backup_root_dir(self) -> str:
        if self.project_folder and os.path.isdir(self.project_folder):
            return os.path.join(self.project_folder, ".patch_backups")
        return os.path.join(BASE_DIR, ".patch_backups")

    def record_backup_entry(self, original_path: str, backup_path: str, ops_summary: str, *, was_created: bool = False):
        backup_root = self.get_backup_root_dir()
        os.makedirs(backup_root, exist_ok=True)
        manifest_path = os.path.join(backup_root, "backup_manifest.json")
        entries = []
        if os.path.isfile(manifest_path):
            with open(manifest_path, "r", encoding="utf-8") as f:
                entries = json.load(f)
            if not isinstance(entries, list):
                raise ValueError("The backup manifest is not a list; fix or restore it before patching.")

        entries.append({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "script_name": os.path.basename(original_path),
            "original_path": original_path,
            "backup_path": backup_path,
            "ops_summary": ops_summary,
            "was_created": was_created,
        })

        # A failed manifest write must not destroy the previous backup history.
        temp_manifest = ""
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=backup_root,
                prefix=".backup_manifest_", suffix=".tmp", delete=False
            ) as f:
                temp_manifest = f.name
                json.dump(entries, f, indent=2)
            os.replace(temp_manifest, manifest_path)
        finally:
            if temp_manifest and os.path.exists(temp_manifest):
                os.remove(temp_manifest)

    def apply_all_patches(self):
        if (
            not self.btn_apply_all.isEnabled() or not self.simulated_files
            or len(self.original_files) != len(self.simulated_files)
            or not all(op.status == "OK" for op in self.parsed_ops)
        ):
            return

        # Check the whole batch before writing. New files must still be absent,
        # and an existing file must still match the preview's original text.
        for file_path, original_text in self.original_files.items():
            try:
                if original_text is None:
                    if os.path.lexists(file_path):
                        raise ValueError("A file now exists at the planned CREATE path.")
                    if os.path.normcase(os.path.realpath(file_path)) != os.path.normcase(file_path):
                        raise ValueError("The planned CREATE path changed through a symlink.")
                else:
                    with open(file_path, "r", encoding="utf-8", newline="") as f:
                        if f.read() != original_text:
                            raise ValueError("The file changed after the preview was generated.")
            except (OSError, UnicodeError, ValueError) as e:
                QMessageBox.warning(
                    self, "File Changed Since Preview",
                    f"{file_path}\n{e}\n\nNo files were patched. Review the updated preview."
                )
                self.analyze_current_patch()
                return

        backup_root = self.get_backup_root_dir()
        manifest_path = os.path.join(backup_root, "backup_manifest.json")
        if self.chk_auto_backup.isChecked() and os.path.exists(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    entries = json.load(f)
                if not isinstance(entries, list):
                    raise ValueError("The backup manifest is not a list.")
            except (OSError, UnicodeError, ValueError) as e:
                QMessageBox.critical(
                    self, "Backup Manifest Error",
                    f"Could not read {manifest_path}:\n{e}\n\nNo files were patched."
                )
                return

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        patched_list = []

        try:
            for file_path, new_content in self.simulated_files.items():
                file_ops = [op for op in self.parsed_ops if op.resolved_path == file_path]
                ops_desc = ", ".join(f"{op.op_type}({op.target})" for op in file_ops)
                was_created = self.original_files[file_path] is None
                os.makedirs(os.path.dirname(file_path), exist_ok=True)

                if was_created:
                    # Exclusive creation prevents overwriting a file that appears
                    # between the preflight check and this write.
                    with open(file_path, "x", encoding="utf-8", newline="") as f:
                        f.write(new_content)
                    ops_desc += " [CREATED]"

                if self.chk_auto_backup.isChecked():
                    os.makedirs(backup_root, exist_ok=True)
                    fd, bak_path = tempfile.mkstemp(
                        prefix=f"{os.path.basename(file_path)}.{stamp}.", suffix=".bak", dir=backup_root
                    )
                    os.close(fd)
                    try:
                        if not was_created:
                            shutil.copy2(file_path, bak_path)
                        self.record_backup_entry(file_path, bak_path, ops_desc, was_created=was_created)
                    except Exception:
                        if os.path.exists(bak_path):
                            os.remove(bak_path)
                        raise

                if not was_created:
                    with open(file_path, "w", encoding="utf-8", newline="") as f:
                        f.write(new_content)

                patched_list.append(os.path.basename(file_path))

            summary_names = "\n• ".join(patched_list)
            backup_note = (
                f"Backups saved in:\n{backup_root}"
                if self.chk_auto_backup.isChecked()
                else "Automatic backups were disabled."
            )
            QMessageBox.information(
                self,
                "Batch Patch Complete",
                f"Successfully patched {len(patched_list)} script(s):\n\n• {summary_names}\n\n{backup_note}"
            )
            self.clear_patch_input()

        except Exception as e:
            QMessageBox.critical(
                self, "Batch Patch Error",
                f"An error occurred while writing files:\n{e}\n\n"
                "Some files may already have changed. Review your files and backups before retrying."
            )

    def open_backup_manager(self):
        dlg = BackupManagerDialog(self, self.get_backup_root_dir())
        dlg.exec()
        self.analyze_current_patch()

    def save_settings(self):
        try:
            data = {
                "project_folder": self.txt_project_dir.text().strip(),
                "manual_files": self.manual_files,
                "auto_backup": self.chk_auto_backup.isChecked()
            }
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    def load_settings(self):
        if not os.path.isfile(SETTINGS_FILE):
            return
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            folder = data.get("project_folder", "")
            if folder and os.path.isdir(folder):
                self.txt_project_dir.setText(folder)
                self.scan_project_folder(folder)

            saved_manual = data.get("manual_files", [])
            self.manual_files = [p for p in saved_manual if os.path.isfile(p)]
            self.chk_auto_backup.setChecked(data.get("auto_backup", True))
            self.update_index_label()
        except Exception:
            pass


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = SurgicalPatcherV2()
    win.show()
    sys.exit(app.exec())