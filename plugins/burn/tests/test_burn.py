import ast
import json
import os
import subprocess
import sys
import time
from decimal import Decimal

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "skills", "burn", "scripts")
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, HERE)
import burn  # noqa: E402
import synth  # noqa: E402

NOW = "1800000000"


@pytest.fixture
def hist(tmp_path, monkeypatch):
    ch, xh, S = synth.build(str(tmp_path))
    env = {"CLAUDE_CONFIG_DIR": ch, "CODEX_HOME": xh, "BURN_HOME": str(tmp_path / "state"), "BURN_NOW": NOW,
           "HOME": str(tmp_path / "home"), "BURN_NO_PING": "1"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return tmp_path, env, S


AGENT_VARS = ("CLAUDECODE", "CLAUDE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SANDBOX")


def run(env, *args, cwd=None, extra=None):
    e = {k: v for k, v in os.environ.items() if k not in AGENT_VARS}  # tests may run inside Claude Code
    e.update(env, **(extra or {}))
    return subprocess.run([sys.executable, os.path.join(SCRIPTS, "burn.py")] + list(args), cwd=cwd, env=e,
                          capture_output=True, text=True, check=True).stdout


def result(env, *args, **kw):
    return json.loads(run(env, "--json", *args, **kw))


def by_id(env, *args):
    sessions, _ = burn.scan(*args, now=float(NOW))
    return {s["id"]: s for s in sessions}


# ---------------------------------------------------------------- the rules

def test_classify_each_kind(hist):
    _, env, S = hist
    s = by_id(env)
    assert not s[S["resolved"].sid]["failed"]
    assert s[S["corrected"].sid]["corrected"] and s[S["corrected"].sid]["corrections"] == 2
    assert not s[S["corrected"].sid]["gave_up"]
    assert s[S["gave_up"].sid]["gave_up"] and not s[S["gave_up"].sid]["corrected"]
    assert s[S["repeated"].sid]["repeated"] and not s[S["repeated"].sid]["corrected"]
    assert not s[S["short"].sid]["counted"]
    r = s[S["interrupts"].sid]
    assert r["corrections"] == 2 and r["corrected"] and r["gave_up"]
    assert not s[S["resumed"].sid]["failed"]
    c = S["codex"]
    assert not s[c["legacy"]]["failed"] and s[c["legacy"]]["human"] == 2
    assert s[c["paged"]]["corrected"] and s[c["paged"]]["gave_up"] and s[c["paged"]]["human"] == 3
    assert s[c["fork"]]["human"] == 2 and not s[c["fork"]]["failed"]  # copied parent messages not counted


@pytest.mark.parametrize("text,want", [
    ("no, use the other file", True), ("No.", True), ("nope", True), ("that's wrong", True),
    ("revert that change", True), ("why did you delete it?", True), ("this is not what I asked for", True),
    ("stop adding comments", True), ("no problem, carry on", False), ("now add the tests", False),
    ("nothing else", False), ("I know you can do it", False), ("great, ship it", False),
])
def test_is_correction(text, want):
    assert burn.is_correction(text) is want


@pytest.mark.parametrize("events,failed", [
    ([("H", "add caching", False), ("A",), ("I",), ("H", "also cover the null case", True), ("A",), ("I",),
      ("H", "and the empty list", True), ("A",), ("H", "great, commit it", True), ("A",)], False),   # Esc to steer
    ([("H", "update readme", False), ("A",), ("H", "No, that looks good. Now update the docs", True), ("A",),
      ("H", "No. Ship it, thanks", True), ("A",)], False),                                           # "no" = answer
    ([("H", "fix bug", False), ("A",), ("H", "run the tests", True), ("A",), ("H", "run the tests", True), ("A",)], False),
    ([("H", "why is dns failing", False), ("A",), ("H", "never mind, found it myself, it was dns. thanks!", True)], False),
    ([("H", "fix 401", False), ("A",), ("H", "ok I will handle this manually", True)], True),
    ([("H", "add retry", False), ("A",), ("H", "no, use exponential backoff", True), ("A",)], False),  # answered
    ([("H", "add retry", False), ("A",), ("H", "no, use exponential backoff", True)], True),           # unanswered
])
def test_rules_v2_cases(events, failed):
    c = burn.classify(events)
    assert (c["corrected"] or c["repeated"] or c["gave_up"]) is failed


def test_correction_needs_an_agent_turn_first():
    assert burn.classify([("H", "no, wrong", False), ("H", "no, wrong again", False)])["corrections"] == 0


# ---------------------------------------------------------------- tokens and money

def test_split_lines_and_resumed_copies_counted_once(hist):
    _, env, S = hist
    s = by_id(env)
    one = 100000 + 50000 + 1000000 + 2000000
    assert s[S["resolved"].sid]["tokens"] == 2 * one          # 3 split lines + 2 copies in session 6 -> still 2
    assert s[S["resumed"].sid]["tokens"] == 2 * one           # only its own two responses
    assert s[S["resolved"].sid]["cost"] == 2 * Decimal("9.8")  # 100k*4 + 50k*20 + 1M*8 + 2M*0.2 per 1M


def test_subagent_tokens_go_to_parent(hist):
    _, env, S = hist
    s = by_id(env)[S["subagent"].sid]
    assert s["tokens"] == 2 * 3150000 + 11000
    assert s["human"] == 2  # sub-agent messages are not human messages
    assert s["cost"] == 2 * Decimal("9.8") + (10000 * 1 + 1000 * 5) / Decimal(10 ** 6)


def test_unknown_model_counted_but_not_priced(hist):
    _, env, S = hist
    s = by_id(env)[S["unknown"].sid]
    assert s["tokens"] == 2 * 3150000 and s["unpriced"] == 2 * 3150000 and s["cost"] == 0
    r = result(env)
    assert r["unpriced_tokens"] == 2 * 3150000
    assert {"model": "claude-future-9", "counted": 1, "rate": 100, "tokens": 6300000, "cost": None} in r["by_model"]


def test_codex_running_total_and_fill_line(hist):
    _, env, S = hist
    s = by_id(env)[S["codex"]["legacy"]]
    assert s["tokens"] == 3000000 + 300000  # max of the running total, the reset line adds nothing
    # fresh 2M * 1.75 + cached 1M * 0.175 + out 300k * 14
    assert s["cost"] == (2000000 * Decimal("1.75") + 1000000 * Decimal("0.175") + 300000 * 14) / 10 ** 6


def test_codex_fork_counts_only_its_own_tokens(hist):
    _, env, S = hist
    s = by_id(env)[S["codex"]["fork"]]
    assert s["tokens"] == 400000 + 40000
    assert s["cost"] == (200000 * Decimal("1.75") + 200000 * Decimal("0.175") + 40000 * 14) / 10 ** 6


def test_fork_without_marker_counts_everything(tmp_path, monkeypatch):
    xh = str(tmp_path / "codex")
    t = "c0000009-aaaa-4bbb-8ccc-000000000000"
    synth.codex_file(xh, t, [synth.cx("session_meta", {"id": t, "cwd": "/x", "forked_from_id": "zzz"}),
                             synth.cx("turn_context", {"model": "gpt-5"}),
                             synth.cx("event_msg", {"type": "user_message", "message": "hello there friend"}),
                             synth.cx("event_msg", synth.tok(1000, 0, 100))])
    monkeypatch.setenv("CODEX_HOME", xh)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "none"))
    (s,), _ = burn.scan(now=float(NOW))
    assert s["tokens"] == 1100 and s["human"] == 1


def test_price_rules():
    t = burn.load_prices()["anthropic"]
    u = dict(**{"in": 1000000, "out": 0, "cw5": 0, "cw1": 0, "cr": 0}, fast=False, us=False)
    assert burn.claude_cost(t, "claude-opus-5-5", u) == 4
    assert burn.claude_cost(t, "claude-opus-4-5-20251101", u) == 5       # dated id
    assert burn.claude_cost(t, "claude-sonnet-5-5[1m]", u) == 2         # [1m] suffix
    assert burn.claude_cost(t, "claude-opus-5-5", dict(u, fast=True)) == 8
    assert burn.claude_cost(t, "claude-opus-5-5", dict(u, us=True)) == Decimal("4.4")
    assert burn.claude_cost(t, "claude-haiku-5-5", dict(u, **{"in": 50000})) == Decimal("0.005")
    assert burn.claude_cost(t, "claude-haiku-5-5", dict(u, **{"in": 150000})) == Decimal("0.075")  # over 100k
    assert burn.claude_cost(t, "claude-3-opus", u) is None
    p = burn.load_prices()
    assert p["checked"] and set(p) >= {"anthropic", "openai", "sources"}


# ---------------------------------------------------------------- the three numbers

def test_headline_numbers(hist):
    _, env, S = hist
    r = result(env)
    # counted: claude 1,2,3,4,6,7,8,9 (5 too short) + codex 3 = 11; failed: 2,3,4,9,c2 = 5
    assert (r["sessions"], r["counted"], r["too_short"], r["failed"], r["resolved"]) == (12, 11, 1, 5, 6)
    assert r["resolution_rate"] == 55  # 6/11 = 54.5 -> 55
    s = by_id(env)
    burned = sum(s[k]["cost"] for k in (S["corrected"].sid, S["gave_up"].sid, S["repeated"].sid,
                                         S["interrupts"].sid, S["codex"]["paged"]))
    assert r["burned_cost"] == burn.money(burned)
    total = sum(x["cost"] for x in s.values())
    assert r["cost_per_resolved"] == burn.money(total / 6)
    assert r["reasons"] == {"corrected": 3, "repeated": 1, "gave_up": 3}
    assert r["top_failed"][0]["reasons"]
    assert r["history_start"] == {"claude": "2026-10-05", "codex": "2026-10-08"}


def test_summary_text(hist):
    _, env, S = hist
    out = run(env, "--session-id", "ffffffff-0000-4000-8000-000000000000")
    assert out.startswith("Burn  (Claude Code + Codex, whole device)\n\nResolution rate     55%   (6 of 11 sessions judged)\n"
                          "Failed sessions     ")
    assert "at API prices" in out and "Most expensive failed sessions" in out and "Why API prices" in out
    assert "Your current session is not counted." not in out  # that id is not in the history
    assert "Full report: " in out and "calls" not in out.lower()


# ---------------------------------------------------------------- same device, same answer

def test_same_answer_from_any_folder_and_session(hist, tmp_path):
    _, env, _ = hist
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    o1 = run(env, "--json", "--session-id", "11111111-0000-4000-8000-000000000000", cwd=str(a), extra={"PYTHONHASHSEED": "1"})
    o2 = run(env, "--json", "--session-id", "22222222-0000-4000-8000-000000000000", cwd=str(b), extra={"PYTHONHASHSEED": "999"})
    o3 = run(env, "--json", cwd=str(b), extra={"PYTHONHASHSEED": "random"})
    assert o1 == o2 == o3


def test_one_new_session_moves_only_its_numbers(hist):
    tmp, env, _ = hist
    before = result(env)
    s = synth.Claude(env["CLAUDE_CONFIG_DIR"], "/home/dev/new-proj", synth.sid(50), day=20)
    s.human("write a script").agent().human("no, in python").agent().human("wrong, python 3").agent()
    s.write()
    after = result(env)
    assert after["sessions"] == before["sessions"] + 1
    assert after["counted"] == before["counted"] + 1 and after["failed"] == before["failed"] + 1
    assert after["resolved"] == before["resolved"]
    assert Decimal(after["burned_cost"]) - Decimal(before["burned_cost"]) == Decimal("29.40")
    assert after["reasons"]["corrected"] == before["reasons"]["corrected"] + 1
    assert [p["key"] for p in after["projects"]] == sorted(
        [p["key"] for p in before["projects"]] + ["new-proj"],
        key=lambda k: [(-Decimal(p["burned"]), -Decimal(p["cost"]), p["key"]) for p in after["projects"] if p["key"] == k][0])
    for k in ("by_agent", "weeks"):
        assert len(after[k]) >= len(before[k])
    old_projects = {p["key"]: p for p in before["projects"]}
    for p in after["projects"]:
        if p["key"] != "new-proj":
            assert p == old_projects[p["key"]]


def test_current_session_is_skipped(hist):
    _, env, S = hist
    r = result(env, "--session-id", S["corrected"].sid)
    assert r["current_excluded"] and r["sessions"] == 11 and r["failed"] == 4
    assert "Your current session is not counted." in run(env, "--session-id", S["corrected"].sid)


def test_running_session_is_skipped(hist):
    _, env, S = hist
    p = os.path.join(S["gave_up"].folder(), S["gave_up"].sid + ".jsonl")
    os.utime(p, (int(NOW) - 60, int(NOW) - 60))
    r = result(env)
    assert r["still_running"] == 1 and r["sessions"] == 11
    assert "1 session still running was not counted." in run(env)


def test_project_scope(hist):
    _, env, S = hist
    r = result(env, "--scope", "project", "--project-dir", "/home/dev/billing-api")
    assert r["project"] == "billing-api" and r["sessions"] == 3  # claude 3, 4 + codex c2
    assert "this project: billing-api" in run(env, "--scope", "project", "--project-dir", "/home/dev/billing-api")


def test_empty_history(tmp_path):
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path / "c"), "CODEX_HOME": str(tmp_path / "x"), "BURN_HOME": str(tmp_path / "s"),
           "BURN_NOW": NOW}
    out = run(env)
    assert "No Claude Code or Codex sessions found on this computer yet." in out
    assert result(env)["resolution_rate"] is None


def test_compressed_codex_without_zstd(hist):
    _, env, _ = hist
    d = os.path.join(env["CODEX_HOME"], "archived_sessions")
    os.makedirs(d)
    open(os.path.join(d, "rollout-2026-01-01T00-00-00-c0000099-aaaa-4bbb-8ccc-000000000000.jsonl.zst"), "wb").close()
    try:
        import compression.zstd  # noqa: F401
        pytest.skip("Python has zstd")
    except ImportError:
        assert result(env)["compressed_skipped"] == 1


def test_bad_lines_do_not_crash(hist):
    _, env, S = hist
    p = os.path.join(S["resolved"].folder(), S["resolved"].sid + ".jsonl")
    with open(p, "a") as f:
        f.write('{"type":"assistant", broken\n\x00\x01 not json\n')
    os.utime(p, (synth.OLD, synth.OLD))
    assert result(env)["bad_lines"] == 1


def test_large_history_is_fast(tmp_path):
    ch = str(tmp_path / "c")
    for i in range(400):
        s = synth.Claude(ch, "/home/dev/p%d" % (i % 20), synth.sid(1000 + i), day=i % 40)
        for j in range(40):
            s.human("step %d of the task, please do it" % j).tool_result().agent(split=2)
        s.write()
    env = {"CLAUDE_CONFIG_DIR": ch, "CODEX_HOME": str(tmp_path / "x"), "BURN_HOME": str(tmp_path / "s"), "BURN_NOW": NOW}
    t = time.time()
    r = result(env)
    assert r["sessions"] == 400 and time.time() - t < 30


# ---------------------------------------------------------------- report and privacy

def test_html_report_has_no_message_text(hist):
    _, env, S = hist
    sessions, info = burn.scan(now=float(NOW))
    doc = burn.html_report(burn.compute(sessions, info, "device", None))
    for s in sessions:  # only a session's name may appear (its title, or the first line of its first message)
        humans = [e[1] for e in s["events"] if e[0] == "H"]
        for t in humans[1:]:
            assert t not in doc or t in s["name"]
    assert "at API prices" in doc and "Download image" in doc and "<canvas" in doc
    assert "<script>" in doc and "billing-api" in doc


def test_html_escapes_project_names(tmp_path, monkeypatch):
    ch = str(tmp_path / "c")
    s = synth.Claude(ch, "/home/<img src=x onerror=alert(1)>", synth.sid(70))
    s.human("do the thing please").agent().human("no, the other thing").agent().human("wrong again").agent()
    s.write()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", ch)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "x"))
    sessions, info = burn.scan(now=float(NOW))
    doc = burn.html_report(burn.compute(sessions, info, "device", None))
    assert "<img" not in doc and "&lt;img src=x onerror=alert(1)&gt;" in doc


NETWORK = {"socket", "ssl", "http", "urllib", "requests", "ftplib", "smtplib", "asyncio", "webbrowser", "subprocess"}


@pytest.mark.parametrize("name", ["burn.py", "state.py"])
def test_scanner_has_no_network_imports(name):
    tree = ast.parse(open(os.path.join(SCRIPTS, name)).read())
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module.split(".")[0])
    assert not mods & NETWORK, mods & NETWORK


def test_compressed_codex_read_on_314(hist):
    _, env, S = hist
    try:
        from compression import zstd
    except ImportError:
        pytest.skip("needs Python 3.14")
    t = "c0000077-aaaa-4bbb-8ccc-000000000000"
    p = synth.codex_file(env["CODEX_HOME"], t, [synth.cx("session_meta", {"id": t, "cwd": "/x"}),
                                                 synth.cx("turn_context", {"model": "gpt-5"}),
                                                 synth.cx("event_msg", synth.tok(1000, 0, 100))], day=6)
    raw = open(p, "rb").read()
    os.remove(p)
    with zstd.open(p + ".zst", "wb") as f:
        f.write(raw)
    os.utime(p + ".zst", (synth.OLD, synth.OLD))
    assert by_id(env)[t]["tokens"] == 1100


def test_session_id_from_environment(hist):
    _, env, S = hist
    r = result(env, "--session-id", "${CLAUDE_SESSION_ID}", "--agent", "all", extra={"CLAUDE_SESSION_ID": S["gave_up"].sid})
    assert r["current_excluded"] and r["sessions"] == 11


def test_tar_one_liner_layout(tmp_path):
    import tarfile
    repo = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
    tarball = tmp_path / "main.tar.gz"
    with tarfile.open(tarball, "w:gz") as t:  # GitHub's archive has one top folder <repo>-main/
        t.add(os.path.join(repo, "plugins"), arcname="groundhog-main/plugins",
              filter=lambda i: None if "__pycache__" in i.name else i)
    skills = tmp_path / "home" / ".claude" / "skills"
    skills.mkdir(parents=True)
    subprocess.run("cat {} | tar -xz --strip-components=4 -C {} groundhog-main/plugins/burn/skills/burn".format(tarball, skills),
                   shell=True, check=True)
    d = skills / "burn"
    assert (d / "SKILL.md").is_file() and (d / "prices.json").is_file()
    assert sorted(os.listdir(d / "scripts")) == ["burn.py", "open_report.py", "ping.py", "state.py"]


def test_project_known_even_without_timestamps(tmp_path, monkeypatch):
    ch = str(tmp_path / "c")
    s = synth.Claude(ch, "/home/dev/no-times", synth.sid(80))
    s.human("do the thing please").agent().human("thanks, done").agent()
    for o in s.lines:
        o["timestamp"] = "not a date"
    s.write()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", ch)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "x"))
    (x,), _ = burn.scan(now=float(NOW))
    assert x["project"] == "no-times" and x["start"] is None
    r = burn.compute([x], {"current_excluded": False, "still_running": 0, "compressed_skipped": 0, "unreadable": 0}, "device", None)
    assert r["weeks"] == [] and r["history_start"] == {}


def test_reads_only_the_agent_it_runs_in(hist):
    _, env, S = hist
    both = result(env, "--agent", "all")
    claude = result(env, "--agent", "claude")
    codex = result(env, "--agent", "codex")
    assert (both["sessions"], claude["sessions"], codex["sessions"]) == (12, 9, 3)
    assert claude["agents"] == ["claude"] and codex["agents"] == ["codex"]
    assert run(env, "--agent", "codex").startswith("Burn  (Codex, whole device)")
    assert result(env, extra={"CLAUDECODE": "1"})["agent"] == "claude"
    assert result(env, extra={"CODEX_THREAD_ID": "t1"})["agent"] == "codex"
    assert result(env)["agent"] == "all"


def test_detect_agent():
    assert burn.detect_agent({"CLAUDECODE": "1"}) == "claude"
    assert burn.detect_agent({"CODEX_THREAD_ID": "x"}) == "codex"
    assert burn.detect_agent({}) == "all"


def test_empty_history_names_the_agent(tmp_path):
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path / "c"), "CODEX_HOME": str(tmp_path / "x"), "BURN_HOME": str(tmp_path / "s"), "BURN_NOW": NOW}
    assert "No Codex sessions found on this computer yet." in run(env, "--agent", "codex")


def test_codex_install_layout(tmp_path):
    import tarfile
    repo = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
    tarball = tmp_path / "main.tar.gz"
    with tarfile.open(tarball, "w:gz") as t:
        t.add(os.path.join(repo, "plugins"), arcname="groundhog-main/plugins",
              filter=lambda i: None if "__pycache__" in i.name else i)
    skills = tmp_path / "home" / ".agents" / "skills"
    skills.mkdir(parents=True)
    line = [l for l in open(os.path.join(os.path.dirname(HERE), "README.md")) if ".agents/skills" in l and "curl" in l][0]
    cmd = line.strip().replace("curl -fsSL https://github.com/VoxMith/groundhog/archive/refs/heads/main.tar.gz", "cat " + str(tarball))
    subprocess.run(cmd.replace("~", str(tmp_path / "home")), shell=True, check=True)
    d = skills / "burn"
    assert "# Burn (Codex)" in (d / "SKILL.md").read_text() and not (d / "SKILL.codex.md").exists()
    assert (d / "scripts" / "burn.py").is_file() and (d / "prices.json").is_file()


def test_codex_thread_is_skipped(hist):
    _, env, S = hist
    r = result(env, "--agent", "codex", extra={"CODEX_THREAD_ID": S["codex"]["fork"]})
    assert r["current_excluded"] and r["sessions"] == 2


def test_numbers_agree_everywhere(hist):
    _, env, _ = hist
    r = result(env, "--agent", "all")
    for k in ("projects", "weeks", "by_agent"):
        assert sum(x["counted"] for x in r[k]) == r["counted"], k
    assert sum(m["counted"] for m in r["by_model"]) == r["counted"]
    assert r["failed_share"] == burn.pct(Decimal(r["burned_cost"]), Decimal(r["total_cost"])) or abs(
        r["failed_share"] - float(r["burned_cost"]) * 100 / float(r["total_cost"])) <= 1
    assert "{}% of your usage".format(r["failed_share"]) in run(env, "--agent", "all")


def test_session_names(tmp_path, monkeypatch):
    ch = str(tmp_path / "c")
    def make(n, extra, first="fix the flaky login test please"):
        x = synth.Claude(ch, "/home/dev/app", synth.sid(n))
        x.human(first).agent().human("no, only the client").agent().human("wrong, revert it").agent()
        x.write(extra_lines=extra)
    make(90, [{"type": "ai-title", "aiTitle": "Old title", "sessionId": synth.sid(90)},
              {"type": "ai-title", "aiTitle": "Fix flaky login test", "sessionId": synth.sid(90)}])
    make(91, [{"type": "ai-title", "aiTitle": "Auto title", "sessionId": synth.sid(91)},
              {"type": "custom-title", "customTitle": "My login fix", "sessionId": synth.sid(91)}])
    make(92, [], first="x" * 100 + "\nsecond line")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", ch)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "x"))
    ss, info = burn.scan(now=float(NOW))
    names = {x["id"]: x["name"] for x in ss}
    assert names[synth.sid(90)] == "Fix flaky login test"          # latest automatic title
    assert names[synth.sid(91)] == "My login fix"                   # your own name wins
    assert names[synth.sid(92)] == "x" * 59 + "\u2026"              # first message, first line, cut
    r = burn.compute(ss, info, "device", None, "claude")
    assert {x["name"] for x in r["top_failed"]} == set(names.values())
    out = burn.summary(r)
    assert '          "My login fix"' in out
    doc = burn.html_report(r)
    assert "claude --resume " + synth.sid(91) in doc and "My login fix" in doc


def test_share_card_shows_rounded_api_cost(hist):
    _, env, _ = hist
    sessions, info = burn.scan(now=float(NOW))
    r = burn.compute(sessions, info, "device", None)
    approx = burn.approx_money(r["burned_cost"])
    assert approx.startswith("~$") and "." not in approx
    assert approx + " at API prices" in burn.share_text(r)
    assert json.dumps(approx + " at API prices") in burn.html_report(r)
    assert burn.approx_money("1234.50") == "~$1,235" and burn.approx_money("0.40") == "~$0"



def test_report_links_to_your_spot(hist, tmp_path, monkeypatch):
    import state
    monkeypatch.setenv("BURN_HOME", str(tmp_path / "st"))
    sessions, info = burn.scan(now=float(NOW))
    r = burn.compute(sessions, info, "device", None)
    assert "?utm_source=burn&amp;utm_medium=report\"" in burn.html_report(r)
    state.save({"view_token": "Ab3_x-9QzTt7"})
    assert "&amp;me=Ab3_x-9QzTt7" in burn.html_report(r)
    state.save({"view_token": "\"><script>"})
    assert "script>" not in burn.html_report(r).split("See your rank")[0].split("<a class=board")[1]



def test_last_result_saves_money_for_the_board(hist):
    _, env, _ = hist
    run(env)
    import state as st_mod
    lr = json.load(open(os.path.join(env["BURN_HOME"], "state.json")))["last_result"]
    r = result(env)
    assert lr["failed_cents"] == int(Decimal(r["burned_cost"]) * 100)
    assert lr["failed_share"] == r["failed_share"]
    assert lr["cpr_cents"] == int(Decimal(r["cost_per_resolved"]) * 100)
