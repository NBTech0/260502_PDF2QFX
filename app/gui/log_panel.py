from datetime import datetime

import customtkinter as ctk


class LogPanel(ctk.CTkTextbox):
    """
    Append-only, thread-safe log widget.

    log(message)               — normal text (default foreground)
    log(message, level="warn") — orange text  (WARN lines)
    log(message, level="error")— red text     (ERROR lines)
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
        # Configure colour tags on the underlying tk.Text widget
        for tag, colour in self._TAG_COLORS.items():
            self._textbox.tag_configure(tag, foreground=colour)

    def log(self, message: str, level: str = "normal") -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        text = f"[{timestamp}] {message}\n"
        self.after(0, self._append, text, level)

    def _append(self, text: str, level: str = "normal") -> None:
        self.configure(state="normal")
        if level in self._TAG_COLORS:
            # Record the line index before inserting so we can tag just this line
            line_before = self._textbox.index("end").split(".")[0]
            self.insert("end", text)
            line_after = self._textbox.index("end").split(".")[0]
            self._textbox.tag_add(level, f"{line_before}.0", f"{line_after}.0")
        else:
            self.insert("end", text)
        self.see("end")
        self.configure(state="disabled")

    def clear(self) -> None:
        self.configure(state="normal")
        self.delete("1.0", "end")
        self.configure(state="disabled")
