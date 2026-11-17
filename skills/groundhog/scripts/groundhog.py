#!/usr/bin/env python3
"""Groundhog: find the instruction you repeat most to your coding agent.

Reads Claude Code history on this computer only. This file has NO network code
(a test enforces it) and never starts other programs. Standard library only, Python 3.9+.

  groundhog.py --count
  groundhog.py --scan --scope all|project|session [--days 90] [--max 150] [--resume]
  groundhog.py --stats clusters.json        (path, - for stdin, or: saved)
  groundhog.py --render clusters.json       (the exact result screen, as markdown)
  groundhog.py --html [--share] clusters.json   (writes the report, prints its path)
  groundhog.py --summary clusters.json      (short block for the terminal)
  groundhog.py --welcome | --mark-written | --forget
"""
import argparse
import collections
import datetime
import difflib
import html as htmllib
import json
import math
import os
import re
import secrets
import sys
import tempfile

import state

VERSION = "0.2.0"
SECONDS_PER_REPEAT = 30
MESSAGES_PER_REPEAT = 2
MIN_REPEAT = {"all": 3, "project": 3, "session": 2}
TINY_MESSAGES = 10          # fewer human messages than this: "Not enough history yet"
MAX_CAND_CHARS = 200
R = "[redacted]"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
BLOCKS = "▁▂▃▄▅▆▇█"
SESSION_ID_RE = re.compile(r"^[0-9A-Za-z-]{8,64}$")

# ---------------------------------------------------------------- redaction (spec 4.2)


def _luhn(digits):
    s, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        s += d
        alt = not alt
    return s % 10 == 0


def _url(m):
    u = m.group(0)
    return R if ("?" in u or "@" in u) else u


def _card(m):
    digits = re.sub(r"\D", "", m.group(0))
    return R if 13 <= len(digits) <= 19 and _luhn(digits) else m.group(0)


_ASSIGN = re.compile(r"(?i)\b([\w.-]*(?:key|token|secret|password|passwd|pwd)[\w.-]*)\s*[:=]\s*(\"[^\"]*\"|'[^']*'|\S+)")
_SUBS = [  # (compiled regex, replacement or function), applied in this order
    (re.compile(r"/home/[^/\s]+/"), "~/"),
    (re.compile(r"/Users/[^/\s]+/"), "~/"),
    (re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+\\"), "~\\\\"),
    (re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s\"'<>]*"), _url),
    (re.compile(r"\b[\w.-]+\.[A-Za-z]{2,}(?:/[^\s?]*)?\?[^\s]+"), R),  # scheme-less URL with a query string
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), R),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"), R),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{10,}"), R),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), R),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), R),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), R),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), R),
    (re.compile(r"\bxox[bap]-[A-Za-z0-9-]{10,}"), R),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), R),
    (_ASSIGN, lambda m: m.group(1) + "=" + R),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), R),
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), R),
    (re.compile(r"\b(?=[A-Za-z0-9+/_-]*\d)(?=[A-Za-z0-9+/_-]*[A-Za-z])[A-Za-z0-9+/_-]{32,}={0,2}"), R),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), R),
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), _card),
]


def redact(text):
    for rx, rep in _SUBS:
        text = rx.sub(rep, text)
    return text


def too_redacted(text):
    return text.count(R) * len(R) > 0.4 * max(len(text), 1)


# ---------------------------------------------------------------- text helpers

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


def _is_rule(sentence):
    s = sentence.replace("’", "'")
    return bool(_OPENER.search(s) or _CORRECT.search(s.strip()))


def candidates_from(text, follows_assistant, after_interrupt):
    """Sentences of one human message that look like an instruction (spec 4.3)."""
    words = len(text.split())
    short = words < 25
    sents = [s.strip() for s in _SPLIT.split(text) if s.strip()]
    if len(text) > 600:
        return [s for s in sents if _is_rule(s)]
    if short and (follows_assistant or after_interrupt):
        if _is_rule(text) or (words >= 3 and not _FILLER.match(text.strip())):
            return [text]
        return []
    return [s for s in sents if _is_rule(s)]


# ---------------------------------------------------------------- reading history


def config_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def projects_root():
    return os.environ.get("GROUNDHOG_PROJECTS_ROOT") or os.path.join(config_dir(), "projects")


def _label(path):
    return os.path.basename((path or "").replace("\\", "/").rstrip("/")) or "unknown"


def _ts(o, fallback):
    try:
        return int(datetime.datetime.fromisoformat(o["timestamp"].replace("Z", "+00:00")).timestamp())
    except Exception:
        return int(fallback)


def extract(path):
    """One transcript file -> (items, human_messages, bad_lines). Item = [ts, session, project, [sentences]].
    Keep-rule is spec 1.3. Streams line by line; only user lines are parsed."""
    items, human, bad = [], 0, 0
    stem = os.path.splitext(os.path.basename(path))[0]
    mtime = os.path.getmtime(path)
    prev_asst = prev_int = False
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"type":"user"' not in line and '"type": "user"' not in line:
                if '"type":"assistant"' in line or '"type": "assistant"' in line:
                    prev_asst = True
                continue
            try:
                o = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if not isinstance(o, dict) or o.get("type") != "user":
                continue
            if o.get("isSidechain") or o.get("isMeta") or o.get("isCompactSummary"):
                continue
            msg = o.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                blocks = [b for b in content if isinstance(b, dict)]
                parts = [b.get("text") for b in blocks if b.get("type") == "text" and isinstance(b.get("text"), str)]
                if not parts:
                    continue  # tool_result only (or image only)
                text = "\n".join(parts)
            else:
                continue
            t = text.strip()
            if t.startswith("[Request interrupted"):
                prev_int = True
                continue
            origin, ps = o.get("origin"), o.get("promptSource")
            kind = origin.get("kind") if isinstance(origin, dict) else None
            human_ok = (kind == "human" or (origin is None and (ps is None or ps in ("typed", "queued", "suggestion_accepted"))))
            if not human_ok or not t:
                continue
            if t.startswith(("<", "[Pasted", "This session is being continued")) or t.lower().startswith("/groundhog"):
                continue
            human += 1
            follows, interrupted = prev_asst, prev_int
            prev_asst = prev_int = False
            sents = []
            for s in candidates_from(redact(t), follows, interrupted):
                s = s[:MAX_CAND_CHARS].strip()
                if len(s) >= 3 and not too_redacted(s):
                    sents.append(s)
            items.append([_ts(o, mtime), o.get("sessionId") or stem, _label(o.get("cwd")), sents])
    return items, human, bad


def encode_project(path):
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def _jsonl(folder):
    try:
        return [e.path for e in os.scandir(folder) if e.is_file() and e.name.endswith(".jsonl")]
    except OSError:
        return []


def project_folder(root, project_path):
    d = os.path.join(root, encode_project(project_path))
    if os.path.isdir(d):
        return d
    try:  # truncated/hashed folder name: match on the cwd field of the newest file instead
        for e in os.scandir(root):
            files = sorted(_jsonl(e.path), key=os.path.getmtime, reverse=True)
            if not files:
                continue
            with open(files[0], encoding="utf-8", errors="replace") as f:
                for _, line in zip(range(20), f):
                    try:
                        if json.loads(line).get("cwd") == project_path:
                            return e.path
                    except (ValueError, AttributeError):
                        pass
    except OSError:
        pass
    return None


def list_files(scope, days, project_path, session_id):
    """-> (files, info). info.fallback is True when the newest session stood in for the current one."""
    root = projects_root()
    info = {"fallback": False}
    # Whole UTC days, so two runs on the same day see the same window.
    midnight = datetime.datetime.now(datetime.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = (midnight - datetime.timedelta(days=days)).timestamp()

    def settled(files):
        """Sorted, inside the window, and without the session that is running right now (it keeps
        growing while we read it). Kept only if it is the sole session, so a first run still works."""
        files = sorted(p for p in files if os.path.getmtime(p) >= cutoff)
        rest = [p for p in files if os.path.basename(p) != (session_id or "") + ".jsonl"]
        return rest or files

    if scope == "all":
        files = []
        try:
            for e in os.scandir(root):
                if e.is_dir():
                    files += _jsonl(e.path)
        except OSError:
            pass
        return settled(files), info
    folder = project_folder(root, project_path)
    if folder is None:
        return [], info
    files = _jsonl(folder)
    if scope == "project":
        return settled(files), info
    one = os.path.join(folder, session_id + ".jsonl") if SESSION_ID_RE.match(session_id or "") else None
    if one and os.path.isfile(one):
        return [one], info
    if files:
        info["fallback"] = True
        return [max(files, key=os.path.getmtime)], info
    return [], info


def _real(v):
    """An unsubstituted ${CLAUDE_...} placeholder (it can reach us as literal text) counts as missing."""
    return v if v and "${" not in v else ""


def current_project(args):
    return _real(args.project_dir) or _real(os.environ.get("CLAUDE_PROJECT_DIR")) or os.getcwd()


def current_session(args):
    return _real(args.session_id) or _real(os.environ.get("CLAUDE_SESSION_ID"))


def do_count(args):
    root = projects_root()
    pp = current_project(args)
    out = {"no_history": not os.path.isdir(root), "days": args.days}
    for scope in ("all", "project", "session"):
        files, info = list_files(scope, args.days, pp, current_session(args))
        out[scope] = {"sessions": len(files), "mb": round(sum(os.path.getsize(p) for p in files) / 1e6, 1)}
        if scope == "session":
            out[scope]["fallback"] = info["fallback"]
    name = _label(pp)
    out["project"]["name"] = name
    n_all, n_proj = out["all"]["sessions"], out["project"]["sessions"]
    out["large"] = n_all > 400 or out["all"]["mb"] > 1000
    out["only_current_session"] = n_all <= 1
    out["menu"] = out["collapsed"] = None
    if out["only_current_session"] and n_all == 1:
        out["collapsed"] = "I only found this session, so I'll check that."
    elif n_all > 1:
        out["menu"] = ("What should I check?\n\n"
                       "> 1. Check all my sessions         ({} sessions on this computer)   <- recommended\n"
                       "  2. This project only             ({} sessions in \"{}\")\n"
                       "  3. This session only             (the one you're in now)\n\n"
                       "Reply 1, 2 or 3, or just press Enter for 1.").format(n_all, n_proj, name)
    out["progress"] = {"all": "Reading {} sessions on this computer...".format(n_all),
                       "project": "Reading {} sessions in this project...".format(n_proj),
                       "session": "Reading 1 session..."}
    out["session_note"] = "using your most recent session" if out["session"]["fallback"] else None
    try:
        state.write_private("count.json", out)
    except OSError:
        pass
    print(json.dumps(out, indent=2))


# ---------------------------------------------------------------- grouping (spec 4.1 step 6-7)


def _similar(a, b, ta, tb):
    if ta and tb and len(ta & tb) / len(ta | tb) >= 0.6:
        return True
    sm = difflib.SequenceMatcher(None, a, b)
    return sm.real_quick_ratio() >= 0.85 and sm.quick_ratio() >= 0.85 and sm.ratio() >= 0.85


def group(items):
    """items -> list of {text, msgs:{msg_index}, ...} groups, unranked."""
    occ, disp = collections.defaultdict(set), collections.defaultdict(collections.Counter)
    for mi, (_, _, _, sents) in enumerate(items):
        for s in sents:
            n = norm(s)
            if len(n) >= 3:
                occ[n].add(mi)
                disp[n][s] += 1
    uniq = sorted(occ, key=lambda n: (-len(occ[n]), n))
    bucketed = len(uniq) > 5000
    groups, buckets = [], collections.defaultdict(list)  # ponytail: bucketing trades recall for speed above 5000 unique lines
    for n in uniq:
        toks = set(re.findall(r"\w+", n))
        key = " ".join(n.split()[:2]) if bucketed else ""
        for g in (buckets[key] if bucketed else groups):
            if _similar(n, g["rep"], toks, g["toks"]):
                g["norms"].append(n)
                break
        else:
            g = {"rep": n, "toks": toks, "norms": [n]}
            groups.append(g)
            buckets[key].append(g)
    out = []
    for g in groups:
        msgs, shown = set(), collections.Counter()
        for n in g["norms"]:
            msgs |= occ[n]
            shown.update(disp[n])
        text = sorted(shown, key=lambda s: (-shown[s], len(s), s))[0]
        out.append({"text": text, "msgs": msgs, "norms": g["norms"]})
    return out


def load_groups():
    """GROUNDHOG_HOME/groups.json: {"norms": {normalised line: label, or "" when it is not a rule},
    "labels": {label: {"quote", "line"}}}. Written from Claude's grouping, read by every later scan."""
    g = state.read_json("groups.json", {})
    return {"norms": g.get("norms") or {}, "labels": g.get("labels") or {}}


def decide(group_norms, saved):
    """Label most of this group's known lines got (None = never seen, "" = not a rule). Ties: first by name."""
    votes = collections.Counter(saved["norms"][n] for n in group_norms if n in saved["norms"])
    if not votes:
        return None
    return sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def merge_saved(data, scan):
    """Claude's clusters for the new lines + the saved ones -> the full cluster list. Remembers the new
    decisions so the next run (in any session) groups the same way."""
    saved = load_groups()
    by_key, out = {}, []

    def cluster(label, quote="", line=""):
        k = label.strip().lower()
        if k not in by_key:
            keep = saved["labels"].get(label, {})
            by_key[k] = {"label": label, "quote": keep.get("quote") or quote, "line": keep.get("line") or line,
                         "candidate_ids": []}
            out.append(by_key[k])
        return by_key[k]

    for label in sorted(scan.get("known", {})):
        cluster(label)["candidate_ids"] += scan["known"][label]
    assigned = set()
    for c in data.get("clusters", []):
        label = (c.get("label") or "").strip()
        if not label:
            continue
        t = cluster(label, c.get("quote") or "", c.get("line") or "")
        t["line"] = t["line"] or c.get("line") or ""
        for i in c.get("candidate_ids", []):
            if str(i) in scan.get("norms", {}) and i not in t["candidate_ids"]:
                t["candidate_ids"].append(i)
                assigned.add(i)
    for t in out:
        saved["labels"][t["label"]] = {"quote": t["quote"], "line": t["line"]}
        for i in t["candidate_ids"]:
            for n in scan["norms"].get(str(i), []):
                saved["norms"][n] = t["label"]
    for i in scan.get("shown", []):  # shown to Claude and left out: not a rule, don't ask again
        if i not in assigned:
            for n in scan["norms"].get(str(i), []):
                saved["norms"].setdefault(n, "")
    state.write_private("groups.json", saved)
    return {"clusters": out}


def score(count, sessions, projects):
    return sessions * 2 + count + 3 * projects


def do_scan(args):
    scope = args.scope
    try:
        os.unlink(os.path.join(state.home(), "report.json"))  # a new scan starts without a stale report
    except OSError:
        pass
    files, info = list_files(scope, args.days, current_project(args), current_session(args))
    res = {"window_days": args.days, "scope": scope, "sessions_scanned": 0, "human_messages": 0,
           "candidate_messages": 0, "candidates": [], "tail": 0, "session_note": None,
           "skipped": {"no_history": not files, "unreadable_files": 0, "bad_lines": 0}}
    if info["fallback"]:
        res["session_note"] = "using your most recent session"
    cache = state.read_json("cache.json", {}) if args.resume else {}
    items, total = [], len(files)
    for i, p in enumerate(files, 1):
        try:
            st = os.stat(p)
            c = cache.get(p)
            if c and c["m"] == st.st_mtime and c["s"] == st.st_size:
                its, hum, bad = c["items"], c["n"], c["bad"]
            else:
                its, hum, bad = extract(p)
                if args.resume:
                    cache[p] = {"m": st.st_mtime, "s": st.st_size, "items": its, "n": hum, "bad": bad}
            items += its
            res["human_messages"] += hum
            res["skipped"]["bad_lines"] += bad
            res["sessions_scanned"] += 1
        except (OSError, ValueError, KeyError):
            res["skipped"]["unreadable_files"] += 1
        if i % 50 == 0 and i < total:
            print("Read {} of {} sessions...".format(i, total), file=sys.stderr)
            if args.resume:
                state.write_private("cache.json", cache)
    if args.resume and files:
        state.write_private("cache.json", cache)
    items.sort(key=lambda m: (m[0], m[1], m[3]))  # same order whatever order the files were read in
    sess, proj = {}, {}
    ranked = []
    for g in group(items):
        ms = [items[m] for m in sorted(g["msgs"])]
        s_ids = {m[1] for m in ms}
        p_ids = {m[2] for m in ms}
        ranked.append((score(len(ms), len(s_ids), len(p_ids)), len(s_ids), g, ms))
    ranked.sort(key=lambda r: (-r[0], -r[1], r[2]["text"]))
    res["candidate_messages"] = len({m for r in ranked for m in r[2]["msgs"]})
    # Groupings Claude made on an earlier run are reused, so every session on this computer gets the
    # same answer. Only lines never seen before go to Claude.
    saved = load_groups()
    known, new = {}, []
    for r in ranked:
        label = decide(r[2]["norms"], saved)
        if label is None:
            new.append(r)
        elif label:
            known.setdefault(label, []).append(r)
    res["tail"] = max(0, len(new) - args.max)
    cands, norms, known_ids = {}, {}, {}

    def add(cid, g, ms):
        cands[str(cid)] = {"occ": [[m[0], sess.setdefault(m[1], len(sess)), proj.setdefault(m[2], len(proj)), mi]
                                   for mi, m in zip(sorted(g["msgs"]), ms)]}
        norms[str(cid)] = g["norms"]

    for cid, (_, _, g, ms) in enumerate(new[:args.max]):
        ts = [m[0] for m in ms]
        res["candidates"].append({"id": cid, "text": g["text"], "count": len(ms),
                                  "sessions": len({m[1] for m in ms}), "projects": len({m[2] for m in ms}),
                                  "first": _day(min(ts)).isoformat(), "last": _day(max(ts)).isoformat()})
        add(cid, g, ms)
    cid = len(res["candidates"])
    for label in sorted(known):
        for _, _, g, ms in known[label]:
            add(cid, g, ms)
            known_ids.setdefault(label, []).append(cid)
            cid += 1
    res["known_groups"] = sorted(known)
    scratch = {"scope": scope, "sessions_scanned": res["sessions_scanned"], "human_messages": res["human_messages"],
               "unreadable": res["skipped"]["unreadable_files"], "projects": sorted(proj, key=proj.get),
               "texts": {str(c["id"]): c["text"] for c in res["candidates"]}, "cands": cands,
               "norms": norms, "known": known_ids, "shown": [c["id"] for c in res["candidates"]]}
    state.write_private("scan.json", scratch)
    print(json.dumps(res, indent=1, ensure_ascii=False))


# ---------------------------------------------------------------- stats (spec 4.7)


def _day(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).date()


def fmt_d(d):
    return "{} {}".format(MONTHS[d.month - 1], d.day)


def plural(n, word):
    return "{} {}{}".format(n, word, "" if n == 1 else "s")


def bar(count, top, width=20, floor=False):
    v = count / top * width if top else 0
    return max(1, int(v) if floor else int(v + 0.5))


def sparkline(counts):
    peak = max(counts) if counts else 0
    return "".join("·" if c == 0 else BLOCKS[min(7, max(0, math.ceil(c * 8 / peak) - 1))] for c in counts)


def parse_clusters(raw):
    """JSON {"clusters": [...]}, or one cluster per line: `ids | label | quote | line` (ids like 3,17,42).
    The line format exists because Claude Code's shell checker blocks heredocs that hold JSON (brace + quote)."""
    raw = raw.strip()
    if raw.startswith("{"):
        return json.loads(raw)
    out = []
    for ln in raw.splitlines():
        ln = ln.strip()
        if not ln or ln.lower() == "none":
            continue
        parts = [x.strip() for x in ln.split(" | ")]
        ids = [int(x) for x in re.findall(r"\d+", parts[0])]
        if not ids or len(parts) < 2:
            raise ValueError("bad cluster line")
        parts += [""] * (4 - len(parts))
        out.append({"candidate_ids": ids, "label": parts[1], "quote": parts[2], "line": " | ".join(parts[3:]).strip() if len(parts) > 4 else parts[3]})
    return {"clusters": out}


def load_json_arg(arg):
    """clusters from stdin (-), a file, or "saved" (the last ones, kept in GROUNDHOG_HOME/clusters.json)."""
    if arg == "saved":
        return state.read_json("clusters.json", {"clusters": []})
    raw = sys.stdin.read() if arg == "-" else open(arg).read()
    data = parse_clusters(raw)
    scan = state.read_json("scan.json", {})
    if "norms" in scan:
        data = merge_saved(data, scan)
    try:
        state.write_private("clusters.json", data)
    except OSError:
        pass
    return data


def compute(clusters, scan, today):
    scope = scan["scope"]
    out = []
    for c in clusters.get("clusters", []):
        occ = {}
        for i in c.get("candidate_ids", []):
            for ts, si, pi, mi in scan["cands"].get(str(i), {}).get("occ", []):
                occ[mi] = (ts, si, pi)
        if not occ:
            continue
        v = list(occ.values())
        days = [_day(x[0]) for x in v]
        first, last = min(days), max(days)
        mon = lambda d: d - datetime.timedelta(days=d.weekday())
        wk = collections.Counter(mon(d) for d in days)
        weeks, w = [], mon(first)
        while w <= mon(last):
            weeks.append(w)
            w += datetime.timedelta(days=7)
        weeks = weeks[-52:]
        counts = [wk.get(w, 0) for w in weeks]
        peak = max(counts)
        pc = collections.Counter(scan["projects"][x[2]] for x in v)
        n, s, p = len(v), len({x[1] for x in v}), len({x[2] for x in v})
        out.append({"label": c.get("label") or "", "quote": c.get("quote") or c.get("label") or "",
                    "line": c.get("line") or "", "count": n, "sessions": s, "projects": p, "score": score(n, s, p),
                    "first": first.isoformat(), "last": last.isoformat(), "days_ago": (today - last).days,
                    "weeks": [{"week": w.isoformat(), "count": k} for w, k in zip(weeks, counts)],
                    "sparkline": sparkline(counts), "peak_week": weeks[counts.index(peak)].isoformat(),
                    "peak_count": peak, "top_projects": [{"name": a, "count": b} for a, b in pc.most_common(3)],
                    "minutes": int(n * SECONDS_PER_REPEAT / 60 + 0.5),
                    "extra_messages": n * MESSAGES_PER_REPEAT, "last_ts": max(x[0] for x in v),
                    "occ": [[x[0], x[1]] for x in v]})
    out.sort(key=lambda c: (-c["count"], -c["sessions"]))  # biggest count first, so the table never looks out of order
    mx = max([c["count"] for c in out] or [1])
    for c in out:
        c["bar"] = bar(c["count"], mx)
        for pr in c["top_projects"]:
            pr["bar"] = bar(pr["count"], c["top_projects"][0]["count"], 12, floor=True)
    return {"scope": scope, "min_repeat": MIN_REPEAT[scope], "sessions_scanned": scan["sessions_scanned"],
            "human_messages": scan["human_messages"], "unreadable": scan.get("unreadable", 0),
            "seconds_per_repeat": SECONDS_PER_REPEAT, "messages_per_repeat": MESSAGES_PER_REPEAT,
            "clusters": out}


def _ago(n):
    return "today" if n <= 0 else ("1 day ago" if n == 1 else "{} days ago".format(n))


def cl(label):
    return label.replace("|", "/").replace("\n", " ")


def welcome_text(st):
    lr = st.get("last_result")
    if not lr:
        return None
    d = datetime.date.fromisoformat(lr["date"])
    return "Welcome back. Last time ({}) your top instruction was \"{}\", {} times.".format(fmt_d(d), lr["label"], lr["count"])


def render(stats, st, today, all_sessions):
    """The result screen, exactly as in the spec (headings, tables and charts as markdown)."""
    scope, cs, mn = stats["scope"], stats["clusters"], stats["min_repeat"]
    msgs, nsess = "{:,}".format(stats["human_messages"]), stats["sessions_scanned"]
    sec = collections.OrderedDict()
    top = cs[0] if cs else None
    wide = ("Want me to check all {} sessions instead? (yes / no)".format(all_sessions) if all_sessions
            else "Want me to check all your sessions instead? (yes / no)")  # all_sessions comes from the last --count
    if scope != "session" and stats["human_messages"] < TINY_MESSAGES:
        sec["main"] = ("# Groundhog\n\n## Not enough history yet\n\n"
                       "I found only {} short {} ({}). Claude Code keeps about 30 days of history by default, "
                       "so come back after a few more sessions.\n").format(
                           nsess, "session" if nsess == 1 else "sessions", plural(stats["human_messages"], "message"))
    elif top is None or top["count"] < mn:
        if scope == "session":
            sec["main"] = ("# Groundhog\n\n## Nothing repeated in this session\n\n"
                           "I read 1 session ({} of your messages) and nothing came up twice. "
                           "That's normal: one session is too short to show a habit.\n\n{}\n").format(msgs, wide)
        else:
            where = "{} sessions{}".format(nsess, " in this project" if scope == "project" else "")
            closest = (" The closest was \"{}\", {}.".format(top["label"] or top["quote"], plural(top["count"], "time"))
                       if top else " Nothing came up more than once.")
            sec["main"] = ("# Groundhog\n\n## No instruction came up 3 times or more\n\n"
                           "I checked {} ({} of your messages).{}\n"
                           "That's a good sign: you're not repeating yourself much.\n").format(where, msgs, closest)
            if top:
                sec["main"] += "\nWant the line for \"{}\" anyway? (yes / no)\n".format(top["label"] or top["quote"])
    else:
        rows = [c for c in cs if c["count"] >= mn][:10]
        n, m, x = top["count"], top["minutes"], top["extra_messages"]
        mword = "1 minute" if m == 1 else "{} minutes".format(m)
        head = {"all": "You've said this {} times", "project": "You've said this {} times in this project",
                "session": "You said this {} times in this session"}[scope].format(n)
        s = "# Groundhog\n\n## {}\n\n> **\"{}\"**\n\n".format(head, top["quote"])
        if scope == "all":
            s += "Told {} across {} since {} (last: {}, {}).\nThat's {} out of {}.\n\n".format(
                plural(n, "time"), plural(top["projects"], "project"), fmt_d(datetime.date.fromisoformat(top["first"])),
                fmt_d(datetime.date.fromisoformat(top["last"])), _ago(top["days_ago"]),
                plural(top["sessions"], "session"), nsess)
        elif scope == "project":
            s += "Told {} across {} since {} (last: {}, {}).\n\n".format(
                plural(n, "time"), plural(top["sessions"], "session"), fmt_d(datetime.date.fromisoformat(top["first"])),
                fmt_d(datetime.date.fromisoformat(top["last"])), _ago(top["days_ago"]))
        lr = st.get("last_result")
        if lr and scope != "session":
            k = [o for o in top["occ"] if o[0] > lr.get("ts", 0)]
            s_new = len({o[1] for o in k})
            s += ("Since last run: {} more {} in {} new {}.\n".format(len(k), "time" if len(k) == 1 else "times", s_new,
                                                                         "session" if s_new == 1 else "sessions")
                  if k else "Since last run: not repeated. The line seems to be working.\n")
            if lr.get("written"):
                s += "You added this line on {}.\n".format(fmt_d(datetime.date.fromisoformat(lr["written"])))
            s += "\n"
        if scope == "session":
            s += "That's about {} and {} extra messages ({} seconds each, one redo reply each).\n\n".format(
                mword, x, SECONDS_PER_REPEAT)
        else:
            s += ("### What it costs you\nAbout **{}** and **{} extra messages** ({} from you, {} replies you didn't need).\n"
                  "Assumes {} seconds each time you repeated yourself and one redo reply from the agent. "
                  "Your real number could be higher or lower.\n\n").format(mword, x, n, n, SECONDS_PER_REPEAT)
            w = top["weeks"]
            s += ("### When you say it\nWeek by week, {} to {}:\n\n```\n{}      peak: week of {}, {}\n```\n\n").format(
                fmt_d(datetime.date.fromisoformat(w[0]["week"])), fmt_d(datetime.date.fromisoformat(w[-1]["week"])),
                top["sparkline"], fmt_d(datetime.date.fromisoformat(top["peak_week"])), plural(top["peak_count"], "time"))
        if scope == "session":
            s += "### Your repeated instructions in this session\n\n| # | Instruction | Times | |\n|---|---|---|---|\n"
            s += "".join("| {} | {} | {} | {} |\n".format(i, cl(c["label"] or c["quote"]), c["count"], "█" * c["bar"])
                          for i, c in enumerate(rows, 1))
            s += "\nHabits show up over many sessions. " + wide + "\n"
        else:
            s += "### Your top 10 repeated instructions\n\n| # | Instruction | Times | Sessions | |\n|---|---|---|---|---|\n"
            s += "".join("| {} | {} | {} | {} | {} |\n".format(i, cl(c["label"] or c["quote"]), c["count"], c["sessions"],
                                                                  "█" * c["bar"]) for i, c in enumerate(rows, 1))
            s += "\n"
            if scope == "all":
                pr = top["top_projects"]
                wn, wc = max(len(p["name"]) for p in pr) + 3, len(str(max(p["count"] for p in pr)))
                s += "### Where it happens most\n```\n" + "".join(
                    "{}{}  {}\n".format(p["name"].ljust(wn), ("█" * p["bar"]).ljust(12), str(p["count"]).rjust(wc))
                    for p in pr) + "```\n\n"
            lines = [c["line"] for c in rows if c["line"]][:5]
            if lines:
                s += "### Add these to CLAUDE.md\n" + "".join("{}. `{}`\n".format(i, l) for i, l in enumerate(lines, 1))
                sec["claude_md"] = s[s.index("### Add these"):]
                s += "\nWhich ones should I add? Reply with numbers (like 1,2,5), \"all\" or \"none\".\n"
        sec["main"] = s
    if scope == "session" and top and top["count"] >= mn:
        lines = [c["line"] for c in cs if c["line"] and c["count"] >= mn][:5]
        sec["claude_md"] = "### Add these to CLAUDE.md\n" + "".join("{}. `{}`\n".format(i, l) for i, l in enumerate(lines, 1))
    if stats["unreadable"]:
        sec["main"] += "\n(Skipped {} files I couldn't read.)\n".format(stats["unreadable"])
    return sec


def summary(stats, st, today, all_sessions, report):
    """Short terminal block (about 10 lines) the model prints verbatim. Falls back to the short render() texts."""
    scope, cs, mn = stats["scope"], stats["clusters"], stats["min_repeat"]
    top = cs[0] if cs else None
    wb = welcome_text(st)
    wb = wb + "\n\n" if wb else ""   # second and later runs open with this line
    if (scope != "session" and stats["human_messages"] < TINY_MESSAGES) or top is None or top["count"] < mn:
        return wb + render(stats, st, today, all_sessions)["main"]
    n = top["count"]
    m = "1 minute" if top["minutes"] == 1 else "{} minutes".format(top["minutes"])
    head = {"all": "You've said this {} times", "project": "You've said this {} times in this project",
            "session": "You said this {} times in this session"}[scope].format(n)
    first = fmt_d(datetime.date.fromisoformat(top["first"]))
    if scope == "all":
        told = "Told {} times across {} since {}.".format(n, plural(top["projects"], "project"), first)
    elif scope == "project":
        told = "Told {} times across {} since {}.".format(n, plural(top["sessions"], "session"), first)
    else:
        told = "Told {} times in this session.".format(n)
    out = wb + "## {}\n\n> **\"{}\"**\n\n{} About {} and {} extra messages.\n".format(head, top["quote"], told, m, top["extra_messages"])
    lr = st.get("last_result")
    if lr and scope != "session":
        k = len([o for o in top["occ"] if o[0] > lr.get("ts", 0)])
        out += ("Since last run: {} more {}.\n".format(k, "time" if k == 1 else "times") if k
                else "Since last run: not repeated. The line seems to be working.\n")
    if report and report.get("path"):
        out += ("Full report: opened in your browser.\n" if report.get("opened")
                else "Full report: saved at {}\n".format(report["path"]))
    lines = [c["line"] for c in cs if c["line"] and c["count"] >= mn][:5]
    if lines:
        out += "\nLines for CLAUDE.md:\n" + "".join("{}. `{}`\n".format(i, l) for i, l in enumerate(lines, 1))
        out += "\nWhich ones should I add? Reply with numbers (like 1,2,5), \"all\" or \"none\".\n"
    if scope == "session":
        out += "\nHabits show up over many sessions. " + (
            "Want me to check all {} sessions instead? (yes / no)".format(all_sessions) if all_sessions
            else "Want me to check all your sessions instead? (yes / no)") + "\n"
    if stats["unreadable"]:
        out += "\n(Skipped {} files I couldn't read.)\n".format(stats["unreadable"])
    return out


def save_last(stats, today):
    cs = stats["clusters"]
    if not cs:
        return
    st = state.load()
    t = cs[0]
    st["last_result"] = {"date": today.isoformat(), "ts": int(datetime.datetime.now().timestamp()),
                         "label": t["quote"] or t["label"], "count": t["count"], "top_count": t["count"],
                         "scope": stats["scope"], "written": None}
    try:
        state.save(st)
    except OSError:
        pass


# ---------------------------------------------------------------- html report (spec 6c)

_CSS = """:root{--bg:#f3f6fb;--card:#fff;--fg:#13222c;--mut:#5a6b78;--bd:#e1e8f0;--acc:#026cfe;--accd:#0258d6;--bar:#026cfe;--track:#eef5ff;--chip:#eef5ff;--hero:linear-gradient(135deg,#13222c 0%,#1d3340 100%)}
@media (prefers-color-scheme:dark){:root{--bg:#0a1319;--card:#13222c;--fg:#e8eff5;--mut:#9db0bf;--bd:#233845;--acc:#4d96ff;--accd:#8ab9ff;--bar:#3b8bff;--track:#1d3340;--chip:#1d3340;--hero:linear-gradient(135deg,#1d3340 0%,#13222c 100%)}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{background:#13222c;color:#fff}header div{max-width:720px;margin:0 auto;padding:14px 16px;display:flex;justify-content:space-between;align-items:center;gap:12px}
.logo{font-weight:800;letter-spacing:.02em}.logo b{color:#4d96ff}.by{font-size:13px;color:#b8d4ff}
main{max-width:720px;margin:0 auto;padding:16px 16px 8px}
.note{display:flex;gap:8px;align-items:center;background:#fff7e0;color:#6b4500;border:1px solid #f0d58a;border-radius:10px;padding:8px 12px;font-size:14px;font-weight:600;margin:0 0 14px}
@media (prefers-color-scheme:dark){.note{background:#3a2e0d;color:#ffe3a0;border-color:#6b5415}}
.hero{background:var(--hero);color:#fff;border-radius:16px;padding:24px 20px;margin:0 0 14px;box-shadow:0 6px 20px rgba(19,34,44,.18)}
.eyebrow{font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:#b8d4ff;margin:0}
.big{display:flex;align-items:baseline;gap:10px;margin:6px 0 4px}.big b{font-size:72px;line-height:1;font-weight:800;color:#fff}.big span{font-size:22px;color:#b8d4ff}
.q{font-size:22px;line-height:1.3;font-weight:700;margin:10px 0 12px;overflow-wrap:anywhere;border-left:4px solid #4d96ff;padding-left:14px}
.facts{margin:0;color:#cfe0f5;font-size:15px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:14px;padding:18px 16px;margin:0 0 14px}
h2{font-size:13px;letter-spacing:.07em;text-transform:uppercase;color:var(--mut);margin:0 0 12px}
.stats{display:grid;grid-template-columns:1fr 1fr;gap:12px}.stat b{display:block;font-size:28px;line-height:1.1;color:var(--acc);white-space:nowrap}.stat span{font-size:14px;color:var(--mut)}
.fine{font-size:13px;color:var(--mut);margin:12px 0 0}
.weeks{display:flex;align-items:flex-end;gap:4px;height:120px}.w{flex:1;min-width:0;height:100%;display:flex;flex-direction:column;justify-content:flex-end;align-items:center}
.w i{display:block;width:100%;background:var(--bar);border-radius:4px 4px 0 0;min-height:3px}.w.z i{background:var(--bd)}.w span{font-size:11px;color:var(--mut);height:16px}
.axis{display:flex;justify-content:space-between;font-size:12px;color:var(--mut);margin-top:6px}
ol.top{list-style:none;margin:0;padding:0}ol.top li{display:grid;grid-template-columns:28px 1fr auto;gap:2px 10px;padding:10px 0;border-top:1px solid var(--bd)}ol.top li:first-child{border-top:0;padding-top:0}
.rk{grid-row:1/3;color:var(--mut);font-weight:700;padding-top:1px}.tx{overflow-wrap:anywhere;font-weight:600}.n{font-weight:800;white-space:nowrap}.n small{display:block;color:var(--mut);font-weight:500;font-size:12px;text-align:right}
.track{grid-column:2/4;height:10px;background:var(--track);border-radius:6px;overflow:hidden}.track i{display:block;height:100%;background:var(--bar);border-radius:6px;min-width:6px}
.proj{display:grid;grid-template-columns:minmax(80px,32%) 1fr 32px;gap:10px;align-items:center;margin:8px 0}.proj .track{grid-column:auto}.proj .pn{overflow-wrap:anywhere;font-size:15px}.proj .n{text-align:right}
ol.lines{margin:0;padding:0;list-style:none;counter-reset:l}ol.lines li{counter-increment:l;display:flex;gap:12px;align-items:flex-start;padding:10px 0;border-top:1px solid var(--bd)}ol.lines li:first-child{border-top:0;padding-top:0}
ol.lines li:before{content:counter(l);flex:none;width:26px;height:26px;border-radius:50%;background:var(--acc);color:#fff;font-weight:700;font-size:14px;display:flex;align-items:center;justify-content:center}
ol.lines code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:14px;background:var(--chip);border-radius:6px;padding:3px 8px;overflow-wrap:anywhere}
footer{max-width:720px;margin:0 auto;text-align:center;color:var(--mut);font-size:13px;padding:14px 16px 28px}
@media (max-width:420px){.stat b{font-size:24px}.big b{font-size:60px}.q{font-size:19px}.proj{grid-template-columns:90px 1fr 28px}}"""


def html_report(stats, share=False):
    """Self-contained report. Default = the owner's own view (real wording and project names, banner).
    share=True = generic wording and Project A/B/C, no banner."""
    e = htmllib.escape
    rows = [c for c in stats["clusters"] if c["count"] >= stats["min_repeat"]][:10]
    if not rows:
        raise ValueError("nothing repeated enough to report")
    top, scope = rows[0], stats["scope"]
    names = {}
    pname = lambda n: n if not share else names.setdefault(n, "Project " + chr(65 + len(names) % 26))
    text = lambda c: (c["label"] or "A repeated instruction") if share else (c["quote"] or c["label"])
    first, last = datetime.date.fromisoformat(top["first"]), datetime.date.fromisoformat(top["last"])
    D = lambda iso: fmt_d(datetime.date.fromisoformat(iso))
    scope_txt = {"all": "across {} and {}".format(plural(top["sessions"], "session"), plural(top["projects"], "project")),
                 "project": "across {} in this project".format(plural(top["sessions"], "session")),
                 "session": "in this session"}[scope]
    p = ['<!doctype html><html lang="en"><head><meta charset="utf-8">'
         '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; img-src data:">'
         '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Groundhog report</title><style>' + _CSS +
         '</style></head><body><header><div><span class="logo">Ground<b>hog</b></span><span class="by">by VoxMith</span></div></header><main>']
    if not share:
        p.append('<div class="note">Only on your computer. Don\'t share this page.</div>')
    p.append('<section class="hero"><p class="eyebrow">{}</p><div class="big"><b>{}</b><span>times</span></div>'
             '<div class="q">{}</div><p class="facts">Since {} (last: {}, {}) {}</p></section>'.format(
                 e("You told your agent the same thing" if scope != "session" else "You repeated yourself"), top["count"],
                 e('“' + text(top) + '”'), e(fmt_d(first)), e(fmt_d(last)), e(_ago(top["days_ago"])),
                 e(scope_txt)))
    p.append('<section class="card"><h2>What it costs you</h2><div class="stats"><div class="stat"><b>{}</b><span>of your time</span></div>'
             '<div class="stat"><b>{}</b><span>extra messages</span></div></div><p class="fine">Assumes {} seconds each time you repeated '
             "yourself and one redo reply from the agent. Your real number could be higher or lower.</p></section>".format(
                 e("1 minute" if top["minutes"] == 1 else "{} minutes".format(top["minutes"])), top["extra_messages"],
                 stats["seconds_per_repeat"]))
    if scope != "session":
        wk = top["weeks"]
        pk = max(w["count"] for w in wk) or 1
        bars = "".join('<div class="w{}" title="week of {}: {}">{}<i style="height:{}%"></i></div>'.format(
            " z" if w["count"] == 0 else "", e(D(w["week"])), w["count"],
            "<span>{}</span>".format(w["count"]) if len(wk) <= 20 else "", max(2, round(w["count"] / pk * 100))) for w in wk)
        p.append('<section class="card"><h2>When you say it</h2><div class="weeks">{}</div><div class="axis"><span>{}</span><span>peak: week of {}, {}</span>'
                 "<span>{}</span></div></section>".format(bars, e(D(wk[0]["week"])), e(D(top["peak_week"])),
                                                           plural(top["peak_count"], "time"), e(D(wk[-1]["week"]))))
    li = "".join('<li><span class="rk">{}</span><span class="tx">{}</span><span class="n">{}<small>{}</small></span>'
                 '<div class="track"><i style="width:{}%"></i></div></li>'.format(
                     i, e(text(c)), c["count"], e(plural(c["sessions"], "session")), round(c["bar"] / 20 * 100))
                 for i, c in enumerate(rows, 1))
    p.append('<section class="card"><h2>Your top repeated instructions</h2><ol class="top">{}</ol></section>'.format(li))
    if scope == "all":
        pr = "".join('<div class="proj"><span class="pn">{}</span><div class="track"><i style="width:{}%"></i></div><span class="n">{}</span></div>'.format(
            e(pname(x["name"])), round(x["bar"] / 12 * 100), x["count"]) for x in top["top_projects"])
        p.append('<section class="card"><h2>Where it happens most</h2>{}</section>'.format(pr))
    lines = [c["line"] for c in rows if c["line"]][:5]
    if lines:
        p.append('<section class="card"><h2>Lines for CLAUDE.md</h2><ol class="lines">{}</ol></section>'.format(
            "".join("<li><code>{}</code></li>".format(e(l)) for l in lines)))
    p.append("<footer>Made with Groundhog, voxmith.com/groundhog</footer></main></body></html>")
    return "".join(p)


def write_html(doc):
    path = os.path.join(tempfile.gettempdir(), "groundhog-report-{}.html".format(secrets.token_hex(4)))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(doc)
    try:
        state.write_private("report.json", {"path": path, "opened": False})  # open_report.py flips "opened"
    except OSError:
        pass
    return path


# ---------------------------------------------------------------- main


def main(argv):
    ap = argparse.ArgumentParser(prog="groundhog.py")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--count", action="store_true")
    g.add_argument("--scan", action="store_true")
    g.add_argument("--stats", metavar="CLUSTERS")
    g.add_argument("--render", metavar="CLUSTERS")
    g.add_argument("--html", metavar="CLUSTERS")
    g.add_argument("--summary", metavar="CLUSTERS")
    g.add_argument("--welcome", action="store_true")
    g.add_argument("--mark-written", action="store_true")
    g.add_argument("--forget", action="store_true")
    ap.add_argument("--scope", choices=("all", "project", "session"), default="all")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--max", type=int, default=150)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--share", action="store_true", help="html: generic wording, Project A/B/C, no banner")
    ap.add_argument("--section", choices=("main", "claude_md"), default="main")
    ap.add_argument("--project-dir")
    ap.add_argument("--session-id")
    ap.add_argument("--today", help=argparse.SUPPRESS)
    ap.add_argument("--no-save", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    today = datetime.date.fromisoformat(a.today) if a.today else datetime.date.today()
    try:
        if a.count:
            do_count(a)
        elif a.scan:
            do_scan(a)
        elif a.forget:
            try:
                os.unlink(os.path.join(state.home(), "groups.json"))
            except OSError:
                pass
            print("Done. I forgot how I grouped your instructions. The next run groups them again from scratch.")
        elif a.welcome:
            print(welcome_text(state.load()) or "")
        elif a.mark_written:
            st = state.load()
            if st.get("last_result"):
                st["last_result"]["written"] = today.isoformat()
                state.save(st)
        else:
            clusters = load_json_arg(a.stats or a.render or a.html or a.summary)
            stats = compute(clusters, state.read_json("scan.json", {}), today)
            if a.stats:
                for c in stats["clusters"]:
                    del c["occ"]
                print(json.dumps(stats, indent=1, ensure_ascii=False))
            elif a.render:
                st = state.load()
                sec = render(stats, st, today, state.read_json("count.json", {}).get("all", {}).get("sessions"))
                print(sec.get(a.section, ""))
                if not a.no_save and a.section == "main":
                    save_last(stats, today)
            elif a.summary:
                st = state.load()
                text = summary(stats, st, today, state.read_json("count.json", {}).get("all", {}).get("sessions"),
                               state.read_json("report.json", {}))
                print(text)
                if not a.no_save:
                    save_last(stats, today)
            else:
                top = stats["clusters"][0] if stats["clusters"] else None
                if (stats["scope"] != "session" and stats["human_messages"] < TINY_MESSAGES) or not top or top["count"] < stats["min_repeat"]:
                    print("NO_REPORT\ntop_count: {}".format(top["count"] if top else 0))  # --summary prints the short message
                    return 0
                path = write_html(html_report(stats, a.share))
                print("Saved: {}\nfile://{}\ntop_count: {}".format(path, path, top["count"]))
    except Exception as ex:  # no stack trace, no paths
        print("Something went wrong reading your history: {}. Nothing was changed.".format(type(ex).__name__), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
