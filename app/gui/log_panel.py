from datetime import datetime

import customtkinter as ctk


class LogPanel(ctk.CTkTextbox):
    """
    Append-only, thread-safe log widget.

    log(message)                — normal text (default foreground)
    log(message, level="warn")  — orange text  (WARN lines)
    log(message, level="error") — red text     (ERROR lines)
    """

    _TAG_COLORS = {
        "warn":  "#E67E22",   # orange
        "error": "#E74C3C",   # red
    }

    def __init__(self, master, **kwargs):
        kwargs.setdefault("state", "disabled")
        kwargs.setdefault("wrap", "word")
        kwargs.setdefault("font", ("Consolas", 12))
        super().__init__(master, **kwargs)
        # Configure colour tags directly on the underlying tk.Text widget.
        # Must be done after super().__init__() which creates self._textbox.
        for tag, colour in self._TAG_COLORS.items():
            self._textbox.tag_configure(tag, foreground=colour)

    def log(self, message: str, level: str = "normal") -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        text = f"[{timestamp}] {message}\n"
        self.after(0, self._append, text, level)

    def _append(self, text: str, level: str = "normal") -> None:
        # Work directly on the underlying tk.Text widget so that:
        # (a) we can insert text with a tag in one atomic call, and
        # (b) CTkTextbox theming does not override our tag colours.
        tb = self._textbox
        tb.configure(state="normal")
        if level in self._TAG_COLORS:
            tb.insert("end", text, (level,))
        else:
            tb.insert("end", text)
        tb.see("end")
        tb.configure(state="disabled")

    def clear(self) -> None:
        tb = self._textbox
        tb.configure(state="normal")
        tb.delete("1.0", "end")
        tb.configure(state="disabled")
