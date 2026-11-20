# Groundhog

Finds the instruction you repeat most to your coding agent, counts it, and writes the line for CLAUDE.md so you never say it again.

Groundhog tells VoxMith that you ran it, with your account email and a count. It never sends your chats or what you said.

Made by [VoxMith](https://voxmith.com). Status: private repo, pre-launch.

## Install (pick ONE)

Do not install both. They define the same skill, and two skills with one name conflict.

**1. Plugin** (gives `/groundhog:groundhog`). Inside Claude Code, one line at a time:

```
/plugin marketplace add VoxMith/groundhog
/plugin install groundhog@voxmith
```

Type `/plugin`; if you see "groundhog" in the list, you are done. From a normal terminal on a recent Claude Code: `claude plugin install groundhog --marketplace VoxMith/groundhog`.

**2. Copy-paste install** (gives a bare `/groundhog`):

```
mkdir -p ~/.claude/skills && curl -fsSL https://github.com/VoxMith/groundhog/archive/refs/heads/main.tar.gz | tar -xz --strip-components=2 -C ~/.claude/skills groundhog-main/skills/groundhog
```

Then restart Claude Code and type `/groundhog`. To update, run the same command again. To remove, delete `~/.claude/skills/groundhog`.

You can read the whole skill before installing: `skills/groundhog/SKILL.md` and `skills/groundhog/scripts/`.

Until the repo is public, neither command works for people outside the VoxMith GitHub org. To try a checkout locally use `/plugin marketplace add ./groundhog` (or the path of your clone).

## Use

Type `/groundhog` (or `/groundhog:groundhog`). Optional words: `all`, `project` or `session` (skips the question), a number of days (default 90), `off`, `on`, `status`, `fresh`. Groundhog remembers how it grouped your instructions, so every session on the same computer gives the same answer (the session you are in is not counted); `fresh` forgets that and groups again.

With no scope word it asks what to check:

1. all your sessions (the default: habits show over many sessions),
2. this project only,
3. this session only.

Then it reads your history and groups repeated instructions (your own Claude session does the grouping). The main result is a web page saved on your computer that opens in your browser by itself (`open_report.py`): the big count, the instruction in your own words, what it costs you, a weekly chart, your top 10, where it happens most, and the lines for CLAUDE.md. The page says "Only on your computer. Don't share this page." In the terminal you get a short summary and a question about which lines to add. Groundhog only writes to CLAUDE.md after you say yes, and only appends. If you want to post a screenshot, the skill can make a share version (`--share`) with general wording and "Project A, B, C" instead of your words and folder names.

## Privacy

Groundhog reads your Claude Code history on your own computer. The grouping step is done by this same Claude session, which already sees your conversation. Your chats and the instructions it finds are never sent to VoxMith.

Each time you run it, Groundhog sends VoxMith exactly these things:

| Field | What it is |
|---|---|
| `v` | ping format version (1) |
| `skill_version` | the Groundhog version |
| `run_count` | how many times you have run it (a counter on your computer) |
| `top_count` | one number: how many times your top instruction came up |
| `scope` | `session`, `project` or `all` |
| `account_id` | the email of your Claude Code account, or your git email if there is none, or null |
| `account_source` | `claude`, `git` or `none` |
| `install_id` | a random ID made on first run |

We use it to count how many people use Groundhog. To have your data deleted, email manvendra.singh@voxmith.com.

The ping is built by `skills/groundhog/scripts/ping.py`, the only file with network code, from two checked values (`--top-count`, `--scope`) plus your local state. It never reads scan output, so no text can travel through it. It waits at most 3 seconds and ignores every failure. `groundhog.py`, which reads your history, has no network code and starts no other programs; a test checks that.

The first result ends with the same sentence as a footnote, once. Turn the ping off:

- `/groundhog off` (remembered), `/groundhog on` to turn it back on, `/groundhog status` to see exactly what would be sent and whether it is on;
- or set `GROUNDHOG_NO_PING=1`, or `DO_NOT_TRACK=1`. Any of them wins.

Everything stays on your computer in `~/.groundhog/`: `state.json` (ping on/off, install ID, run count, your last result), plus scratch files of the last scan (redacted candidate sentences only). Delete the folder any time.

Redaction: before anything is shown or saved, API-key shapes, tokens, assignments like `password=...`, long hex/base64 runs, emails, URLs with credentials or query strings, home paths, IPv4 addresses and card-like numbers become `[redacted]`. A line that is mostly redacted is dropped.

## Environment variables

| Variable | Effect |
|---|---|
| `GROUNDHOG_NO_PING=1` | send nothing |
| `DO_NOT_TRACK=1` | send nothing |
| `GROUNDHOG_NO_OPEN=1` | do not open the report in a browser (it is still saved; the summary says where) |
| `GROUNDHOG_PING_URL` | ping URL (default `https://api.voxmith.com/api/v1/public/groundhog/ping`). Dev rehearsal: `GROUNDHOG_PING_URL=https://dev-api.voxmith.com/api/v1/public/groundhog/ping` |
| `GROUNDHOG_HOME` | folder for Groundhog's own state (default `~/.groundhog`); used by tests |
| `GROUNDHOG_PROJECTS_ROOT` | folder holding the project transcript folders (default `<claude config dir>/projects`); used by tests and fixtures |
| `CLAUDE_CONFIG_DIR` | honoured: Claude Code's config folder (history in `projects/`, account in `.claude.json`) |

## Notes and limits

- Projects are matched by working-directory path. A session started in a subfolder or a git worktree counts as a different project.
- Claude Code keeps about 30 days of history by default, so a fresh install can only see about a month.
- English instruction cues only for the local pre-filter; non-English repeats are found on a best-effort basis.
- The history format is internal to Claude Code and can change. The reader ignores unknown fields and skips bad lines.
- Windows: needs `python3` on the PATH; if it is missing, run Claude Code under WSL. Not tested on Windows.
- A full run reads every transcript line by line. `--resume` keeps a per-file cache of redacted candidate lines so an interrupted run can continue.

## Develop

Python 3.9+, standard library only. Tests: `python3 -m pytest tests` (needs `pytest`). `tests/fixtures/` holds made-up transcripts only. `claude plugin validate .` checks the plugin and marketplace files.

Layout: `.claude-plugin/` (plugin and marketplace manifests), `skills/groundhog/SKILL.md`, `skills/groundhog/scripts/` (`groundhog.py` scan/stats/summary/render/html, `open_report.py` opens the report, `ping.py` ping and switch, `state.py` local state).

## License

MIT, see `LICENSE`.
