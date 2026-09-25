"""Release gust from main: python3 .github/release.py

Releasing, for maintainers: on main at version "<N>-dev", run this script. Once
v<N> is pushed, the Release workflow (.github/workflows/release.yml) runs and a
draft release for v<N> appears on GitHub, with gust.py attached. Write the notes
on that draft and publish it.

The steps, each printed as it begins:
  - checks: on main, a clean tree, main in sync with origin/main, gust.py at
    "<N>-dev", and no tag v<N> here or on origin
  - version "<N>", the unit tests, commit "Release v<N>", signed tag v<N>
  - main and v<N> pushed together, so both reach origin or neither does
  - version "<N+1>-dev", commit "Start v<N+1>-dev", main pushed
At the first failure the run stops, with a note on the state of the checkout
and how to go on. The GitHub release is not created here; that is the job of
the Release workflow.

Standard library only. Needs git on PATH, with signing set up for commits and
tags, and push access to origin.
"""

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUST = ROOT / "gust.py"
VERSION_LINE = re.compile(r'^__version__ = "([^"\r\n]*)"(?=\r?$)', re.MULTILINE)
DEV_VERSION = re.compile(r"([1-9][0-9]*)-dev")
RELEASES = "https://github.com/alllex/gust/releases"


class ReleaseError(Exception):
    pass


def read_version(text: str) -> str:
    """The version in the text of gust.py, from its one `__version__ = "..."` line."""
    found = VERSION_LINE.findall(text)
    if len(found) != 1:
        raise ReleaseError(f'gust.py needs exactly one line __version__ = "...", found {len(found)}')
    return found[0]


def release_versions(version: str) -> tuple[str, str]:
    """For "<N>-dev": the release version "<N>" and the next dev version "<N+1>-dev"."""
    match = DEV_VERSION.fullmatch(version)
    if not match:
        raise ReleaseError(f'gust.py is at version "{version}", not "<N>-dev"; only a dev version can be released')
    n = int(match.group(1))
    return str(n), f"{n + 1}-dev"


def with_version(text: str, version: str) -> str:
    """The text of gust.py with its version line set to version."""
    read_version(text)
    return VERSION_LINE.sub(lambda _: f'__version__ = "{version}"', text)


def git(*args: str, show: bool = False) -> str:
    """Run git in the checkout. With show, the command and its output are on the console; else its stdout is returned."""
    if show:
        print(f"$ {shlex.join(['git', *args])}", flush=True)
    proc = subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=not show)
    if proc.returncode != 0:
        detail = "" if show else f": {(proc.stderr or proc.stdout).strip()}"
        raise ReleaseError(f"git {args[0]} failed (exit {proc.returncode}){detail}")
    return (proc.stdout or "").strip()


def step(title: str) -> None:
    print(f"\n== {title}", flush=True)


def set_version(version: str) -> None:
    GUST.write_bytes(with_version(GUST.read_bytes().decode("utf-8"), version).encode("utf-8"))


def restore_gust() -> None:
    git("checkout", "HEAD", "--", "gust.py")


def release() -> None:
    step("Checking the checkout")
    branch = git("branch", "--show-current")
    if branch != "main":
        raise ReleaseError(f"on {branch or 'a detached HEAD'}, not main; nothing is changed")
    if git("status", "--porcelain"):
        raise ReleaseError("the working tree has changes; commit or stash them first. Nothing is changed")
    git("fetch", "--quiet", "origin", "main")
    head, upstream = git("rev-parse", "HEAD"), git("rev-parse", "origin/main")
    if head != upstream:
        raise ReleaseError(f"main is at {head[:7]} and origin/main at {upstream[:7]}; bring them in sync first. "
                           "Nothing is changed")
    current = read_version(GUST.read_bytes().decode("utf-8"))
    version, next_dev = release_versions(current)
    tag = f"v{version}"
    if git("tag", "--list", tag):
        raise ReleaseError(f"tag {tag} exists here already; nothing is changed")
    if git("ls-remote", "--tags", "origin", f"refs/tags/{tag}"):
        raise ReleaseError(f"tag {tag} exists on origin already; nothing is changed")
    print(f"main at {head[:7]}, gust.py at \"{current}\": releasing {tag}")

    step(f'Setting the version to "{version}"')
    set_version(version)

    step("Running the unit tests")
    tests = subprocess.run([sys.executable, "gust.tests.py"], cwd=ROOT, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    if tests.returncode != 0:
        restore_gust()
        raise ReleaseError(f"the unit tests failed; gust.py is back at \"{current}\" and nothing is committed")

    step(f"Committing and tagging {tag}")
    try:
        git("commit", "-S", "-m", f"Release {tag}", "gust.py", show=True)
    except ReleaseError as e:
        restore_gust()
        raise ReleaseError(f"{e}; gust.py is back at \"{current}\" and nothing is committed") from e
    try:
        git("tag", "-s", tag, "-m", tag, show=True)
    except ReleaseError as e:
        raise ReleaseError(f"{e}; the commit 'Release {tag}' is local only. To undo it: "
                           "git reset --hard origin/main") from e

    step(f"Pushing main and {tag} together")
    try:
        git("push", "--atomic", "origin", "main", f"refs/tags/{tag}", show=True)
    except ReleaseError as e:
        raise ReleaseError(f"{e}; nothing reached origin. To try again: git push --atomic origin main refs/tags/{tag}. "
                           f"To undo: git tag -d {tag}, then git reset --hard origin/main") from e

    step(f'Setting the version to "{next_dev}"')
    set_version(next_dev)
    try:
        git("commit", "-S", "-m", f"Start v{next_dev}", "gust.py", show=True)
    except ReleaseError as e:
        raise ReleaseError(f"{e}; {tag} is on origin, and gust.py is at \"{next_dev}\" but not committed. "
                           f"To finish: commit it as 'Start v{next_dev}' and push main") from e
    try:
        git("push", "origin", "main", show=True)
    except ReleaseError as e:
        raise ReleaseError(f"{e}; {tag} is on origin, and the commit 'Start v{next_dev}' is local only. "
                           "To finish: git push origin main") from e

    print(f"\n{tag} is on origin, and main is at \"{next_dev}\". Once the Release workflow is done, write the notes on "
          f"the draft release and publish it: {RELEASES}")


def main() -> int:
    try:
        release()
    except ReleaseError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
