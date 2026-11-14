#!/usr/bin/env python3
"""Open a Groundhog report in the default browser. Prints OPENED or NOT_OPENED, always exits 0.

Only accepts a file named groundhog-report-*.html inside the system temp folder. Anything else is ignored.
This is the only file that imports webbrowser. It sends nothing anywhere itself.
"""
import fnmatch
import os
import sys
import tempfile
import webbrowser

import state


def ok(path):
    try:
        real = os.path.realpath(path)
        tmp = os.path.realpath(tempfile.gettempdir())
        return (os.path.dirname(real) == tmp and fnmatch.fnmatch(os.path.basename(real), "groundhog-report-*.html")
                and os.path.isfile(real) and real)
    except Exception:
        return False


def main(argv):
    opened = False
    try:
        real = ok(argv[0]) if len(argv) == 1 else False
        if real and os.environ.get("GROUNDHOG_NO_OPEN") != "1":
            opened = bool(webbrowser.open("file://" + real))
    except Exception:
        opened = False
    try:
        rep = state.read_json("report.json", {})
        if rep:
            rep["opened"] = opened
            state.write_private("report.json", rep)
    except Exception:
        pass
    print("OPENED" if opened else "NOT_OPENED")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
