import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "skills", "burn", "scripts")
sys.path.insert(0, SCRIPTS)
import ping  # noqa: E402
import state  # noqa: E402

KEYS = ["v", "skill_version", "run_count", "resolution_rate", "sessions_judged", "sessions_resolved",
        "failed_cost_cents", "failed_share", "cost_per_resolved_cents", "agent", "listed",
        "scope", "account_id", "account_source", "install_id"]


def setup(tmp_path, monkeypatch, last=None):
    monkeypatch.setenv("BURN_HOME", str(tmp_path / "s"))
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "c"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "nogit"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for k in ("BURN_NO_PING", "DO_NOT_TRACK", "BURN_PING_URL"):
        monkeypatch.delenv(k, raising=False)
    if last is not None:
        state.save({"last_result": last})
    sent = []
    monkeypatch.setattr(ping, "send", lambda body: sent.append(body) or REPLY.get("r", {}))
    monkeypatch.setattr(ping, "unlist", lambda iid: sent.append({"unlist": iid}))
    return sent


REPLY = {}


def test_sends_exactly_the_agreed_fields(tmp_path, monkeypatch):
    sent = setup(tmp_path, monkeypatch, {"rate": 71, "scope": "project", "report": "/tmp/x", "judged": 40,
                                         "resolved": 28, "agent": "claude", "failed_cents": 41459,
                                         "failed_share": 49, "cpr_cents": 661})
    ping.run()
    assert list(sent[0]) == KEYS
    assert (sent[0]["resolution_rate"], sent[0]["scope"], sent[0]["run_count"]) == (71, "project", 1)
    assert (sent[0]["sessions_judged"], sent[0]["sessions_resolved"], sent[0]["agent"]) == (40, 28, "claude")
    assert (sent[0]["failed_cost_cents"], sent[0]["failed_share"], sent[0]["cost_per_resolved_cents"]) == (41459, 49, 661)


def test_bad_saved_values_are_not_sent(tmp_path, monkeypatch):
    sent = setup(tmp_path, monkeypatch, {"rate": "my-secret-project", "scope": "everything", "judged": 5,
                                         "resolved": 9, "agent": "my-app", "failed_cents": "lots",
                                         "failed_share": 150, "cpr_cents": -1})
    ping.run()
    assert sent[0]["resolution_rate"] is None and sent[0]["scope"] == "device"
    assert sent[0]["sessions_judged"] is None and sent[0]["sessions_resolved"] is None and sent[0]["agent"] is None
    assert sent[0]["failed_cost_cents"] is None and sent[0]["failed_share"] is None and sent[0]["cost_per_resolved_cents"] is None


def test_off_switches(tmp_path, monkeypatch):
    sent = setup(tmp_path, monkeypatch, {"rate": 50, "scope": "device"})
    monkeypatch.setenv("BURN_NO_PING", "1")
    ping.run()
    monkeypatch.delenv("BURN_NO_PING")
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    ping.run()
    monkeypatch.delenv("DO_NOT_TRACK")
    ping.set_switch("off")
    ping.run()
    st = state.load()  # no ping was sent; /burn off only asked to be taken off the board
    assert sent == [{"unlist": st["install_id"]}] and st["run_count"] == 3


def test_status_and_notice(tmp_path, monkeypatch, capsys):
    setup(tmp_path, monkeypatch, {"rate": 40, "scope": "device"})
    ping.status()
    out = capsys.readouterr().out
    body = json.loads(out[out.index("{"):out.rindex("}") + 1])
    assert list(body) == KEYS and body["resolution_rate"] == 40
    assert "It never sends your chats, prompts, file names or project names." in out and '"listed": true' in out
    ping.notice()
    first = capsys.readouterr().out
    ping.notice()
    assert first.startswith("Privacy: Burn sends your end result") and "/burn off" in first and capsys.readouterr().out == ""


def test_cli_off(tmp_path):
    env = dict(os.environ, BURN_HOME=str(tmp_path / "s"))
    out = subprocess.run([sys.executable, os.path.join(SCRIPTS, "ping.py"), "off"], env=env,
                         capture_output=True, text=True).stdout
    assert out.startswith("Done. Burn will not tell VoxMith anything")



def test_listed_from_the_first_run(tmp_path, monkeypatch, capsys):
    sent = setup(tmp_path, monkeypatch, {"rate": 61, "scope": "device", "judged": 213, "resolved": 129, "agent": "claude"})
    REPLY["r"] = {"ranked": True, "rank": 12, "of": 340, "better_than": 72, "score": 54, "me": "Ab3_x-9QzTt7"}
    ping.run()                     # the very first run is listed and shows the rank line
    assert sent[-1]["listed"] is True
    out = capsys.readouterr().out
    assert out == ("Leaderboard: better than 72% of Burn users (rank 12 of 340). "
                   "analyze.voxmith.com/burn/leaderboard?me=Ab3_x-9QzTt7  Opt out: /burn off\n")
    assert state.load()["view_token"] == "Ab3_x-9QzTt7"
    REPLY["r"] = {"ranked": True, "rank": 1, "of": 1, "better_than": 0, "score": 32}
    ping.run()
    assert capsys.readouterr().out.startswith("Leaderboard: you're the first one on the Burn leaderboard.")
    REPLY["r"] = {"ranked": False, "min_sessions": 20}
    ping.run()
    assert "you need 20 judged sessions" in capsys.readouterr().out
    REPLY["r"] = {"ranked": True, "rank": "<script>", "of": 3, "better_than": 50}
    ping.run()
    assert capsys.readouterr().out == ""  # anything odd in the reply prints nothing
    REPLY.clear()

def test_off_takes_you_off_the_board(tmp_path, monkeypatch, capsys):
    sent = setup(tmp_path, monkeypatch, {"rate": 61, "scope": "device"})
    ping.run()
    iid = state.load()["install_id"]
    ping.set_switch("off")
    assert sent[-1] == {"unlist": iid}
    assert "you are off the leaderboard" in capsys.readouterr().out
    def boom(_):
        raise OSError("offline")
    monkeypatch.setattr(ping, "unlist", boom)
    ping.set_switch("off")
    assert "email manvendra.singh@voxmith.com" in capsys.readouterr().out


def test_project_runs_print_no_board_line(tmp_path, monkeypatch, capsys):
    setup(tmp_path, monkeypatch, {"rate": 61, "scope": "project", "judged": 50, "resolved": 30})
    REPLY["r"] = {"ranked": True, "rank": 1, "of": 2, "better_than": 100}
    ping.run()
    assert capsys.readouterr().out == ""
    REPLY.clear()



def test_bad_find_me_code_is_ignored(tmp_path, monkeypatch, capsys):
    setup(tmp_path, monkeypatch, {"rate": 61, "scope": "device", "judged": 213, "resolved": 129})
    REPLY["r"] = {"ranked": True, "rank": 1, "of": 2, "better_than": 100, "me": "<script>alert(1)</script>"}
    ping.run()
    out = capsys.readouterr().out
    assert "?me=" not in out and "script" not in out and "view_token" not in state.load()
    REPLY.clear()
