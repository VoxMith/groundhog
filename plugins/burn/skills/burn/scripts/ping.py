#!/usr/bin/env python3
"""Burn usage ping and its on/off switch. The ONLY file here that talks to the network.

The body is built here from local state only: the resolution rate (an integer 0-100 or null) and the
scope word that burn.py saved after the last run, both validated again here. No chat text, prompt,
file name or project name can travel through it.

  ping.py send                       send the ping (last step of a run)
  ping.py off | on | status | notice
"""
import datetime
import json
import os
import re
import subprocess
import sys
import urllib.request
import uuid

import state

SKILL_VERSION = "0.1.1"
DEFAULT_URL = "https://api.voxmith.com/api/v1/public/burn/ping"
SCOPES = ("device", "project")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
BODY_KEYS = ("v", "skill_version", "run_count", "resolution_rate", "sessions_judged", "sessions_resolved",
             "failed_cost_cents", "failed_share", "cost_per_resolved_cents", "agent", "listed", "scope",
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
    return (st.get("ping") == "off" or os.environ.get("BURN_NO_PING") == "1"
            or os.environ.get("DO_NOT_TRACK") == "1")


def build_body(st, rate, scope, run_count, judged=None, resolved=None, agent=None, money=(None, None, None)):
    acc, src = account()
    body = {"v": 1, "skill_version": SKILL_VERSION, "run_count": run_count, "resolution_rate": rate,
            "sessions_judged": judged, "sessions_resolved": resolved, "failed_cost_cents": money[0],
            "failed_share": money[1], "cost_per_resolved_cents": money[2], "agent": agent, "listed": True,
            "scope": scope, "account_id": acc, "account_source": src,
            "install_id": st.get("install_id") or "(created on first run)"}
    assert tuple(body) == BODY_KEYS
    return body


def _post(url, body):
    if not url.startswith(("http://", "https://")):
        raise ValueError("bad url")
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Content-Type": "application/json", "User-Agent": "burn/" + SKILL_VERSION})
    with urllib.request.urlopen(req, timeout=3) as r:
        return r.read(2048)


def send(body):
    """POST the ping; returns the server's reply (rank on the leaderboard) as a dict, or {}."""
    raw = _post(os.environ.get("BURN_PING_URL") or DEFAULT_URL, body)
    try:
        reply = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return reply if isinstance(reply, dict) else {}


def unlist(install_id):
    url = (os.environ.get("BURN_PING_URL") or DEFAULT_URL).rsplit("/", 1)[0] + "/unlist"
    _post(url, {"install_id": install_id})


def _int(v, hi):
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= hi else None


_TOKEN = re.compile(r"^[A-Za-z0-9_-]{8,40}$")


def my_link(token):
    """Your private find-me link: the board scrolled to you, with the people just above and below."""
    base = state.BOARD_URL.replace("https://", "")
    return base + "?me=" + token if token and _TOKEN.match(token) else base


def board_line(reply):
    """The one line shown after a run. Built only from validated numbers in the server's reply."""
    url = my_link(reply.get("me") if isinstance(reply.get("me"), str) else None)
    rank, of, better = _int(reply.get("rank"), 10 ** 7), _int(reply.get("of"), 10 ** 7), _int(reply.get("better_than"), 100)
    if reply.get("ranked") is True and rank == 1 and of == 1:
        return "Leaderboard: you're the first one on the Burn leaderboard. {}  Opt out: /burn off".format(url)
    if reply.get("ranked") is True and rank and of and better is not None:
        return "Leaderboard: better than {}% of Burn users (rank {} of {}). {}  Opt out: /burn off".format(better, rank, of, url)
    if reply.get("ranked") is False:
        need = _int(reply.get("min_sessions"), 10 ** 6) or 20
        return "Leaderboard: you need {} judged sessions to be ranked. {}  Opt out: /burn off".format(need, url)
    return ""


def last(st):
    """What burn.py saved after the last run, validated: (rate, scope, judged, resolved, agent)."""
    lr = st.get("last_result") if isinstance(st.get("last_result"), dict) else {}
    rate = _int(lr.get("rate"), 100)
    scope = lr.get("scope") if lr.get("scope") in SCOPES else "device"
    judged, resolved = _int(lr.get("judged"), 10 ** 6), _int(lr.get("resolved"), 10 ** 6)
    if judged is None or resolved is None or resolved > judged:
        judged = resolved = None
    agent = lr.get("agent") if lr.get("agent") in ("claude", "codex", "all") else None
    money = (_int(lr.get("failed_cents"), 10 ** 10), _int(lr.get("failed_share"), 100), _int(lr.get("cpr_cents"), 10 ** 10))
    return rate, scope, judged, resolved, agent, money


def run():
    st = state.load()
    st["run_count"] = int(st.get("run_count") or 0) + 1  # local counter, counted even when the ping is off
    st.setdefault("install_id", str(uuid.uuid4()))
    line = ""
    if not suppressed(st):
        rate, scope, judged, resolved, agent, money = last(st)
        try:
            body = build_body(st, rate, scope, st["run_count"], judged, resolved, agent, money)
            reply = send(body)
            line = board_line(reply) if scope == "device" else ""  # project runs are not ranked
            if isinstance(reply.get("me"), str) and _TOKEN.match(reply["me"]):
                st["view_token"] = reply["me"]  # the next report links straight to your spot
            res = "ok"
        except Exception:  # every failure is ignored, never shown, never blocks
            res = "failed"
        st["last_ping"] = {"result": res, "at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
    try:
        state.save(st)
    except OSError:
        pass
    if line:
        print(line)


def status():
    st = state.load()
    on = not suppressed(st)
    why = []
    if st.get("ping") == "off":
        why.append("/burn off")
    if os.environ.get("BURN_NO_PING") == "1":
        why.append("BURN_NO_PING=1")
    if os.environ.get("DO_NOT_TRACK") == "1":
        why.append("DO_NOT_TRACK=1")
    rate, scope, judged, resolved, agent, money = last(st)
    body = build_body(st, rate, scope, int(st.get("run_count") or 0) + 1, judged, resolved, agent, money)
    lp = st.get("last_ping") or {}
    print("Usage ping: " + ("ON" if on else "OFF (" + ", ".join(why) + ")"))
    print(state.DISCLOSURE)
    print("These are exactly the fields a run sends" + ("" if on else " (a run does not send these while it is off)") + ":")
    print(json.dumps(body, indent=2))
    print("last ping: " + (lp.get("result", "never") + (" at " + lp["at"] if lp.get("at") else "")))


def set_switch(value):
    st = state.load()
    st["ping"] = value
    try:
        state.save(st)
    except OSError:
        print("I couldn't save that setting here (the agent may only write inside your project). "
              "Set BURN_NO_PING=1 in your shell instead.")
        return
    if value == "off":
        removed = not st.get("install_id")  # never pinged: nothing to take down
        if st.get("install_id"):
            try:  # one last request: take this install off the public leaderboard
                unlist(st["install_id"])
                removed = True
            except Exception:
                pass
        print("Done. Burn will not tell VoxMith anything from now on" + (" and you are off the leaderboard." if removed else
              ". I couldn't reach VoxMith to take you off the leaderboard; email hello@voxmith.com to be removed.")
              + " The scan still works.")
    else:
        print("Done. " + state.DISCLOSURE + " Opt out any time: /burn off")


def notice():
    """Print the first-run footnote once (and again only if the fields sent change)."""
    st = state.load()
    if st.get("notice_shown") and st.get("notice_version") == state.NOTICE_VERSION:
        return
    print(state.FOOTNOTE)
    st["notice_shown"] = True
    st["notice_version"] = state.NOTICE_VERSION
    try:
        state.save(st)
    except OSError:  # e.g. a sandbox that only lets the agent write inside the project
        pass


def main(argv):
    if argv and argv[0] in ("off", "on", "status", "notice"):
        {"off": lambda: set_switch("off"), "on": lambda: set_switch("on"),
         "status": status, "notice": notice}[argv[0]]()
        return 0
    if argv == ["send"]:
        run()
        return 0
    print("usage: ping.py send | off | on | status | notice", file=sys.stderr)
    return 2

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
