#!/usr/bin/env python3
"""Run the test-suite straight from a checkout - nothing has to be installed (only PyYAML is needed).

    python run_tests.py                 everything that can run on this platform (Windows: the portable subset)
    python run_tests.py --portable      only the tests that need no POSIX shell (what CI requires on Windows)
    python run_tests.py --all           force the full suite, also on Windows
    python run_tests.py -k rules -v     select by name substring, verbose
    python run_tests.py --threaded-io --tcp   exercise the Windows I/O model (helper threads + loopback TCP) on Linux/macOS
    python run_tests.py --smoke         afterwards run scripts/smoke.py (real daemon, HTTP, MCP)
"""
import argparse
import os
import subprocess
import sys
import unittest
import warnings

ROOT = os.path.dirname(os.path.abspath(__file__))
PORTABLE = ["tests.test_ansi", "tests.test_keys", "tests.test_layout", "tests.test_docs", "tests.test_platform", "tests.test_web_ui",
            "tests.test_fuzz", "tests.test_golden", "tests.test_project", "tests.test_recording", "tests.test_notify",
            "tests.test_selection", "tests.test_ansiart", "tests.test_chart_glyphs"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-k", dest="pattern", action="append", help="only tests whose id contains this text (repeatable)")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("-f", "--failfast", action="store_true")
    ap.add_argument("--portable", action="store_true", help="only tests that need no POSIX shell")
    ap.add_argument("--all", action="store_true", help="run everything even on Windows")
    ap.add_argument("--threaded-io", action="store_true", help="PYTERMWM_THREADED_IO=1 (Windows-style I/O on POSIX)")
    ap.add_argument("--tcp", action="store_true", help="PYTERMWM_TCP=1 (loopback TCP endpoints instead of unix sockets)")
    ap.add_argument("--list", action="store_true", help="list the test ids and exit")
    ap.add_argument("--smoke", action="store_true", help="run scripts/smoke.py afterwards")
    args = ap.parse_args(argv)

    if args.threaded_io:
        os.environ["PYTERMWM_THREADED_IO"] = "1"
    if args.tcp:
        os.environ["PYTERMWM_TCP"] = "1"
    sys.path.insert(0, ROOT)
    try:
        import yaml  # noqa: F401
    except ImportError:
        sys.stderr.write("PyYAML is required: %s -m pip install -r requirements.txt\n" % sys.executable)
        return 2
    warnings.simplefilter("ignore", ResourceWarning)
    os.chdir(ROOT)

    loader = unittest.TestLoader()
    if args.portable or (os.name == "nt" and not args.all):
        suite = loader.loadTestsFromNames(PORTABLE)
        if os.name == "nt" and not args.all:
            print("Windows: running the portable subset (use --all for the full suite)")
    else:
        suite = loader.discover(os.path.join(ROOT, "tests"), top_level_dir=ROOT)

    def flatten(s):
        for t in s:
            if isinstance(t, unittest.TestSuite):
                yield from flatten(t)
            else:
                yield t
    tests = list(flatten(suite))
    if args.pattern:
        tests = [t for t in tests if any(p in t.id() for p in args.pattern)]
    if args.list:
        for t in tests:
            print(t.id())
        return 0
    suite = unittest.TestSuite(tests)
    result = unittest.TextTestRunner(verbosity=2 if args.verbose else 1, failfast=args.failfast).run(suite)
    code = 0 if result.wasSuccessful() else 1
    if args.smoke and code == 0:
        code = subprocess.call([sys.executable, os.path.join(ROOT, "scripts", "smoke.py")], cwd=ROOT)
    return code


if __name__ == "__main__":
    sys.exit(main())
