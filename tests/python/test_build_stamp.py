"""scripts/build.sh stamps the commit it builds from into the binary (#113, #167).

The compiler is a stub that records its arguments, so no real build runs. The commit comes from a real git
repository in a temporary directory. Two cases use a stub git instead: a short id of twelve digits that starts
with 0 (about one commit in 2800, and no real repository hands one over on request), and a git whose
`status` fails.

The define carries a `g` in front of the id. The compiler reads a define made only of digits as a number and
prints it without its leading zeros (`000000123456` came back as `123456`); no hex id starts with `g`, so
`build_id()` in src/main.mojo strips the marker.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "build.sh"
FAKE_MOJO = "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" > \"$FAKE_ARGS\"\n"
COMMIT = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]


def clean_env(tmp: Path) -> dict[str, str]:
    """The caller's environment without any variable git reads from it. A git hook exports GIT_DIR and
    GIT_INDEX_FILE, which would point the scratch repository's commands at the real one."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(HOME=str(tmp), GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    return env


def git(tmp: Path, cwd: Path, *args: str) -> str:
    p = subprocess.run(["git", *COMMIT, *args], cwd=cwd, env=clean_env(tmp), capture_output=True, text=True,
                       check=True, timeout=60)
    return p.stdout.strip()


def make_tree(base: Path, tmp: Path, *, init: bool) -> Path:
    """A tree with scripts/build.sh, src/main.mojo and a README; a git repository with one commit when `init`."""
    tree = base / "repo"
    (tree / "scripts").mkdir(parents=True)
    (tree / "src").mkdir()
    shutil.copy(SCRIPT, tree / "scripts" / "build.sh")
    (tree / "src" / "main.mojo").write_text("def main():\n    pass\n")
    (tree / "README.md").write_text("readme\n")
    if init:
        git(tmp, tree, "init", "-q")
        git(tmp, tree, "add", "-A")
        git(tmp, tree, "commit", "-q", "-m", "first")
    return tree


def stamp(tree: Path, tmp: Path, git_tool: str | None) -> str:
    """The value build.sh hands the compiler as MOJO_DLLM_GIT_SHA. `git_tool` is "real", None (no git on the
    PATH) or the body of a stub git script."""
    bin_dir = tmp / "bin"
    bin_dir.mkdir(exist_ok=True)
    for tool in ("bash", "dirname", "mkdir"):
        (bin_dir / tool).symlink_to(shutil.which(tool) or tool)
    if git_tool == "real":
        (bin_dir / "git").symlink_to(shutil.which("git") or "git")
    elif git_tool is not None:
        (bin_dir / "git").write_text("#!/usr/bin/env bash\n" + git_tool)
        (bin_dir / "git").chmod(0o755)
    mojo = tmp / "fake-mojo"
    mojo.write_text(FAKE_MOJO)
    mojo.chmod(0o755)
    args = tmp / "args"
    env = {"PATH": str(bin_dir), "MOJO": str(mojo), "FAKE_ARGS": str(args), "MOJO_DLLM_GPU_ARCH": "sm_80"}
    p = subprocess.run(["bash", str(tree / "scripts" / "build.sh"), "src/main.mojo", str(tmp / "out" / "bin")],
                       env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    m = re.search(r"-D MOJO_DLLM_GIT_SHA=(\S+)", args.read_text())
    assert m, args.read_text()
    return m.group(1)


def test_a_clean_tree_is_stamped_with_the_short_head(tmp_path: Path) -> None:
    tree = make_tree(tmp_path, tmp_path, init=True)
    head = git(tmp_path, tree, "rev-parse", "--short=12", "HEAD")
    assert len(head) == 12
    assert stamp(tree, tmp_path, "real") == "g" + head


def test_a_tracked_edit_under_src_adds_dirty(tmp_path: Path) -> None:
    tree = make_tree(tmp_path, tmp_path, init=True)
    head = git(tmp_path, tree, "rev-parse", "--short=12", "HEAD")
    (tree / "src" / "main.mojo").write_text("def main():\n    print(1)\n")
    assert stamp(tree, tmp_path, "real") == "g" + head + "-dirty"


def test_a_new_file_under_src_adds_dirty(tmp_path: Path) -> None:
    tree = make_tree(tmp_path, tmp_path, init=True)
    head = git(tmp_path, tree, "rev-parse", "--short=12", "HEAD")
    (tree / "src" / "extra.mojo").write_text("def extra():\n    pass\n")
    assert stamp(tree, tmp_path, "real") == "g" + head + "-dirty"


def test_an_edit_outside_src_leaves_the_stamp_clean(tmp_path: Path) -> None:
    tree = make_tree(tmp_path, tmp_path, init=True)
    head = git(tmp_path, tree, "rev-parse", "--short=12", "HEAD")
    (tree / "README.md").write_text("changed\n")
    assert stamp(tree, tmp_path, "real") == "g" + head


def test_a_tree_without_git_metadata_is_unknown_even_inside_another_repository(tmp_path: Path) -> None:
    """A source archive unpacked inside someone else's checkout must not take that checkout's commit."""
    outer = tmp_path / "outer"
    outer.mkdir()
    (outer / "file").write_text("x\n")
    git(tmp_path, outer, "init", "-q")
    git(tmp_path, outer, "add", "-A")
    git(tmp_path, outer, "commit", "-q", "-m", "outer")
    tree = make_tree(outer, tmp_path, init=False)
    assert stamp(tree, tmp_path, "real") == "gunknown"


def test_no_git_on_the_path_is_unknown(tmp_path: Path) -> None:
    tree = make_tree(tmp_path, tmp_path, init=True)
    assert stamp(tree, tmp_path, None) == "gunknown"


def test_a_git_that_cannot_report_status_is_unknown(tmp_path: Path) -> None:
    tree = make_tree(tmp_path, tmp_path, init=True)
    broken = 'case "$1" in rev-parse) echo 0123456789ab ;; *) exit 1 ;; esac\n'
    assert stamp(tree, tmp_path, broken) == "gunknown"


def test_a_short_id_of_digits_with_leading_zeros_keeps_them(tmp_path: Path) -> None:
    tree = make_tree(tmp_path, tmp_path, init=False)
    (tree / ".git").mkdir()
    zeros = 'case "$1" in rev-parse) echo 000000123456 ;; status) ;; esac\n'
    assert stamp(tree, tmp_path, zeros) == "g000000123456"


def test_the_end_to_end_check_refuses_without_git(tmp_path: Path) -> None:
    """With no git both sides of its comparison say `unknown`; it must stop before comparing them."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("bash", "dirname"):
        (bin_dir / tool).symlink_to(shutil.which(tool) or tool)
    p = subprocess.run(["bash", str(ROOT / "scripts" / "check_build_id.sh")], env={"PATH": str(bin_dir)},
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 1
    assert "git is not on PATH" in p.stderr
