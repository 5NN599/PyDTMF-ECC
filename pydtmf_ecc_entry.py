# -*- coding: utf-8 -*-
"""Production entry point for PyDTMF-ECC."""
from __future__ import annotations
import sys
import ctypes


def _show_start_error(exc: Exception) -> None:
    message = (
        "PyDTMF-ECC could not start.\n\n"
        f"Reason: {type(exc).__name__}: {exc}\n\n"
        "Check that Windows is up to date and try launching the app again."
    )
    try:
        if sys.platform.startswith("win"):
            ctypes.windll.user32.MessageBoxW(0, message, "PyDTMF-ECC", 0x10)
        else:
            print(message, file=sys.stderr)
    except Exception:
        print(message, file=sys.stderr)


def main() -> int:
    try:
        import audio_file_transfer_gui as gui
        app = gui.App()
        app.mainloop()
        return 0
    except Exception as exc:
        _show_start_error(exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
