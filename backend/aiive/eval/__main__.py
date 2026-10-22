"""Entrypoint for python -m aiive.eval."""
from aiive.eval.cli import main

if __name__ == "__main__":
    import sys
    sys.exit(main())
