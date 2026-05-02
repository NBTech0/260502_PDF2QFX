import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from app.gui.main_window import App

if __name__ == "__main__":
    app = App()
    app.mainloop()
