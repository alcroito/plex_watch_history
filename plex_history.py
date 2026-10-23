#!/usr/bin/env python3
"""Plex watch history tools; see `plex_history.py --help`."""

import os
import sys

from plexhistory.cli import main

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        # Progress is recorded per item, and the Plex target journals the item in progress.
        print("\nInterrupted; run the same command again to continue.", file=sys.stderr)
        sys.exit(130)
    except BrokenPipeError:
        # Output piped into e.g. head was closed early; silence Python's flush error at exit.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(1)
