"""Build a SYNTHETIC Claude Code + Codex history in a folder. No real history is ever read."""
import datetime
import json
import os

T0 = "2026-10-05T10:00:00.000Z"
OLD = 1700000000  # file mtime far in the past, so nothing looks "still running"


def sid(n):
    return "{:08d}-aaaa-4bbb-8ccc-000000000000".format(n)


def _ts(minute):
    t = datetime.datetime(2026, 10, 5, tzinfo=datetime.timezone.utc) + datetime.timedelta(minutes=minute)
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


class Claude:
    def __init__(self, root, cwd, session, day=0):
        self.root, self.cwd, self.sid, self.lines, self.t, self.n = root, cwd, session, [], day * 24 * 60, 0

    def human(self, text):
        self.t += 1
        self.lines.append({"type": "user", "sessionId": self.sid, "cwd": self.cwd, "timestamp": _ts(self.t),
                           "message": {"role": "user", "content": text}})
        return self

    def interrupt(self):
        self.t += 1
        self.lines.append({"type": "user", "sessionId": self.sid, "cwd": self.cwd, "timestamp": _ts(self.t),
                           "message": {"role": "user", "content": [{"type": "text", "text": "[Request interrupted by user]"}]}})
        return self

    def tool_result(self):
        self.t += 1
        self.lines.append({"type": "user", "sessionId": self.sid, "cwd": self.cwd, "timestamp": _ts(self.t),
                           "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}})
        return self

    def agent(self, model="claude-opus-5-5", inp=100000, out=50000, cw1=1000000, cw5=0, cr=2000000, split=1,
              msg_id=None, speed="standard"):
        """One API response, written as `split` lines that repeat the same usage (as Claude Code does)."""
        self.t += 1
        self.n += 1
        mid = msg_id or "msg_{}_{}".format(self.sid[:8], self.n)
        usage = {"input_tokens": inp, "output_tokens": out, "cache_creation_input_tokens": cw1 + cw5,
                 "cache_read_input_tokens": cr, "speed": speed,
                 "cache_creation": {"ephemeral_1h_input_tokens": cw1, "ephemeral_5m_input_tokens": cw5}}
        for k in range(split):
            self.lines.append({"type": "assistant", "sessionId": self.sid, "cwd": self.cwd, "timestamp": _ts(self.t),
                               "requestId": "req_" + mid, "message": {"id": mid, "model": model, "role": "assistant",
                                                                      "content": [{"type": "text", "text": "part %d" % k}],
                                                                      "usage": usage}})
        return self

    def folder(self):
        return os.path.join(self.root, "projects", self.cwd.replace("/", "-"))

    def write(self, extra_lines=()):
        os.makedirs(self.folder(), exist_ok=True)
        p = os.path.join(self.folder(), self.sid + ".jsonl")
        with open(p, "w") as f:
            for o in list(extra_lines) + self.lines:
                f.write(json.dumps(o) + "\n")
        os.utime(p, (OLD, OLD))
        return p

    def write_subagent(self, agent_name, lines):
        d = os.path.join(self.folder(), self.sid, "subagents")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "agent-" + agent_name + ".jsonl")
        with open(p, "w") as f:
            for o in lines:
                f.write(json.dumps(o) + "\n")
        os.utime(p, (OLD, OLD))
        return p


def codex_file(root, thread, lines, day=0):
    d = os.path.join(root, "sessions", "2026", "10", "{:02d}".format(5 + day))  # tests use day < 27
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "rollout-2026-10-{:02d}T10-00-00-{}.jsonl".format(5 + day, thread))
    with open(p, "w") as f:
        for o in lines:
            f.write(json.dumps(o) + "\n")
    os.utime(p, (OLD, OLD))
    return p


def cx(kind, payload, minute=0, day=0):
    return {"timestamp": _ts(day * 24 * 60 + minute), "type": kind, "payload": payload}


def tok(inp, cached, out, total=None):
    return {"type": "token_count", "info": {"total_token_usage": {
        "input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out,
        "reasoning_output_tokens": 0, "total_tokens": total if total is not None else inp + out}}}


def build(base):
    """Write the standard synthetic history. Returns dict of names -> session ids."""
    claude_home = os.path.join(base, "claude")
    codex_home = os.path.join(base, "codex")
    S = {}
    # 1 resolved; first response is split over 3 lines with the same usage (counted once)
    s = Claude(claude_home, "/home/dev/my-app", sid(1), day=0)
    s.human("rename foo to bar everywhere").agent(split=3).human("thanks, now run the tests").agent()
    s.write()
    S["resolved"] = s
    # 2 corrected twice
    s = Claude(claude_home, "/home/dev/my-app", sid(2), day=1)
    s.human("add a retry to the upload").agent().human("no, only the client, not the server").agent()
    s.human("that's wrong, revert the server change").agent().human("ok, now update the docs").agent()
    s.write()
    S["corrected"] = s
    # 3 gave up
    s = Claude(claude_home, "/home/dev/billing-api", sid(3), day=2)
    s.human("fix the flaky login test").agent(out=400000).human("never mind, I'll do it myself")
    s.write()
    S["gave_up"] = s
    # 4 repeated yourself
    s = Claude(claude_home, "/home/dev/billing-api", sid(4), day=8)
    s.human("use tabs not spaces in this file please").agent().human("update the readme").agent()
    s.human("use tabs not spaces in this file").agent().human("looks good, add a changelog entry").agent()
    s.write()
    S["repeated"] = s
    # 5 too short to judge
    s = Claude(claude_home, "/home/dev/my-app", sid(5), day=9)
    s.human("what does this regex do").agent()
    s.write()
    S["short"] = s
    # 6 resumed from 1: Claude Code copies session 1's lines into the new file, then continues
    s = Claude(claude_home, "/home/dev/my-app", sid(6), day=10)
    s.human("continue the rename in the tests folder").agent().human("great, commit it").agent()
    s.write(extra_lines=S["resolved"].lines)
    S["resumed"] = s
    # 7 with a sub-agent; the sub-agent's tokens belong to session 7
    s = Claude(claude_home, "/home/dev/docs-site", sid(7), day=15)
    s.human("research the docs build").agent().human("good, write it up").agent()
    s.write()
    sub = Claude(claude_home, "/home/dev/docs-site", sid(7), day=15).human("sub task").agent(model="claude-haiku-4-5", inp=10000, out=1000, cw1=0, cr=0, msg_id="msg_sub_a1")
    for o in sub.lines:
        o["isSidechain"] = True
    s.write_subagent("a1", sub.lines)
    S["subagent"] = s
    # 8 unknown model (tokens counted, no price)
    s = Claude(claude_home, "/home/dev/docs-site", sid(8), day=16)
    s.human("summarise the changelog").agent(model="claude-future-9").human("thanks, ship it").agent(model="claude-future-9")
    s.write()
    S["unknown"] = s
    # 9 Esc alone never counts; the messages after it do: two corrections, the last one unanswered = gave up
    s = Claude(claude_home, "/home/dev/my-app", sid(9), day=17)
    s.human("build it").agent().interrupt().human("no, use make").agent().interrupt()
    s.human("that's wrong, use the Makefile target")
    s.write()
    S["interrupts"] = s

    # Codex: c1 legacy format, resolved; one "fill to context window" line must not add tokens
    c1 = "c0000001-aaaa-4bbb-8ccc-000000000000"
    codex_file(codex_home, c1, [
        cx("session_meta", {"id": c1, "cwd": "/home/dev/my-app"}, 0, 3),
        cx("turn_context", {"model": "gpt-5.3-codex", "cwd": "/home/dev/my-app"}, 1, 3),
        cx("response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>x</environment_context>"}]}, 1, 3),
        cx("event_msg", {"type": "user_message", "message": "add a health check endpoint"}, 2, 3),
        cx("event_msg", {"type": "agent_message", "message": "done"}, 3, 3),
        cx("event_msg", tok(1000000, 400000, 100000), 4, 3),
        cx("event_msg", tok(0, 0, 0, total=272000), 5, 3),
        cx("event_msg", {"type": "user_message", "message": "thanks, add a test for it"}, 6, 3),
        cx("event_msg", {"type": "agent_message", "message": "done"}, 7, 3),
        cx("event_msg", tok(3000000, 1000000, 300000), 8, 3),
    ], day=3)
    # c2 paginated format: a rollback + "that's wrong" = corrected x2; that correction is never answered = gave up
    c2 = "c0000002-aaaa-4bbb-8ccc-000000000000"
    codex_file(codex_home, c2, [
        cx("session_meta", {"id": c2, "cwd": "/home/dev/billing-api", "history_mode": "paginated"}, 0, 4),
        cx("turn_context", {"model": "gpt-5.4", "cwd": "/home/dev/billing-api"}, 1, 4),
        cx("event_msg", {"type": "item_completed", "item": {"type": "UserMessage", "content": [{"type": "text", "text": "migrate the invoices table"}]}}, 2, 4),
        cx("event_msg", {"type": "item_completed", "item": {"type": "AgentMessage", "content": [{"type": "text", "text": "working"}]}}, 3, 4),
        cx("event_msg", tok(2000000, 0, 200000), 4, 4),
        cx("event_msg", {"type": "turn_aborted", "reason": "interrupted"}, 5, 4),
        cx("event_msg", {"type": "item_completed", "item": {"type": "UserMessage", "content": [{"type": "text", "text": "keep the old column"}]}}, 6, 4),
        cx("event_msg", {"type": "thread_rolled_back"}, 7, 4),
        cx("event_msg", {"type": "item_completed", "item": {"type": "AgentMessage", "content": [{"type": "text", "text": "dropping the column"}]}}, 7, 4),
        cx("event_msg", {"type": "item_completed", "item": {"type": "UserMessage", "content": [{"type": "text", "text": "that's wrong, keep the old column"}]}}, 8, 4),
        cx("event_msg", tok(2500000, 0, 250000), 9, 4),
        cx("event_msg", {"type": "turn_aborted", "reason": "interrupted"}, 10, 4),
    ], day=4)
    # c3 a Copied fork of c1: the copy (re-stamped, so timestamps differ) ends at thread_settings_applied
    c3 = "c0000003-aaaa-4bbb-8ccc-000000000000"
    codex_file(codex_home, c3, [
        cx("session_meta", {"id": c3, "cwd": "/home/dev/my-app", "forked_from_id": c1}, 0, 5),
        cx("turn_context", {"model": "gpt-5.3-codex", "cwd": "/home/dev/my-app"}, 0, 5),
        cx("event_msg", {"type": "user_message", "message": "add a health check endpoint"}, 0, 5),
        cx("event_msg", tok(1000000, 400000, 100000), 0, 5),
        cx("event_msg", {"type": "user_message", "message": "thanks, add a test for it"}, 0, 5),
        cx("event_msg", tok(3000000, 1000000, 300000), 0, 5),
        cx("event_msg", {"type": "thread_settings_applied"}, 0, 5),
        cx("event_msg", {"type": "user_message", "message": "now try it with a timeout of 5 seconds"}, 2, 5),
        cx("event_msg", {"type": "agent_message", "message": "done"}, 3, 5),
        cx("event_msg", tok(3400000, 1200000, 340000), 4, 5),
        cx("event_msg", {"type": "user_message", "message": "great"}, 5, 5),
    ], day=5)
    S["codex"] = {"legacy": c1, "paged": c2, "fork": c3}
    return claude_home, codex_home, S
