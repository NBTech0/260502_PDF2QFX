from datetime import datetime

import customtkinter as ctk


class LogPanel(ctk.CTkTextbox):

    def __init__(self, master, **kwargs):
        kwargs.setdefault("state", "disabled")
        kwargs.setdefault("wrap", "word")
        kwargs.setdefault("font", ("Consolas", 12))
        super().__init__(master, **kwargs)

    def log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        text = f"[{timestamp}] {message}\n"
        self.after(0, self._append, text)

    def _append(self, text: str) -> None:
        self.configure(state="normal")
        self.insert("end", text)
        self.see("end")
        self.configure(state="disabled")

    def clear(self) -> None:
        self.configure(state="normal")
        self.delete("1.0", "end")
        self.configure(state="disabled")
