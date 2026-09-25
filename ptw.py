#!/usr/bin/env python3
"""Run pytermwm straight from a source checkout - no installation needed (only PyYAML).

    python ptw.py                      attach to (or create) the default session
    python ptw.py --standalone         everything in one process
    python ptw.py doctor               check this machine (pty / ConPTY, sockets, terminal)
    python ptw.py --web 8765 start     background session with the web UI

Same command line as `python -m pytermwm`.  On Linux/macOS use `./ptw`, on Windows `ptw.cmd`.
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
# the background server is started as `python -m pytermwm`: make sure it finds this checkout too
os.environ["PYTHONPATH"] = ROOT + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")

if sys.version_info < (3, 9):
    sys.stderr.write("pytermwm needs Python 3.9 or newer (this is %s)\n" % sys.version.split()[0])
    sys.exit(2)
try:
    import yaml  # noqa: F401
except ImportError:
    sys.stderr.write("PyYAML is missing.  Install it with:\n    %s -m pip install -r %s\n" % (sys.executable, os.path.join(ROOT, "requirements.txt")))
    sys.exit(2)

from pytermwm.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
