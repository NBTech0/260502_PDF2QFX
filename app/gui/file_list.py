from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field

import customtkinter as ctk

from app.parsers.detector import detect_statement_type
from app.models.statement import AccountType


_TYPE_LABELS = {
    AccountType.CREDITCARD: "Mastercard",
    AccountType.CHECKING: "BMO Bank",
    AccountType.SAVINGS: "BMO Savings",
}

_STATUS_COLORS = {
    "Pending":      ("gray60", "gray40"),
    "Detecting...": ("gray60", "gray40"),
    "Converting...": ("#E67E22", "#CA6F1E"),
    "Done":    ("#27AE60", "#1E8449"),
    "Error":        ("#E74C3C", "#C0392B"),
}


@dataclass
class _FileRow:
    pdf_path: str
    frame: ctk.CTkFrame
    name_label: ctk.CTkLabel
    type_label: ctk.CTkLabel
    status_label: ctk.CTkLabel
    remove_btn: ctk.CTkButton


class FileListWidget(ctk.CTkScrollableFrame):

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self._rows: dict[str, _FileRow] = {}  # pdf_path -> row
        self._build_header()

    def _build_header(self) -> None:
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=4, pady=(4, 2))
        header.columnconfigure(0, weight=3)
        header.columnconfigure(1, weight=2)
        header.columnconfigure(2, weight=2)
        header.columnconfigure(3, minsize=30)
        font = ctk.CTkFont(size=12, weight="bold")
        ctk.CTkLabel(header, text="Filename", font=font, anchor="w").grid(row=0, column=0, sticky="w", padx=4)
        ctk.CTkLabel(header, text="Type",     font=font, anchor="w").grid(row=0, column=1, sticky="w", padx=4)
        ctk.CTkLabel(header, text="Status",   font=font, anchor="w").grid(row=0, column=2, sticky="w", padx=4)

    def add_files(self, paths: list[str]) -> None:
        for path in paths:
            if path in self._rows:
                continue
            self._add_row(path)

    def _add_row(self, path: str) -> None:
        frame = ctk.CTkFrame(self, corner_radius=6)
        frame.pack(fill="x", padx=4, pady=2)
        frame.columnconfigure(0, weight=3)
        frame.columnconfigure(1, weight=2)
        frame.columnconfigure(2, weight=2)
        frame.columnconfigure(3, minsize=34)

        name = os.path.basename(path)
        name_label = ctk.CTkLabel(frame, text=name, anchor="w", font=ctk.CTkFont(size=12))
        name_label.grid(row=0, column=0, sticky="ew", padx=(8, 4), pady=4)

        type_label = ctk.CTkLabel(frame, text="Detecting...", anchor="w", font=ctk.CTkFont(size=12))
        type_label.grid(row=0, column=1, sticky="ew", padx=4, pady=4)

        status_label = ctk.CTkLabel(frame, text="Pending", anchor="w", font=ctk.CTkFont(size=12))
        status_label.grid(row=0, column=2, sticky="ew", padx=4, pady=4)

        remove_btn = ctk.CTkButton(
            frame, text="✕", width=28, height=28,
            fg_color="transparent", hover_color=("gray70", "gray30"),
            command=lambda p=path: self.remove_file(p),
        )
        remove_btn.grid(row=0, column=3, padx=(4, 6), pady=4)

        row = _FileRow(path, frame, name_label, type_label, status_label, remove_btn)
        self._rows[path] = row

        threading.Thread(target=self._detect_type, args=(path,), daemon=True).start()

    def _detect_type(self, path: str) -> None:
        try:
            account_type = detect_statement_type(path)
            label = _TYPE_LABELS.get(account_type, account_type.value)
        except Exception:
            label = "Unknown"
        self.after(0, self._update_type_label, path, label)

    def _update_type_label(self, path: str, label: str) -> None:
        if path in self._rows:
            self._rows[path].type_label.configure(text=label)

    def remove_file(self, path: str) -> None:
        if path in self._rows:
            self._rows[path].frame.destroy()
            del self._rows[path]

    def clear(self) -> None:
        for path in list(self._rows):
            self._rows[path].frame.destroy()
        self._rows.clear()

    def get_pending_paths(self) -> list[str]:
        return [
            path for path, row in self._rows.items()
            if row.status_label.cget("text") in ("Pending", "Error")
        ]

    def set_status(self, path: str, status: str) -> None:
        if path not in self._rows:
            return
        colors = _STATUS_COLORS.get(status, ("gray60", "gray40"))
        self._rows[path].status_label.configure(text=status, text_color=colors)
