"""Run gust against a real Gradle: the scenarios next to this file and the README quick start.

Needs gradle and a JDK on PATH, and the network. Everything happens in a new working dir whose path has a
space in it. Exits nonzero at the first unexpected exit code.
"""

import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
GUST = HERE.parent.parent / "gust.py"
sys.path.insert(0, str(GUST.parent))
import gust  # noqa: E402

WORK = Path(tempfile.mkdtemp(prefix="gust real ")).resolve()


def gust_cli(expected, *args):
    """Run gust in WORK and check its exit code against expected (a code or a tuple of codes). Returns it."""
    expected = expected if isinstance(expected, tuple) else (expected,)
    started = time.monotonic()
    proc = subprocess.run([sys.executable, str(GUST), *args], cwd=WORK, capture_output=True, text=True)
    print(f"::group::gust {' '.join(args)}: exit {proc.returncode} in {time.monotonic() - started:.1f}s")
    print(proc.stdout + proc.stderr)
    print("::endgroup::")
    if proc.returncode not in expected:
        sys.exit(f"gust {' '.join(args)}: exit {proc.returncode}, expected {' or '.join(map(str, expected))}\n{proc.stderr}")
    return proc


def main():
    print(f"working dir: {WORK}")
    (WORK / "loop.toml").write_text(gust.EXAMPLE, encoding="utf-8")        # the README quick start
    for scenario in HERE.glob("*.toml"):
        (WORK / scenario.name).write_text(scenario.read_text(encoding="utf-8"), encoding="utf-8")
    gust_cli(0, "run", "loop.toml")                                        # Gradle from PATH
    if "greetsByName() FAILED" not in (WORK / "loop.out" / "step-01.log").read_text(encoding="utf-8", errors="replace"):
        sys.exit("loop.toml: the first step must fail on the test, not on the build")
    gust_cli(0, "run", "wrapper.toml", "--gradle", "9.7.1")                # the wrapper install and gradlew.bat
    gust_cli(0, "run", "shell.toml")
    # a daemon left running, then a setup over its project
    gust_cli(0, "setup", "cycle.toml")
    gust_cli(0, "run", "cycle.toml", "--keep-daemons")
    again = gust_cli((0, 2), "setup", "cycle.toml")
    if again.returncode == 2:
        if "gust stop-daemons" not in again.stderr or not (WORK / "cycle.out" / gust.MARKER).is_file():
            sys.exit("a failed setup must name gust stop-daemons and keep the marker")
        print("setup over the daemon's project failed as expected; stopping the daemon")
        gust_cli(0, "stop-daemons", "cycle.toml")
        gust_cli(0, "setup", "cycle.toml")
    else:
        print("setup over the daemon's project went through")
    gust_cli(0, "stop-daemons", "cycle.toml")                              # the runner ends with no daemon


if __name__ == "__main__":
    main()
