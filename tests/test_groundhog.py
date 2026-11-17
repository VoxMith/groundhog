import ast
import json
import os
import re
import stat

import pytest

import groundhog as G
from conftest import FIX, REPO, SCRIPTS, SID, gh, run

SECRETS = json.load(open(os.path.join(FIX, "secrets.json")))
KINDS = os.path.join(FIX, "projects", "-home-dev-kinds", "kinds000-aaaa-4bbb-8ccc-000000000000.jsonl")


# ---------------------------------------------------------------- 1. keep rule (spec 1.3)
def test_keep_rule_keeps_exactly_the_human_lines():
    items, human, bad = G.extract(KINDS)
    assert bad == 0
    texts = [s for it in items for s in it[3]]
    assert human == 4
    assert [it[3] for it in items] == [
        ["Never use the any type in TypeScript."],
        ["Always run the tests before saying it is done."],
        ["no that is not what I wanted, use pnpm instead of npm"],   # follows an interruption marker
        ["Don't restart the dev server."],                           # old format line (no promptSource/origin)
    ]
    for dropped in ("Pasted", "task-notification", "peer", "Continue the work", "system-reminder", "command-name",
                    "local-command", "continued", "config file", "groundhog", "other agent"):
        assert not any(dropped.lower() in t.lower() for t in texts), dropped


def test_bad_json_line_is_counted_not_fatal(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"type":"user" this is broken\n' + '{"type":"user","message":{"content":"Never swear."},"sessionId":"s","cwd":"/a/b"}\n')
    items, human, bad = G.extract(str(p))
    assert (human, bad) == (1, 1)


# ---------------------------------------------------------------- 2. redaction, one test per pattern (spec 4.2)
@pytest.mark.parametrize("name", sorted(SECRETS))
def test_redaction_per_pattern(name):
    v = SECRETS[name]
    out = G.redact("please check {} now".format(v))
    assert v not in out
    if name == "home":
        assert "jane" not in out and "~/" in out
    elif name == "assign":
        assert "hunter2hunter2" not in out and "password=[redacted]" in out
    else:
        assert "[redacted]" in out


def test_redaction_leaves_ordinary_text_and_bad_luhn():
    t = "Use the existing helper, don't write a new one. See https://docs.example.com/guide and call it 1234 5678 9012 3456."
    assert G.redact(t) == t
    assert G.too_redacted("[redacted] [redacted] a")
    assert not G.too_redacted("Never print [redacted] in the logs")


# ---------------------------------------------------------------- acceptance 2 and 3
def scan(env, *args):
    r = gh("--scan", "--days", "3650", *args)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_acceptance2_reworded_instruction_is_one_group(env):
    out = scan(env, "--scope", "all")
    top = out["candidates"][0]
    assert top["text"].lower().startswith("don't add comments to code i didn't ask you about")
    assert (top["count"], top["sessions"], top["projects"]) == (4, 3, 2)
    assert out["sessions_scanned"] == 4 and set(out) >= {"window_days", "sessions_scanned", "human_messages", "candidates", "skipped"}


def test_acceptance3_planted_secrets_never_appear(tmp_path, monkeypatch, env):
    import shutil
    shutil.copytree(os.path.join(FIX, "projects", "-home-dev-secrets"), env / "projects" / "-home-dev-secrets")
    shutil.copytree(os.path.join(FIX, "projects", "-home-dev-kinds"), env / "projects" / "-home-dev-kinds")
    r = gh("--scan", "--days", "3650")
    assert r.returncode == 0
    blob = r.stdout + r.stderr + open(env / "gh" / "scan.json").read()
    for name, v in SECRETS.items():
        assert v not in blob, name
    for frag in ("hunter2", "jane.doe", "/home/jane", "s3cretpw", "10.20.30.40", "4111"):
        assert frag not in blob, frag
    assert "[redacted]" in blob


# ---------------------------------------------------------------- 4c scope
def test_scope_file_sets_and_counts(env):
    # The running session (my1) is left out of "all" and "project": it grows while we read it.
    c = json.loads(gh("--count", "--project-dir", "/home/dev/my-app", "--session-id", SID["my1"]).stdout)
    assert (c["all"]["sessions"], c["project"]["sessions"], c["session"]["sessions"]) == (3, 1, 1)
    assert c["project"]["name"] == "my-app" and c["menu"] and "(3 sessions on this computer)" in c["menu"]
    assert '(1 session' in c["menu"]
    c = json.loads(gh("--count", "--project-dir", "/home/dev/my-app").stdout)
    a = scan(env, "--scope", "all")
    p = scan(env, "--scope", "project", "--project-dir", "/home/dev/my-app")
    s = scan(env, "--scope", "session", "--project-dir", "/home/dev/my-app", "--session-id", SID["my1"])
    assert (a["sessions_scanned"], p["sessions_scanned"], s["sessions_scanned"]) == (4, 2, 1)
    assert (p["candidates"][0]["count"], p["candidates"][0]["sessions"]) == (3, 2)
    assert s["candidates"][0]["count"] == 2 and s["candidates"][0]["sessions"] == 1
    assert [x["sessions_scanned"] for x in (a, p, s)] == [c["all"]["sessions"], c["project"]["sessions"], c["session"]["sessions"]]


def test_session_fallbacks(env):
    for sid in ("", "${CLAUDE_SESSION_ID}", "ffffffff-0000-0000-0000-000000000000"):
        c = json.loads(gh("--count", "--project-dir", "/home/dev/my-app", "--session-id", sid).stdout)
        assert c["session"]["sessions"] == 1 and c["session_note"] == "using your most recent session"
    s = scan(env, "--scope", "session", "--project-dir", "/home/dev/my-app")
    assert s["session_note"] == "using your most recent session" and s["sessions_scanned"] == 1


def test_project_folder_found_by_cwd_when_name_differs(env):
    root = env / "projects"
    (root / "-home-dev-my-app").rename(root / "-hashed-truncated-name-abc123")
    s = scan(env, "--scope", "project", "--project-dir", "/home/dev/my-app")
    assert s["sessions_scanned"] == 2


def test_collapsed_menu_and_empty_history(env):
    import shutil
    for d in ("-home-dev-billing-api", "-home-dev-docs-site", "-home-dev-my-app"):
        pass
    root = env / "projects"
    shutil.rmtree(root / "-home-dev-billing-api")
    shutil.rmtree(root / "-home-dev-docs-site")
    os.remove(root / "-home-dev-my-app" / (SID["my2"] + ".jsonl"))
    c = json.loads(gh("--count", "--project-dir", "/home/dev/my-app").stdout)
    assert c["menu"] is None and c["collapsed"] == "I only found this session, so I'll check that."
    shutil.rmtree(root)
    c = json.loads(gh("--count").stdout)
    assert c["no_history"] and c["menu"] is None
    out = json.loads(gh("--scan").stdout)
    assert out["skipped"]["no_history"] and out["candidates"] == []


def test_current_groundhog_message_excluded(env):
    p = env / "projects" / "-home-dev-my-app" / (SID["my1"] + ".jsonl")
    with open(p, "a") as f:
        f.write(json.dumps({"type": "user", "message": {"content": "/groundhog all"}, "sessionId": SID["my1"], "cwd": "/home/dev/my-app"}) + "\n")
    out = scan(env, "--scope", "session", "--project-dir", "/home/dev/my-app", "--session-id", SID["my1"])
    assert not any("groundhog" in c["text"] for c in out["candidates"])


def test_resume_cache(env):
    a = scan(env, "--scope", "all", "--resume")
    cache = env / "gh" / "cache.json"
    assert cache.exists() and stat.S_IMODE(os.stat(cache).st_mode) == 0o600
    b = scan(env, "--scope", "all", "--resume")
    assert a == b


# ---------------------------------------------------------------- 4d stats and the printed screens
def crafted(scope="all"):
    """scan.json + clusters that reproduce the spec's example screen (year 2025)."""
    import datetime as dt
    counts = [23, 19, 14, 11, 8, 6, 5, 4, 4, 3]
    sess = [14, 12, 9, 8, 6, 5, 4, 4, 3, 3]
    weeks0 = dt.datetime(2025, 9, 18, 12, tzinfo=dt.timezone.utc)   # Thursday of the week of Sep 15
    weekly = [1, 2, 3, 4, 6, 3, 2, 0, 2]                             # sums to 23, peak 6 in the week of Oct 13
    top_days = []
    for w, n in enumerate(weekly):
        top_days += [weeks0 + dt.timedelta(days=7 * w + (i % 3)) for i in range(n)]
    top_days[-2:] = [dt.datetime(2025, 11, 11, 12, tzinfo=dt.timezone.utc), dt.datetime(2025, 11, 12, 12, tzinfo=dt.timezone.utc)]
    proj_of_top = [0] * 11 + [1] * 7 + [2] * 3 + [3] * 2
    cands, mi = {}, 0
    for k, (c, s) in enumerate(zip(counts, sess)):
        occ = []
        for j in range(c):
            ts = int((top_days[j] if k == 0 else weeks0 + dt.timedelta(days=j)).timestamp())
            occ.append([ts, j % s, proj_of_top[j] if k == 0 else 0, mi])
            mi += 1
        cands[str(k)] = {"occ": occ}
    scan = {"scope": scope, "sessions_scanned": 212, "human_messages": 3480, "unreadable": 0,
            "projects": ["my-app", "billing-api", "docs-site", "scripts"], "texts": {}, "cands": cands}
    labels = ["Don't add comments I didn't ask for", "Use the existing helper, don't write a new one", "Don't use `any`",
              "Keep answers short", "Run the tests before saying it's done", "Ask before deleting files",
              "Use pnpm, not npm", "Don't restart the dev server", "Show me the diff before committing", "Stop apologising"]
    lines = ["Do not add code comments unless I ask for them.", "Reuse existing helpers before writing new ones.",
             "Never use the TypeScript type any; use a specific type or unknown.", "Keep answers short.",
             "Run the tests before saying a task is done."]
    cl = [{"label": l, "candidate_ids": [i], "line": lines[i] if i < 5 else ""} for i, l in enumerate(labels)]
    cl[0]["quote"] = "Don't add comments to code I didn't ask you about"
    return scan, {"clusters": cl}


def setup_crafted(env, scope="all", **kw):
    import state
    scan, cl = crafted(scope)
    state.write_private("scan.json", scan)
    return cl


def test_stats_numbers_match_planted_data(env):
    cl = setup_crafted(env)
    r = gh("--stats", "-", "--today", "2025-11-14", stdin=json.dumps(cl))
    st = json.loads(r.stdout)
    top = st["clusters"][0]
    assert (top["count"], top["sessions"], top["projects"]) == (23, 14, 4)
    assert [w["count"] for w in top["weeks"]] == [1, 2, 3, 4, 6, 3, 2, 0, 2]
    assert top["sparkline"] == "▂▃▄▆█▄▃·▃" and top["peak_week"] == "2025-10-13" and top["peak_count"] == 6
    assert top["minutes"] == 12 and top["extra_messages"] == 46 and top["bar"] == 20
    assert [(p["name"], p["count"], p["bar"]) for p in top["top_projects"]] == [("my-app", 11, 12), ("billing-api", 7, 7), ("docs-site", 3, 3)]
    assert [c["bar"] for c in st["clusters"]] == [20, 17, 12, 10, 7, 5, 4, 3, 3, 3]
    assert (top["first"], top["last"], top["days_ago"]) == ("2025-09-18", "2025-11-12", 2)
    assert st["seconds_per_repeat"] == 30 and st["messages_per_repeat"] == 2


FULL = """# Groundhog

## You've said this 23 times

> **"Don't add comments to code I didn't ask you about"**

Told 23 times across 4 projects since Sep 18 (last: Nov 12, 2 days ago).
That's 14 sessions out of 212.

### What it costs you
About **12 minutes** and **46 extra messages** (23 from you, 23 replies you didn't need).
Assumes 30 seconds each time you repeated yourself and one redo reply from the agent. Your real number could be higher or lower.

### When you say it
Week by week, Sep 15 to Nov 10:

```
▂▃▄▆█▄▃·▃      peak: week of Oct 13, 6 times
```

### Your top 10 repeated instructions

| # | Instruction | Times | Sessions | |
|---|---|---|---|---|
| 1 | Don't add comments I didn't ask for | 23 | 14 | ████████████████████ |
| 2 | Use the existing helper, don't write a new one | 19 | 12 | █████████████████ |
| 3 | Don't use `any` | 14 | 9 | ████████████ |
| 4 | Keep answers short | 11 | 8 | ██████████ |
| 5 | Run the tests before saying it's done | 8 | 6 | ███████ |
| 6 | Ask before deleting files | 6 | 5 | █████ |
| 7 | Use pnpm, not npm | 5 | 4 | ████ |
| 8 | Don't restart the dev server | 4 | 4 | ███ |
| 9 | Show me the diff before committing | 4 | 3 | ███ |
| 10 | Stop apologising | 3 | 3 | ███ |

### Where it happens most
```
my-app        ████████████  11
billing-api   ███████        7
docs-site     ███            3
```

### Add these to CLAUDE.md
1. `Do not add code comments unless I ask for them.`
2. `Reuse existing helpers before writing new ones.`
3. `Never use the TypeScript type any; use a specific type or unknown.`
4. `Keep answers short.`
5. `Run the tests before saying a task is done.`

Which ones should I add? Reply with numbers (like 1,2,5), "all" or "none".
"""


def test_rendered_full_screen_matches_spec_example(env):
    cl = setup_crafted(env)
    r = gh("--render", "-", "--today", "2025-11-14", stdin=json.dumps(cl))
    assert r.stdout == FULL + "\n"   # print() adds the final newline


def test_welcome_back_and_since_last_run(env):
    import state
    cl = setup_crafted(env)
    gh("--render", "-", "--today", "2025-11-14", stdin=json.dumps(cl))
    assert gh("--welcome").stdout.strip() == 'Welcome back. Last time (Nov 14) your top instruction was "Don\'t add comments to code I didn\'t ask you about", 23 times.'
    st = state.load()
    st["last_result"]["ts"] = 0   # pretend the last run was long ago
    state.save(st)
    r = gh("--render", "-", "--today", "2025-11-20", stdin=json.dumps(cl)).stdout
    assert "Since last run: 23 more times in 14 new sessions." in r
    st = state.load()
    st["last_result"]["ts"] = 4102444800   # far future: nothing newer
    state.save(st)
    gh("--mark-written", "--today", "2025-11-21")
    r = gh("--render", "-", "--today", "2025-11-22", stdin=json.dumps(cl)).stdout
    assert "Since last run: not repeated. The line seems to be working." in r and "You added this line on Nov 21." in r


def one_cluster(scope, n_msgs, sessions=1, label="Don't touch the config file"):
    import state
    occ = [[1760000000 + i * 3600, i % sessions, 0, i] for i in range(n_msgs)]
    state.write_private("scan.json", {"scope": scope, "sessions_scanned": 1 if scope == "session" else 38, "human_messages": 42,
                                      "unreadable": 0, "projects": ["my-app"], "texts": {}, "cands": {"0": {"occ": occ}}})
    return json.dumps({"clusters": [{"label": label, "quote": "No, " + label.lower(), "line": "Never touch the config file.", "candidate_ids": [0]}]})


def test_session_screens(env):
    import state
    state.write_private("count.json", {"all": {"sessions": 360}})
    s = gh("--render", "-", "--today", "2025-11-14", stdin=one_cluster("session", 3)).stdout
    assert s.startswith("# Groundhog\n\n## You said this 3 times in this session\n\n> **\"No, don't touch the config file\"**\n\n"
                        "That's about 2 minutes and 6 extra messages (30 seconds each, one redo reply each).\n\n"
                        "### Your repeated instructions in this session\n\n| # | Instruction | Times | |\n|---|---|---|---|\n"
                        "| 1 | Don't touch the config file | 3 | ████████████████████ |\n\n"
                        "Habits show up over many sessions. Want me to check all 360 sessions instead? (yes / no)\n")
    assert "When you say it" not in s and "Where it happens" not in s
    s = gh("--render", "-", stdin=one_cluster("session", 1)).stdout
    assert s == ("# Groundhog\n\n## Nothing repeated in this session\n\nI read 1 session (42 of your messages) and nothing came up twice. "
                 "That's normal: one session is too short to show a habit.\n\nWant me to check all 360 sessions instead? (yes / no)\n\n")


def test_nothing_found_and_tiny_screens(env):
    import state
    s = gh("--render", "-", stdin=one_cluster("all", 2, label="keep answers short")).stdout
    assert s == ("# Groundhog\n\n## No instruction came up 3 times or more\n\nI checked 38 sessions (42 of your messages). "
                 "The closest was \"keep answers short\", 2 times.\nThat's a good sign: you're not repeating yourself much.\n\n"
                 "Want the line for \"keep answers short\" anyway? (yes / no)\n\n")
    state.write_private("scan.json", {"scope": "all", "sessions_scanned": 1, "human_messages": 6, "unreadable": 0, "projects": [], "texts": {}, "cands": {}})
    s = gh("--render", "-", stdin='{"clusters": []}').stdout
    assert s == ("# Groundhog\n\n## Not enough history yet\n\nI found only 1 short session (6 messages). Claude Code keeps about 30 days "
                 "of history by default, so come back after a few more sessions.\n\n")
    state.write_private("scan.json", {"scope": "project", "sessions_scanned": 5, "human_messages": 90, "unreadable": 2, "projects": [], "texts": {}, "cands": {}})
    s = gh("--render", "-", stdin='{"clusters": []}').stdout
    assert "I checked 5 sessions in this project (90 of your messages). Nothing came up more than once." in s
    assert "(Skipped 2 files I couldn't read.)" in s


def test_project_screen_wording(env):
    s = gh("--render", "-", "--today", "2025-11-14", stdin=one_cluster("project", 5, sessions=3)).stdout
    assert "## You've said this 5 times in this project" in s and "Told 5 times across 3 sessions since" in s
    assert "Where it happens most" not in s and "When you say it" in s


def test_claude_md_section_alone(env):
    s = gh("--render", "-", "--section", "claude_md", stdin=one_cluster("session", 3)).stdout
    assert s.startswith("### Add these to CLAUDE.md\n1. `Never touch the config file.`")


# ---------------------------------------------------------------- html report
def html_for(env, share):
    cl = setup_crafted(env)
    cl["clusters"][0]["quote"] = "Never touch <script>alert(1)</script> for hunter2 in SecretProj"
    cl["clusters"][0]["label"] = "Don't touch <b>things</b> I did not ask about"
    import state
    scan, _ = crafted()
    scan["projects"] = ["SecretProj", "OtherRealName", "ThirdReal", "Fourth"]
    state.write_private("scan.json", scan)
    args = ["--html", "-", "--today", "2025-11-14"] + (["--share"] if share else [])
    r = gh(*args, stdin=json.dumps(cl), extra_env={"TMPDIR": str(env)})
    assert r.returncode == 0, r.stderr
    path = r.stdout.splitlines()[0].split("Saved: ")[1]
    assert os.path.basename(path).startswith("groundhog-report-") and stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert "file://" + path in r.stdout
    assert state.read_json("report.json", {}) == {"path": path, "opened": False}
    return open(path).read()


def test_html_default_is_the_owners_own_view(env):
    h = html_for(env, False)
    assert "Only on your computer. Don't share this page." in h
    assert "SecretProj" in h and "OtherRealName" in h
    assert "<script" not in h and "&lt;script&gt;alert(1)&lt;/script&gt;" in h      # real words, but escaped
    assert "http://" not in h and "https://" not in h and "Content-Security-Policy" in h
    assert "Lines for CLAUDE.md" in h and "Made with Groundhog, voxmith.com/groundhog" in h and len(h) < 30000


def test_html_share_variant_is_generic(env):
    h = html_for(env, True)
    for banned in ("SecretProj", "OtherRealName", "hunter2", "alert(1)", "Only on your computer", "never touch"):
        assert banned.lower() not in h.lower(), banned
    assert "Project A" in h and "<script" not in h
    assert "&lt;b&gt;things&lt;/b&gt;" in h          # the model's generic label is escaped
    assert "http://" not in h and "https://" not in h


def test_html_top10_is_sorted_by_count(env):
    h = html_for(env, False)
    nums = [int(x) for x in re.findall(r'<span class="n">(\d+)<small>', h)]
    assert nums == sorted(nums, reverse=True) and len(nums) == 10


# ---------------------------------------------------------------- summary (short terminal block)
def test_summary_is_short_and_has_the_question(env):
    cl = setup_crafted(env)
    import state
    state.write_private("report.json", {"path": "/tmp/groundhog-report-abcd1234.html", "opened": True})
    out = gh("--summary", "-", "--today", "2025-11-14", stdin=json.dumps(cl)).stdout
    assert out == ('## You\'ve said this 23 times\n\n> **"Don\'t add comments to code I didn\'t ask you about"**\n\n'
                   "Told 23 times across 4 projects since Sep 18. About 12 minutes and 46 extra messages.\n"
                   "Full report: opened in your browser.\n\nLines for CLAUDE.md:\n"
                   "1. `Do not add code comments unless I ask for them.`\n2. `Reuse existing helpers before writing new ones.`\n"
                   "3. `Never use the TypeScript type any; use a specific type or unknown.`\n4. `Keep answers short.`\n"
                   "5. `Run the tests before saying a task is done.`\n\n"
                   'Which ones should I add? Reply with numbers (like 1,2,5), "all" or "none".\n\n')
    assert len([l for l in out.splitlines() if l.strip()]) <= 12
    state.write_private("report.json", {"path": "/tmp/groundhog-report-abcd1234.html", "opened": False})
    out = gh("--summary", "-", "--today", "2025-11-14", stdin=json.dumps(cl)).stdout
    assert "Full report: saved at /tmp/groundhog-report-abcd1234.html" in out


def test_summary_variants_stay_short(env):
    import state
    s = gh("--summary", "-", "--no-save", stdin=one_cluster("session", 3)).stdout
    assert "## You said this 3 times in this session" in s and "Told 3 times in this session." in s
    assert "Which ones should I add?" in s and len([l for l in s.splitlines() if l.strip()]) <= 12
    s = gh("--summary", "-", "--no-save", stdin=one_cluster("all", 2, label="keep answers short")).stdout
    assert s.startswith("# Groundhog\n\n## No instruction came up 3 times or more") and len([l for l in s.splitlines() if l.strip()]) <= 10
    assert gh("--html", "-", stdin=one_cluster("all", 2)).stdout.split() == ["NO_REPORT", "top_count:", "2"]


def test_scan_clears_stale_report(env):
    import state
    state.write_private("report.json", {"path": "/x", "opened": True})
    scan(env, "--scope", "all")
    assert state.read_json("report.json", {}) == {}


# ---------------------------------------------------------------- network test (acceptance 4)
FORBIDDEN = {"socket", "urllib", "http", "requests", "subprocess", "ssl", "ftplib", "smtplib", "asyncio", "telnetlib",
             "xmlrpc", "socketserver", "webbrowser", "os"}


def imports(path):
    mods = set()
    for n in ast.walk(ast.parse(open(path).read())):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            mods.add((n.module or "").split(".")[0])
    return mods


@pytest.mark.parametrize("f", ["groundhog.py", "state.py"])
def test_scan_code_has_no_network_or_subprocess(f):
    path = os.path.join(SCRIPTS, f)
    assert not (imports(path) & (FORBIDDEN - {"os"})), imports(path)
    src = open(path).read()
    assert not re.search(r"urllib|socket|subprocess|requests|ftplib|smtplib|asyncio|os\.system|os\.popen|__import__|importlib", src)
    assert not re.search(r"^\s*(import|from)\s+http", src, re.M)


def test_only_ping_py_uses_urllib():
    users = [f for f in os.listdir(SCRIPTS) if f.endswith(".py") and "urllib" in open(os.path.join(SCRIPTS, f)).read()]
    assert users == ["ping.py"]
    wb = [f for f in os.listdir(SCRIPTS) if f.endswith(".py") and "webbrowser" in imports(os.path.join(SCRIPTS, f))]
    assert wb == ["open_report.py"]
    assert not (imports(os.path.join(SCRIPTS, "open_report.py")) & {"urllib", "socket", "subprocess", "requests", "http"})


def test_open_report_only_accepts_report_files_in_tmp(env, tmp_path):
    import tempfile
    import time
    good = os.path.join(tempfile.gettempdir(), "groundhog-report-test%d.html" % os.getpid())
    open(good, "w").write("<html></html>")
    fake = tmp_path / "bin"
    fake.mkdir()
    marker = tmp_path / "opened.txt"
    for n in ("xdg-open", "open", "sensible-browser", "firefox", "google-chrome", "chromium", "x-www-browser"):
        (fake / n).write_text("#!/bin/sh\necho \"$@\" >> %s\n" % marker)
        os.chmod(fake / n, 0o755)
    e = {"PATH": str(fake) + os.pathsep + os.environ["PATH"], "BROWSER": str(fake / "xdg-open") + " %s", "DISPLAY": ":0"}
    try:
        for bad in (str(tmp_path / "groundhog-report-x.html"), "/etc/passwd", os.path.join(tempfile.gettempdir(), "other.html"), "", "../x"):
            r = run("open_report.py", bad, extra_env=e)
            assert (r.returncode, r.stdout.strip()) == (0, "NOT_OPENED")
        assert run("open_report.py", extra_env=e).stdout.strip() == "NOT_OPENED"
        assert not marker.exists()
        r = run("open_report.py", good, extra_env=e)
        assert r.returncode == 0 and r.stdout.strip() in ("OPENED", "NOT_OPENED")
        time.sleep(0.5)
        r = run("open_report.py", good, extra_env=dict(e, GROUNDHOG_NO_OPEN="1"))
        assert r.stdout.strip() == "NOT_OPENED"
    finally:
        os.remove(good)


def test_open_report_updates_state(env):
    import tempfile
    import state
    good = os.path.join(tempfile.gettempdir(), "groundhog-report-state%d.html" % os.getpid())
    open(good, "w").write("x")
    try:
        state.write_private("report.json", {"path": good, "opened": False})
        run("open_report.py", good, extra_env={"GROUNDHOG_NO_OPEN": "1"})
        assert state.read_json("report.json", {})["opened"] is False
    finally:
        os.remove(good)


def test_line_format_clusters_equal_json(env):
    import state
    cl = setup_crafted(env)
    lines = "\n".join("{} | {} | {} | {}".format(c["candidate_ids"][0], c["label"], c.get("quote", ""), c["line"]) for c in cl["clusters"])
    a = gh("--summary", "-", "--today", "2025-11-14", "--no-save", stdin=json.dumps(cl)).stdout
    b = gh("--summary", "-", "--today", "2025-11-14", "--no-save", stdin=lines).stdout
    assert a == b and "Which ones should I add?" in b
    assert state.read_json("clusters.json", {})["clusters"][0]["candidate_ids"] == [0]
    assert gh("--summary", "saved", "--today", "2025-11-14", "--no-save").stdout == b
    assert G.parse_clusters("none") == {"clusters": []}
    assert G.parse_clusters("1, 2 | a | b | c | d")["clusters"][0] == {"candidate_ids": [1, 2], "label": "a", "quote": "b", "line": "c | d"}


def test_summary_opens_with_welcome_back_on_later_runs(env):
    cl = setup_crafted(env)
    first = gh("--summary", "-", "--today", "2025-11-14", stdin=json.dumps(cl)).stdout
    assert first.startswith("## You've said")
    second = gh("--summary", "saved", "--today", "2025-11-20").stdout
    assert second.startswith('Welcome back. Last time (Nov 14) your top instruction was "Don\'t add comments to code I didn\'t ask you about", 23 times.\n\n## You')


# ---------------------------------------------------------------- same answer in every session
def full_run(env, sid, cwd, clusters):
    """scan + Claude's grouping (given) + stats, as the skill does it, from one session and folder."""
    os.chdir(cwd)
    r = gh("--scan", "--days", "3650", "--scope", "all", "--session-id", sid)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    st = gh("--stats", "-", "--today", "2025-11-14", stdin=clusters(out)).stdout
    return out, json.loads(st)


def test_same_answer_from_every_session_even_if_claude_groups_differently(env, tmp_path):
    def first(out):   # run 1: Claude groups the top line under one label
        return "{} | No extra comments | {} | Do not add code comments unless I ask.".format(out["candidates"][0]["id"], out["candidates"][0]["text"])

    def other(out):   # later runs: Claude would word it differently and drop the line
        return "none"

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    o1, s1 = full_run(env, "11111111-aaaa-4bbb-8ccc-000000000000", tmp_path / "a", first)
    o2, s2 = full_run(env, "22222222-aaaa-4bbb-8ccc-000000000000", tmp_path / "b", other)
    o3, s3 = full_run(env, "33333333-aaaa-4bbb-8ccc-000000000000", tmp_path / "a", other)
    assert s1["clusters"][0]["label"] == "No extra comments" and s1["clusters"][0]["count"] == 4
    assert s1 == s2 == s3
    assert o2["known_groups"] == ["No extra comments"] and all(c["text"] != o1["candidates"][0]["text"] for c in o2["candidates"])
    # lines Claude saw and left out are not asked about again
    assert o3["candidates"] == []
    assert "forgot" in gh("--forget").stdout
    assert json.loads(gh("--scan", "--days", "3650", "--scope", "all").stdout)["candidates"] == o1["candidates"]


def test_scan_output_is_identical_from_any_folder_and_session(env, tmp_path):
    outs = []
    for i, d in enumerate(("x", "y")):
        (tmp_path / d).mkdir()
        os.chdir(tmp_path / d)
        outs.append(gh("--scan", "--days", "3650", "--scope", "all", "--session-id", "{}0000000-aaaa-4bbb-8ccc-000000000000".format(i + 5)).stdout)
    assert outs[0] == outs[1]


def test_running_session_is_left_out_unless_it_is_the_only_one(env):
    a = json.loads(gh("--scan", "--days", "3650", "--scope", "all", "--session-id", SID["bill"]).stdout)
    assert a["sessions_scanned"] == 3
    one = G.list_files("session", 3650, "/home/dev/billing-api", SID["bill"])[0]
    assert len(one) == 1  # "this session only" still reads it
