"""Local state for Burn (~/.burn/state.json). Standard library only, no network."""
import json
import os
import tempfile

BOARD_URL = os.environ.get("BURN_BOARD_URL") or "https://analyze.voxmith.com/burn/leaderboard"
DISCLOSURE = ("Burn sends your end result (resolution rate, sessions judged, failed-session cost and cost per "
              "resolved session) with your account email to VoxMith "
              "and lists it on the public Burn leaderboard: " + BOARD_URL.replace("https://", "") + ". "
              "It never sends your chats, prompts, file names or project names.")
FOOTNOTE = "Privacy: " + DISCLOSURE + " Opt out any time: /burn off"
# Bump when what is sent or shown changes: the footnote shows once more, and the leaderboard only lists
# a run after the user has seen the current footnote.
NOTICE_VERSION = 3


def home():
    return os.environ.get("BURN_HOME") or os.path.join(os.path.expanduser("~"), ".burn")


def _path(name):
    return os.path.join(home(), name)


def write_private(name, obj):
    """Atomically write JSON to BURN_HOME/name with mode 0600."""
    os.makedirs(home(), mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=home(), prefix=name + ".")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, _path(name))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(name, default):
    try:
        with open(_path(name)) as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def load():
    return read_json("state.json", {})


def save(st):
    write_private("state.json", st)
