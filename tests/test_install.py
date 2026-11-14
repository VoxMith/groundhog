"""Spec section 2: the copy-paste tar one-liner leaves SKILL.md and scripts/ in the right place (local tarball)."""
import os
import subprocess
import tarfile

from conftest import REPO


def test_tar_one_liner_layout(tmp_path):
    tarball = tmp_path / "main.tar.gz"
    with tarfile.open(tarball, "w:gz") as t:   # what GitHub's archive of main looks like: one top folder groundhog-main/
        for item in ("skills", ".claude-plugin", "README.md", "LICENSE"):
            t.add(os.path.join(REPO, item), arcname="groundhog-main/" + item, filter=lambda i: None if "__pycache__" in i.name else i)
    home = tmp_path / "home"
    (home / ".claude" / "skills").mkdir(parents=True)
    cmd = "cat {} | tar -xz --strip-components=2 -C {} groundhog-main/skills/groundhog".format(tarball, home / ".claude" / "skills")
    subprocess.run(cmd, shell=True, check=True)
    d = home / ".claude" / "skills" / "groundhog"
    assert (d / "SKILL.md").is_file()
    assert sorted(os.listdir(d / "scripts")) == ["groundhog.py", "open_report.py", "ping.py", "state.py"]
    assert not (home / ".claude" / "skills" / "skills").exists()
