#!/usr/bin/env python3
"""Groundhog usage ping and its on/off switch. The ONLY file here that talks to the network.

The body is built here from two validated values (--top-count, --scope) plus local state.
It never reads scan output, so no chat text or instruction text can travel through it.

  ping.py --top-count N --scope session|project|all   send the ping (last step of a run)
  ping.py off | on | status | notice
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import urllib.request
import uuid

import state

SKILL_VERSION = "0.1.0"
DEFAULT_URL = "https://api.voxmith.com/api/v1/public/groundhog/ping"
SCOPES = ("session", "project", "all")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
BODY_KEYS = ("v", "skill_version", "run_count", "top_count", "scope",
             "account_id", "account_source", "install_id")


def _clean_email(v):
    if not isinstance(v, str):
        return None
    v = v.strip().lower()
    return v if len(v) <= 254 and EMAIL_RE.match(v) else None


def account():
    """(account_id, source). Claude Code account email first, then git email, else none."""
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    cfg = os.path.join(base, ".claude.json") if base else os.path.join(os.path.expanduser("~"), ".claude.json")
    try:
        with open(cfg) as f:
            email = _clean_email(((json.load(f) or {}).get("oauthAccount") or {}).get("emailAddress"))
        if email:  # only this one key is ever read out of the file
            return email, "claude"
    except Exception:
        pass
    try:
        out = subprocess.run(["git", "config", "user.email"], capture_output=True, text=True, timeout=2)
        email = _clean_email(out.stdout) if out.returncode == 0 else None
        if email:
            return email, "git"
    except Exception:
        pass
    return None, "none"


def suppressed(st):
    return (st.get("ping") == "off" or os.environ.get("GROUNDHOG_NO_PING") == "1"
            or os.environ.get("DO_NOT_TRACK") == "1")


def build_body(st, top_count, scope, run_count):
    acc, src = account()
    body = {"v": 1, "skill_version": SKILL_VERSION, "run_count": run_count, "top_count": top_count,
            "scope": scope, "account_id": acc, "account_source": src,
            "install_id": st.get("install_id") or "(created on first run)"}
    assert tuple(body) == BODY_KEYS
    return body


def send(body):
    url = os.environ.get("GROUNDHOG_PING_URL") or DEFAULT_URL
    if not url.startswith(("http://", "https://")):
        raise ValueError("bad url")
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Content-Type": "application/json", "User-Agent": "groundhog/" + SKILL_VERSION})
    with urllib.request.urlopen(req, timeout=3) as r:
        r.read(1)


def run(top_count, scope):
    st = state.load()
    st["run_count"] = int(st.get("run_count") or 0) + 1  # local counter, counted even when the ping is off
    st.setdefault("install_id", str(uuid.uuid4()))
    if not suppressed(st):
        try:
            send(build_body(st, top_count, scope, st["run_count"]))
            res = "ok"
        except Exception:  # every failure is ignored, never shown, never blocks
            res = "failed"
        st["last_ping"] = {"result": res, "at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
    try:
        state.save(st)
    except OSError:
        pass


def status():
    st = state.load()
    on = not suppressed(st)
    why = []
    if st.get("ping") == "off":
        why.append("/groundhog off")
    if os.environ.get("GROUNDHOG_NO_PING") == "1":
        why.append("GROUNDHOG_NO_PING=1")
    if os.environ.get("DO_NOT_TRACK") == "1":
        why.append("DO_NOT_TRACK=1")
    lr = st.get("last_result") or {}
    body = build_body(st, int(lr.get("top_count") or 0), lr.get("scope") or "all",
                      int(st.get("run_count") or 0) + 1)
    lp = st.get("last_ping") or {}
    print("Usage ping: " + ("ON" if on else "OFF (" + ", ".join(why) + ")"))
    print(state.DISCLOSURE)
    print("These are exactly the fields a run sends" + ("" if on else " (a run does not send these while it is off)") + ":")
    print(json.dumps(body, indent=2))
    print("last ping: " + (lp.get("result", "never") + (" at " + lp["at"] if lp.get("at") else "")))


def set_switch(value):
    st = state.load()
    st["ping"] = value
    state.save(st)
    if value == "off":
        print("Done. Groundhog will not tell VoxMith anything from now on. The scan still works.")
    else:
        print("Done. " + state.DISCLOSURE + " Turn it off any time: /groundhog off")


def notice():
    """Print the first-run footnote once (and again only if the fields sent change)."""
    st = state.load()
    if st.get("notice_shown") and st.get("notice_version") == state.NOTICE_VERSION:
        return
    print(state.FOOTNOTE)
    st["notice_shown"] = True
    st["notice_version"] = state.NOTICE_VERSION
    state.save(st)


def main(argv):
    if argv and argv[0] in ("off", "on", "status", "notice"):
        {"off": lambda: set_switch("off"), "on": lambda: set_switch("on"),
         "status": status, "notice": notice}[argv[0]]()
        return 0
    ap = argparse.ArgumentParser(prog="ping.py")
    ap.add_argument("--top-count", type=int, required=True)
    ap.add_argument("--scope", choices=SCOPES, required=True)
    a = ap.parse_args(argv)
    if not 0 <= a.top_count <= 100000:
        print("--top-count must be 0..100000", file=sys.stderr)
        return 2
    run(a.top_count, a.scope)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
