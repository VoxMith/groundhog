import json
import os
import shutil
import subprocess
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "skills", "groundhog", "scripts")
FIX = os.path.join(REPO, "tests", "fixtures")
sys.path.insert(0, SCRIPTS)

SID = {"my1": "00000001-aaaa-4bbb-8ccc-000000000000", "my2": "00000002-aaaa-4bbb-8ccc-000000000000",
       "bill": "00000003-aaaa-4bbb-8ccc-000000000000", "docs": "00000004-aaaa-4bbb-8ccc-000000000000"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated HOME, GROUNDHOG_HOME and a copy of the fixture history (three plain projects only)."""
    root = tmp_path / "projects"
    shutil.copytree(os.path.join(FIX, "projects"), root)
    for extra in ("-home-dev-kinds", "-home-dev-secrets"):
        shutil.rmtree(root / extra)
    now = time.time()
    for dp, _, fs in os.walk(root):
        for f in fs:
            os.utime(os.path.join(dp, f), (now, now))
    home = tmp_path / "home"
    home.mkdir()
    for k, v in {"HOME": str(home), "GROUNDHOG_HOME": str(tmp_path / "gh"), "GROUNDHOG_PROJECTS_ROOT": str(root),
                 "GIT_CONFIG_GLOBAL": str(tmp_path / "nogit"), "GIT_CONFIG_NOSYSTEM": "1", "XDG_CONFIG_HOME": str(tmp_path / "x")}.items():
        monkeypatch.setenv(k, v)
    for k in ("CLAUDE_CONFIG_DIR", "GROUNDHOG_NO_PING", "DO_NOT_TRACK", "GROUNDHOG_PING_URL", "CLAUDE_SESSION_ID", "CLAUDE_PROJECT_DIR"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def run(script, *args, stdin=None, extra_env=None):
    e = dict(os.environ)
    e.update(extra_env or {})
    return subprocess.run([sys.executable, os.path.join(SCRIPTS, script), *args], input=stdin, capture_output=True,
                          text=True, env=e, timeout=60)


def gh(*args, **kw):
    return run("groundhog.py", *args, **kw)
