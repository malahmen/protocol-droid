#!/usr/bin/env python3
"""protocol-droid's entry point. protocol-droid.sh runs this; so can you."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pdroid.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
