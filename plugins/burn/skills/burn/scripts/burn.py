#!/usr/bin/env python3
"""Burn: what did your coding agent's failures cost you?

Reads local Claude Code and Codex history, decides with fixed rules which sessions failed,
and adds up their tokens at API prices. Python 3.9+, standard library only, NO network.
Every number and label is computed here; the model only shows the output.

  burn.py [--scope device|project] [--project-dir DIR] [--session-id ID]   summary + HTML report
  burn.py ... --json                                                       result JSON only (tests)
"""
import argparse
import collections
import datetime
import difflib
import html
import json
import os
import re
import sys
import tempfile
import time
from decimal import ROUND_HALF_UP, Decimal

import state

VERSION = "0.1.1"
RULES_VERSION = 2
MIN_HUMAN = 2            # fewer human messages: "too short to judge"
MIN_CORRECTIONS = 2      # corrections needed for "corrected"
ACTIVE_SECONDS = 600     # a session file changed this recently is still running: not counted
HERE = os.path.dirname(os.path.abspath(__file__))
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
M = Decimal(1000000)

# ---------------------------------------------------------------- rules (RULES_VERSION 1)

_CORRECT_START = re.compile(
    r"(?i)^\s*(?:no(?:[,.!]|\s*$)|nope\b|wrong\b|stop\b|undo\b|revert\b|why did you\b|i said\b|"
    r"again[,:]|that'?s (?:wrong|not (?:right|it|what i))|that is wrong|this is wrong|don'?t do that)")
_CORRECT_ANY = re.compile(
    r"(?i)\b(?:not what i (?:asked|wanted|meant)|you broke|that'?s wrong|that is wrong|"
    r"(?:revert|undo) (?:that|this|it|the|your|what)|why did you)\b")
_GIVE_UP = re.compile(
    r"(?i)\b(?:forget it|never ?mind|nvm|i'?ll do it myself|i'?ll just do it|i'?ll handle (?:it|this)|"
    r"i will handle (?:it|this)|(?:handle|do) (?:it|this) (?:manually|by hand)|this isn'?t working|"
    r"this is not working|this is useless|give up|giving up)\b")
# A "no" opener that is really an answer ("No, that looks good", "No. Ship it") or a give-up phrase that
# ends well ("never mind, found it, thanks") is not a failure.
_POSITIVE = re.compile(
    r"(?i)\b(?:looks? (?:good|great|fine|right)|that'?s (?:good|great|fine|perfect|all|it)|perfect|ship it|"
    r"thanks|thank you|thx|lgtm|all good|nothing else|works now|it works|found it|figured it out|got it|"
    r"fixed it|my bad)\b")
_NO_OPENER = re.compile(r"(?i)^\s*(?:no(?:[,.!]|\s*$)|nope\b)")

# Copied from Groundhog (scripts/groundhog.py) so both tools agree on what an instruction is.
_OPENER = re.compile(r"(?i)\b(?:don'?t|do not|never|stop|no more|without|avoid|always|make sure|remember to|"
                     r"you should|instead of|only|keep it|i said|again|as i told you|why did you)\b|\buse\b.+\bnot\b")
_CORRECT = re.compile(r"(?i)^(?:no[,.!]|no$|nope|wrong|that'?s not what i)")
_FILLER = re.compile(r"(?i)^(?:yes|yeah|yep|ok|okay|sure|thanks|thank you|continue|go ahead|go on|proceed|"
                     r"lgtm|looks good|do it|done|great|good|perfect|nice)\b[\s.!,]*(?:thanks|thank you|please)?[\s.!]*$")
_LEAD = re.compile(r"^(?:(?:please|also|and|ok|okay|hey|so)\b[\s,.:-]*)+")
_TRAIL = re.compile(r"[\s,.!]*(?:thanks|thank you|thx)[\s.!]*$")
_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def norm(t):
    t = t.replace("’", "'").lower()
    t = re.sub(r"\s+", " ", t).strip()
    t = _LEAD.sub("", t)
    t = _TRAIL.sub("", t)
    return t.strip(" .!?,;:")


def _is_rule(s):
    s = s.replace("’", "'")
    return bool(_OPENER.search(s) or _CORRECT.search(s.strip()))


def candidates_from(text, follows_assistant, after_interrupt):
    words = len(text.split())
    sents = [s.strip() for s in _SPLIT.split(text) if s.strip()]
    if len(text) > 600:
        return [s for s in sents if _is_rule(s)]
    if words < 25 and (follows_assistant or after_interrupt):
        if _is_rule(text) or (words >= 3 and not _FILLER.match(text.strip())):
            return [text]
        return []
    return [s for s in sents if _is_rule(s)]


def _similar(a, b, ta, tb):
    if ta and tb and len(ta & tb) / len(ta | tb) >= 0.6:
        return True
    sm = difflib.SequenceMatcher(None, a, b)
    return sm.real_quick_ratio() >= 0.85 and sm.quick_ratio() >= 0.85 and sm.ratio() >= 0.85
# ---------------------------------------------------------------- end of copied code


def is_correction(text):
    t = text.replace("’", "'")[:300]
    if _CORRECT_ANY.search(t):
        return True
    if not _CORRECT_START.search(t):
        return False
    return not (_NO_OPENER.search(t) and _POSITIVE.search(t[:80]))


def is_give_up(text):
    t = text.replace("’", "'")[:300]
    return bool(_GIVE_UP.search(t)) and not _POSITIVE.search(t)


def classify(events):
    """events: ordered list of ("H", text, follows_agent), ("A",) an agent turn, ("I",) an interrupt (Esc),
    ("R",) the user rolled the thread back (Codex). -> dict of verdicts. Rules version 2."""
    humans = [e for e in events if e[0] == "H"]
    # corrected: a message right after the agent that corrects it, or a rollback. Esc alone is steering, not a
    # correction: only the message you type after it decides.
    corrections = sum(1 for e in events if e[0] == "R" or (e[0] == "H" and e[2] and is_correction(e[1])))
    # repeated: two different messages carry the same instruction (a do/don't rule, or 5+ words)
    cands, after_int = [], False
    for k, e in enumerate(events):
        if e[0] != "H":
            after_int = after_int or e[0] == "I"
            continue
        for s in candidates_from(e[1], e[2], after_int):
            n = norm(s[:200])
            if len(n.split()) >= 3 and (_is_rule(s) or len(n.split()) >= 5):
                cands.append((k, n, set(re.findall(r"\w+", n))))
        after_int = False
    repeated = any(a[0] != b[0] and _similar(a[1], b[1], a[2], b[2])
                   for i, a in enumerate(cands) for b in cands[i + 1:])
    # gave up: the last message gives up ("forget it", not "never mind, found it"), or is a correction
    # the agent never got to answer
    gave_up = False
    if humans:
        last_i = max(k for k, e in enumerate(events) if e[0] == "H")
        last = events[last_i][1]
        answered = any(e[0] == "A" for e in events[last_i + 1:])
        gave_up = is_give_up(last) or (is_correction(last) and not answered)
    return {"human": len(humans), "corrections": corrections,
            "corrected": corrections >= MIN_CORRECTIONS, "repeated": repeated, "gave_up": gave_up}


# ---------------------------------------------------------------- prices

def load_prices():
    with open(os.path.join(HERE, "..", "prices.json")) as f:
        return json.load(f)


def model_key(m):
    m = (m or "").lower().strip()
    m = re.sub(r"\[.*?\]$", "", m)
    m = re.sub(r"(?:-\d{8}|@\d{8}|-\d{4}-\d{2}-\d{2})$", "", m)
    return m


def D(x):
    return Decimal(str(x))


def claude_cost(table, model, u):
    """u: dict in, out, cw5, cw1, cr, fast(bool), us(bool). -> Decimal or None (unknown model)."""
    p = table.get(model_key(model))
    if p is None:
        return None
    if "over_100k" in p and u["in"] + u["cw5"] + u["cw1"] + u["cr"] > 100000:
        p = dict(p, **p["over_100k"])
    k = Decimal(1)
    pin, pout = D(p["input"]), D(p["output"])
    if u["fast"] and "fast" in p:
        k = D(p["fast"]["input"]) / D(p["input"])  # ponytail: page lists no fast cache rates; scaled by the same ratio
        pin, pout = D(p["fast"]["input"]), D(p["fast"]["output"])
    c = (u["in"] * pin + u["out"] * pout + u["cw5"] * D(p["cache_write_5m"]) * k
         + u["cw1"] * D(p["cache_write_1h"]) * k + u["cr"] * D(p["cache_read"]) * k) / M
    return c * D("1.1") if u["us"] else c


def codex_cost(table, model, d):
    p = table.get(model_key(model))
    if p is None:
        return None
    fresh = max(d["in"] - d["cached"], 0)
    return (fresh * D(p["input"]) + d["cached"] * D(p["cached_input"]) + d["out"] * D(p["output"])) / M


# ---------------------------------------------------------------- reading files

def _iso(ts):
    try:
        return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(datetime.timezone.utc)
    except Exception:
        return None


def _label(path):
    return os.path.basename((path or "").replace("\\", "/").rstrip("/")) or "unknown"


def claude_root():
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(base, "projects")


def codex_root():
    return os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")


def _mtime(p):
    try:
        return os.path.getmtime(p)
    except OSError:
        return 0


def list_claude():
    """-> sorted [(session_id, main_path_or_None, [subagent paths])]."""
    root, out = claude_root(), {}
    try:
        projects = sorted(e.path for e in os.scandir(root) if e.is_dir())
    except OSError:
        return []
    for d in projects:
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError:
            continue
        for e in entries:
            if e.is_file() and e.name.endswith(".jsonl"):
                out.setdefault(e.name[:-6], [None, []])[0] = e.path
            elif e.is_dir():
                sub = os.path.join(e.path, "subagents")
                try:
                    subs = sorted(x.path for x in os.scandir(sub) if x.is_file() and x.name.endswith(".jsonl"))
                except OSError:
                    subs = []
                if subs:
                    out.setdefault(e.name, [None, []])[1].extend(subs)
    return sorted((sid, v[0], sorted(v[1])) for sid, v in out.items())


def list_codex():
    """-> (sorted [path], compressed_count)."""
    files, zst = [], 0
    for sub in ("sessions", "archived_sessions"):
        for dp, dns, fns in os.walk(os.path.join(codex_root(), sub)):
            dns.sort()
            for fn in fns:
                if fn.startswith("rollout-") and fn.endswith(".jsonl"):
                    files.append(os.path.join(dp, fn))
                elif fn.startswith("rollout-") and fn.endswith(".jsonl.zst"):
                    files.append(os.path.join(dp, fn))
                    zst += 1
    return sorted(files), zst


def _open_text(path):
    if path.endswith(".zst"):
        from compression import zstd  # Python 3.14+ only; caller catches ImportError
        return zstd.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, encoding="utf-8", errors="replace")


def _human_text(o):
    """Claude Code user line -> typed text, "I" for an interrupt, or None. Same keep-rule as Groundhog."""
    if o.get("isSidechain") or o.get("isMeta") or o.get("isCompactSummary"):
        return None
    msg = o.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = [b.get("text") for b in content if isinstance(b, dict) and b.get("type") == "text"
                 and isinstance(b.get("text"), str)]
        if not parts:
            return None
        text = "\n".join(parts)
    else:
        return None
    t = text.strip()
    if t.startswith("[Request interrupted"):
        return "I"
    origin, ps = o.get("origin"), o.get("promptSource")
    kind = origin.get("kind") if isinstance(origin, dict) else None
    if not (kind == "human" or (origin is None and (ps is None or ps in ("typed", "queued", "suggestion_accepted")))):
        return None
    if not t or t.startswith(("<", "[Pasted", "This session is being continued")) or t.lower().startswith("/burn"):
        return None
    return t


def read_claude_file(path, sid, is_sub, recs, sess):
    """Adds usage records to recs[(msg id, request id)] (keeps the best key) and events to sess."""
    s = sess.setdefault(("claude", sid), new_session("claude", sid))
    prev_agent = False
    with open(path, encoding="utf-8", errors="replace") as f:
        for n, line in enumerate(f):
            ua = '"type":"user"' in line or '"type": "user"' in line
            aa = '"type":"assistant"' in line or '"type": "assistant"' in line
            if not (ua or aa):
                if not is_sub and ('"custom-title"' in line or '"ai-title"' in line):
                    try:
                        o = json.loads(line)
                    except ValueError:
                        continue
                    for key, field in (("customTitle", "custom_title"), ("aiTitle", "ai_title")):
                        if isinstance(o, dict) and isinstance(o.get(key), str) and o[key].strip():
                            s[field] = o[key].strip()  # the latest one wins, as in Claude Code's own list
                continue
            try:
                o = json.loads(line)
            except ValueError:
                s["bad_lines"] += 1
                continue
            if not isinstance(o, dict):
                continue
            ts = _iso(o.get("timestamp"))
            if not is_sub:
                if ts:
                    s["start"] = min(s["start"] or ts, ts)
                if not s["cwd"] and isinstance(o.get("cwd"), str):
                    s["cwd"] = o["cwd"]
            if o.get("type") == "assistant":
                if not prev_agent and not is_sub:
                    s["events"].append(("A",))
                prev_agent = True
                msg = o.get("message") if isinstance(o.get("message"), dict) else {}
                u = msg.get("usage")
                model = msg.get("model")
                if not isinstance(u, dict) or model == "<synthetic>":
                    continue
                key = (str(msg.get("id")), str(o.get("requestId")))
                if key == ("None", "None"):
                    key = (path, str(n))
                pref = 0 if (o.get("sessionId") in (sid, None)) else 1
                rank = (ts.isoformat() if ts else "9999", pref, path, n)
                if key not in recs or rank < recs[key][0]:
                    recs[key] = (rank, sid, model, _claude_usage(u))
            elif o.get("type") == "user" and not is_sub:
                t = _human_text(o)
                if t == "I":
                    s["events"].append(("I",))
                elif t:
                    s["events"].append(("H", t, prev_agent))
                    prev_agent = False


def _int(v):
    return v if isinstance(v, int) and v > 0 else 0


def _claude_usage(u):
    cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else None
    total_cw = _int(u.get("cache_creation_input_tokens"))
    if cc:
        cw5, cw1 = _int(cc.get("ephemeral_5m_input_tokens")), _int(cc.get("ephemeral_1h_input_tokens"))
        cw5 += max(total_cw - cw5 - cw1, 0)
    else:
        cw5, cw1 = total_cw, 0
    return {"in": _int(u.get("input_tokens")), "out": _int(u.get("output_tokens")), "cw5": cw5, "cw1": cw1,
            "cr": _int(u.get("cache_read_input_tokens")), "fast": u.get("speed") == "fast",
            "us": str(u.get("inference_geo") or "").lower() == "us"}


_INJECTED = ("<", "# AGENTS.md", "#AGENTS.md")
_TOK = ("input_tokens", "cached_input_tokens", "output_tokens")


def _codex_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text") for b in content if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def read_codex_file(path):
    """One rollout file -> dict(id, cwd, start, forked_from, events, tokens=[(ts, totals, model)], bad)."""
    r = {"id": None, "cwd": None, "start": None, "forked_from": None, "events": [], "tokens": [], "bad": 0}
    model, prev_agent, legacy, paged = None, False, [], []

    def agent_turn(prev):
        if not prev:
            r["events"].append(("A",))
        return True
    # A forked thread may start with a copy of its parent's lines (re-stamped, so timestamps don't help),
    # ended by its first thread_settings_applied event. Copied token totals move the running total but
    # add no tokens; copied messages are not this session's. No marker in a fork: nothing is treated as copied.
    copying, marker, cut = False, False, 0
    with _open_text(path) as f:
        for line in f:
            try:
                o = json.loads(line)
            except ValueError:
                r["bad"] += 1
                continue
            if not isinstance(o, dict):
                continue
            typ, p = o.get("type"), o.get("payload") if isinstance(o.get("payload"), dict) else {}
            ts = _iso(o.get("timestamp"))
            if ts:
                r["start"] = min(r["start"] or ts, ts)
            if typ == "session_meta":
                if r["id"] is None:  # the first meta is this thread's own
                    r["id"], r["cwd"], r["forked_from"] = p.get("id"), p.get("cwd"), p.get("forked_from_id")
                    copying = bool(r["forked_from"])
            elif typ == "turn_context":
                model = p.get("model") or model
                r["cwd"] = r["cwd"] or p.get("cwd")
            elif typ == "response_item":
                if p.get("role") == "assistant":
                    prev_agent = agent_turn(prev_agent)
            elif typ == "event_msg":
                et = p.get("type")
                if et == "token_count":
                    tot = (p.get("info") or {}).get("total_token_usage") if isinstance(p.get("info"), dict) else None
                    if isinstance(tot, dict):
                        r["tokens"].append([copying, {k: _int(tot.get(k)) for k in _TOK}, model])
                elif et == "thread_settings_applied" and copying:
                    copying, marker, cut = False, True, len(r["events"])
                elif et in ("agent_message", "agent_reasoning"):
                    prev_agent = agent_turn(prev_agent)
                elif et == "user_message":
                    t = (p.get("message") or "").strip() if isinstance(p.get("message"), str) else ""
                    if t and not t.startswith(_INJECTED):
                        legacy.append((len(r["events"]), ("H", t, prev_agent)))
                        r["events"].append(None)  # placeholder, filled below
                        prev_agent = False
                elif et == "item_completed":
                    it = p.get("item") if isinstance(p.get("item"), dict) else {}
                    if it.get("type") == "UserMessage":
                        t = _codex_text(it.get("content")).strip()
                        if t and not t.startswith(_INJECTED):
                            paged.append((len(r["events"]), ("H", t, prev_agent)))
                            r["events"].append(None)
                            prev_agent = False
                    elif it.get("type") in ("AgentMessage", "Reasoning"):
                        prev_agent = agent_turn(prev_agent)
                elif et == "turn_aborted" and p.get("reason") == "interrupted":
                    r["events"].append(("I",))
                elif et == "thread_rolled_back":
                    r["events"].append(("R",))
    if r["forked_from"] and not marker:  # fork without a copy marker: nothing was copied
        for t in r["tokens"]:
            t[0] = False
    # a file has either the old user_message lines or the new item_completed ones; never count both
    keep = paged if paged else legacy
    for i, e in keep:
        r["events"][i] = e
    r["events"] = [e for e in r["events"][cut:] if e is not None]
    return r


def new_session(agent, sid):
    return {"agent": agent, "id": sid, "cwd": None, "start": None, "events": [], "bad_lines": 0,
            "custom_title": None, "ai_title": None,
            "models": collections.defaultdict(lambda: {"tokens": 0, "cost": Decimal(0), "priced": True}),
            "cost": Decimal(0), "tokens": 0, "unpriced": 0}


def add_tokens(s, model, tokens, cost):
    m = s["models"][model_key(model) or "unknown"]
    m["tokens"] += tokens
    s["tokens"] += tokens
    if cost is None:
        m["priced"] = False
        s["unpriced"] += tokens
    else:
        m["cost"] += cost
        s["cost"] += cost


# ---------------------------------------------------------------- scan

def session_name(s, width=60):
    """The name you gave the session, else Claude Code's own title, else your first message (first line)."""
    name = s.get("custom_title") or s.get("ai_title")
    if not name:
        first = next((e[1] for e in s["events"] if e[0] == "H"), "")
        name = first.strip().splitlines()[0] if first.strip() else ""
    name = re.sub(r"\s+", " ", name).strip()
    return (name[:width - 1].rstrip() + "\u2026") if len(name) > width else (name or "(no name)")


def _in_project(cwd, project_dir):
    if not project_dir:
        return True
    if not cwd:
        return False
    a, b = os.path.normpath(cwd), os.path.normpath(project_dir)
    return a == b or a.startswith(b.rstrip(os.sep) + os.sep)


def scan(scope="device", project_dir=None, session_id=None, now=None, agent="all"):
    """agent: "claude" or "codex" reads only that agent's history; "all" reads both."""
    now = time.time() if now is None else now
    prices = load_prices()
    sess, recs = {}, {}
    info = {"current_excluded": False, "still_running": 0, "compressed_skipped": 0, "unreadable": 0}

    def skip(sid, paths):
        if session_id and sid == session_id:
            info["current_excluded"] = True
            return True
        if max(_mtime(p) for p in paths) > now - ACTIVE_SECONDS:
            info["still_running"] += 1
            return True
        return False

    for sid, main, subs in (list_claude() if agent in ("all", "claude") else []):
        paths = ([main] if main else []) + subs
        if skip(sid, paths):
            continue
        for p in paths:
            try:
                read_claude_file(p, sid, p != main, recs, sess)
            except OSError:
                info["unreadable"] += 1
    for key in sorted(recs, key=lambda k: recs[k][0]):
        _, sid, model, u = recs[key]
        tok = u["in"] + u["out"] + u["cw5"] + u["cw1"] + u["cr"]
        add_tokens(sess[("claude", sid)], model, tok, claude_cost(prices["anthropic"], model, u))

    files, _ = list_codex() if agent in ("all", "codex") else ([], 0)
    rollouts = []
    for p in files:
        if p.endswith(".zst"):
            try:
                import compression.zstd  # noqa: F401
            except ImportError:
                info["compressed_skipped"] += 1
                continue
        sid_guess = re.sub(r"\.jsonl(\.zst)?$", "", os.path.basename(p))[-36:]
        if skip(sid_guess, [p]):
            continue
        try:
            r = read_codex_file(p)
        except OSError:
            info["unreadable"] += 1
            continue
        r["id"] = r["id"] or sid_guess
        if session_id and r["id"] == session_id:
            info["current_excluded"] = True
            continue
        rollouts.append((r["start"].isoformat() if r["start"] else "9999", p, r))
    rollouts.sort(key=lambda x: (x[0], x[1]))
    for _, p, r in rollouts:
        s = sess.setdefault(("codex", r["id"]), new_session("codex", r["id"]))
        s["cwd"], s["start"], s["bad_lines"] = s["cwd"] or r["cwd"], r["start"], s["bad_lines"] + r["bad"]
        s["events"].extend(r["events"])
        top = {k: 0 for k in _TOK}
        for copied, tot, model in r["tokens"]:
            d = {k: max(tot[k] - top[k], 0) for k in _TOK}
            for k in _TOK:
                top[k] = max(top[k], tot[k])
            if copied:
                continue
            dd = {"in": d["input_tokens"], "cached": min(d["cached_input_tokens"], d["input_tokens"]),
                  "out": d["output_tokens"]}
            if dd["in"] or dd["out"]:
                add_tokens(s, model, dd["in"] + dd["out"], codex_cost(prices["openai"], model, dd))

    out = []
    for (agent, sid), s in sorted(sess.items()):
        if scope == "project" and not _in_project(s["cwd"], project_dir):
            continue
        v = classify(s["events"])
        s.update(v)
        s["counted"] = v["human"] >= MIN_HUMAN
        s["failed"] = s["counted"] and (v["corrected"] or v["repeated"] or v["gave_up"])
        s["project"] = _label(s["cwd"])
        s["name"] = session_name(s)
        out.append(s)
    return out, info


# ---------------------------------------------------------------- numbers

def money(d):
    return str(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def fmt_money(s):
    return "$" + "{:,.2f}".format(Decimal(s))


def cents(s):
    return int((Decimal(s) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def approx_money(s):
    """"~$415": whole dollars, for the share card and share text."""
    return "~$" + "{:,}".format(int(Decimal(s).quantize(Decimal(1), rounding=ROUND_HALF_UP)))


def fmt_tokens(n):
    for size, suf in ((10 ** 9, "B"), (10 ** 6, "M"), (10 ** 3, "K")):
        if n >= size:
            v = (Decimal(n) / size).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
            return "{}{}".format(v, suf)
    return str(n)


def _date(dt):
    return dt.strftime("%Y-%m-%d") if dt else None


def reasons(s):
    r = []
    if s["corrected"]:
        r.append("corrected x{}".format(s["corrections"]))
    if s["repeated"]:
        r.append("repeated yourself")
    if s["gave_up"]:
        r.append("gave up")
    return r


def pct(num, den):
    return None if not den else int((Decimal(num) * 100 / den).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def compute(sessions, info, scope, project_dir, agent="all"):
    counted = [s for s in sessions if s["counted"]]
    failed = [s for s in counted if s["failed"]]
    resolved = len(counted) - len(failed)
    total = sum((s["cost"] for s in sessions), Decimal(0))
    burned = sum((s["cost"] for s in failed), Decimal(0))
    rate = pct(resolved, len(counted))

    def row(s):
        return {"agent": s["agent"], "session": s["id"], "name": s["name"], "date": _date(s["start"]), "project": s["project"],
                "reasons": reasons(s), "cost": money(s["cost"]), "tokens": s["tokens"]}
    top = sorted(failed, key=lambda s: (-s["cost"], -s["tokens"], s["agent"], s["id"]))

    def group(keyf):
        g = collections.OrderedDict()
        for s in sorted(sessions, key=lambda s: (s["agent"], s["id"])):
            k = keyf(s)
            if k is None:
                continue
            x = g.setdefault(k, {"sessions": 0, "counted": 0, "resolved": 0, "cost": Decimal(0),
                                 "burned": Decimal(0), "tokens": 0})
            x["sessions"] += 1
            x["counted"] += s["counted"]
            x["resolved"] += s["counted"] and not s["failed"]
            x["cost"] += s["cost"]
            x["burned"] += s["cost"] if s["failed"] else 0
            x["tokens"] += s["tokens"]
        return [dict(key=k, sessions=x["sessions"], counted=x["counted"], resolved=x["resolved"],
                     rate=pct(x["resolved"], x["counted"]),
                     cost=money(x["cost"]), burned=money(x["burned"]), tokens=x["tokens"]) for k, x in g.items()]

    def monday(s):
        return _date(s["start"] - datetime.timedelta(days=s["start"].weekday())) if s["start"] else None
    weeks = sorted(group(monday), key=lambda r: r["key"])
    projects = sorted(group(lambda s: s["project"]), key=lambda r: (-Decimal(r["burned"]), -Decimal(r["cost"]), r["key"]))
    agents = sorted(group(lambda s: s["agent"]), key=lambda r: r["key"])
    models = collections.defaultdict(lambda: {"tokens": 0, "cost": Decimal(0), "priced": True, "counted": 0, "resolved": 0})
    for s in sessions:
        for mk, m in s["models"].items():
            x = models[mk]
            x["tokens"] += m["tokens"]
            x["cost"] += m["cost"]
            x["priced"] = x["priced"] and m["priced"]
        if s["counted"] and s["models"]:  # a session is judged under the model that used most of its tokens
            main = sorted(s["models"].items(), key=lambda kv: (-kv[1]["tokens"], kv[0]))[0][0]
            models[main]["counted"] += 1
            models[main]["resolved"] += not s["failed"]
    model_rows = sorted(({"model": k, "counted": x["counted"], "rate": pct(x["resolved"], x["counted"]), "tokens": x["tokens"],
                          "cost": money(x["cost"]) if x["priced"] else None} for k, x in models.items()),
                        key=lambda r: (-r["tokens"], r["model"]))
    starts = {}
    for s in sessions:
        if s["start"]:
            starts[s["agent"]] = min(starts.get(s["agent"], s["start"]), s["start"])
    return {
        "version": VERSION, "rules_version": RULES_VERSION, "scope": scope, "agent": agent,
        "project": _label(project_dir) if scope == "project" else None,
        "agents": sorted({s["agent"] for s in sessions}),
        "sessions": len(sessions), "counted": len(counted), "too_short": len(sessions) - len(counted),
        "resolved": resolved, "failed": len(failed), "resolution_rate": rate,
        "total_cost": money(total), "total_tokens": sum(s["tokens"] for s in sessions),
        "burned_cost": money(burned), "burned_tokens": sum(s["tokens"] for s in failed),
        # share of your usage that went to failed sessions: by cost, or by tokens when nothing could be priced
        "failed_share": pct(burned, total) if total else pct(sum(s["tokens"] for s in failed), sum(s["tokens"] for s in sessions)),
        "cost_per_resolved": money(total / resolved) if resolved else None,
        "unpriced_tokens": sum(s["unpriced"] for s in sessions),
        "reasons": {"corrected": sum(s["corrected"] for s in failed), "repeated": sum(s["repeated"] for s in failed),
                    "gave_up": sum(s["gave_up"] for s in failed)},
        "top_failed": [row(s) for s in top[:10]],
        "weeks": weeks, "projects": projects, "by_agent": agents, "by_model": model_rows,
        "history_start": {a: _date(d) for a, d in sorted(starts.items())},
        "current_excluded": info["current_excluded"], "still_running": info["still_running"],
        "compressed_skipped": info["compressed_skipped"], "unreadable": info["unreadable"],
        "bad_lines": sum(s["bad_lines"] for s in sessions),
    }


# ---------------------------------------------------------------- output

AGENT = {"claude": "Claude Code", "codex": "Codex"}


def short_date(iso):
    y, m, d = iso.split("-")
    return "{} {}".format(MONTHS[int(m) - 1], int(d))


def long_date(iso):
    return "{}, {}".format(short_date(iso), iso[:4])


def summary(r, report_path=None):
    names = AGENT.get(r.get("agent"), "Claude Code + Codex")
    where = "this project: " + r["project"] if r["scope"] == "project" else "whole device"
    L = ["Burn  ({}, {})".format(names, where), ""]
    if r["sessions"] == 0:
        L.append("No {} sessions found {} yet.".format(names.replace(" + ", " or "), "on this computer" if r["scope"] == "device" else "for this folder"))
    elif r["counted"] == 0:
        L.append("Not enough to judge yet: none of your {} sessions has 2 or more messages from you.".format(r["sessions"]))
    else:
        L.append("Resolution rate     {}%   ({} of {} sessions judged)".format(r["resolution_rate"], r["resolved"], r["counted"]))
        L.append("Failed sessions     {}% of your usage   ({} at API prices, {} tokens)".format(
            r["failed_share"], fmt_money(r["burned_cost"]), fmt_tokens(r["burned_tokens"])))
        L.append("Cost per resolved   " + (fmt_money(r["cost_per_resolved"]) + " at API prices" if r["cost_per_resolved"]
                                         else "no resolved sessions yet"))
        if r["top_failed"]:
            L += ["", "Most expensive failed sessions"]
            w = max(len(x["project"][:24]) for x in r["top_failed"][:3])
            for x in r["top_failed"][:3]:
                L.append("  {:<6}  {:<{w}}  {:>9}  {}".format(short_date(x["date"]) if x["date"] else "?",
                                                            x["project"][:24], fmt_money(x["cost"]), ", ".join(x["reasons"]), w=w))
                L.append("          \"{}\"".format(x["name"]))
    L.append("")
    if r["current_excluded"]:
        L.append("Your current session is not counted.")
    if r["still_running"]:
        L.append("{} session{} still running {} not counted.".format(
            r["still_running"], "" if r["still_running"] == 1 else "s", "was" if r["still_running"] == 1 else "were"))
    if r["too_short"] and r["counted"]:
        L.append("{} session{} too short to judge (under 2 messages from you). {} in the totals.".format(
            r["too_short"], " was" if r["too_short"] == 1 else "s were", "Its cost is" if r["too_short"] == 1 else "Their cost is"))
    if r["unpriced_tokens"]:
        L.append("{} tokens used a model with no known price; they are not in the dollar figures.".format(fmt_tokens(r["unpriced_tokens"])))
    if r["compressed_skipped"]:
        L.append("{} compressed Codex sessions skipped (needs Python 3.14).".format(r["compressed_skipped"]))
    if r["unreadable"]:
        L.append("Skipped {} files I couldn't read.".format(r["unreadable"]))
    for a, d in r["history_start"].items():
        L.append("{} history starts {}{}.".format(AGENT[a], long_date(d),
                                                 " (Claude Code keeps about 30 days by default)" if a == "claude" else ""))
    if r["sessions"]:
        L.append("Why API prices: it's what these tokens cost pay-as-you-go, one public price list for everyone, so")
        L.append("numbers compare across people and plans. On a subscription you paid a flat fee instead, not this.")
    if report_path:
        L.append("Full report: " + report_path)
    if r["counted"]:
        L.append("Want fewer repeats? Try /groundhog.")
    return "\n".join(L)


_CSS = """:root{--bg:#0d1419;--card:#15212a;--fg:#e8eef2;--mut:#93a4b1;--bd:#243440;--acc:#ff7a3d;--ok:#3ccf91;--track:#1d2c37}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:960px;margin:0 auto;padding:24px 16px 64px}h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:32px 0 12px}
.mut{color:var(--mut)}.banner{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:8px 12px;font-size:13px;color:var(--mut);margin:12px 0}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-top:16px}
.kpi{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:16px}.kpi b{display:block;font-size:30px;line-height:1.2}
.kpi.burn b{color:var(--acc)}.kpi.rate b{color:var(--ok)}.kpi span{font-size:13px;color:var(--mut)}
.tw{overflow-x:auto;background:var(--card);border:1px solid var(--bd);border-radius:12px}table{width:100%;border-collapse:collapse;font-size:14px}
th,td{padding:8px 12px;text-align:left;border-bottom:1px solid var(--bd);white-space:nowrap}th{color:var(--mut);font-weight:600}td.n,th.n{text-align:right}
tr:last-child td{border-bottom:0}.top{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.board{background:var(--card);border:1px solid var(--acc);color:var(--acc);border-radius:999px;padding:8px 16px;font-weight:600;text-decoration:none;white-space:nowrap}.board:hover{background:var(--acc);color:#1a0d05}.nm{display:block;white-space:normal;min-width:10em}.rs{display:block;font-size:11px;color:var(--mut);white-space:normal;word-break:break-all}@media(max-width:640px){.hs{display:none}th,td{white-space:normal;padding:8px}td:first-child{white-space:nowrap}}@media(max-width:380px){.rs{display:none}.nm{min-width:0}}.bar{height:8px;background:var(--track);border-radius:4px;min-width:60px}.bar i{display:block;height:8px;background:var(--acc);border-radius:4px}
.chips span{display:inline-block;background:var(--card);border:1px solid var(--bd);border-radius:999px;padding:4px 12px;margin:0 8px 8px 0;font-size:14px}
pre{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:12px;white-space:pre-wrap;font-size:14px}
button{background:var(--acc);color:#1a0d05;border:0;border-radius:8px;padding:10px 16px;font-weight:600;cursor:pointer;margin-right:8px}
canvas{max-width:100%;height:auto;border-radius:12px;display:block;margin:12px 0}footer{margin-top:40px;font-size:13px;color:var(--mut)}"""


def _e(s):
    return html.escape(str(s), quote=True)


def _table(head, rows, nums=(), small=()):
    """nums: right-aligned columns; small: columns hidden on phone screens (kept readable without side scroll)."""
    def cls(i):
        c = ("n " if i in nums else "") + ("hs" if i in small else "")
        return ' class="{}"'.format(c.strip()) if c.strip() else ""
    h = "".join("<th{}>{}</th>".format(cls(i), _e(c)) for i, c in enumerate(head))
    b = "".join("<tr>" + "".join("<td{}>{}</td>".format(cls(i), c) for i, c in enumerate(r)) + "</tr>" for r in rows)
    return '<div class="tw"><table><thead><tr>{}</tr></thead><tbody>{}</tbody></table></div>'.format(h, b)


def resume_cmd(x):
    return ("claude --resume " if x["agent"] == "claude" else "codex resume ") + x["session"]


def _pct(v):
    return "-" if v is None else "{}%".format(v)


def share_text(r):
    return ("{}% of my coding-agent usage ({} at API prices) went to sessions that failed. My agent resolved {}% of my "
            "sessions ({} per resolved session). Measured with Burn: analyze.voxmith.com/burn").format(
        r["failed_share"], approx_money(r["burned_cost"]), r["resolution_rate"],
        fmt_money(r["cost_per_resolved"]) if r["cost_per_resolved"] else "n/a")


def board_url():
    """The leaderboard link for the report: your private find-me link once a ping has returned one."""
    try:
        token = state.load().get("view_token")
    except Exception:
        token = None
    me = "&me=" + token if isinstance(token, str) and re.match(r"^[A-Za-z0-9_-]{8,40}$", token) else ""
    return state.BOARD_URL + "?utm_source=burn&utm_medium=report" + me


def html_report(r):
    names = AGENT.get(r.get("agent"), "Claude Code + Codex")
    where = "this project: " + r["project"] if r["scope"] == "project" else "whole device"
    many = len(r["by_agent"]) > 1
    P = ['<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
         "<title>Burn report</title><style>{}</style></head><body><main>".format(_CSS),
         '<div class=top><div><h1>Burn</h1><div class=mut>{} &middot; {}</div></div>'
         '<a class=board href="{}" target=_blank rel=noopener>See your rank on the leaderboard &rarr;</a></div>'.format(
             _e(names), _e(where), _e(board_url())),
         '<div class="banner">Only on your computer. It lists project and session names, so think before you share this page. The share card below has none.</div>']
    if not r["counted"]:
        P.append("<p>Not enough sessions to judge yet.</p>")
    else:
        P.append('<div class="kpis">'
                 '<div class="kpi rate"><span>Resolution rate</span><b>{}%</b><span>{} of {} sessions judged</span></div>'.format(
                     r["resolution_rate"], r["resolved"], r["counted"])
                 + '<div class="kpi burn"><span>Spent on failed sessions</span><b>{}</b>'
                   '<span>at API prices &middot; {}% of your usage &middot; {} tokens</span></div>'.format(
                     _e(fmt_money(r["burned_cost"])), r["failed_share"], fmt_tokens(r["burned_tokens"]))
                 + '<div class="kpi"><span>Cost per resolved session</span><b>{}</b><span>at API prices</span></div></div>'.format(
                     _e(fmt_money(r["cost_per_resolved"])) if r["cost_per_resolved"] else "-"))
        P.append('<p class=mut>Every table counts the same {} judged sessions (sessions with 2 or more messages from you). '
                 'Costs include all sessions.</p>'.format(r["counted"]))
        top_b = max([Decimal(w["burned"]) for w in r["weeks"]] + [Decimal("0.01")])
        P.append("<h2>Week by week</h2>")
        P.append(_table(["Week of", "Judged", "Resolution rate", "Failed sessions cost", ""], [
            [_e(long_date(w["key"])), w["counted"], _pct(w["rate"]), _e(fmt_money(w["burned"])),
             '<div class="bar"><i style="width:{}%"></i></div>'.format(int(Decimal(w["burned"]) * 100 / top_b))]
            for w in r["weeks"]], nums=(1, 2, 3), small=(1, 4)))
        P.append("<h2>By project</h2>")
        P.append(_table(["Project", "Judged", "Resolution rate", "Failed sessions cost", "Total cost"], [
            [_e(p["key"]), p["counted"], _pct(p["rate"]), _e(fmt_money(p["burned"])), _e(fmt_money(p["cost"]))]
            for p in r["projects"]], nums=(1, 2, 3, 4), small=(1, 4)))
        P.append("<h2>By model</h2>")
        if many:
            P.append(_table(["Agent", "Judged", "Resolution rate", "Failed sessions cost", "Total cost"], [
                [AGENT[a["key"]], a["counted"], _pct(a["rate"]), _e(fmt_money(a["burned"])), _e(fmt_money(a["cost"]))]
                for a in r["by_agent"]], nums=(1, 2, 3, 4), small=(1, 4)))
            P.append("<div style='height:12px'></div>")
        P.append(_table(["Model", "Judged", "Resolution rate", "Tokens", "Cost"], [
            [_e(m["model"]), m["counted"], _pct(m["rate"]), fmt_tokens(m["tokens"]), _e(fmt_money(m["cost"])) if m["cost"] else "price unknown"]
            for m in r["by_model"]], nums=(1, 2, 3, 4), small=(1, 3)))
        P.append("<p class=mut>A session is judged under the model that used most of its tokens.</p>")
        rs = r["reasons"]
        P.append("<h2>Why sessions failed</h2><div class=chips><span>Corrected 2+ times: {}</span><span>Repeated yourself: {}</span>"
                 "<span>Gave up: {}</span></div><div class=mut>{} failed sessions; one session can have more than one reason.</div>".format(
                     rs["corrected"], rs["repeated"], rs["gave_up"], r["failed"]))
        P.append("<h2>Most expensive failed sessions</h2>")
        head = ["Date", "Session", "Project"] + (["Agent"] if many else []) + ["Why", "Cost", "Tokens"]
        rows = [[_e(short_date(x["date"])) if x["date"] else "?",
                 '<span class=nm>{}</span><code class=rs>{}</code>'.format(_e(x["name"]), _e(resume_cmd(x)))]
                + [_e(x["project"])] + ([AGENT[x["agent"]]] if many else [])
                + [_e(", ".join(x["reasons"])), _e(fmt_money(x["cost"])), fmt_tokens(x["tokens"])] for x in r["top_failed"]]
        n = len(head)
        P.append(_table(head, rows, nums=(n - 2, n - 1), small=(2, n - 1) + ((3,) if many else ())))
        st = share_text(r)
        P.append("<h2>Share your number</h2><pre id=st>{}</pre><button onclick=\"navigator.clipboard&&navigator.clipboard.writeText("
                 "document.getElementById('st').textContent)\">Copy text</button><button onclick=dl()>Download image</button>"
                 "<canvas id=card width=1200 height=630></canvas>".format(_e(st)))
        card = {"rate": "{}%".format(r["resolution_rate"]), "share": "{}%".format(r["failed_share"]),
                "cost": approx_money(r["burned_cost"]) + " at API prices",
                "cpr": fmt_money(r["cost_per_resolved"]) if r["cost_per_resolved"] else "n/a", "n": r["counted"]}
        P.append("<script>const C={};".format(json.dumps(card)) + _CARD_JS + "</script>")
    notes = ["How this works: a session failed if you corrected the agent 2 or more times (a message like “no,” or "
             "“that's wrong” right after it answered; pressing Esc alone does not count), repeated the same instruction, "
             "or gave up (your last message was “forget it” or “I'll do it myself”, or a correction the agent "
             "never answered). Sessions with fewer than 2 messages from you are not judged but their cost is in the totals. "
             "Usage that went to failed sessions = their cost / the cost of all sessions. "
             "Cost per resolved session = total cost / resolved sessions.",
             "Why API prices: dollars are what these tokens cost on pay-as-you-go API pricing (prices checked {}), one public "
             "price list for everyone, so your numbers compare across people and plans. On a subscription (Claude Pro or Max, "
             "ChatGPT) you paid a flat fee instead, so read the dollars as the value of the work, not your bill. "
             "Tokens are counted once even when a line repeats across files.".format(_e(load_prices()["checked"])),
             "Rules version {}, Burn {}. Your current session and sessions still running are not counted.".format(RULES_VERSION, VERSION)]
    if r["unpriced_tokens"]:
        notes.append("{} tokens used a model with no known price and are not in the dollar figures.".format(fmt_tokens(r["unpriced_tokens"])))
    P.append("<footer>" + "".join("<p>{}</p>".format(n) for n in notes) +
             "<p>Your end result and account email are on the public Burn leaderboard. Opt out any time: /burn off.</p>"
             "<p>Burn is from VoxMith. analyze.voxmith.com/burn</p></footer></main></body></html>")
    return "".join(P)


_CARD_JS = """const cv=document.getElementById('card'),x=cv.getContext('2d');
const g=x.createLinearGradient(0,0,1200,630);g.addColorStop(0,'#0d1419');g.addColorStop(1,'#1d2c37');x.fillStyle=g;x.fillRect(0,0,1200,630);
x.fillStyle='#93a4b1';x.font='600 30px -apple-system,Segoe UI,Roboto,sans-serif';x.fillText('What did my coding agent\\u2019s failures cost me?',70,100);
const k=[['Resolution rate',C.rate,'#3ccf91'],['Usage on failures',C.share,'#ff7a3d'],['Cost per resolved',C.cpr,'#e8eef2']];
k.forEach((v,i)=>{const X=70+i*370;x.fillStyle='#93a4b1';x.font='26px -apple-system,Segoe UI,Roboto,sans-serif';x.fillText(v[0],X,250);
x.fillStyle=v[2];x.font='700 76px -apple-system,Segoe UI,Roboto,sans-serif';x.fillText(v[1],X,340);
if(i===1){x.fillStyle='#ffb48a';x.font='600 28px -apple-system,Segoe UI,Roboto,sans-serif';x.fillText(C.cost,X,385);}});
x.fillStyle='#93a4b1';x.font='24px -apple-system,Segoe UI,Roboto,sans-serif';x.fillText('Across '+C.n+' sessions. Cost at API prices.',70,460);
x.fillStyle='#ff7a3d';x.font='600 30px -apple-system,Segoe UI,Roboto,sans-serif';x.fillText('Burn  \\u00b7  analyze.voxmith.com/burn',70,560);
function dl(){const a=document.createElement('a');a.download='burn-card.png';a.href=cv.toDataURL('image/png');a.click();}"""


def write_html(doc):
    fd, path = tempfile.mkstemp(prefix="burn-report-", suffix=".html")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(doc)
    return path


def detect_agent(env=None):
    """Which agent is this running inside? Claude Code sets CLAUDECODE=1 for its tools; Codex sets CODEX_THREAD_ID.
    Neither (a plain terminal): read both."""
    env = os.environ if env is None else env
    if env.get("CLAUDECODE") == "1" or env.get("CLAUDE_SESSION_ID"):
        return "claude"
    if env.get("CODEX_THREAD_ID") or env.get("CODEX_SANDBOX"):
        return "codex"
    return "all"


def main(argv):
    ap = argparse.ArgumentParser(prog="burn.py")
    ap.add_argument("--scope", choices=("device", "project"), default="device")
    ap.add_argument("--project-dir")
    ap.add_argument("--session-id")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--agent", choices=("claude", "codex", "all", "auto"), default="auto",
                    help="whose history to read; auto = the agent this runs inside")
    a = ap.parse_args([x for x in argv if x.strip()])
    sid = a.session_id if a.session_id and not a.session_id.startswith("$") else (os.environ.get("CLAUDE_SESSION_ID") or os.environ.get("CODEX_THREAD_ID"))
    sid = sid if sid and re.match(r"^[0-9A-Za-z-]{8,64}$", sid) else None
    pdir = a.project_dir if a.project_dir and not a.project_dir.startswith("$") else os.getcwd()
    now = float(os.environ["BURN_NOW"]) if os.environ.get("BURN_NOW") else None
    agent = detect_agent() if a.agent == "auto" else a.agent
    sessions, info = scan(a.scope, pdir if a.scope == "project" else None, sid, now, agent)
    r = compute(sessions, info, a.scope, pdir, agent)
    if a.json:
        print(json.dumps(r, sort_keys=True, indent=1))
        return 0
    path = None
    if r["counted"]:
        path = write_html(html_report(r))
    try:
        st = state.load()
        st["last_result"] = {"rate": r["resolution_rate"], "scope": a.scope, "report": path, "agent": agent,
                             "judged": r["counted"], "resolved": r["resolved"],
                             # money at API prices in US cents, for the public leaderboard card
                             "failed_cents": cents(r["burned_cost"]), "failed_share": r["failed_share"],
                             "cpr_cents": cents(r["cost_per_resolved"]) if r["cost_per_resolved"] else None}
        state.save(st)
    except OSError:
        pass
    print(summary(r, path))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as e:  # never a stack trace with user paths
        print("Something went wrong reading your history: {}. Nothing was changed.".format(type(e).__name__))
        sys.exit(1)
