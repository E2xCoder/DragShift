"""Entry point for PyInstaller and for `python run.py`."""
import sys

from dragshift.app import main

if __name__ == "__main__":
    sys.exit(main())
