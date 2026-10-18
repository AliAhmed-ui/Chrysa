#!/usr/bin/env python3
"""Shim: the torrent search code lives in chrysa/bd.py so the app can import it."""
from chrysa.bd import main
import sys

if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        sys.exit(130)
