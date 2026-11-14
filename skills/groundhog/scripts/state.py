"""Local state for Groundhog (~/.groundhog/state.json). Standard library only, no network."""
import json
import os
import tempfile

DISCLOSURE = ("Groundhog tells VoxMith that you ran it, with your account email and a count. "
              "It never sends your chats or what you said.")
FOOTNOTE = "Privacy: " + DISCLOSURE + " Turn it off any time: /groundhog off"
NOTICE_VERSION = 1  # bump when the fields sent to VoxMith change, so the footnote shows once more


def home():
    return os.environ.get("GROUNDHOG_HOME") or os.path.join(os.path.expanduser("~"), ".groundhog")


def _path(name):
    return os.path.join(home(), name)


def write_private(name, obj):
    """Atomically write JSON to GROUNDHOG_HOME/name with mode 0600."""
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
