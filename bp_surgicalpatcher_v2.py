import os
import sys
import json
import re
import shutil
import difflib
import tempfile
from datetime import datetime
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QColor
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QTextEdit, QFileDialog, QMessageBox,
    QGroupBox, QSplitter, QListWidget, QListWidgetItem, QComboBox, QCheckBox,
    QDialog
)

APP_VERSION = "v2.0"

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SETTINGS_FILE = os.path.join(BASE_DIR, "patcher_settings.json")

AI_PROMPT_INSTRUCTIONS = """CRITICAL INSTRUCTION — MULTI-SCRIPT SURGICAL CODE PATCH FORMAT:
Do NOT output entire scripts. Output ONLY the changed, added, or deleted sections using the exact format below so my multi-script patcher can locate the relevant files and preview the changes before applying them.

Rules:
1. Before the operations for each script, ALWAYS specify the file header: <<<FILE: ScriptName.cs>>> (or relative path).
2. You may include multiple <<<FILE: ...>>> sections and multiple <<<OPERATION: ...>>> blocks in a single response.
3. In <<<SEARCH>>>, include the exact existing lines (or the full existing method/void) to be modified or used as an anchor.
4. Keep indentation inside <<<SEARCH>>> and <<<REPLACE>>> accurate to the original script.

Use this exact structure for EVERY script you modify:

<<<FILE: FirstScript.cs>>>

<<<OPERATION: REPLACE>>>
<<<TARGET: MethodNameOrDescription>>>
<<<SEARCH>>>
// Exact existing code or full void/function from FirstScript.cs
<<<REPLACE>>>
// New updated code to replace the SEARCH block
<<<END>>>

<<<FILE: SecondScript.cs>>>

<<<OPERATION: INSERT_AFTER>>>
<<<TARGET: Start>>>
<<<SEARCH>>>
// Exact existing anchor lines in SecondScript.cs
<<<REPLACE>>>
// New code to insert right after the anchor lines
<<<END>>>

---
Supported operations:
- REPLACE: Replaces <<<SEARCH>>> with <<<REPLACE>>>.
- INSERT_AFTER: Inserts <<<REPLACE>>> immediately after <<<SEARCH>>>.
- INSERT_BEFORE: Inserts <<<REPLACE>>> immediately before <<<SEARCH>>>.
- DELETE: Deletes the exact lines in <<<SEARCH>>> (leave <<<REPLACE>>> empty).
"""


class PatchOperation:
    def __init__(self, op_type: str, target: str, search_block: str, replace_block: str, file_hint: str = ""):
        self.op_type = op_type.upper().strip()
        self.target = target.strip()
        self.search_block = search_block
        self.replace_block = replace_block
        self.file_hint = file_hint.strip()
        self.resolved_path = ""
        self.status = "PENDING"
        self.message = ""
        self.match_start_line = -1
        self.match_end_line = -1


class FilePatchPlan:
    """Holds all operations and simulation results for a single resolved script."""
    def __init__(self, file_hint: str, resolved_path: str):
        self.file_hint = file_hint
        self.resolved_path = resolved_path
        self.operations: list[PatchOperation] = []
        self.original_text = ""
        self.new_text = ""
        self.diff_text = ""
        self.all_valid = False


def strip_code_fences(text: str) -> str:
    """Removes leading/trailing markdown ``` fences if the AI wrapped the block inside one."""
    lines = text.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines)


def parse_ai_response(raw_text: str) -> list[PatchOperation]:
    """
    Extracts all <<<FILE: ...>>> headers and <<<OPERATION: ...>>> blocks sequentially
    so a single AI response can patch multiple scripts at once.
    """
    operations = []
    file_pattern = re.compile(r"<<<FILE:\s*(.+?)>>>", re.IGNORECASE)
    file_markers = [(m.start(), m.group(1).strip()) for m in file_pattern.finditer(raw_text)]

    op_pattern = re.compile(
        r"<<<OPERATION:\s*([A-Z_]+)\s*>>>\s*"
        r"(?:<<<TARGET:\s*(.*?)\s*>>>\s*)?"
        r"<<<SEARCH>>>\r?\n(.*?)\r?\n<<<REPLACE>>>(?:\r?\n(.*?))?\r?\n<<<END>>>",
        re.DOTALL | re.IGNORECASE
    )

    for match in op_pattern.finditer(raw_text):
        op_pos = match.start()
        current_file_hint = ""
        for f_pos, f_name in file_markers:
            if f_pos < op_pos:
                current_file_hint = f_name
            else:
                break

        op_type = match.group(1)
        target = match.group(2) or "Code Block"
        search_blk = strip_code_fences(match.group(3))
        replace_blk = strip_code_fences(match.group(4) or "")
        operations.append(PatchOperation(op_type, target, search_blk, replace_blk, current_file_hint))

    return operations


def find_csharp_method_span(source_lines: list[str], method_signature: str) -> list[tuple[int, int]]:
    """
    State-machine tokenizer that finds a C#/Java/JS method by name/signature and tracks
    curly braces { } while ignoring strings and comments.
    """
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
                and not stripped.startswith("if")
                and not stripped.startswith("while")
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
    """
    Locates the exact line span in source_lines.
    Returns (status, start_idx, end_idx_inclusive, message)
    """
    search_lines = search_text.splitlines()
    if not search_lines:
        return ("NOT_FOUND", -1, -1, "Search block is empty.")

    # Pass 1: Exact rstrip line-by-line match
    norm_source = [ln.rstrip() for ln in source_lines]
    norm_search = [ln.rstrip() for ln in search_lines]
    m_len = len(norm_search)
    matches = []

    for idx in range(len(norm_source) - m_len + 1):
        if norm_source[idx:idx + m_len] == norm_search:
            matches.append((idx, idx + m_len - 1))

    if len(matches) == 1:
        s, e = matches[0]
        return ("OK", s, e, f"Exact match on lines {s + 1}–{e + 1}")
    elif len(matches) > 1:
        locs = ", ".join([f"L{s + 1}" for s, _ in matches])
        return ("AMBIGUOUS", -1, -1, f"Found {len(matches)} identical matches ({locs}). Include more surrounding lines.")

    # Pass 2: Indentation-insensitive match
    strip_source = [ln.strip() for ln in source_lines]
    strip_search = [ln.strip() for ln in search_lines]

    matches_pass2 = []
    for idx in range(len(strip_source) - m_len + 1):
        if strip_source[idx:idx + m_len] == strip_search:
            matches_pass2.append((idx, idx + m_len - 1))

    if len(matches_pass2) == 1:
        s, e = matches_pass2[0]
        return ("OK", s, e, f"Matched (indentation-adjusted) on lines {s + 1}–{e + 1}")
    elif len(matches_pass2) > 1:
        locs = ", ".join([f"L{s + 1}" for s, _ in matches_pass2])
        return ("AMBIGUOUS", -1, -1, f"Found {len(matches_pass2)} matches ({locs}). Ambiguous anchor.")

    # Pass 3: State-Machine Brace Counter fallback using TARGET method signature
    if target_hint and target_hint.lower() != "code block":
        brace_matches = find_csharp_method_span(source_lines, target_hint)
        if len(brace_matches) == 1:
            s, e = brace_matches[0]
            return ("OK", s, e, f"Brace-Scanner matched method '{target_hint}' on lines {s + 1}–{e + 1}")
        elif len(brace_matches) > 1:
            locs = ", ".join([f"L{s + 1}" for s, _ in brace_matches])
            return ("AMBIGUOUS", -1, -1, f"Multiple overloaded methods found for '{target_hint}' ({locs}).")

    return ("NOT_FOUND", -1, -1, "Target block not found in script.")


def simulate_patches(original_text: str, operations: list[PatchOperation], file_label: str = "Script") -> tuple[bool, str, str]:
    """
    Simulates applying all operations for a single file.
    Returns (all_valid, new_full_text, unified_diff_text)
    """
    has_crlf = "\r\n" in original_text
    line_sep = "\r\n" if has_crlf else "\n"
    working_lines = original_text.splitlines()
    all_ok = True

    for op in operations:
        status, s_idx, e_idx, msg = locate_block_in_lines(working_lines, op.search_block, op.target)
        op.status = status
        op.message = msg
        op.match_start_line = s_idx + 1 if s_idx >= 0 else -1
        op.match_end_line = e_idx + 1 if e_idx >= 0 else -1

        if status != "OK":
            all_ok = False
            continue

        orig_first_line = working_lines[s_idx]
        orig_indent = len(orig_first_line) - len(orig_first_line.lstrip(" \t"))
        search_first = op.search_block.splitlines()[0] if op.search_block.splitlines() else ""
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
            op.message = f"Unknown operation type: {op.op_type}"
            all_ok = False

    new_text = line_sep.join(working_lines)
    if original_text.endswith(("\n", "\r\n")) and not new_text.endswith(("\n", "\r\n")):
        new_text += line_sep

    diff_lines = list(difflib.unified_diff(
        original_text.splitlines(),
        new_text.splitlines(),
        fromfile=f"a/{file_label}",
        tofile=f"b/{file_label}",
        lineterm=""
    ))
    diff_text = "\n".join(diff_lines) if diff_lines else f"--- {file_label}: No changes detected ---"
    return all_ok, new_text, diff_text


class BackupManagerDialog(QDialog):
    """
    Dedicated Backup & Revert Manager allowing the user to view all historical backups,
    filter by individual script, inspect the diff against the current file on disk,
    and manually revert any single script at any time.
    """
    def __init__(self, parent, backup_root: str, on_restored_callback):
        super().__init__(parent)
        self.setWindowTitle("🕒 Backup History Manager — Revert Any Individual Script")
        self.resize(980, 640)
        self.backup_root = backup_root
        self.on_restored_callback = on_restored_callback
        self.records = []

        self.setup_ui()
        self.load_records()

    def setup_ui(self):
        layout = QVBoxLayout(self)

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Filter by Script:"))
        self.cmb_filter = QComboBox()
        self.cmb_filter.currentIndexChanged.connect(self.populate_list)
        top_row.addWidget(self.cmb_filter, stretch=1)

        btn_refresh = QPushButton("🔄 Refresh")
        btn_refresh.clicked.connect(self.load_records)
        top_row.addWidget(btn_refresh)
        layout.addLayout(top_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("Saved Script Backups (Newest First):"))
        self.list_backups = QListWidget()
        self.list_backups.setFont(QFont("Consolas", 10))
        self.list_backups.currentItemChanged.connect(self.on_backup_selected)
        left_layout.addWidget(self.list_backups)
        splitter.addWidget(left_widget)

        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(QLabel("Diff Preview (Current Live File vs Selected Backup):"))
        self.txt_preview = QTextEdit()
        self.txt_preview.setReadOnly(True)
        self.txt_preview.setFont(QFont("Consolas", 10))
        right_layout.addWidget(self.txt_preview)
        splitter.addWidget(right_widget)

        splitter.setSizes([430, 550])
        layout.addWidget(splitter, stretch=1)

        bottom_row = QHBoxLayout()
        self.lbl_info = QLabel("Select a backup entry on the left to inspect or revert.")
        self.lbl_info.setStyleSheet("color: #a1a1aa;")

        self.btn_delete = QPushButton("🗑️ Delete Selected Backup")
        self.btn_delete.clicked.connect(self.delete_selected_backup)
        self.btn_delete.setEnabled(False)

        self.btn_revert = QPushButton("↩️ Revert ONLY This Script to Selected Backup")
        self.btn_revert.setObjectName("undo_btn")
        self.btn_revert.clicked.connect(self.revert_selected_backup)
        self.btn_revert.setEnabled(False)

        bottom_row.addWidget(self.lbl_info, stretch=1)
        bottom_row.addWidget(self.btn_delete)
        bottom_row.addWidget(self.btn_revert)
        layout.addLayout(bottom_row)

    def get_index_file(self) -> str:
        return os.path.join(self.backup_root, "backup_index.json")

    def load_records(self):
        self.records.clear()
        idx_file = self.get_index_file()
        if os.path.isfile(idx_file):
            try:
                with open(idx_file, "r", encoding="utf-8") as f:
                    raw_list = json.load(f)
                self.records = [r for r in raw_list if os.path.isfile(r.get("backup_path", ""))]
            except Exception:
                self.records = []

        self.records.sort(key=lambda r: r.get("timestamp", ""), reverse=True)

        self.cmb_filter.blockSignals(True)
        current_filter = self.cmb_filter.currentText()
        self.cmb_filter.clear()
        self.cmb_filter.addItem("All Scripts")

        script_names = sorted({r.get("file_name", "Unknown") for r in self.records})
        for name in script_names:
            self.cmb_filter.addItem(name)

        idx = self.cmb_filter.findText(current_filter)
        if idx >= 0:
            self.cmb_filter.setCurrentIndex(idx)
        self.cmb_filter.blockSignals(False)

        self.populate_list()

    def populate_list(self):
        self.list_backups.clear()
        self.txt_preview.clear()
        self.btn_revert.setEnabled(False)
        self.btn_delete.setEnabled(False)

        filt = self.cmb_filter.currentText()
        for rec in self.records:
            fname = rec.get("file_name", "Unknown")
            if filt and filt != "All Scripts" and fname != filt:
                continue

            ts = rec.get("display_time", rec.get("timestamp", ""))
            ops_summary = rec.get("ops_summary", "Patch backup")
            item = QListWidgetItem(f"📄 {fname}  |  🕒 {ts}\n   └─ {ops_summary}")
            item.setData(Qt.ItemDataRole.UserRole, rec)
            self.list_backups.addItem(item)

    def on_backup_selected(self, current: QListWidgetItem, _previous: QListWidgetItem):
        if not current:
            self.btn_revert.setEnabled(False)
            self.btn_delete.setEnabled(False)
            return

        rec = current.data(Qt.ItemDataRole.UserRole)
        bak_path = rec.get("backup_path", "")
        orig_path = rec.get("original_path", "")
        self.btn_revert.setEnabled(True)
        self.btn_delete.setEnabled(True)
        self.lbl_info.setText(f"Target: {orig_path}")

        try:
            with open(bak_path, "r", encoding="utf-8", errors="replace") as f:
                bak_text = f.read()
        except Exception as e:
            self.txt_preview.setPlainText(f"Error reading backup file:\n{e}")
            return

        if os.path.isfile(orig_path):
            try:
                with open(orig_path, "r", encoding="utf-8", errors="replace") as f:
                    live_text = f.read()
            except Exception:
                live_text = ""
        else:
            live_text = ""

        diff_lines = list(difflib.unified_diff(
            live_text.splitlines(),
            bak_text.splitlines(),
            fromfile=f"Current Live ({os.path.basename(orig_path)})",
            tofile=f"Backup ({rec.get('display_time', '')})",
            lineterm=""
        ))

        if not diff_lines:
            self.txt_preview.setHtml(
                "<pre style='color: #4ade80; font-family: Consolas, monospace;'>"
                "✔ The current live file on disk is already identical to this backup."
                "</pre>"
            )
            return

        html_lines = ["<pre style='font-family: Consolas, monospace; font-size: 12px;'>"]
        for line in diff_lines:
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
        self.txt_preview.setHtml("\n".join(html_lines))

    def revert_selected_backup(self):
        item = self.list_backups.currentItem()
        if not item:
            return
        rec = item.data(Qt.ItemDataRole.UserRole)
        bak_path = rec.get("backup_path", "")
        orig_path = rec.get("original_path", "")
        fname = rec.get("file_name", os.path.basename(orig_path))

        reply = QMessageBox.question(
            self,
            "Confirm Single-Script Revert",
            f"Are you sure you want to revert:\n\n{fname}\n\nback to the backup from {rec.get('display_time', '')}?\n"
            f"(All other scripts will remain untouched.)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            os.makedirs(os.path.dirname(orig_path), exist_ok=True)
            shutil.copy2(bak_path, orig_path)
            QMessageBox.information(self, "Script Reverted", f"Successfully restored:\n{orig_path}")
            self.on_backup_selected(item, None)
            if self.on_restored_callback:
                self.on_restored_callback()
        except Exception as e:
            QMessageBox.critical(self, "Restore Error", f"Failed to restore script:\n{e}")

    def delete_selected_backup(self):
        item = self.list_backups.currentItem()
        if not item:
            return
        rec = item.data(Qt.ItemDataRole.UserRole)
        bak_path = rec.get("backup_path", "")

        try:
            if os.path.isfile(bak_path):
                os.remove(bak_path)
            self.records = [r for r in self.records if r.get("backup_path") != bak_path]
            with open(self.get_index_file(), "w", encoding="utf-8") as f:
                json.dump(self.records, f, indent=2)
            self.load_records()
        except Exception as e:
            QMessageBox.warning(self, "Delete Error", f"Could not delete backup:\n{e}")


class SurgicalPatcherApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"🦇 BATPAN SurgicalPatcher — Multi-Script AI Batch Patcher & Backup Manager {APP_VERSION}")
        self.resize(1260, 880)

        self.project_files: list[str] = []
        self.checked_files: set[str] = set()
        self.file_plans: list[FilePatchPlan] = []
        self.combined_diff_text = ""

        self.setup_ui()
        self.apply_theme()
        self.load_settings()

    def apply_theme(self):
        self.setStyleSheet("""
            QMainWindow, QDialog, QWidget {
                background-color: #121215;
                color: #e4e4e7;
                font-family: 'Segoe UI', sans-serif;
                font-size: 13px;
            }
            QGroupBox {
                border: 1px solid #27272a;
                border-radius: 6px;
                margin-top: 10px;
                font-weight: bold;
                padding: 10px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0 6px;
                color: #a1a1aa;
            }
            QLineEdit, QTextEdit, QListWidget, QComboBox {
                background-color: #18181c;
                border: 1px solid #27272a;
                border-radius: 5px;
                color: #fafafa;
                padding: 6px;
            }
            QLineEdit:focus, QTextEdit:focus, QComboBox:focus {
                border: 1px solid #6366f1;
            }
            QPushButton {
                background-color: #27272a;
                border: 1px solid #3f3f46;
                border-radius: 5px;
                padding: 7px 14px;
                font-weight: bold;
                color: #fafafa;
            }
            QPushButton:hover {
                background-color: #3f3f46;
            }
            QPushButton#prompt_btn {
                background-color: #1e1b4b;
                border: 1px solid #6366f1;
                color: #c7d2fe;
                font-size: 13px;
            }
            QPushButton#prompt_btn:hover {
                background-color: #312e81;
                color: #ffffff;
            }
            QPushButton#apply_btn {
                background-color: #15803d;
                border: 1px solid #22c55e;
                color: #ffffff;
                font-size: 14px;
            }
            QPushButton#apply_btn:hover {
                background-color: #16a34a;
            }
            QPushButton#apply_btn:disabled {
                background-color: #1f2937;
                border-color: #374151;
                color: #6b7280;
            }
            QPushButton#undo_btn {
                background-color: #7f1d1d;
                border: 1px solid #ef4444;
                color: #fecaca;
            }
            QPushButton#undo_btn:hover {
                background-color: #991b1b;
                color: #ffffff;
            }
        """)

    def setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(10)

        # 1. Top Bar: AI Prompt Instructions & Multi-Script Context Copy
        top_bar = QHBoxLayout()
        lbl_title = QLabel("⚡ Multi-Script Surgical Code Patcher (Folder Auto-Search + Per-Script Revert)")
        lbl_title.setStyleSheet("font-size: 15px; font-weight: bold; color: #38bdf8;")

        self.btn_copy_prompt = QPushButton("📋 Copy AI System Prompt Only")
        self.btn_copy_prompt.setObjectName("prompt_btn")
        self.btn_copy_prompt.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_copy_prompt.clicked.connect(self.copy_ai_prompt)

        self.btn_copy_context = QPushButton("📦 Copy Checked Scripts + AI Prompt")
        self.btn_copy_context.setObjectName("prompt_btn")
        self.btn_copy_context.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_copy_context.clicked.connect(self.copy_checked_scripts_with_prompt)

        top_bar.addWidget(lbl_title)
        top_bar.addStretch()
        top_bar.addWidget(self.btn_copy_prompt)
        top_bar.addWidget(self.btn_copy_context)
        main_layout.addLayout(top_bar)

        # 2. Target Project Folder & Multi-Script Selection Box
        file_box = QGroupBox("1. Project Folder & Multi-Script Selection (Auto-Searches Folder When Applying Patches)")
        file_layout = QVBoxLayout(file_box)

        proj_row = QHBoxLayout()
        proj_row.addWidget(QLabel("Project Folder:"))
        self.txt_project_dir = QLineEdit()
        self.txt_project_dir.setPlaceholderText("Select your Unity / Python project folder so the patcher can find any script automatically...")
        btn_browse_proj = QPushButton("📁 Browse Folder")
        btn_browse_proj.clicked.connect(self.browse_project_folder)

        btn_add_files = QPushButton("📄 + Select Individual Scripts...")
        btn_add_files.clicked.connect(self.pick_multiple_scripts)

        proj_row.addWidget(self.txt_project_dir, stretch=1)
        proj_row.addWidget(btn_browse_proj)
        proj_row.addWidget(btn_add_files)
        file_layout.addLayout(proj_row)

        filter_row = QHBoxLayout()
        self.txt_script_filter = QLineEdit()
        self.txt_script_filter.setPlaceholderText("🔍 Search/filter scripts in folder (e.g. Player, Manager, .cs)...")
        self.txt_script_filter.textChanged.connect(self.refresh_script_checklist)

        btn_check_visible = QPushButton("☑️ Check Visible")
        btn_check_visible.clicked.connect(self.check_visible_scripts)

        btn_uncheck_all = QPushButton("⬜ Uncheck All")
        btn_uncheck_all.clicked.connect(self.uncheck_all_scripts)

        self.lbl_selection_stats = QLabel("0 scripts in pool | 0 checked")
        self.lbl_selection_stats.setStyleSheet("color: #4ade80; font-weight: bold;")

        filter_row.addWidget(self.txt_script_filter, stretch=1)
        filter_row.addWidget(btn_check_visible)
        filter_row.addWidget(btn_uncheck_all)
        filter_row.addWidget(self.lbl_selection_stats)
        file_layout.addLayout(filter_row)

        self.list_scripts = QListWidget()
        self.list_scripts.setFixedHeight(115)
        self.list_scripts.setFont(QFont("Consolas", 10))
        self.list_scripts.itemChanged.connect(self.on_script_check_changed)
        file_layout.addWidget(self.list_scripts)

        main_layout.addWidget(file_box)

        # 3. Middle Splitter: Left (Paste Multi-Script AI Patch) | Right (Batch Verification & Diff Preview)
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left Panel: AI Patch Input
        left_box = QGroupBox("2. Paste Full AI Answer Here (Supports 1 or Many Scripts at Once)")
        left_layout = QVBoxLayout(left_box)

        paste_btn_row = QHBoxLayout()
        btn_paste_analyze = QPushButton("📋 Paste from Clipboard & Auto-Match Scripts")
        btn_paste_analyze.setObjectName("prompt_btn")
        btn_paste_analyze.clicked.connect(self.paste_and_verify)

        btn_clear_patch = QPushButton("Clear")
        btn_clear_patch.clicked.connect(self.clear_patch_input)

        paste_btn_row.addWidget(btn_paste_analyze, stretch=1)
        paste_btn_row.addWidget(btn_clear_patch)
        left_layout.addLayout(paste_btn_row)

        self.txt_ai_patch = QTextEdit()
        self.txt_ai_patch.setFont(QFont("Consolas", 10))
        self.txt_ai_patch.setPlaceholderText(
            "Paste the entire AI response here!\n\n"
            "It can contain multiple <<<FILE: ScriptName.cs>>> sections and multiple <<<OPERATION: ...>>> blocks.\n"
            "The patcher will automatically search your folder, match every script, and verify all changes before applying."
        )
        self.txt_ai_patch.textChanged.connect(self.analyze_current_patch)
        left_layout.addWidget(self.txt_ai_patch)

        splitter.addWidget(left_box)

        # Right Panel: Verification Checklist & Live Diff Preview
        right_box = QGroupBox("3. Multi-Script Verification & Live Diff Preview")
        right_layout = QVBoxLayout(right_box)

        ops_header_row = QHBoxLayout()
        ops_header_row.addWidget(QLabel("Detected Scripts & Operations (Click any item to filter diff):"))
        btn_show_all_diffs = QPushButton("🔎 Show All Scripts Diff")
        btn_show_all_diffs.clicked.connect(lambda: self.render_colored_diff(self.combined_diff_text))
        ops_header_row.addStretch()
        ops_header_row.addWidget(btn_show_all_diffs)
        right_layout.addLayout(ops_header_row)

        self.list_ops = QListWidget()
        self.list_ops.setFixedHeight(165)
        self.list_ops.setFont(QFont("Consolas", 10))
        self.list_ops.currentItemChanged.connect(self.on_op_item_clicked)
        right_layout.addWidget(self.list_ops)

        right_layout.addWidget(QLabel("Live Code Diff Preview (- Removed / + Added):"))
        self.txt_diff_preview = QTextEdit()
        self.txt_diff_preview.setReadOnly(True)
        self.txt_diff_preview.setFont(QFont("Consolas", 10))
        right_layout.addWidget(self.txt_diff_preview)

        splitter.addWidget(right_box)
        splitter.setSizes([520, 700])
        main_layout.addWidget(splitter, stretch=1)

        # 4. Bottom Action Bar: Backup Manager & Batch Apply
        bottom_bar = QHBoxLayout()
        self.chk_auto_backup = QCheckBox("Save automatic per-script backups before patching (.patch_backups)")
        self.chk_auto_backup.setChecked(True)
        self.chk_auto_backup.setStyleSheet("color: #4ade80; font-weight: bold;")

        self.btn_backup_mgr = QPushButton("🕒 Backup Manager (Revert Any Script)")
        self.btn_backup_mgr.setObjectName("undo_btn")
        self.btn_backup_mgr.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_backup_mgr.clicked.connect(self.open_backup_manager)

        self.btn_apply = QPushButton("✅ Apply Verified Changes to All Matched Scripts")
        self.btn_apply.setObjectName("apply_btn")
        self.btn_apply.setEnabled(False)
        self.btn_apply.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_apply.clicked.connect(self.apply_all_patches)

        bottom_bar.addWidget(self.chk_auto_backup)
        bottom_bar.addStretch()
        bottom_bar.addWidget(self.btn_backup_mgr)
        bottom_bar.addWidget(self.btn_apply)
        main_layout.addLayout(bottom_bar)

    # --- FOLDER & MULTI-SCRIPT MANAGEMENT ---
    def get_backup_root_dir(self) -> str:
        proj_dir = self.txt_project_dir.text().strip()
        if proj_dir and os.path.isdir(proj_dir):
            return os.path.join(proj_dir, ".patch_backups")
        return os.path.join(BASE_DIR, ".patch_backups")

    def browse_project_folder(self):
        start_dir = self.txt_project_dir.text().strip()
        if not start_dir or not os.path.isdir(start_dir):
            start_dir = os.path.expanduser("~/Desktop")

        folder = QFileDialog.getExistingDirectory(self, "Select Project Folder", start_dir)
        if folder:
            self.txt_project_dir.setText(folder)
            self.scan_project_folder(folder)
            self.save_settings()
            self.analyze_current_patch()

    def scan_project_folder(self, folder: str):
        if not folder or not os.path.isdir(folder):
            return

        exts = {".cs", ".py", ".pyw", ".js", ".ts", ".cpp", ".h", ".gd", ".json", ".shader"}
        ignore_dirs = {".git", "__pycache__", "Library", "Temp", "Obj", "Build", ".patch_backups", "node_modules"}

        found = []
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if d not in ignore_dirs]
            for fn in files:
                if os.path.splitext(fn)[1].lower() in exts:
                    full_path = os.path.normpath(os.path.join(root, fn))
                    found.append(full_path)

        existing_manual = [p for p in self.project_files if os.path.isfile(p) and not p.startswith(os.path.normpath(folder))]
        self.project_files = sorted(set(found + existing_manual))
        self.refresh_script_checklist()

    def pick_multiple_scripts(self):
        start_dir = self.txt_project_dir.text().strip()
        if not start_dir or not os.path.isdir(start_dir):
            start_dir = os.path.expanduser("~/Desktop")

        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Select One or More Scripts",
            start_dir,
            "Code Files (*.cs *.py *.pyw *.js *.ts *.cpp *.h *.gd *.json *.shader);;All Files (*.*)"
        )
        if files:
            for f in files:
                norm_f = os.path.normpath(f)
                if norm_f not in self.project_files:
                    self.project_files.append(norm_f)
                self.checked_files.add(norm_f)

            if not self.txt_project_dir.text().strip():
                common_dir = os.path.dirname(os.path.normpath(files[0]))
                self.txt_project_dir.setText(common_dir)

            self.refresh_script_checklist()
            self.save_settings()
            self.analyze_current_patch()

    def refresh_script_checklist(self):
        query = self.txt_script_filter.text().strip().lower()
        proj_dir = self.txt_project_dir.text().strip()

        self.list_scripts.blockSignals(True)
        self.list_scripts.clear()

        for full_path in self.project_files:
            if proj_dir and os.path.isdir(proj_dir) and full_path.startswith(os.path.normpath(proj_dir)):
                display_rel = os.path.relpath(full_path, proj_dir)
            else:
                display_rel = full_path

            if query and query not in display_rel.lower():
                continue

            item = QListWidgetItem(display_rel)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            state = Qt.CheckState.Checked if full_path in self.checked_files else Qt.CheckState.Unchecked
            item.setCheckState(state)
            item.setData(Qt.ItemDataRole.UserRole, full_path)
            if state == Qt.CheckState.Checked:
                item.setForeground(QColor("#4ade80"))
            self.list_scripts.addItem(item)

        self.list_scripts.blockSignals(False)
        self.update_selection_stats()

    def on_script_check_changed(self, item: QListWidgetItem):
        full_path = item.data(Qt.ItemDataRole.UserRole)
        if not full_path:
            return
        if item.checkState() == Qt.CheckState.Checked:
            self.checked_files.add(full_path)
            item.setForeground(QColor("#4ade80"))
        else:
            self.checked_files.discard(full_path)
            item.setForeground(QColor("#fafafa"))

        self.update_selection_stats()
        self.save_settings()
        self.analyze_current_patch()

    def check_visible_scripts(self):
        self.list_scripts.blockSignals(True)
        for idx in range(self.list_scripts.count()):
            item = self.list_scripts.item(idx)
            full_path = item.data(Qt.ItemDataRole.UserRole)
            item.setCheckState(Qt.CheckState.Checked)
            item.setForeground(QColor("#4ade80"))
            if full_path:
                self.checked_files.add(full_path)
        self.list_scripts.blockSignals(False)
        self.update_selection_stats()
        self.save_settings()
        self.analyze_current_patch()

    def uncheck_all_scripts(self):
        self.checked_files.clear()
        self.refresh_script_checklist()
        self.save_settings()
        self.analyze_current_patch()

    def update_selection_stats(self):
        self.lbl_selection_stats.setText(
            f"{len(self.project_files)} scripts in folder | {len(self.checked_files)} checked"
        )

    # --- PROMPT & CONTEXT COPY ---
    def copy_ai_prompt(self):
        QApplication.clipboard().setText(AI_PROMPT_INSTRUCTIONS)
        self.btn_copy_prompt.setText("✅ Copied System Prompt!")
        QTimer.singleShot(2500, lambda: self.btn_copy_prompt.setText("📋 Copy AI System Prompt Only"))

    def copy_checked_scripts_with_prompt(self):
        checked_list = [p for p in self.project_files if p in self.checked_files and os.path.isfile(p)]
        if not checked_list:
            QMessageBox.warning(
                self,
                "No Scripts Checked",
                "Check one or more scripts in the list first so they can be bundled with the AI prompt!"
            )
            return

        proj_dir = self.txt_project_dir.text().strip()
        chunks = [AI_PROMPT_INSTRUCTIONS.strip(), "\n\n=== SELECTED PROJECT SCRIPTS ==="]

        for path in checked_list:
            rel_name = (
                os.path.relpath(path, proj_dir)
                if (proj_dir and os.path.isdir(proj_dir) and path.startswith(os.path.normpath(proj_dir)))
                else os.path.basename(path)
            )
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    code = f.read()
                chunks.append(f"\n<<<FILE: {rel_name}>>>\n```\n{code}\n```")
            except Exception as e:
                chunks.append(f"\n<<<FILE: {rel_name}>>> (Error reading: {e})")

        QApplication.clipboard().setText("\n".join(chunks))
        self.btn_copy_context.setText(f"✅ Copied {len(checked_list)} Script(s) + Prompt!")
        QTimer.singleShot(
            2800,
            lambda: self.btn_copy_context.setText("📦 Copy Checked Scripts + AI Prompt")
        )

    # --- AUTO-RESOLUTION OF SCRIPTS IN FOLDER ---
    def resolve_script_from_hint(self, file_hint: str, op_Sample: PatchOperation = None) -> tuple[str, str]:
        """
        Finds the target script on disk from <<<FILE: ...>>> hint or by content scan.
        Priority:
          1. Checked files matching relative path or filename
          2. All project folder files matching relative path or filename
          3. If no <<<FILE: ...>>> was given and only 1 script is checked, use that script
          4. Content scan across checked scripts (or folder scripts) where <<<SEARCH>>> matches
        Returns (resolved_full_path, resolution_note)
        """
        checked_list = [p for p in self.project_files if p in self.checked_files and os.path.isfile(p)]
        all_list = [p for p in self.project_files if os.path.isfile(p)]

        if file_hint:
            clean_hint = file_hint.replace("\\", "/").strip("/").lower()
            hint_base = os.path.basename(clean_hint)

            # 1. Exact suffix/relative path match in checked files, then all files
            for pool in (checked_list, all_list):
                suffix_matches = [
                    p for p in pool
                    if p.replace("\\", "/").lower().endswith("/" + clean_hint)
                    or p.replace("\\", "/").lower() == clean_hint
                ]
                if len(suffix_matches) == 1:
                    return suffix_matches[0], ""

            # 2. Basename match in checked files, then all files
            for pool in (checked_list, all_list):
                base_matches = [p for p in pool if os.path.basename(p).lower() == hint_base]
                if len(base_matches) == 1:
                    return base_matches[0], ""
                elif len(base_matches) > 1 and op_Sample:
                    for candidate in base_matches:
                        try:
                            with open(candidate, "r", encoding="utf-8", errors="replace") as f:
                                lines = f.read().splitlines()
                            st, _, _, _ = locate_block_in_lines(lines, op_Sample.search_block, op_Sample.target)
                            if st == "OK":
                                return candidate, "Disambiguated by code match"
                        except Exception:
                            pass
                    return base_matches[0], f"Multiple '{hint_base}' found; picked first"

            return "", f"File '{file_hint}' not found in selected folder."

        # No <<<FILE: ...>>> tag provided by AI:
        if len(checked_list) == 1:
            return checked_list[0], "Matched single checked script"

        # Content search across checked scripts first, then all project scripts
        if op_Sample:
            for pool in (checked_list, all_list):
                matching_files = []
                for candidate in pool:
                    try:
                        with open(candidate, "r", encoding="utf-8", errors="replace") as f:
                            lines = f.read().splitlines()
                        st, _, _, _ = locate_block_in_lines(lines, op_Sample.search_block, op_Sample.target)
                        if st == "OK":
                            matching_files.append(candidate)
                    except Exception:
                        pass
                if len(matching_files) == 1:
                    return matching_files[0], "Auto-detected script by code content"

        return "", "Missing <<<FILE: ScriptName>>> header and could not auto-detect file."

    # --- MULTI-SCRIPT PATCH ANALYSIS & SIMULATION ---
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
        self.file_plans.clear()
        self.combined_diff_text = ""
        self.btn_apply.setEnabled(False)

    def analyze_current_patch(self):
        raw_patch = self.txt_ai_patch.toPlainText().strip()
        self.list_ops.clear()
        self.txt_diff_preview.clear()
        self.file_plans.clear()
        self.combined_diff_text = ""
        self.btn_apply.setEnabled(False)

        if not raw_patch:
            return

        parsed_ops = parse_ai_response(raw_patch)
        if not parsed_ops:
            item = QListWidgetItem("❌ No valid <<<OPERATION: ...>>> blocks detected in pasted text.")
            item.setForeground(QColor("#f87171"))
            self.list_ops.addItem(item)
            return

        plans_by_key: dict[str, FilePatchPlan] = {}
        unresolved_errors = False

        for op in parsed_ops:
            resolved_path, note = self.resolve_script_from_hint(op.file_hint, op)
            op.resolved_path = resolved_path
            key = resolved_path if resolved_path else f"UNRESOLVED::{op.file_hint or 'Unknown'}"

            if key not in plans_by_key:
                plans_by_key[key] = FilePatchPlan(op.file_hint or os.path.basename(resolved_path), resolved_path)
            plans_by_key[key].operations.append(op)
            if note and not op.message:
                op.message = note

        self.file_plans = list(plans_by_key.values())
        all_plans_valid = True
        combined_diffs = []
        proj_dir = self.txt_project_dir.text().strip()

        for plan in self.file_plans:
            if not plan.resolved_path or not os.path.isfile(plan.resolved_path):
                all_plans_valid = False
                unresolved_errors = True
                hdr = QListWidgetItem(f"❌ [FILE NOT FOUND] {plan.file_hint or 'Unspecified Script'} — Select folder or check script")
                hdr.setForeground(QColor("#f87171"))
                self.list_ops.addItem(hdr)
                continue

            rel_label = (
                os.path.relpath(plan.resolved_path, proj_dir)
                if (proj_dir and os.path.isdir(proj_dir) and plan.resolved_path.startswith(os.path.normpath(proj_dir)))
                else os.path.basename(plan.resolved_path)
            )

            try:
                # Refuse non-UTF-8 input rather than silently replacing bytes, and
                # retain the original line endings for the preview and write.
                with open(plan.resolved_path, "r", encoding="utf-8", newline="") as f:
                    plan.original_text = f.read()
            except Exception as e:
                all_plans_valid = False
                hdr = QListWidgetItem(f"❌ [READ ERROR] {rel_label}: {e}")
                hdr.setForeground(QColor("#f87171"))
                self.list_ops.addItem(hdr)
                continue

            valid, new_text, diff_text = simulate_patches(plan.original_text, plan.operations, rel_label)
            plan.all_valid = valid
            plan.new_text = new_text
            plan.diff_text = diff_text
            combined_diffs.append(diff_text)

            if not valid:
                all_plans_valid = False

            status_badge = "✅" if valid else "⚠️"
            hdr_item = QListWidgetItem(f"{status_badge} 📄 {rel_label} ({len(plan.operations)} operation(s))")
            hdr_item.setForeground(QColor("#38bdf8") if valid else QColor("#facc15"))
            hdr_item.setData(Qt.ItemDataRole.UserRole, plan.diff_text)
            self.list_ops.addItem(hdr_item)

            for idx, op in enumerate(plan.operations, 1):
                if op.status == "OK":
                    icon = "   ✔"
                    color = QColor("#4ade80")
                elif op.status == "AMBIGUOUS":
                    icon = "   ⚠️"
                    color = QColor("#facc15")
                else:
                    icon = "   ❌"
                    color = QColor("#f87171")

                op_item = QListWidgetItem(f"{icon} [{idx}] {op.op_type} ({op.target}) — {op.message}")
                op_item.setForeground(color)
                op_item.setData(Qt.ItemDataRole.UserRole, plan.diff_text)
                self.list_ops.addItem(op_item)

        self.combined_diff_text = "\n\n".join(combined_diffs)
        self.render_colored_diff(self.combined_diff_text)

        valid_file_count = sum(1 for p in self.file_plans if p.all_valid)
        total_ops_count = sum(len(p.operations) for p in self.file_plans)
        if all_plans_valid and not unresolved_errors and len(self.file_plans) > 0:
            self.btn_apply.setEnabled(True)
            self.btn_apply.setText(f"✅ Apply {total_ops_count} Change(s) Across {valid_file_count} Script(s)")
        else:
            self.btn_apply.setEnabled(False)
            self.btn_apply.setText("✅ Apply Verified Changes to All Matched Scripts")

    def on_op_item_clicked(self, current: QListWidgetItem, _previous: QListWidgetItem):
        if not current:
            return
        plan_diff = current.data(Qt.ItemDataRole.UserRole)
        if plan_diff:
            self.render_colored_diff(plan_diff)

    def render_colored_diff(self, diff_text: str):
        if not diff_text:
            self.txt_diff_preview.clear()
            return
        html_lines = ["<pre style='font-family: Consolas, monospace; font-size: 12px;'>"]
        for line in diff_text.splitlines():
            escaped = (
                line.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            if line.startswith("+++") or line.startswith("---"):
                html_lines.append(f"<span style='color: #facc15; font-weight: bold;'>{escaped}</span>")
            elif line.startswith("+"):
                html_lines.append(f"<span style='color: #4ade80; background-color: #142e1f;'>{escaped}</span>")
            elif line.startswith("-"):
                html_lines.append(f"<span style='color: #f87171; background-color: #361717;'>{escaped}</span>")
            elif line.startswith("@@"):
                html_lines.append(f"<span style='color: #38bdf8; font-weight: bold;'>{escaped}</span>")
            else:
                html_lines.append(f"<span style='color: #a1a1aa;'>{escaped}</span>")
        html_lines.append("</pre>")
        self.txt_diff_preview.setHtml("\n".join(html_lines))

    # --- BATCH APPLY & PER-SCRIPT BACKUP RECORDING ---
    def record_backup_entry(self, original_path: str, backup_path: str, ops: list[PatchOperation], stamp_raw: str, stamp_display: str):
        backup_root = self.get_backup_root_dir()
        os.makedirs(backup_root, exist_ok=True)
        index_file = os.path.join(backup_root, "backup_index.json")

        records = []
        if os.path.isfile(index_file):
            with open(index_file, "r", encoding="utf-8") as f:
                records = json.load(f)
            if not isinstance(records, list):
                raise ValueError("The backup index is not a list; fix or restore it before patching.")

        ops_summary = ", ".join(f"{op.op_type}({op.target})" for op in ops[:4])
        if len(ops) > 4:
            ops_summary += f" +{len(ops) - 4} more"

        records.append({
            "timestamp": stamp_raw,
            "display_time": stamp_display,
            "file_name": os.path.basename(original_path),
            "original_path": original_path,
            "backup_path": backup_path,
            "ops_count": len(ops),
            "ops_summary": ops_summary
        })

        # Update the index through a temporary file so a failed write does not
        # destroy earlier history. Propagate errors before modifying this file.
        temp_index = ""
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=backup_root,
                prefix=".backup_index_", suffix=".tmp", delete=False
            ) as f:
                temp_index = f.name
                json.dump(records, f, indent=2)
            os.replace(temp_index, index_file)
        finally:
            if temp_index and os.path.exists(temp_index):
                os.remove(temp_index)

    def apply_all_patches(self):
        if not self.file_plans or not all(p.all_valid for p in self.file_plans):
            return

        # Check every file before writing any of them. The preview must not
        # overwrite edits made by an editor or another process in the meantime.
        for plan in self.file_plans:
            try:
                with open(plan.resolved_path, "r", encoding="utf-8", newline="") as f:
                    current_text = f.read()
            except (OSError, UnicodeError) as e:
                QMessageBox.warning(
                    self, "File Changed or Unreadable",
                    f"Could not re-read {plan.resolved_path}:\n{e}\n\nReview the updated preview before applying."
                )
                self.analyze_current_patch()
                return
            if current_text != plan.original_text:
                QMessageBox.warning(
                    self, "File Changed Since Preview",
                    f"{plan.resolved_path} changed after the preview was generated. "
                    "No files were patched. Review the updated preview before applying."
                )
                self.analyze_current_patch()
                return

        now = datetime.now()
        stamp_raw = now.strftime("%Y%m%d_%H%M%S_%f")
        stamp_display = now.strftime("%Y-%m-%d %H:%M:%S")
        backup_root = self.get_backup_root_dir()

        patched_files_summary = []

        try:
            for plan_number, plan in enumerate(self.file_plans, start=1):
                if self.chk_auto_backup.isChecked():
                    os.makedirs(backup_root, exist_ok=True)
                    base_name = os.path.basename(plan.resolved_path)
                    # The sequence distinguishes same-named files in one batch.
                    bak_name = f"{base_name}.{stamp_raw}.{plan_number:04d}.bak"
                    bak_path = os.path.join(backup_root, bak_name)
                    shutil.copy2(plan.resolved_path, bak_path)
                    self.record_backup_entry(
                        plan.resolved_path, bak_path, plan.operations, stamp_raw, stamp_display
                    )

                with open(plan.resolved_path, "w", encoding="utf-8", newline="") as f:
                    f.write(plan.new_text)

                patched_files_summary.append(
                    f"• {os.path.basename(plan.resolved_path)} ({len(plan.operations)} operation(s))"
                )

            backup_note = (
                "\n\nYou can restore an individual script via '🕒 Backup Manager'."
                if self.chk_auto_backup.isChecked()
                else "\n\nAutomatic backups were disabled; use version control or another backup to restore files."
            )
            QMessageBox.information(
                self,
                "Batch Patch Applied",
                f"Successfully patched {len(patched_files_summary)} script(s):\n\n"
                + "\n".join(patched_files_summary)
                + backup_note
            )
            self.clear_patch_input()

        except Exception as e:
            QMessageBox.critical(self, "Batch Write Error", f"Failed while applying patches:\n{e}")

    def open_backup_manager(self):
        dlg = BackupManagerDialog(
            self,
            backup_root=self.get_backup_root_dir(),
            on_restored_callback=self.analyze_current_patch
        )
        dlg.exec()

    # --- SETTINGS PERSISTENCE ---
    def save_settings(self):
        try:
            data = {
                "project_dir": self.txt_project_dir.text().strip(),
                "checked_files": list(self.checked_files),
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

            saved_checked = data.get("checked_files", [])
            self.checked_files = {os.path.normpath(p) for p in saved_checked if os.path.isfile(p)}

            proj_dir = data.get("project_dir", "")
            if proj_dir and os.path.isdir(proj_dir):
                self.txt_project_dir.setText(proj_dir)
                self.scan_project_folder(proj_dir)
            elif self.checked_files:
                self.project_files = sorted(self.checked_files)
                self.refresh_script_checklist()

            self.chk_auto_backup.setChecked(data.get("auto_backup", True))
        except Exception:
            pass


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = SurgicalPatcherApp()
    win.show()
    sys.exit(app.exec())