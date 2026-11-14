"""Acceptance 4a/4b: the exact user-facing strings live in SKILL.md; the disclosure is identical everywhere."""
import os

import pytest

import state
from conftest import REPO

SKILL = open(os.path.join(REPO, "skills", "groundhog", "SKILL.md")).read()
README = open(os.path.join(REPO, "README.md")).read()

UX = [
    'What should I check?',
    '> 1. Check all my sessions         ({N} sessions on this computer)   <- recommended',
    '  2. This project only             ({n} sessions in "{project}")',
    "  3. This session only             (the one you're in now)",
    'Reply 1, 2 or 3, or just press Enter for 1.',
    "I only found this session, so I'll check that.",
    '`Reading {N} sessions on this computer...`',
    '`Reading {N} sessions in this project...`',
    '`Reading 1 session...`',
    "`You've said this {n} times`",
    "`You've said this {n} times in this project`",
    '`You said this {n} times in this session`',
    '`across {S} sessions and {P} projects since {date}`',
    '`across {S} sessions since {date}`',
    '`in this session`',
    'This usually takes a few seconds.',
    "That's a lot of history, so this can take up to a minute. I'm working through it.",
    "Want only the last 30 days? That's faster. (yes / no)",
    'That took longer than one step allows. Continuing where I stopped.',
    'Found {M} things you told me to do or not do. Grouping the ones that mean the same thing...',
    '# Groundhog',
    "## You've said this 23 times",
    '> **"Don\'t add comments to code I didn\'t ask you about"**',
    'Which ones should I add? Reply with numbers (like 1,2,5), "all" or "none".',
    '## No instruction came up 3 times or more',
    'I checked 360 sessions (3,480 of your messages). The closest was "keep answers short", 2 times.',
    "That's a good sign: you're not repeating yourself much.",
    'Want the line for "keep answers short" anyway? (yes / no)',
    '## Nothing repeated in this session',
    "I read 1 session (42 of your messages) and nothing came up twice. That's normal: one session is too short to show a habit.",
    'Want me to check all 360 sessions instead? (yes / no)',
    '## Not enough history yet',
    'I found only 1 short session (6 messages). Claude Code keeps about 30 days of history by default, so come back after a few more sessions.',
    'Saved: {path}. Open it in your browser. It stays on this computer; delete it any time.',
    'Where should I put them?',
    '  1. This project  (CLAUDE.md in {project folder name})',
    '  2. All my projects  (your personal CLAUDE.md)',
    "  3. Don't write anything, I'll copy them myself",
    "I'll add these lines at the end of {path}:",
    '# Added by Groundhog',
    'Add them? (yes / no)',
    "That file doesn't exist yet. I'll create it with just these lines. Create it? (yes / no)",
    'Done. Added {k} lines to {path}. Claude reads this at the start of every new session.',
    'Your CLAUDE.md already says: "{existing line}". You still repeated it {count} times, so it may not be strong enough.',
    '1. Replace it with the new line 2. Keep it and add the new line 3. Leave it as is.',
    'If this must never happen, a hook is stricter than a CLAUDE.md line. Ask me how.',
    "Want to share it? Here's a card. It has only the number and a general phrase, none of your own words:",
    '  I told my AI coding agent "stop adding comments" 23 times.',
    '  Groundhog counted it and wrote the fix. voxmith.com/groundhog',
    'Reply: 1 use as is (default), 2 count only, 3 use my exact words, 4 skip.',
    'Groundhog is from VoxMith, which shows teams where their customer-facing AI agents go wrong. voxmith.com/groundhog',
    'Privacy: Groundhog tells VoxMith that you ran it, with your account email and a count. It never sends your chats or what you said. Turn it off any time: /groundhog off',
    'Welcome back. Last time ({date}) your top instruction was "{last line}", {last count} times.',
    "Groundhog needs Python 3 to read your history and I couldn't find it. Install Python 3 from python.org, then run /groundhog again. Nothing was read.",
    "(Skipped {k} files I couldn't read.)",
    'Something went wrong reading your history: {short reason}. Nothing was changed. If it keeps happening, tell us at',
    'Done. Groundhog will not tell VoxMith anything from now on. The scan still works.',
    'No Claude Code history found at ~/.claude/projects (it keeps about 30 days by default)',
    'Told 23 times across 4 projects since Sep 18. About 12 minutes and 46 extra messages.',
    'Full report: opened in your browser.',
    'Lines for CLAUDE.md:',
    "## You've said this {n} times in this project",
    'Told {n} times across {S} sessions since {date}.',
    '## You said this {n} times in this session',
    'Told {n} times in this session.',
    "Only on your computer. Don't share this page.",
    'Want a version of the report that is safe to share? It uses general wording and "Project A, B, C". (yes / no)',
    'scripts/open_report.py {path}',
    'ping.py notice',
    'groundhog.py --summary saved',
    "groundhog.py --html - <<'GROUNDHOG_CLUSTERS'",
]


@pytest.mark.parametrize("s", UX)
def test_ux_string_in_skill_md(s):
    assert s in SKILL


def test_disclosure_identical_everywhere():
    d = "Groundhog tells VoxMith that you ran it, with your account email and a count. It never sends your chats or what you said."
    assert state.DISCLOSURE == d and d in SKILL and d in README


def test_banned_phrases_absent():
    for text in (SKILL, README):
        low = text.lower()
        assert "nothing leaves your machine" not in low and "nothing is sent" not in low
    assert " calls" not in SKILL.replace("tool call", "").replace("LLM call", "").lower().replace("a call", "")


def test_frontmatter_and_size():
    head = SKILL.split("---")[1]
    assert "name: groundhog" in head and "disable-model-invocation: true" in head
    assert "Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/groundhog.py *)" in head and "Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/ping.py *)" in head
    assert SKILL.startswith("---\n") and len(SKILL.splitlines()) < 500


def test_skill_md_uses_the_ping_flags_the_script_accepts():
    assert "ping.py --top-count {N} --scope {scope}" in SKILL


def test_flow_rules_present():
    for rule in ("Your reply text is the only thing the user sees", "Do not paraphrase", "Never set GROUNDHOG_NO_PING or DO_NOT_TRACK yourself",
                 "Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/open_report.py *)"):
        assert rule in SKILL
    assert SKILL.index("--html - <<") < SKILL.index("open_report.py {path}") < SKILL.index("ping.py --top-count") < SKILL.index("--summary saved")


def test_readme_describes_new_flow():
    for t in ("open_report.py", "GROUNDHOG_NO_OPEN", "--share", "Only on your computer. Don't share this page."):
        assert t in README
