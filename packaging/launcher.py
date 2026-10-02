"""Entry point of the standalone app built with PyInstaller.

Starts the web interface. A .pptx passed on the command line (or dropped onto the
app icon on Windows) is opened right away.
"""
import sys

from pptx2template.ui.server import main

if __name__ == "__main__":
    sys.exit(main())
