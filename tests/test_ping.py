import http.server
import json
import os
import threading
import time

import pytest

import state
from conftest import REPO, SCRIPTS, run

KEYS = ["v", "skill_version", "run_count", "top_count", "scope", "account_id", "account_source", "install_id"]


class Stub:
    def __init__(self, delay=0, status=204):
        got = self.got = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                got.append((self.path, json.loads(body)))
                time.sleep(delay)
                self.send_response(status)
                self.end_headers()

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:{}/api/v1/public/groundhog/ping".format(self.srv.server_address[1])
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


@pytest.fixture
def stub():
    s = Stub()
    yield s
    s.close()


def ping(stub_or_url, *args, **env):
    url = stub_or_url if isinstance(stub_or_url, str) else stub_or_url.url
    return run("ping.py", *args, extra_env=dict(GROUNDHOG_PING_URL=url, **env))


def set_claude_email(home, email):
    (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": email, "accountUuid": "u-1", "displayName": "Jane Q"}, "x": 1}))


def test_body_has_exactly_the_spec_keys_and_claude_email(env, stub):
    set_claude_email(env / "home", " Jane.Doe@Example.COM ")
    assert ping(stub, "--top-count", "23", "--scope", "all").returncode == 0
    path, body = stub.got[0]
    assert path == "/api/v1/public/groundhog/ping" and list(body) == KEYS
    assert (body["v"], body["skill_version"], body["run_count"], body["top_count"], body["scope"]) == (1, "0.2.1", 1, 23, "all")
    assert body["account_id"] == "jane.doe@example.com" and body["account_source"] == "claude"
    assert len(body["install_id"]) == 36
    assert "accountUuid" not in json.dumps(body) and "Jane Q" not in json.dumps(body)
    ping(stub, "--top-count", "0", "--scope", "session")
    assert stub.got[1][1]["run_count"] == 2 and stub.got[1][1]["install_id"] == body["install_id"]
    assert state.load()["last_ping"]["result"] == "ok"


def test_claude_config_dir_is_honoured(env, stub):
    d = env / "cfg"
    d.mkdir()
    (d / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "cfg@example.com"}}))
    ping(stub, "--top-count", "1", "--scope", "all", CLAUDE_CONFIG_DIR=str(d))
    assert stub.got[0][1]["account_id"] == "cfg@example.com"


def test_git_email_then_none(env, stub):
    (env / "gitcfg").write_text("[user]\n\temail = Dev@Corp.io\n")
    ping(stub, "--top-count", "1", "--scope", "all", GIT_CONFIG_GLOBAL=str(env / "gitcfg"))
    assert (stub.got[0][1]["account_id"], stub.got[0][1]["account_source"]) == ("dev@corp.io", "git")
    ping(stub, "--top-count", "1", "--scope", "all")
    assert (stub.got[1][1]["account_id"], stub.got[1][1]["account_source"]) == (None, "none")
    set_claude_email(env / "home", "not an email")   # invalid claude value is ignored
    ping(stub, "--top-count", "1", "--scope", "all", GIT_CONFIG_GLOBAL=str(env / "gitcfg"))
    assert stub.got[2][1]["account_source"] == "git"


def test_claude_email_wins_over_git(env, stub):
    (env / "gitcfg").write_text("[user]\n\temail = dev@corp.io\n")
    set_claude_email(env / "home", "me@claude.ai")
    ping(stub, "--top-count", "1", "--scope", "all", GIT_CONFIG_GLOBAL=str(env / "gitcfg"))
    assert stub.got[0][1]["account_source"] == "claude"


def test_off_is_remembered_and_nothing_is_sent(env, stub):
    r = run("ping.py", "off")
    assert r.stdout.strip() == "Done. Groundhog will not tell VoxMith anything from now on. The scan still works."
    assert state.load()["ping"] == "off"
    ping(stub, "--top-count", "5", "--scope", "all")
    assert stub.got == []
    assert state.load()["run_count"] == 1          # the local counter still counts
    assert "OFF" in run("ping.py", "status").stdout
    run("ping.py", "on")
    ping(stub, "--top-count", "5", "--scope", "all")
    assert len(stub.got) == 1


@pytest.mark.parametrize("name", ["GROUNDHOG_NO_PING", "DO_NOT_TRACK"])
def test_env_switches_suppress(env, stub, name):
    ping(stub, "--top-count", "5", "--scope", "all", **{name: "1"})
    assert stub.got == []
    ping(stub, "--top-count", "5", "--scope", "all", **{name: "0"})
    assert len(stub.got) == 1


def test_failure_is_silent_and_recorded(env):
    r = ping("http://127.0.0.1:9/nope", "--top-count", "1", "--scope", "all")
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    assert state.load()["last_ping"]["result"] == "failed"
    assert "last ping: failed" in run("ping.py", "status").stdout


def test_server_error_counts_as_failed(env):
    s = Stub(status=500)
    try:
        ping(s, "--top-count", "1", "--scope", "all")
        assert state.load()["last_ping"]["result"] == "failed"
    finally:
        s.close()


def test_three_second_timeout(env):
    s = Stub(delay=8)
    try:
        t = time.time()
        r = ping(s, "--top-count", "1", "--scope", "all")
        assert r.returncode == 0 and time.time() - t < 6
        assert state.load()["last_ping"]["result"] == "failed"
    finally:
        s.srv.shutdown()


def test_bad_arguments_are_rejected_before_any_send(env, stub):
    for args in (["--top-count", "100001", "--scope", "all"], ["--top-count", "-1", "--scope", "all"],
                 ["--top-count", "x", "--scope", "all"], ["--top-count", "1", "--scope", "everything"], ["--top-count", "1"]):
        assert ping(stub, *args).returncode != 0
    assert stub.got == []


def test_no_way_to_pass_text(env, stub):
    r = ping(stub, "--top-count", "3", "--scope", "all", "--text", "my secret words")
    assert r.returncode != 0 and stub.got == []


def test_bad_url_scheme_is_ignored(env):
    r = ping("file:///etc/passwd", "--top-count", "1", "--scope", "all")
    assert r.returncode == 0 and state.load()["last_ping"]["result"] == "failed"


def test_first_run_footnote_shows_once(env):
    want = ("Privacy: Groundhog tells VoxMith that you ran it, with your account email and a count. "
            "It never sends your chats or what you said. Turn it off any time: /groundhog off")
    assert run("ping.py", "notice").stdout.strip() == want
    assert run("ping.py", "notice").stdout == ""
    st = state.load()
    assert st["notice_shown"] is True and st["notice_version"] == state.NOTICE_VERSION
    st["notice_version"] = 0           # the sent fields changed: shown once more
    state.save(st)
    assert run("ping.py", "notice").stdout.strip() == want
    assert run("ping.py", "notice").stdout == ""


def test_status_shows_exactly_the_fields(env):
    set_claude_email(env / "home", "me@claude.ai")
    out = run("ping.py", "status").stdout
    assert out.startswith("Usage ping: ON")
    body = json.loads(out[out.index("{"):out.rindex("}") + 1])
    assert list(body) == KEYS and body["account_id"] == "me@claude.ai"
    assert not (env / "gh" / "state.json").exists()   # status never creates state


def test_state_file_is_private(env, stub):
    ping(stub, "--top-count", "1", "--scope", "all")
    assert os.stat(env / "gh" / "state.json").st_mode & 0o777 == 0o600
