"""Compatibility entry point: ``python server.py``.

The implementation lives in :mod:`mystat.server` so the project can be
installed as a package (``uv tool install``) with a console script.
"""

from mystat.server import main

if __name__ == "__main__":
    main()
