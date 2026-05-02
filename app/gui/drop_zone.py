from __future__ import annotations

import re
from typing import Callable

import customtkinter as ctk
from tkinterdnd2 import DND_FILES


class DropZone(ctk.CTkLabel):

    def __init__(self, master, on_drop: Callable[[list[str]], None], **kwargs):
        kwargs.setdefault("text", "  Drop PDF files here  ")
        kwargs.setdefault("height", 60)
        kwargs.setdefault("corner_radius", 8)
        kwargs.setdefault("font", ctk.CTkFont(size=14))
        super().__init__(master, **kwargs)
        self._on_drop = on_drop
        self.drop_target_register(DND_FILES)
        self.dnd_bind("<<Drop>>", self._handle_drop)
        self.dnd_bind("<<DragEnter>>", self._on_enter)
        self.dnd_bind("<<DragLeave>>", self._on_leave)

    def _on_enter(self, event) -> None:
        self.configure(fg_color=("gray70", "gray30"))

    def _on_leave(self, event) -> None:
        self.configure(fg_color=("gray80", "gray20"))

    def _handle_drop(self, event) -> None:
        self.configure(fg_color=("gray80", "gray20"))
        paths = self._parse_drop_data(event.data)
        pdf_paths = [p for p in paths if p.lower().endswith(".pdf")]
        if pdf_paths:
            self._on_drop(pdf_paths)

    @staticmethod
    def _parse_drop_data(data: str) -> list[str]:
        # tkinterdnd2 on Windows wraps paths with spaces in {}
        results = []
        for match in re.finditer(r'\{([^}]+)\}|(\S+)', data):
            path = match.group(1) or match.group(2)
            if path:
                results.append(path)
        return results
