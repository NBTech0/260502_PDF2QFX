from __future__ import annotations

import json
import os
import threading
from tkinter import filedialog, messagebox

import customtkinter as ctk
from tkinterdnd2 import TkinterDnD

from app.converter import convert_pdf_to_qfx
from app.gui.drop_zone import DropZone
from app.gui.file_list import FileListWidget
from app.gui.log_panel import LogPanel
from app.quicken_launcher import is_quicken_running, open_in_quicken

_CONFIG_PATH = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")),
    "BMOConverter", "config.json",
)


def _load_config() -> dict:
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_config(data: dict) -> None:
    """Merge *data* into the persisted config (preserves unrelated keys)."""
    os.makedirs(os.path.dirname(_CONFIG_PATH), exist_ok=True)
    existing = _load_config()
    existing.update(data)
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2)


class App(ctk.CTk, TkinterDnD.DnDWrapper):
    TkType = TkinterDnD.Tk

    def __init__(self):
        super().__init__()
        self.TkdndVersion = TkinterDnD._require(self)

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title("BMO Statement Converter")
        self.geometry("860x660")
        self.minsize(660, 520)

        cfg = _load_config()
        default_dir = os.path.expanduser("~\\Documents")
        saved_dir = cfg.get("output_dir", default_dir)
        if not os.path.isdir(saved_dir):
            saved_dir = default_dir
        self._output_dir = ctk.StringVar(value=saved_dir)
        self._output_dir.trace_add("write", self._on_output_dir_changed)

        self._open_in_quicken = ctk.BooleanVar(value=cfg.get("open_in_quicken", False))
        self._open_in_quicken.trace_add("write", self._on_open_in_quicken_changed)

        self._build_ui()

    def _on_output_dir_changed(self, *_) -> None:
        d = self._output_dir.get().strip()
        if os.path.isdir(d):
            _save_config({"output_dir": d})

    def _on_open_in_quicken_changed(self, *_) -> None:
        _save_config({"open_in_quicken": self._open_in_quicken.get()})

    def _build_ui(self) -> None:
        self.grid_rowconfigure(3, weight=2)   # file list grows
        self.grid_rowconfigure(6, weight=1)   # log panel grows
        self.grid_columnconfigure(0, weight=1)

        # ── Title ──────────────────────────────────────────────────────────
        title = ctk.CTkLabel(
            self,
            text="BMO Statement Converter",
            font=ctk.CTkFont(size=20, weight="bold"),
        )
        title.grid(row=0, column=0, padx=16, pady=(16, 4), sticky="w")

        subtitle = ctk.CTkLabel(
            self,
            text="Convert BMO Bank & Mastercard PDF statements to Quicken QFX files",
            font=ctk.CTkFont(size=13),
            text_color=("gray50", "gray60"),
        )
        subtitle.grid(row=1, column=0, padx=16, pady=(0, 10), sticky="w")

        # ── Drop zone ──────────────────────────────────────────────────────
        self._drop_zone = DropZone(
            self,
            on_drop=self._on_files_added,
            fg_color=("gray80", "gray20"),
            corner_radius=8,
        )
        self._drop_zone.grid(row=2, column=0, padx=16, pady=(0, 6), sticky="ew")

        # ── Toolbar ────────────────────────────────────────────────────────
        toolbar = ctk.CTkFrame(self, fg_color="transparent")
        toolbar.grid(row=3, column=0, padx=16, pady=(0, 4), sticky="ew")

        ctk.CTkButton(
            toolbar, text="+ Add Files", width=120,
            command=self._browse_files,
        ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            toolbar, text="✕ Clear All", width=100,
            fg_color="transparent", border_width=1,
            hover_color=("gray70", "gray30"),
            command=self._clear_all,
        ).pack(side="left")

        # ── File list ──────────────────────────────────────────────────────
        self._file_list = FileListWidget(self, corner_radius=8)
        self._file_list.grid(row=4, column=0, padx=16, pady=(0, 8), sticky="nsew")
        self.grid_rowconfigure(4, weight=2)

        # ── Output folder ──────────────────────────────────────────────────
        folder_frame = ctk.CTkFrame(self, fg_color="transparent")
        folder_frame.grid(row=5, column=0, padx=16, pady=(0, 8), sticky="ew")
        folder_frame.columnconfigure(1, weight=1)

        ctk.CTkLabel(folder_frame, text="Output Folder:", font=ctk.CTkFont(size=13)).grid(
            row=0, column=0, padx=(0, 8), sticky="w"
        )
        ctk.CTkEntry(folder_frame, textvariable=self._output_dir, font=ctk.CTkFont(size=12)).grid(
            row=0, column=1, sticky="ew", padx=(0, 8)
        )
        ctk.CTkButton(folder_frame, text="Browse", width=80, command=self._browse_output).grid(
            row=0, column=2
        )

        ctk.CTkCheckBox(
            folder_frame,
            text="Open in Quicken after successful conversion",
            variable=self._open_in_quicken,
            font=ctk.CTkFont(size=13),
        ).grid(row=1, column=0, columnspan=3, padx=(0, 0), pady=(6, 0), sticky="w")

        # ── Separator ──────────────────────────────────────────────────────
        ctk.CTkFrame(self, height=2, fg_color=("gray70", "gray30")).grid(
            row=6, column=0, padx=16, pady=(0, 8), sticky="ew"
        )

        # ── Convert button ─────────────────────────────────────────────────
        self._convert_btn = ctk.CTkButton(
            self, text="Convert All", height=40,
            font=ctk.CTkFont(size=15, weight="bold"),
            command=self._on_convert,
        )
        self._convert_btn.grid(row=7, column=0, padx=16, pady=(0, 8), sticky="ew")

        # ── Log panel ──────────────────────────────────────────────────────
        log_label = ctk.CTkLabel(self, text="Log", font=ctk.CTkFont(size=12, weight="bold"))
        log_label.grid(row=8, column=0, padx=16, pady=(0, 2), sticky="w")

        self._log = LogPanel(self, height=150, corner_radius=8)
        self._log.grid(row=9, column=0, padx=16, pady=(0, 16), sticky="nsew")
        self.grid_rowconfigure(9, weight=1)

    # ── Event handlers ─────────────────────────────────────────────────────

    def _on_files_added(self, paths: list[str]) -> None:
        self._file_list.add_files(paths)

    def _browse_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Select BMO PDF statements",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
        )
        if paths:
            self._file_list.add_files(list(paths))

    def _clear_all(self) -> None:
        self._file_list.clear()
        self._log.clear()

    def _browse_output(self) -> None:
        folder = filedialog.askdirectory(
            title="Select output folder",
            initialdir=self._output_dir.get(),
        )
        if folder:
            self._output_dir.set(folder)

    def _on_convert(self) -> None:
        paths = self._file_list.get_pending_paths()
        if not paths:
            messagebox.showinfo("Nothing to convert", "Add PDF files first, or all files are already converted.")
            return

        output_dir = self._output_dir.get().strip()
        if not output_dir or not os.path.isdir(output_dir):
            messagebox.showerror("Invalid folder", "Please select a valid output folder.")
            return

        self._convert_btn.configure(state="disabled", text="Converting...")
        self._log.log(f"Starting conversion of {len(paths)} file(s)...")

        thread = threading.Thread(
            target=self._run_conversions,
            args=(paths, output_dir),
            daemon=True,
        )
        thread.start()

    def _run_conversions(self, paths: list[str], output_dir: str) -> None:
        import time

        errors = 0
        clean_qfx: list[str] = []   # QFX paths that converted without any WARN

        for path in paths:
            self.after(0, self._file_list.set_status, path, "Converting...")

            # Per-file warning flag — must be a fresh list each iteration so
            # a warning on one file never contaminates the next.
            had_warning: list[bool] = [False]

            def _cb(msg: str, _flag: list = had_warning) -> None:
                if msg.startswith("WARN"):
                    _flag[0] = True
                self._log.log(msg)

            try:
                out_path = convert_pdf_to_qfx(path, output_dir, progress_callback=_cb)
                self.after(0, self._file_list.set_status, path, "Done")
                if not had_warning[0]:
                    clean_qfx.append(out_path)
                else:
                    self._log.log(
                        f"Skipping Quicken auto-open for {os.path.basename(out_path)}"
                        " — review warnings above before importing"
                    )
            except Exception as exc:
                errors += 1
                self.after(0, self._file_list.set_status, path, "Error")
                self._log.log(f"ERROR [{os.path.basename(path)}]: {exc}")

        summary = f"Finished. {len(paths) - errors} succeeded"
        if errors:
            summary += f", {errors} failed"
        self._log.log(summary)
        self.after(0, lambda: self._convert_btn.configure(state="normal", text="Convert All"))

        # Open each clean QFX in Quicken one at a time.
        # When Quicken is already running, back-to-back calls are fine.
        # When it needs a cold start, wait for it to finish loading before
        # sending subsequent files.
        if self._open_in_quicken.get() and clean_qfx:
            quicken_was_running = is_quicken_running()
            for i, qfx_path in enumerate(clean_qfx):
                if i > 0 and not quicken_was_running:
                    # Quicken was just launched for the first file — give it
                    # time to finish starting before passing the next one.
                    self._log.log("Waiting for Quicken to finish starting...")
                    time.sleep(8)
                open_in_quicken(qfx_path, log=self._log.log)
