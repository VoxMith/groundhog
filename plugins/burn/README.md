# Burn

**What did your coding agent's failures cost you?**

Burn reads your local coding-agent history (Claude Code inside Claude Code, Codex inside Codex), finds the sessions where you corrected the agent, repeated yourself or gave up, and adds up what those sessions cost. You get three numbers:

1. **Resolution rate**: resolved sessions / judged sessions.
2. **Failed sessions**: the share of your usage that went to failed sessions, and what they cost at API prices, plus their tokens.
3. **Cost per resolved session**: total cost / resolved sessions.

The same computer gives the same answer from any folder or session: Python computes every number with fixed rules (no AI in the counting), your current session and sessions still running are left out, and tokens are counted once even when a line is repeated across files.

## Install

In Claude Code, one line at a time:

```
/plugin marketplace add VoxMith/groundhog
/plugin install burn@voxmith
```

Then type `/burn:burn`. From a normal terminal: `claude plugin install burn --marketplace VoxMith/groundhog`.

Want just `/burn`? Copy the skill into your personal skills folder, then restart Claude Code:

```
mkdir -p ~/.claude/skills && curl -fsSL https://github.com/VoxMith/groundhog/archive/refs/heads/main.tar.gz | tar -xz --strip-components=4 -C ~/.claude/skills groundhog-main/plugins/burn/skills/burn
```

### In Codex

Codex reads skills from `~/.agents/skills/`. In a terminal:

```
mkdir -p ~/.agents/skills && curl -fsSL https://github.com/VoxMith/groundhog/archive/refs/heads/main.tar.gz | tar -xz --strip-components=4 -C ~/.agents/skills groundhog-main/plugins/burn/skills/burn && mv ~/.agents/skills/burn/SKILL.codex.md ~/.agents/skills/burn/SKILL.md
```

Then start Codex and type `$burn`. In Codex, Burn reads only your Codex history; in Claude Code, only your Claude Code history. Codex's default sandbox has no network, so the usage ping usually does not reach VoxMith from there.

## Use

- `/burn` checks every Claude Code and Codex session on this computer.
- `/burn project` checks only the folder you are in.
- `/burn status` shows exactly what the usage ping sends; `/burn off` and `/burn on` switch it.

You get a short summary in the terminal and a full report (week by week, by project, by agent and model, why sessions failed, the most expensive failed sessions, and a share card) that opens in your browser. The report stays on your computer.

## What counts as failed (rules version 2)

A session is judged when you wrote at least 2 messages in it. It failed if any of these is true:

- **Corrected**: 2 or more corrections. A correction is a message right after the agent answered that starts with "no,", "wrong", "stop", "undo", "revert", "that's wrong", or contains "not what I asked", "you broke", "why did you". "No, that looks good" is not a correction. Pressing Esc alone does not count (you may be steering); the message you type after it decides. A Codex rollback counts.
- **Repeated yourself**: the same instruction (a do/don't rule, or 5+ words) in 2 or more of your messages. Short routine commands like "run the tests" don't count.
- **Gave up**: your last message gives up ("forget it", "never mind", "I'll do it myself", "I'll handle this manually"; not "never mind, found it, thanks"), or is a correction the agent never answered.

Sessions with fewer than 2 of your messages are not judged, but their cost is in the totals.

The three numbers: **resolution rate** = resolved / judged sessions; **% of your usage** that went to failed sessions = their cost / the cost of all sessions; **cost per resolved session** = total cost / resolved sessions.

## Money

Dollars are at **API prices** from `skills/burn/prices.json` (taken from the official Anthropic and OpenAI pricing pages, date inside the file). Why API prices: it is the one public price list everyone shares, so your numbers compare across people and plans, and it shows what the work was worth. If you are on a subscription (Claude Pro/Max, ChatGPT), you paid a flat fee instead, not this per token; that is why Burn leads with the share of your usage that went to failed sessions. A model that is not in the table is counted in tokens and shown as "price unknown", never guessed.

## Where it reads

- Claude Code: `~/.claude/projects/` (or `$CLAUDE_CONFIG_DIR/projects/`), including sub-agent transcripts, whose tokens count for their parent session. Claude Code keeps about 30 days of history by default.
- Codex: `~/.codex/sessions/` and `~/.codex/archived_sessions/` (or `$CODEX_HOME`). Compressed `.jsonl.zst` files need Python 3.14; on older Python they are skipped and the summary says how many.

## Privacy

The scanner (`burn.py`) has no network code. Burn sends your end result (resolution rate, sessions judged, failed-session cost and cost per resolved session) with your account email to VoxMith and lists it on the public Burn leaderboard: analyze.voxmith.com/burn/leaderboard. It never sends your chats, prompts, file names or project names. Your account email and end result are listed on the public leaderboard (analyze.voxmith.com/burn/leaderboard); you need 20 judged sessions to be ranked, and the rank uses a cautious score (the 95% Wilson lower bound of resolved / judged) so a few lucky sessions cannot top it. Opt out any time: `/burn off` (also takes you off the board), or set `BURN_NO_PING=1` or `DO_NOT_TRACK=1` (stops sending; an existing row stays until `/burn off`). To have your data deleted, write to hello@voxmith.com.

## Development

Python 3.9+, standard library only. Tests: `python3 -m pytest plugins/burn/tests` (needs `pytest`). The tests build made-up histories only (`tests/synth.py`).
