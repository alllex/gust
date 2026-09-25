#!/usr/bin/env python3
"""Gust, the Gradle User Scenario Tool: set up and run Gradle user scenarios.

A scenario is one TOML file: the files of a Gradle project and an ordered list
of steps. The files are laid out in an out dir and the steps run there in
order, until the first step whose outcome differs from what the scenario
expects. Gradle output goes to log files.

Standard library only. Python 3.11+ (tomllib).
"""

from __future__ import annotations

import sys

if sys.version_info < (3, 11):  # ahead of any import that needs 3.11; keep this block old-syntax only
    sys.stderr.write(
        "gust.py needs Python 3.11 or newer (for tomllib); this is Python %s at %s\n"
        % (sys.version.split()[0], sys.executable)
    )
    sys.exit(2)

import argparse
import functools
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

__version__ = "11"

WINDOWS = os.name == "nt"

EXIT_OK = 0          # every step as expected
EXIT_DEVIATED = 1    # a run step deviated; the run stopped there
EXIT_ERROR = 2       # bad input, or a step that could not be carried out

# --- the out dir and the .gust cache
MARKER = ".gust-run"            # marks an out dir as made by gust, so it may be removed and recreated
TMP_ROOT = Path(tempfile.gettempdir() if WINDOWS else "/tmp")
TMP_SHAPE = TMP_ROOT / "gust-<yymmdd>" / "<stem>.<HHMMSS>.out"
INSTALL_LOG = "install-wrapper.log"
WRAPPER_LABEL = "[WRP]"
GUST_DIR_ENV = "GUST_DIR"        # where .gust goes (default: <working dir>/.gust)
SHARED_HOME = "shared-gradle-user-home"   # in .gust: the default Gradle user home
REMOTES_DIR = "remotes"                   # in .gust: one checkout per remote

# --- Gradle
GRADLE_ENV = "GRADLE"            # for shell steps: the Gradle binary
GRADLE_USER_HOME_ENV = "GRADLE_USER_HOME"
GRADLE_ARGS_ENV = "GRADLE_ARGS"  # for shell steps: the arguments after --, shell-quoted
TRACE_PROPERTY = "org.gradle.internal.operations.trace"   # Gradle's internal build operation trace, for --trace
WRAPPER = "./gradlew.bat" if WINDOWS else "./gradlew"   # the project's own wrapper, relative to the project dir
WRAPPER_FILE = WRAPPER[2:]
RUNNABLE_SUFFIXES = (".exe", ".bat", ".cmd", ".com")      # on Windows, the extensions of a file that can be run

# --- the scenario file's vocabulary
TOP_KEYS = {"name", "description", "setup", "steps"}
DEFAULT_NAME = "scenario"        # for a nameless scenario on stdin
SETUP_KEYS = {"gradle", "layout"}
LAYOUT_KEYS = {"base", "remote", "project"}
STEP_KEYS = {"name", "run", "write", "edit"}
RUN_KINDS = ("gradle", "shell")
RUN_KEYS = {"gradle": {"args", "expect"}, "shell": {"command", "expect"}}
EDIT_KEYS = {"file", "replace", "with"}
EXPECT_KEYS = {"exit", "output", "no_output"}
EXPECT_VALUES = ("pass", "fail")

NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
VERSION_RE = re.compile(r"\d+\.\d+(\.\d+)?([-+][A-Za-z0-9.+-]+)?")   # 9.7.1, 9.8.0-rc-2, 9.9.0-20260916091910+0000
SHA_RE = re.compile(r"[0-9a-f]{7,40}")

EXAMPLE = """\
description = "Watch a test fail, fix the assertion, watch it pass."

[[steps]]
name = "the test fails on a wrong expectation"
run.gradle = { args = "test", expect = "fail" }

[[steps]]
name = "fix the expectation"
[[steps.edit]]
file = "src/test/java/demo/GreeterTest.java"
replace = "Hello World!"
with = "Hello, World!"

[[steps]]
name = "the test passes"
run.gradle = "test"

[setup.layout.project]
"settings.gradle.kts" = 'rootProject.name = "testing-loop"'
"build.gradle.kts" = '''
plugins { java }
repositories { mavenCentral() }
testing.suites.named<JvmTestSuite>("test") { useJUnitJupiter("5.11.4") }
'''
"src/test/java/demo/GreeterTest.java" = '''
package demo;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.assertEquals;
class GreeterTest {
    @Test void greetsByName() { assertEquals("Hello World!", "Hello, World!"); }
}
'''
"""

FORMAT_SPEC = """\
Scenario file format (TOML)
===========================

A scenario is one TOML file. Unknown keys are rejected, with the key path in
the message. Paths are relative, may not contain "..", and are written as
quoted keys ("src/Main.java" = ...). Write file contents as literal strings
(''' ... ''') so $ and backslashes need no escaping.

top level
  name          string   optional   slug: [A-Za-z0-9][A-Za-z0-9._-]*. Default: the scenario file's
                                    stem (testing-loop.toml -> testing-loop), or "scenario" on stdin
  description   string   optional   one line; recorded in the summary and shown by check under
                                    scenario:, but not by a run
  setup         table    optional   what exists before the first step; see setup
  steps         array    required   at least one step; see steps[]

setup                               what `gust setup` produces, and the first part of `run`
  gradle        string   optional   the Gradle the steps run with, unless --gradle is given:
                                    "wrapper" for the project's own ./gradlew, which must be in the
                                    remote checkout (the project table cannot hold its jar); or a
                                    version such as "9.7.1", whose wrapper is installed into the
                                    project first (see Gradle version; the layout needs a settings
                                    file). Default: the project's own ./gradlew when the checkout
                                    (or, once set up, the project) has an executable one, else
                                    gradle from PATH.
  layout        table    optional   the project's files; see setup.layout

setup.layout                        base or remote (not both), plus the project table
  base          string   optional   path to a layout file (a template project), relative to the
                                    scenario file or absolute. Its files come first; the project
                                    table's files replace any at the same path
  remote        string   optional   a git commit whose tree (or a subdirectory of it) is the
                                    project, layered like base. Forms:
                                      https://github.com/<owner>/<repo>/tree/<sha>[/<subdir>]
                                      https://github.com/<owner>/<repo>/commit/<sha>
                                      <any git url>#<sha>[/<subdir>]
                                    <sha> is a commit hash (7 to 40 hex digits). Branches and tags
                                    are not accepted, so a cached checkout is never stale. Each
                                    remote is fetched once into .gust/remotes/<host>-<path>-<sha>
                                    and reused while its HEAD is that commit. .git is not copied.
  project       table    optional   the project's files; see setup.layout.project

setup.layout.project                the project table (becomes <out>/project)
  <path> = <content>                one key per file, the content written as-is. No path may be
                                    both a file and the parent directory of another

A layout file (the target of base) has a single [project] table with the same
rules, and no base of its own.

steps[]                             each step has exactly one kind: run, write, or edit
  name          string   optional   label; implied when absent: "Run: gradle test", "Edit: Foo.java"
  run           table    kind       run.gradle or run.shell; see below
  write         table    kind       <path> = <content>, as in the project table; creates or
                                    replaces files
  edit          array    kind       edits applied in order; see steps[].edit[]

steps[].run.gradle                  Gradle with these arguments, run in the project dir
  shorthand     run.gradle = "test"   means { args = "test", expect = "pass" }
  args          string   required   the arguments
  expect        string|table optional  see steps[].run.*.expect; default "pass"

steps[].run.shell                   a command run through sh in the project dir, with $GRADLE set
                                    to the Gradle binary and $GRADLE_ARGS to the arguments after --;
                                    on Windows through the bash of Git for Windows (see Windows)
  shorthand     run.shell = "cmd"     means { command = "cmd", expect = "pass" }
  command       string   required   the command
  expect        string|table optional  see steps[].run.*.expect; default "pass"

steps[].run.*.expect                what must hold after the step; every listed check counts
  shorthand     expect = "fail"       means { exit = "fail" }
  exit          string   optional   "pass" (default, exit code 0) or "fail" (any other exit code)
  output        array    optional   strings that must each appear in the step's combined output,
                                    where a CRLF line end is read as LF
  no_output     array    optional   strings that must not appear in it

steps[].edit[]
  file          string   required   a file from the project table, the remote checkout, or an
                                    earlier write step
  replace       string   required   non-empty; must occur exactly once in the file when the step runs
  with          string   required   the replacement text

The run stops at the first run step that fails a check (a deviation), or at a
write or edit that cannot be applied (an error).

Example
-------

""" + EXAMPLE + """
Out dir
=======

Everything a run touches is in one directory, the out dir. By default it is in
the working directory, named after the scenario file's stem (testing-loop.toml
-> ./testing-loop.out/), or after the scenario's name for stdin:

  <out>/project/         the laid-out project, where run steps execute
  <out>/install-wrapper.log
                         output of the wrapper install, when a Gradle version is in play
  <out>/step-NN.log      output of run step NN
  <out>/step-NN-ops-log.txt
                         with --trace: the build operation trace of run.gradle step NN
  <out>/stop-daemons-before.log, <out>/stop-daemons-after.log
                         output of the daemon stops around the steps
  <out>/summary.json     the summary of the run
  <out>/.gust-run        marker: the directory was made by gust and may be replaced or
                         removed on a later run

An out dir from an earlier run (one with the marker) is removed and recreated
without notice, so nothing of the earlier run is kept; use --tmp for a fresh
dir per run. When a file of the earlier run cannot be removed (on Windows,
one that a Gradle daemon still has open), the run is refused with a hint to
stop the daemons, and the marker stays, so a later run can go through. An empty directory is reused. Any other non-empty directory is
left alone and the run is refused, so nothing gust did not create is ever wiped
through --out.

With --tmp (run and setup) the out dir is

  /tmp/gust-<yymmdd>/<stem>.<HHMMSS>.out

in local time at invocation, with <stem> as above; the same rules apply to it.
To stop the daemons of such a run, pass --out <the printed path>.
--tmp and --out cannot be combined.

The .gust directory
===================

.gust is created in the working directory (or at $GUST_DIR):

  .gust/remotes/<identity>/      one git checkout per remote
  .gust/shared-gradle-user-home/ the Gradle user home for every Gradle invocation: wrapper
                                  distributions, dependency caches, daemons. Shared by all
                                  scenarios run from this working directory, apart from ~/.gradle.

It is a cache. Deleting it costs refetching the remotes and downloading
distributions and dependencies again.

Gradle user home
================

Every Gradle invocation from gust (run.gradle steps, both daemon stops, the
wrapper install, the version probe) starts with

  --gradle-user-home <.gust/shared-gradle-user-home>

so a --gradle-user-home later in a step's arguments takes precedence. The pair
is left out wherever a command is shown (the `$ ...` lines, the summary's
command fields), since the home: header line names the directory. Shell steps
get $GRADLE_USER_HOME set to the same directory, and a --gradle-user-home in
their command takes precedence too. --gradle-user-home on the command line
replaces the default for the whole run. ~/.gradle is left untouched unless a
scenario names it.

Commands
========

  SCENARIO is the path to a scenario .toml, or - to read one from stdin (named by its own
  `name`, else "scenario"; a relative setup.layout.base resolves against the working directory).

  help [COMMAND]                  print usage, or a command's usage
  version                         print gust's version, the Python it runs on, and the script's path
  spec                            print this text
  check SCENARIO [--gradle BIN|VERSION] [--gradle-user-home DIR] [--json]
                                  validate the scenario and settle the Gradle binary as run would,
                                  with nothing set up, installed, or fetched (an uncached remote is
                                  noted). The only thing written is what the --version probe leaves
                                  in the Gradle user home. Output: the header with the description
                                  under scenario:, `steps: N (K gradle, L shell, M write, P edit)`,
                                  and result: ok, or the error. Worth running before a long run.
                                  The --json summary is {scenario, description, gradle,
                                  gradle_version, steps (a count), step_counts, remote {link,
                                  cached} when there is one, status}.
  setup SCENARIO [--out DIR | --tmp] [--gradle BIN|VERSION] [--gradle-user-home DIR] [--show-output]
                                  lay the project out and, with a version in play, install its
                                  wrapper; no step runs. Ends in `result: set up` (or
                                  `result: error` after a failed install) and the out: line.
  run SCENARIO [--out DIR | --tmp] [--gradle BIN|VERSION] [--gradle-user-home DIR] [--json] [--tail N]
               [--show-output] [--keep-daemons] [--trace] [-- GRADLE_ARGS ...]
                                  setup, then stop daemons, run the steps, stop daemons
  stop-daemons SCENARIO [--out DIR] [--gradle BIN] [--gradle-user-home DIR]
                                  run '<gradle> --stop' in <out>/project
  flat SCENARIO                   print the scenario as TOML with setup.layout.base inlined: base is
                                  dropped and the layout file's files are merged into
                                  [setup.layout.project] as for setup, so no layout file is needed.
                                  A remote stays a link. The comment and blank lines at the top of
                                  the file are kept; other comments are lost. Nothing but the TOML
                                  goes to stdout.

--out DIR names the out dir; with --tmp it is under /tmp (see Out dir).

--gradle takes a binary or a version, read in this order:
  1. A value with a "/" is a path to a binary. ./gradlew (or any ./path) is
     relative to the project dir and must be in the project table or the
     remote checkout. Anything else is a filesystem path, such as
     <distribution>/bin/gradle, and must be an executable file (a leading ~ is
     expanded).
  2. A value without a "/" that names an executable on PATH or in the working
     directory (gradle, grl) is a binary.
  3. Anything else must be a Gradle version (9.7.1, 9.8.0-rc-2). Its wrapper
     is installed first, with gradle from PATH (see Gradle version), which
     needs a settings file in the layout. Nothing is installed by
     stop-daemons, so there --gradle takes a binary only (BIN).
A value that fits none of these is refused before the out dir is touched, with
the value and what was tried in the message.

Without --gradle, the order is: setup.gradle (wrapper or version), then the
project's own ./gradlew when the checkout or the set-up project has an
executable one, then gradle from PATH. Nothing is persisted; the choice is
made again on every command. Shell steps get the binary as $GRADLE.

The gradle: header line is `<binary> <version> (<note>)`. For a binary,
./gradlew included, the version comes from `<binary> --version`, run up front.
That doubles as a check that the binary runs and is Gradle; if not, the exit
code is 2, before the out dir is touched. For a wrapper, the probe can include
the distribution download, which the first step needs anyway. A version still
to be installed is shown as given; once installed, its wrapper is probed like
any binary (stop-daemons). The note is the source, (default),
(scenario), or (--gradle), plus ", installed by gradle" when the wrapper is
installed first. In check, "; not checked, remote not cached" is added when the
remote is not fetched yet. The summary has the version as gradle_version. When
nothing runs with Gradle (a shell-only scenario without daemon stops), the
binary is neither checked nor probed and the line has no version.

Console
=======

Once the preconditions pass, a header is printed: scenario:, out:, home:,
gradle:. With a remote, the remote: and checkout: lines come first, while the
checkout is fetched or found in the cache. Then come blocks, each a label line
and an outcome line:

  [WRP] Install Gradle wrapper 9.7.1     a `$ <command>` line, the wrapper task's output with
                                         --show-output, then `ok: exit 0 in 3.1s -> install-wrapper.log`.
                                         On failure: `ERROR: exit N ...`, the log's tail, and
                                         result: error (exit 2)
  [BEF] Stop Gradle daemons -> stop-daemons-before.log
                                         one line, then `ERROR: exit N in 0.2s -> ...` on failure
  [i/N] <step name>                      a run step: `$ <command>`, the output with --show-output, then
                                         `ok: exit 1 in 0.4s -> step-01.log` or `DEVIATION: exit 0 in
                                         0.4s -> step-01.log` and each failed check on its own line,
                                         indented two spaces (`expected fail, got pass`, `output lacks
                                         'BUILD FAILED'`, `output has 'BUILD SUCCESSFUL', which
                                         no_output forbids`), then the log's last lines (--tail N,
                                         default 30) unless the output was just shown. A write or edit
                                         step: `ok: 2 files written`, `ok: 1 file edited`, or
                                         `ERROR: <what went wrong>`.
  [AFT] Stop Gradle daemons -> stop-daemons-after.log
  result: ok (3/3 steps ran)             or deviated, or error; out of the scenario's steps
  out:      <out dir>                    the out dir again, as the last line, to have the path at hand
                                         after a long console (run, setup, stop-daemons; with
                                         --json the JSON line follows it)

Output and log tails sit between two 80-column rule lines: the opening one ends
in the log file name (or "last K of M lines of <log>"), the closing one is all
dashes. Commands are shown without the --gradle-user-home pair (or the trace
properties of --trace). The outcome prefixes ok:, DEVIATION:, and ERROR: go to
stdout; a precondition failure is a single `error: ...` line on stderr, with
nothing on stdout. For stop-daemons the console has the header and a full
[STOP] block (label, `$` line, outcome).

With --json (run, check), the summary is printed as one JSON object on
the last line of stdout; for run it is also in <out>/summary.json. Its keys
follow the TOML's: `name` and `expect` per step, `description` at the top, and
`gradle_args` for the arguments after --.
With --keep-daemons (run), both daemon stops are left out.
With --trace (run), each run.gradle step runs with Gradle's internal trace
properties right after the home pair:

  -Dorg.gradle.internal.operations.trace=<out>/step-NN-ops
  -Dorg.gradle.internal.operations.trace.tree=false

The step's build operations, one JSON object per line, end up in
<out>/step-NN-ops-log.txt (Gradle 4.0 and newer). With Gradle 8.10 and older,
step-NN-ops-tree.json and step-NN-ops-tree.txt appear next to it too. Once the
file exists, its name follows the log's on the outcome line (`-> step-01.log,
step-01-ops-log.txt`) and is the step's trace in the summary. Shell steps are
not traced; put the property in the step's command to trace one.
--tail N is how many log lines are shown after a deviation or a failed wrapper
install (default 30).
With --show-output, each run step's output appears on the console as it runs,
untouched, between the step line and its outcome line, inside the rule lines.
step-NN.log is written either way, and no tail follows a deviation. The daemon
stops are never shown; the install block is.
-- GRADLE_ARGS ... (run): everything after a literal -- is appended
to every run.gradle step's command, after the step's own arguments, so on a
conflict the command line wins over the scenario:

  <binary> --gradle-user-home <home> <step args> <GRADLE_ARGS>

For shell steps they are in $GRADLE_ARGS (shell-quoted, space-joined); write
`$GRADLE $GRADLE_ARGS ...` to pass them on. The daemon stops, the --version
probe, and the wrapper install do not get them. They are part of the shown
commands and the summary's step commands, and the summary has them as
gradle_args. Implied step names do not change.

Gradle version
==============

A local Gradle installation is assumed. When a version is in play
(setup.gradle = "9.7.1", or --gradle 9.7.1), a wrapper for it is installed into
the project right after setup and before anything else runs, with Gradle's own
wrapper task and gradle from PATH:

  gradle wrapper --gradle-version 9.7.1 --no-daemon

The steps and both daemon stops then use ./gradlew. The output is in
<out>/install-wrapper.log. The wrapper task only works inside a Gradle build,
so the layout (or the remote checkout) must hold a settings.gradle,
settings.gradle.kts, or settings.gradle.dcl; without one the run is refused
before the out dir is touched.

A distributionSha256Sum pinned in the project survives the wrapper task, so the
new version's distribution is rejected on the wrapper's first run: the daemon
stop and the first Gradle step fail, with Gradle's message in the log. To avoid
that, overwrite gradle/wrapper/gradle-wrapper.properties in
[setup.layout.project] without the pin.

With setup.gradle = "wrapper", the project's own ./gradlew runs as it is; the
--version probe is the only check.

Daemon stops: `gradle --stop` runs in the project dir before the first step,
so the scenario starts with no warm daemon, and again after the last step that
ran. Neither counts as an [i/N] step. A stop reaches every daemon of that Gradle
version and user home, not only the ones started for this project; Gradle has
no per-project daemon registry, so this is as close as it gets.

Windows
=======

On Windows the project's wrapper is gradlew.bat: read ./gradlew.bat for
./gradlew wherever it appears here, --gradle included, where .\\gradlew.bat
counts as the same. A Gradle command runs without a shell: its arguments are
split by sh's rules, quotes included, with nothing expanded, and the binary is
looked up in the project dir or on PATH (gradle is found as gradle.bat). The
arguments of a .bat file go through cmd.exe, so cmd's special characters in
them (& | < > ^ %) may not arrive as written. Shell steps run with the bash of
Git for Windows, bin\\bash.exe in the install of the git on PATH, so one
scenario file works on every system; any other bash on PATH, such as WSL's, is
not used. $GRADLE is the full path of the binary there, with forward slashes,
unless it is ./gradlew.bat. The files of a remote checkout come with the line
ends of this machine's git settings, so on Windows they may be CRLF. The --tmp
root is the temp dir in place of /tmp.

Preconditions
=============

These are checked before the out dir is touched. Any failure means exit code 2
and one `error: ...` line on stderr; a load error is prefixed with the
scenario's path (or stdin).

  - Python 3.11 or newer.
  - The scenario parses and every key is known; `name`, if given, is a slug
    ([A-Za-z0-9][A-Za-z0-9._-]*); every `edit` file is in the project table,
    the remote checkout, or an earlier write step; no path in the project table
    or a write step is both a file and the parent directory of another.
  - The scenario file exists (any name; the out dir is named after its stem),
    and the out dir does not contain it.
  - --gradle, if given, is a binary or a version as described under Commands;
    without it, the default Gradle is checked the same way when a Gradle step
    or a daemon stop needs it.
  - For stop-daemons: <out> holds a set-up project (the marker and
    <out>/project), and --gradle, if given, is a binary.
  - With setup.layout.remote: git is on PATH and the link has a supported form.
    The checkout is fetched (or its HEAD compared) before the out dir is
    touched, and edit targets are checked against it.
  - On Windows, with a shell step to run: the bash of Git for Windows, found
    from git on PATH.
  - With a Gradle version in play: gradle is on PATH to install its wrapper,
    and the layout or the remote checkout holds a settings file. The install
    itself happens after setup; if the wrapper task fails, the run ends as an
    error (exit 2) with the log's tail.

Exit codes: 0 every step as expected; 1 a run step deviated from its
expectation; 2 bad input (a failed precondition, a malformed scenario, bad
arguments), a step that could not be carried out (an edit whose text was not
found, a write onto a directory), or a failed wrapper install. summary.json is
written for run only, and there it is present whenever the header was printed:
on exit 1, and on exit 2 after the header. The daemon stops never change the
exit code.
"""


# --------------------------------------------------------------------------- model


def gust_dir() -> Path:
    """The cache root: $GUST_DIR, else .gust in the working directory."""
    return Path(os.environ.get(GUST_DIR_ENV) or Path.cwd() / ".gust").resolve()


class GustError(Exception):
    """A malformed scenario, bad arguments, or a failed precondition: exit code 2, before the out dir is touched."""


class StepError(Exception):
    """A write or edit step that could not be carried out; an error, not a deviation."""


@dataclass(frozen=True)
class Expect:
    exit: str = "pass"                     # "pass" | "fail"
    output: tuple[str, ...] = ()           # each must appear in the combined output
    no_output: tuple[str, ...] = ()        # none may appear in it

    def to_json(self) -> dict:
        d: dict = {"exit": self.exit}
        if self.output:
            d["output"] = list(self.output)
        if self.no_output:
            d["no_output"] = list(self.no_output)
        return d

    def deviations(self, exit_code: int, output_text: str) -> list[str]:
        """Every check that did not hold, one short line each."""
        got = "pass" if exit_code == 0 else "fail"
        found = [f"expected {self.exit}, got {got}"] if got != self.exit else []
        found += [f"output lacks {t!r}" for t in self.output if t not in output_text]
        found += [f"output has {t!r}, which no_output forbids" for t in self.no_output if t in output_text]
        return found


@dataclass(frozen=True)
class RunStep:
    kind: str                    # "gradle" | "shell"
    command: str                 # gradle: the arguments; shell: the command
    expect: Expect = Expect()
    name: str | None = None

    def label(self) -> str:
        return self.name or f"Run: {'gradle ' if self.kind == 'gradle' else ''}{self.command}"


@dataclass(frozen=True)
class WriteStep:
    files: dict[str, str]
    name: str | None = None

    def label(self) -> str:
        return self.name or "Write: " + ", ".join(Path(p).name for p in self.files)


@dataclass(frozen=True)
class Edit:
    file: str
    replace: str
    with_: str


@dataclass(frozen=True)
class EditStep:
    edits: list[Edit]
    name: str | None = None

    def label(self) -> str:
        return self.name or "Edit: " + ", ".join(dict.fromkeys(Path(e.file).name for e in self.edits))


Step = RunStep | WriteStep | EditStep


@dataclass(frozen=True)
class Remote:
    link: str          # as written in the scenario
    url: str           # what git fetches from
    sha: str
    subdir: str        # "" for the repository root

    @property
    def identity(self) -> str:
        host_path = re.sub(r"^[a-z+]+://", "", self.url)
        host_path = re.sub(r"\.git$", "", host_path)
        return re.sub(r"[^A-Za-z0-9._-]+", "-", host_path).strip("-") + "-" + self.sha

    @property
    def dir(self) -> Path:
        """The cached checkout: <.gust>/remotes/<identity>."""
        return gust_dir() / REMOTES_DIR / self.identity

    @property
    def source(self) -> Path:
        return self.dir / self.subdir if self.subdir else self.dir


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str | None
    files: dict[str, str]               # path -> content: the layout file's, then the scenario's own
    steps: list[Step]
    remote: Remote | None = None
    gradle: str | None = None           # "wrapper" (the project's own ./gradlew), a version such as "9.7.1", or None

    def counts(self) -> dict[str, int]:
        """The number of steps of each kind: gradle, shell, write, edit."""
        kinds = [s.kind if isinstance(s, RunStep) else "write" if isinstance(s, WriteStep) else "edit" for s in self.steps]
        return {k: kinds.count(k) for k in ("gradle", "shell", "write", "edit")}


# --------------------------------------------------------------------------- loading


def load_scenario(text: str, base_dir: Path | None = None, default_name: str | None = None) -> Scenario:
    """Parse a scenario.

    A relative setup.layout.base is resolved against base_dir, the scenario file's directory.
    default_name (the file's stem, used as is) is the name when the scenario has none; without
    either, the name is "scenario".
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise GustError(f"not valid TOML: {e}") from e
    _expect_keys(data, "top level", TOP_KEYS)
    name = _optional(data, "name", str, "top level")
    if name is not None and not NAME_RE.fullmatch(name):
        raise GustError(f"name: must match {NAME_RE.pattern}, got {name!r}")
    if name is None:
        name = default_name or DEFAULT_NAME
    description = _optional(data, "description", str, "top level")
    setup = data.get("setup", {})
    if not isinstance(setup, dict):
        raise GustError("setup: must be a table")
    _expect_keys(setup, "setup", SETUP_KEYS)
    gradle = _optional(setup, "gradle", str, "setup")
    if gradle is not None and gradle != "wrapper" and not VERSION_RE.fullmatch(gradle):
        raise GustError(f"setup.gradle: must be 'wrapper' or a Gradle version such as '9.7.1', got {gradle!r}")
    files, remote = _load_layout(setup.get("layout", {}), base_dir)

    steps_raw = data.get("steps", [])
    if not isinstance(steps_raw, list):
        raise GustError("[[steps]] must be an array of tables")
    steps = [_load_step(s, i + 1) for i, s in enumerate(steps_raw)]
    if not steps:
        raise GustError("scenario has no steps")
    scenario = Scenario(name=name, description=description, files=files, steps=steps, remote=remote, gradle=gradle)
    if remote is None:
        check_file_flow(scenario, lambda rel: False)
    return scenario


def check_file_flow(scenario: Scenario, exists) -> None:
    """Check that every edit's file exists by then, and that no path is both a file and a directory.

    exists(rel) is whether the remote checkout, if any, has that file.
    """
    files: dict[str, str] = {}   # path -> where it first appears
    dirs: dict[str, str] = {}    # ancestor dir -> a file under it

    def add(path: str, where: str) -> None:
        if path in dirs:
            raise GustError(f"{where}: {path!r} conflicts with {dirs[path]!r} ({files[dirs[path]]}); one is a directory of the other")
        parts = Path(path).parts
        for k in range(1, len(parts)):
            ancestor = "/".join(parts[:k])
            if ancestor in files or exists(ancestor):
                origin = files.get(ancestor, "setup.layout.remote")
                raise GustError(f"{where}: {path!r} conflicts with {ancestor!r} ({origin}); one is a directory of the other")
            dirs.setdefault(ancestor, path)
        files[path] = where

    for path in scenario.files:
        add(path, "setup.layout.project")
    for i, step in enumerate(scenario.steps, start=1):
        if isinstance(step, WriteStep):
            for path in step.files:
                add(path, f"steps[{i}].write")
        elif isinstance(step, EditStep):
            for j, edit in enumerate(step.edits, start=1):
                if edit.file not in files and not exists(edit.file):
                    raise GustError(
                        f"steps[{i}].edit[{j}].file: {edit.file!r} is not in the layout and no earlier step writes it")


def _load_layout(raw: object, base_dir: Path | None) -> tuple[dict[str, str], Remote | None]:
    if not isinstance(raw, dict):
        raise GustError("setup.layout: must be a table of base, remote, and project")
    _expect_keys(raw, "setup.layout", LAYOUT_KEYS)
    if "base" in raw and "remote" in raw:
        raise GustError("setup.layout: base and remote cannot be combined")
    files: dict[str, str] = {}
    remote = None
    if "base" in raw:
        files = _load_base(_require(raw, "base", str, "setup.layout"), base_dir)
    if "remote" in raw:
        remote = _load_remote(_require(raw, "remote", str, "setup.layout"))
    if "project" in raw:
        files.update(_load_files(raw["project"], "setup.layout.project"))   # the scenario's own files win
    return files, remote


def _load_remote(link: str) -> Remote:
    m = re.fullmatch(r"https://github\.com/([^/]+)/([^/]+)/(?:tree|commit)/([^/]+)(?:/(.*))?", link)
    if m:
        owner, repo, sha, subdir = m.groups()
        url = f"https://github.com/{owner}/{repo}.git"
    elif "#" in link:
        url, _, rest = link.rpartition("#")
        sha, _, subdir = rest.partition("/")
    else:
        raise GustError(f"setup.layout.remote: unsupported link {link!r}; use a GitHub tree/commit link or <git url>#<sha>[/<subdir>]")
    if not SHA_RE.fullmatch(sha):
        raise GustError(f"setup.layout.remote: {sha!r} is not a commit hash (7 to 40 hex digits); branches and tags are not accepted")
    subdir = (subdir or "").strip("/")
    if subdir:
        _check_relative_path(subdir, "setup.layout.remote subdirectory")
    return Remote(link=link, url=url, sha=sha, subdir=subdir)


def _load_base(base: str, base_dir: Path | None) -> dict[str, str]:
    """The [project] table of a layout file, which may not have a base of its own."""
    path = Path(base)
    if not path.is_absolute():
        if base_dir is None:
            raise GustError(f"setup.layout.base: {base!r} is relative but the scenario has no directory to resolve it against")
        path = base_dir / path
    if not path.is_file():
        raise GustError(f"setup.layout.base: no such file {path}")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise GustError(f"setup.layout.base: {path} is not valid TOML: {e}") from e
    if "base" in data:
        raise GustError(f"setup.layout.base: {path} has a base of its own; only one level is allowed")
    _expect_keys(data, f"setup.layout.base ({path.name})", {"project"})
    return _load_files(data.get("project", {}), f"setup.layout.base ({path.name}).project")


def _load_files(raw: object, where: str) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise GustError(f"{where}: must be a table of path = content")
    files: dict[str, str] = {}
    for path, content in raw.items():
        _check_relative_path(path, f"{where}.{path!r}")
        if not isinstance(content, str):
            raise GustError(f"{where}.{path!r}: content must be a string")
        files[path] = content
    return files


def _load_step(raw: object, index: int) -> Step:
    where = f"steps[{index}]"
    if not isinstance(raw, dict):
        raise GustError(f"{where}: must be a table")
    _expect_keys(raw, where, STEP_KEYS)
    kinds = [k for k in ("run", "write", "edit") if k in raw]
    if len(kinds) != 1:
        raise GustError(f"{where}: needs exactly one of 'run', 'write', or 'edit'")
    name = _optional(raw, "name", str, where)
    kind = kinds[0]
    if kind == "run":
        return _load_run(raw["run"], f"{where}.run", name)
    if kind == "write":
        files = _load_files(raw["write"], f"{where}.write")
        if not files:
            raise GustError(f"{where}.write: no files")
        return WriteStep(files=files, name=name)
    edits_raw = raw["edit"]
    if not isinstance(edits_raw, list) or not edits_raw:
        raise GustError(f"{where}.edit: must be a non-empty array of tables")
    edits = []
    for j, e in enumerate(edits_raw, start=1):
        ew = f"{where}.edit[{j}]"
        if not isinstance(e, dict):
            raise GustError(f"{ew}: must be a table")
        _expect_keys(e, ew, EDIT_KEYS)
        file = _require(e, "file", str, ew)
        _check_relative_path(file, f"{ew}.file")
        replace = _require(e, "replace", str, ew)
        if not replace:
            raise GustError(f"{ew}.replace: must not be empty")
        edits.append(Edit(file=file, replace=replace, with_=_require(e, "with", str, ew)))
    return EditStep(edits=edits, name=name)


def _load_run(raw: object, where: str, name: str | None) -> RunStep:
    if not isinstance(raw, dict):
        raise GustError(f"{where}: must be run.gradle or run.shell")
    _expect_keys(raw, where, set(RUN_KINDS))
    if len(raw) != 1:
        raise GustError(f"{where}: needs exactly one of 'gradle' or 'shell'")
    kind, value = next(iter(raw.items()))
    where = f"{where}.{kind}"
    if isinstance(value, str):
        return RunStep(kind=kind, command=value, name=name)
    if not isinstance(value, dict):
        raise GustError(f"{where}: must be a string or a table")
    _expect_keys(value, where, RUN_KEYS[kind])
    command = _require(value, "args" if kind == "gradle" else "command", str, where)
    expect = _load_expect(value.get("expect", "pass"), f"{where}.expect")
    return RunStep(kind=kind, command=command, expect=expect, name=name)


def _load_expect(raw: object, where: str) -> Expect:
    if isinstance(raw, str):
        raw = {"exit": raw}
    if not isinstance(raw, dict):
        raise GustError(f"{where}: must be 'pass', 'fail', or a table")
    _expect_keys(raw, where, EXPECT_KEYS)
    exit_ = _optional(raw, "exit", str, where) or "pass"
    if exit_ not in EXPECT_VALUES:
        raise GustError(f"{where}.exit: must be 'pass' or 'fail', got {exit_!r}")
    return Expect(exit=exit_, output=_string_list(raw, "output", where), no_output=_string_list(raw, "no_output", where))


def _string_list(table: dict, key: str, where: str) -> tuple[str, ...]:
    if key not in table:
        return ()
    value = table[key]
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise GustError(f"{where}.{key}: must be an array of non-empty strings")
    return tuple(value)


def _require(table: dict, key: str, typ: type, where: str):
    if key not in table:
        raise GustError(f"{where}: missing required key {key!r}")
    return _optional(table, key, typ, where)


def _optional(table: dict, key: str, typ: type, where: str):
    if key not in table:
        return None
    value = table[key]
    if not isinstance(value, typ):
        raise GustError(f"{where}.{key}: must be {typ.__name__}")
    return value


def _expect_keys(table: dict, where: str, allowed: set[str]) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise GustError(f"{where}: unknown key(s) {', '.join(unknown)}")


def _check_relative_path(path: str, where: str) -> None:
    p = Path(path)
    if not path or p.anchor or ".." in p.parts:       # an anchor is a root or a drive: "/", on Windows also "\" or "C:"
        raise GustError(f"{where}: path must be relative and must not contain '..'")


# --------------------------------------------------------------------------- setting up


def tmp_out(stem: str, now: float | None = None) -> Path:
    """The --tmp out dir, <TMP_ROOT>/gust-<yymmdd>/<stem>.<HHMMSS>.out, in local time.

    stem is the scenario file's stem, or the scenario's name for stdin.
    """
    t = time.localtime(now)
    return TMP_ROOT / time.strftime("gust-%y%m%d", t) / f"{stem}.{time.strftime('%H%M%S', t)}.out"


def check_out_dir(out_dir: Path, scenario_path: Path) -> None:
    if scenario_path.resolve().is_relative_to(out_dir):
        raise GustError(f"out dir {out_dir} contains the scenario file {scenario_path}; it would be removed with the earlier run")


MARKER_TEXT = "Marker for gust: this directory was produced by gust and may be replaced or removed by a later run.\n"


def prepare_out(out_dir: Path) -> None:
    """Make out_dir an empty directory with the marker.

    An earlier out dir (one with the marker) is removed without notice, an empty directory is
    reused, and anything else is refused.
    """
    if out_dir.exists():
        _ours_or_empty(out_dir, "out dir")
        try:
            for child in out_dir.iterdir():            # all but the marker, so after a failure the dir is still gust's
                if child.name == MARKER:
                    continue
                if child.is_dir() and not child.is_symlink():
                    rmtree(child)
                else:
                    child.unlink()
        except OSError as e:
            raise GustError(f"cannot remove {e.filename or out_dir} of the earlier run ({e.strerror}); if a Gradle daemon "
                            f"still has files open there, stop it with 'gust stop-daemons' and try again") from e
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / MARKER).write_text(MARKER_TEXT, encoding="utf-8", newline="")


def rmtree(path: Path, ignore_errors: bool = False) -> None:
    """shutil.rmtree. On Windows a file that cannot be removed is made writable and removed again, since git's
    object files are read-only there."""
    if not WINDOWS:
        shutil.rmtree(path, ignore_errors=ignore_errors)
        return

    def writable_and_again(remove, p, _):
        try:
            os.chmod(p, stat.S_IWRITE)
            remove(p)
        except OSError:
            if not ignore_errors:
                raise
    shutil.rmtree(path, **{"onexc" if sys.version_info >= (3, 12) else "onerror": writable_and_again})


def _ours_or_empty(path: Path, what: str) -> None:
    if not path.is_dir():
        raise GustError(f"{what} is not a directory: {path}")
    if any(path.iterdir()) and not (path / MARKER).is_file():
        raise GustError(f"{what} is not empty and was not created by gust, so it is left alone: {path}; pass another --out or remove it")


def fetch_remote(scenario: Scenario, out=None) -> dict | None:
    """Fetch the remote commit unless it is cached, then check the file flow against the checkout.

    Returns the summary's remote table {"link", "checkout", "fetched"}, or None without a remote.
    Fetch progress is printed to out, if given.
    """
    remote = scenario.remote
    if remote is None:
        return None
    if shutil.which("git") is None:
        raise GustError("setup.layout.remote needs git on PATH")
    target = remote.dir
    fetched = False
    if (target / ".git").exists():
        head = _git(target, "rev-parse", "HEAD").strip()
        if not head.startswith(remote.sha) and not remote.sha.startswith(head):
            raise GustError(f"checkout {target} is at {head[:12]}, not {remote.sha}; delete it to refetch")
        if not remote.source.is_dir():
            raise GustError(f"setup.layout.remote: {remote.subdir!r} is not a directory in {target}")
    else:
        if target.exists():
            raise GustError(f"checkout dir exists but is not a git checkout, so it is left alone: {target}")
        if out is not None:
            print(f"checkout: {target}", file=out)
            print(f"          fetching {remote.sha} from {remote.url}", file=out)
        target.mkdir(parents=True)
        try:
            _git(target, "init", "-q")
            _git(target, "remote", "add", "origin", remote.url)
            if _git(target, "fetch", "-q", "--depth", "1", "origin", remote.sha, check=False) is None:
                _git(target, "fetch", "-q", "origin")     # fetching a single commit is not allowed there: fetch everything
            _git(target, "checkout", "-q", remote.sha)
            if not remote.source.is_dir():
                raise GustError(f"setup.layout.remote: {remote.subdir!r} is not a directory in the commit")
        except GustError:
            rmtree(target, ignore_errors=True)
            raise
        fetched = True
    check_file_flow(scenario, lambda rel: (remote.source / rel).is_file())
    return {"link": remote.link, "checkout": str(target), "fetched": fetched}


def _git(cwd: Path, *args: str, check: bool = True) -> str | None:
    """git's stdout, run in cwd. On failure: GustError, or None when check is False."""
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        if not check:
            return None
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise GustError(f"git {args[0]} failed in {cwd}: {detail[-1] if detail else 'exit ' + str(proc.returncode)}")
    return proc.stdout


@dataclass(frozen=True)
class GradleCommand:
    """A Gradle invocation: the binary, the user home, then the arguments."""
    binary: str
    args: str
    user_home: Path
    trace: Path | None = None    # with --trace: the trace's base path; the file is <trace>-log.txt

    @property
    def full(self) -> str:
        """The command as run. The home and the trace come first, so the same options in args take precedence.
        Trace trees are off by default from Gradle 9.3; with the .tree flag they stay off in 8.11 to 9.2 too."""
        hidden = ["--gradle-user-home", str(self.user_home)]
        if self.trace:
            hidden += [f"-D{TRACE_PROPERTY}={self.trace}", f"-D{TRACE_PROPERTY}.tree=false"]
        return f"{shlex.quote(self.binary)} {shlex.join(hidden)} {self.args}".rstrip()

    @property
    def shown(self) -> str:
        """The command as shown on the console and in the summary: without the --gradle-user-home pair and the trace."""
        return f"{shlex.quote(self.binary)} {self.args}".rstrip()


def gradle_argv(command: str, cwd: Path) -> list[str]:
    """What a Gradle command line runs as: sh -c on POSIX. On Windows there is no sh, so the line is split by sh's
    rules and run directly, with the binary looked up in cwd (./gradlew.bat) or on PATH (gradle.bat)."""
    if not WINDOWS:
        return ["/bin/sh", "-c", command]
    try:
        argv = shlex.split(command)
    except ValueError as e:
        raise GustError(f"cannot split {command!r} into arguments: {e}") from e
    binary = argv[0]
    argv[0] = str(cwd / binary) if binary.startswith("./") else shutil.which(binary) or binary
    return argv


def shell_argv(command: str) -> list[str]:
    """What a run.shell command runs as: sh -c, and on Windows bash -c with Git for Windows' bash, so that one
    scenario file works everywhere."""
    return [git_bash() if WINDOWS else "/bin/sh", "-c", command]


@functools.cache
def git_bash() -> str:
    """Git for Windows' bash: bin/bash.exe in the install that holds `git --exec-path`, for the git on PATH.
    A bash from PATH is not taken, since on Windows that can be WSL's."""
    git = shutil.which("git")
    if git is None:
        raise GustError("shell steps on Windows run with the bash of Git for Windows, but no git is on PATH")
    exec_path = subprocess.run([git, "--exec-path"], capture_output=True, text=True).stdout.strip()
    for parent in Path(exec_path).parents if exec_path else ():
        if (parent / "bin" / "bash.exe").is_file():
            return str(parent / "bin" / "bash.exe")
    raise GustError(f"shell steps on Windows run with the bash of Git for Windows, but there is no bin\\bash.exe "
                    f"above {git}'s exec path {exec_path!r}")


def _check_shell(scenario: Scenario) -> None:
    """Find the bash for the scenario's shell steps, if it has any, before the out dir is touched."""
    if WINDOWS and scenario.counts()["shell"]:
        git_bash()


_VERSION_LINE = re.compile(r"^Gradle (\S+)\s*$", re.M)


def probe_gradle(binary: str, label: str, cwd: Path, user_home: Path) -> str:
    """The version on the "Gradle X" line of `<binary> --version`, run in cwd.

    Raises GustError, prefixed with label, when the binary fails or prints no such line; the
    message ends with the last lines of the output. Nothing is logged.
    """
    command = GradleCommand(binary, "--version", user_home)
    try:
        proc = subprocess.run(gradle_argv(command.full, cwd), cwd=cwd, capture_output=True, text=True, errors="replace")
    except OSError as e:
        raise GustError(f"{label}: cannot run '{command.shown}' ({e.strerror})") from e
    if proc.returncode != 0:
        lines = (proc.stderr or proc.stdout).strip().splitlines()
        detail = "; ".join(lines[-3:]) if lines else f"exit {proc.returncode}"
        raise GustError(f"{label}: cannot run '{command.shown}' ({detail})")
    m = _VERSION_LINE.search(proc.stdout)
    if m is None:
        raise GustError(f"{label}: not a Gradle binary (no 'Gradle <version>' line in the output of --version)")
    return m.group(1)


# --------------------------------------------------------------------------- which Gradle


SETTINGS_FILES = ("settings.gradle", "settings.gradle.kts", "settings.gradle.dcl")   # any one makes a Gradle build


@dataclass(frozen=True)
class GradleChoice:
    """The Gradle the steps run with, settled before the out dir is touched."""
    binary: str
    source: str                        # "default", "scenario" (setup.gradle), or "cli" (--gradle)
    version: str | None = None         # from the --version probe, or the version to install as given
    install: bool = False              # a wrapper for version is installed first, with gradle from PATH
    unchecked: str | None = None       # check only: why nothing was probed (the remote is not cached)

    @property
    def note(self) -> str:
        """The parenthetical of the gradle: header line."""
        note = {"cli": "--gradle", "scenario": "scenario", "default": "default"}[self.source]
        if self.install:
            note += ", installed by gradle"
        if self.unchecked:
            note += f"; not checked, {self.unchecked}"
        return note

    @property
    def header(self) -> str:
        """`<binary> <version> (<note>)`, without the version when there is none."""
        return f"{self.binary} {self.version} ({self.note})" if self.version else f"{self.binary} ({self.note})"


def settle_gradle(scenario: Scenario, cli_value: str | None, root: Path | None, user_home: Path,
                  needed: bool, may_install: bool) -> GradleChoice:
    """Settle the Gradle to run with, and check that it is usable, before the out dir is touched.

    cli_value is the --gradle value: a binary (a name or a path) or a version. Without it, the order
    is setup.gradle ("wrapper", or a version whose wrapper is installed first), then the project's
    own executable ./gradlew, then "gradle" from PATH.
    root holds the project's files so far: the remote checkout before setup, the project dir after
    it, or None for an inline layout or an unfetched remote.
    needed is whether a Gradle step or a daemon stop will run. When it is False and there is no
    --gradle and no version to install, the default is neither checked nor probed.
    may_install is False once the project is set up (stop-daemons). A version from --gradle is
    refused then, and the scenario's version means the wrapper installed at setup, probed like any
    binary.
    """
    def exists(rel: str) -> bool:
        return rel in scenario.files or (root is not None and (root / rel).is_file())

    if cli_value is None:
        source = "scenario" if scenario.gradle is not None else "default"
        install = scenario.gradle if scenario.gradle not in (None, "wrapper") else None
        if scenario.gradle is not None:
            binary = WRAPPER
        elif root is None and scenario.remote is not None and needed:      # check with an unfetched remote: unknown
            return GradleChoice("gradle", source, unchecked="remote not cached")
        else:                                                              # the project's own wrapper, else PATH
            binary = WRAPPER if root is not None and _executable(root / WRAPPER_FILE) else "gradle"
        label = binary if install is None else f"setup.gradle {install!r}"
    else:
        source = "cli"
        binary, install = _settle_binary(cli_value, exists)
        label = f"--gradle {cli_value!r}"
    if install is not None and not may_install:
        if source == "cli":
            raise GustError(f"--gradle {cli_value!r} is a version, but nothing is installed here; pass a binary (a name or a path)")
        install = None                                                   # set up already: the version's wrapper is in place
    if not (needed or cli_value is not None or install is not None):
        return GradleChoice(binary, source)                              # nothing runs with Gradle
    if install is not None:
        if shutil.which("gradle") is None:
            raise GustError("a Gradle version was requested but no 'gradle' is on PATH to install its wrapper")
        if root is None and scenario.remote is not None:                 # check with an unfetched remote
            return GradleChoice(WRAPPER, source, version=install, install=True, unchecked="remote not cached")
        if not any(exists(f) for f in SETTINGS_FILES):
            raise GustError(f"{label}: a wrapper can only be installed into a Gradle build; add a settings file to the layout")
        return GradleChoice(WRAPPER, source, version=install, install=True)
    if cli_value is None:                                                # the default must be usable
        if binary == WRAPPER and root is None and scenario.remote is not None:
            return GradleChoice(binary, source, unchecked="remote not cached")
        if binary == WRAPPER and not exists(WRAPPER_FILE):
            raise GustError(f'setup.gradle = "wrapper" but {WRAPPER_FILE} is not in the layout or the remote checkout')
        if binary != WRAPPER and shutil.which(binary) is None:
            raise GustError(f"'{binary}' is not on PATH")
    if binary.startswith("./") and root is None:                         # project-relative, with nothing on disk yet
        if scenario.remote is not None:
            return GradleChoice(binary, source, unchecked="remote not cached")
        raise GustError("the wrapper's gradle/wrapper/gradle-wrapper.jar cannot come from an inline layout; use setup.layout.remote")
    return GradleChoice(binary, source, version=probe_gradle(binary, label, root if root is not None else Path.cwd(), user_home))


def _settle_binary(value: str, exists) -> tuple[str, str | None]:
    """Read a --gradle value: (binary, None) for a binary, (./gradlew, version) for a version.

    A leading ~ is expanded first. With a "/", the value is a path: ./path is relative to the project
    and must be among its files; any other path must be an executable file. Without a "/", a name on
    PATH or an executable in the working directory is a binary; anything else must be a version.
    """
    value = os.path.expanduser(value)
    if os.sep in value or "/" in value or os.path.splitdrive(value)[0]:
        if value.startswith(("./", "." + os.sep)):
            rel = "/".join(part for part in Path(value).parts if part != ".")   # as the layout's keys are written
            if ".." in Path(value).parts or not exists(rel):
                raise GustError(f"--gradle {value!r} is relative to the project dir but no such file is in setup.layout.project or the remote checkout")
            return value.replace(os.sep, "/"), None                           # .\gradlew.bat as ./gradlew.bat, for bash too
        path = Path(value)
        if not path.is_file() or not _executable(path):
            raise GustError(f"--gradle {value!r}: no executable at {path.resolve()}")
        return (value if path.is_absolute() else str(path.resolve())), None
    found = shutil.which(value)
    if found and not (WINDOWS and os.path.dirname(found) == os.curdir):   # on Windows, the working dir is searched first
        return value, None
    local = Path(found).resolve() if found else Path.cwd() / value
    if local.is_file() and _executable(local):
        return str(local), None
    if VERSION_RE.fullmatch(value):
        return WRAPPER, value
    raise GustError(f"--gradle {value!r}: not found on PATH and not a Gradle version such as 9.7.1")


def _executable(path: Path) -> bool:
    """Whether the file exists and can be run: by its mode on POSIX, by its extension (.bat, .exe, ...) on Windows."""
    return (path.suffix.lower() in RUNNABLE_SUFFIXES and path.exists()) if WINDOWS else os.access(path, os.X_OK)


# --------------------------------------------------------------------------- laying out


def lay_out(scenario: Scenario, out_dir: Path) -> Path:
    """Prepare the out dir and write the project into it: the remote tree, if any, then the project table.
    Returns the project dir."""
    prepare_out(out_dir)
    project = out_dir / "project"
    if scenario.remote is not None:
        shutil.copytree(scenario.remote.source, project, ignore=shutil.ignore_patterns(".git"), symlinks=True)
    for rel, content in scenario.files.items():
        _write_file(project / rel, content)
    project.mkdir(exist_ok=True)
    return project


def _write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="")      # as written: no \r added on Windows


def _apply_edit(project: Path, edit: Edit) -> None:
    path = project / edit.file
    if not path.is_file():
        raise StepError(f"no such file {edit.file}; nothing to edit")
    text = path.read_text(encoding="utf-8")
    n = text.count(edit.replace)
    if n != 1:
        raise StepError(f"{edit.replace!r} occurs {n} times in {edit.file}; an edit needs exactly one")
    path.write_text(text.replace(edit.replace, edit.with_), encoding="utf-8", newline="")


# --------------------------------------------------------------------------- running


@dataclass
class StepResult:
    index: int
    kind: str                    # "gradle" | "shell" | "write" | "edit"
    name: str                    # given or implied
    outcome: str                 # "pass" | "fail" | "done" | "error"
    expect: dict | None = None   # run steps only, like deviations
    deviations: list[str] | None = None   # empty when every check held
    exit_code: int | None = None
    duration_s: float | None = None
    log: str | None = None       # relative to the out dir
    trace: str | None = None     # with --trace: the build operation trace, relative to the out dir, once it exists
    command: str | None = None   # as shown, without the --gradle-user-home pair
    files: list[str] | None = None
    error: str | None = None

    @property
    def deviated(self) -> bool:
        return bool(self.deviations)


@dataclass
class RunSummary:
    scenario: str
    description: str | None
    out: str
    project: str
    gradle: str                          # the binary
    gradle_version: str | None = None    # as on the gradle: header line
    gradle_args: list[str] | None = None   # the arguments after --
    gradle_user_home: str | None = None
    remote: dict | None = None   # {"link", "checkout", "fetched"} when setup.layout.remote is set
    install: dict | None = None  # {"version", "log", "exit_code"} when a wrapper install ran
    status: str = "ok"           # "ok" | "deviated" | "error"
    steps: list[StepResult] = field(default_factory=list)
    stop_daemons: dict | None = None   # {"before", "after"}, each {"exit_code", "duration_s", "log"}

    def to_json(self) -> str:
        """The summary as one JSON line, fields in declaration order, without empty optional ones."""
        always = ("scenario", "out", "project", "gradle", "status", "steps")
        steps = [{k: v for k, v in vars(s).items() if v is not None} for s in self.steps]
        return json.dumps({k: (steps if k == "steps" else v)
                           for k, v in vars(self).items() if v or k in always})


@dataclass
class Run:
    """The command-line state of a run, setup, or stop-daemons command.

    gradle is the --gradle value as given; choice is settled later, in begin() or before stop-daemons.
    """
    scenario: Scenario
    out_dir: Path
    user_home: Path
    gradle: str | None = None
    out: object = None           # the console stream (default: sys.stdout)
    tail: int = 30
    show_output: bool = False
    trace: bool = False
    gradle_args: list[str] = field(default_factory=list)
    choice: GradleChoice | None = None

    def __post_init__(self) -> None:
        if self.out is None:
            self.out = sys.stdout
        self.user_home.mkdir(parents=True, exist_ok=True)

    @property
    def project(self) -> Path:
        return self.out_dir / "project"

    @property
    def env(self) -> dict:
        """The steps' environment: os.environ plus $GRADLE, $GRADLE_USER_HOME, and $GRADLE_ARGS."""
        return dict(os.environ, **{GRADLE_ENV: _for_bash(self.choice.binary), GRADLE_USER_HOME_ENV: str(self.user_home),
                                   GRADLE_ARGS_ENV: shlex.join(self.gradle_args)})

    def command(self, args: str, trace: Path | None = None) -> GradleCommand:
        return GradleCommand(self.choice.binary, args, self.user_home, trace)


def _for_bash(binary: str) -> str:
    """The binary as $GRADLE: as it is, and on Windows as a full path with forward slashes, since bash on Windows
    finds gradle.bat by that name only (it has no PATHEXT). ./gradlew.bat stays, relative to the project."""
    if not WINDOWS or binary.startswith("./"):
        return binary
    return Path(shutil.which(binary) or binary).as_posix()


def print_header(scenario: Scenario, out_dir: Path | None, user_home: Path, choice: GradleChoice, out,
                 description: bool = False) -> None:
    """The header printed once the preconditions pass: scenario, out, home, gradle.

    The description, under scenario:, is only for check, which also passes no out_dir.
    """
    print(f"scenario: {scenario.name}", file=out)
    if description and scenario.description:
        print(f"          {scenario.description}", file=out)
    if out_dir is not None:
        print(f"out:      {out_dir}", file=out)
    print(f"home:     {user_home}", file=out)
    print(f"gradle:   {choice.header}", file=out)


def print_out(run: Run) -> None:
    """The closing `out:` line, after result:."""
    print(f"out:      {run.out_dir}", file=run.out)


def shared_home(gradle_user_home: Path | None) -> Path:
    """--gradle-user-home, else .gust/shared-gradle-user-home, resolved."""
    return (gradle_user_home or gust_dir() / SHARED_HOME).resolve()


def begin(run: Run, needed: bool) -> RunSummary:
    """What comes before the steps, for run and setup: the remote, the Gradle choice, the layout, the header,
    and the wrapper install. Returns the summary, with status "error" when the install failed.
    """
    scenario, out = run.scenario, run.out
    remote_info = None
    if scenario.remote is not None:
        print(f"remote:   {scenario.remote.link}", file=out)
        remote_info = fetch_remote(scenario, out)
        if not remote_info["fetched"]:
            print(f"checkout: {scenario.remote.dir} (cached)", file=out)
    root = scenario.remote.source if scenario.remote else None
    run.choice = settle_gradle(scenario, run.gradle, root, run.user_home, needed=needed, may_install=True)
    lay_out(scenario, run.out_dir)
    print_header(scenario, run.out_dir, run.user_home, run.choice, out)
    summary = RunSummary(scenario=scenario.name, description=scenario.description, out=str(run.out_dir),
                         project=str(run.project), gradle=run.choice.binary, gradle_version=run.choice.version,
                         gradle_args=run.gradle_args or None, gradle_user_home=str(run.user_home), remote=remote_info)
    if run.choice.install:
        summary.install = _install_wrapper(run)
        if summary.install["exit_code"] != 0:                            # a failed install ends the run as an error
            summary.status = "error"
    return summary


def run_scenario(run: Run, stop_daemons: bool = True) -> RunSummary:
    """Set up, stop daemons, run the steps up to the first deviation or error, and stop daemons again.

    Ends with summary.json and the result: and out: lines. No log tail is printed after a deviation
    when the output was shown.
    """
    steps = run.scenario.steps
    _check_shell(run.scenario)
    summary = begin(run, needed=stop_daemons or run.scenario.counts()["gradle"] > 0)
    if summary.status != "error":                                        # a failed install: no step runs
        if stop_daemons:
            summary.stop_daemons = {"before": _stop_daemons(run, "[BEF]", "stop-daemons-before.log", full=False)}
        for i, step in enumerate(steps, start=1):
            print(f"[{i}/{len(steps)}] {step.label()}", file=run.out)
            result = _run_command(run, step, i) if isinstance(step, RunStep) else _change_files(run, step, i)
            summary.steps.append(result)
            if result.outcome == "error":
                summary.status = "error"
                break
            if result.deviated:
                summary.status = "deviated"
                if not run.show_output:
                    _print_tail(run.out_dir / result.log, run.tail, run.out)
                break
        if stop_daemons:
            summary.stop_daemons["after"] = _stop_daemons(run, "[AFT]", "stop-daemons-after.log", full=False)
    (run.out_dir / "summary.json").write_text(summary.to_json() + "\n", encoding="utf-8", newline="")
    print(f"result: {summary.status} ({len(summary.steps)}/{len(steps)} steps ran)", file=run.out)
    print_out(run)
    return summary


def _run_command(run: Run, step: RunStep, index: int) -> StepResult:
    log_rel, trace_rel = f"step-{index:02d}.log", f"step-{index:02d}-ops-log.txt"   # the trace file: its base + -log.txt
    trace = run.out_dir / trace_rel.removesuffix("-log.txt") if run.trace and step.kind == "gradle" else None
    if step.kind == "gradle":
        command = run.command(f"{step.command} {shlex.join(run.gradle_args)}".rstrip(), trace)   # the arguments after -- go last
        argv, shown = gradle_argv(command.full, run.project), command.shown
    else:
        argv, shown = shell_argv(step.command), step.command      # shown as written
    print(f"$ {shown}", file=run.out)
    started = time.monotonic()
    exit_code = _run_logged(argv, run.project, run.env, run.out_dir / log_rel, run.out if run.show_output else None)
    duration = time.monotonic() - started
    outcome = "pass" if exit_code == 0 else "fail"
    output_text = (run.out_dir / log_rel).read_text(encoding="utf-8", errors="replace")   # CRLF read as LF
    deviations = step.expect.deviations(exit_code, output_text)
    traced = trace is not None and (run.out_dir / trace_rel).is_file()
    result = StepResult(
        index=index, kind=step.kind, name=step.label(), outcome=outcome, expect=step.expect.to_json(),
        deviations=deviations, exit_code=exit_code, duration_s=round(duration, 1), log=log_rel,
        trace=trace_rel if traced else None, command=shown,
    )
    checks = len(step.expect.output) + len(step.expect.no_output)
    checks_note = f", {checks} output check{'s' if checks != 1 else ''}" if checks else ""
    verdict = "DEVIATION" if deviations else "ok"
    print(f"{verdict}: exit {exit_code}{checks_note} in {duration:.1f}s -> {log_rel}" + (f", {trace_rel}" if traced else ""),
          file=run.out)
    for deviation in deviations:
        print(f"  {deviation}", file=run.out)
    return result


def _change_files(run: Run, step: WriteStep | EditStep, index: int) -> StepResult:
    """Apply a write or edit step. A failure comes back as an error result, not an exception."""
    kind = "write" if isinstance(step, WriteStep) else "edit"
    files = list(step.files) if isinstance(step, WriteStep) else list(dict.fromkeys(e.file for e in step.edits))
    try:
        if isinstance(step, WriteStep):
            for rel, content in step.files.items():
                _write_file(run.project / rel, content)
        else:
            for edit in step.edits:
                _apply_edit(run.project, edit)
    except StepError as e:
        message = str(e)
    except OSError as e:
        rel = Path(e.filename).relative_to(run.project) if e.filename and Path(e.filename).is_relative_to(run.project) else e.filename
        message = f"cannot {kind} {rel}: {e.strerror}"
    else:
        n = len(files)
        print(f"ok: {n} file{'s' if n != 1 else ''} {'written' if kind == 'write' else 'edited'}", file=run.out)
        return StepResult(index=index, kind=kind, name=step.label(), outcome="done", files=files)
    print(f"ERROR: {message}", file=run.out)
    return StepResult(index=index, kind=kind, name=step.label(), outcome="error", files=files, error=message)


def _stop_daemons(run: Run, label: str, log_name: str, full: bool) -> dict:
    """Run `<gradle> --stop` in the project dir, logged to <out>/<log_name>, never echoed.

    Around a run ([BEF], [AFT]) this is one line, plus an ERROR: line when the stop fails, which is
    never fatal. With full (the stop-daemons command) it is a block like a step's: label, `$` line,
    outcome.
    """
    command = run.command("--stop")
    print(f"{label} Stop Gradle daemons" + ("" if full else f" -> {log_name}"), file=run.out)
    if full:
        print(f"$ {command.shown}", file=run.out)
    started = time.monotonic()
    exit_code = _run_logged(gradle_argv(command.full, run.project), run.project, dict(os.environ), run.out_dir / log_name, None)
    duration = time.monotonic() - started
    if exit_code != 0:
        print(f"ERROR: exit {exit_code} in {duration:.1f}s -> {log_name}", file=run.out)
    elif full:
        print(f"ok: exit {exit_code} in {duration:.1f}s -> {log_name}", file=run.out)
    return {"exit_code": exit_code, "duration_s": round(duration, 1), "log": log_name}


def _install_wrapper(run: Run) -> dict:
    """Run `gradle wrapper --gradle-version <version>` in the project, with gradle from PATH: the [WRP] block.

    Returns {"version", "log", "exit_code"}. A failure does not raise: it is an ERROR: line, the log's
    tail unless the output was shown, and a nonzero exit_code.
    """
    version = run.choice.version
    command = GradleCommand("gradle", f"wrapper --gradle-version {shlex.quote(version)} --no-daemon", run.user_home)
    print(f"{WRAPPER_LABEL} Install Gradle wrapper {version}", file=run.out)
    print(f"$ {command.shown}", file=run.out)
    started = time.monotonic()
    exit_code = _run_logged(gradle_argv(command.full, run.project), run.project, dict(os.environ), run.out_dir / INSTALL_LOG,
                            run.out if run.show_output else None)
    duration = time.monotonic() - started
    if exit_code != 0:
        print(f"ERROR: exit {exit_code} in {duration:.1f}s -> {INSTALL_LOG}", file=run.out)
        if not run.show_output:
            _print_tail(run.out_dir / INSTALL_LOG, run.tail, run.out)
    else:
        print(f"ok: exit 0 in {duration:.1f}s -> {INSTALL_LOG}", file=run.out)
    return {"version": version, "log": INSTALL_LOG, "exit_code": exit_code}


RULE_WIDTH = 80   # fixed, so captured console output looks the same everywhere


def opening_rule(label: str) -> str:
    """`<dashes> <label> -----`, RULE_WIDTH wide, with the label near the end where it stands out."""
    tail = f" {label} -----"
    return "-" * max(RULE_WIDTH - len(tail), 5) + tail


def _run_logged(argv: list[str], cwd: Path, env: dict, log_path: Path, echo) -> int:
    """Run argv, stdout and stderr together into log_path, and return the exit code.

    With echo (a text stream), each line is also written there as it arrives, between two rules.
    """
    if echo is not None:
        echo.write(opening_rule(log_path.name) + "\n")
        echo.flush()
    last = b"\n"
    with open(log_path, "wb") as log, subprocess.Popen(
            argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as proc:
        for line in iter(proc.stdout.readline, b""):
            log.write(line)
            last = line
            if echo is not None:
                text = line.decode("utf-8", errors="replace")
                echo.write(text.replace("\r\n", "\n") if WINDOWS else text)   # else \r\r\n in a redirected stdout
                echo.flush()
    if echo is not None:
        echo.write(("" if last.endswith(b"\n") else "\n") + "-" * RULE_WIDTH + "\n")
        echo.flush()
    return proc.returncode


def _print_tail(log: Path, n: int, out) -> None:
    """Print the last n lines of a log between the same rules as --show-output."""
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    shown = lines[-n:] if n else []
    print(opening_rule(f"last {len(shown)} of {len(lines)} lines of {log.name}"), file=out)
    for line in shown:
        print(line, file=out)
    print("-" * RULE_WIDTH, file=out)


# --------------------------------------------------------------------------- flat


INLINE_WIDTH = 100   # a table whose inline line would be longer gets a section, or in a step one dotted key per entry
FILE_TABLES = {("setup", "layout", "project"), ("steps", "write")}   # path = content tables, always a section
BARE_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")
_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}


def flatten(text: str, source: str, base_dir: Path | None = None, default_name: str | None = None) -> str:
    """The scenario as self-contained TOML: setup.layout.base dropped, its files merged into [setup.layout.project]
    as for setup. The rest keeps its data and, where TOML allows, its key order. The leading block of comment and
    blank lines is kept as written; other comments are lost.

    Raises GustError when the result does not parse back to the same data or is not a valid scenario.
    """
    scenario = _load_named(text, source, base_dir=base_dir, default_name=default_name)
    data = tomllib.loads(text)
    layout = data.get("setup", {}).get("layout", {})
    if "base" in layout:
        del layout["base"]
        layout["project"] = scenario.files          # merged by _load_layout, so the files are the ones laid out by setup
    lines = text.splitlines(keepends=True)
    n = next((i for i, line in enumerate(lines) if line.strip() and not line.lstrip().startswith("#")), len(lines))
    lead = "".join(lines[:n]) if any(line.strip() for line in lines[:n]) else ""
    flat = lead + to_toml(data)
    try:
        same = tomllib.loads(flat) == data
    except tomllib.TOMLDecodeError:
        same = False
    if not same:
        raise GustError(f"{source}: the flattened TOML, once parsed, differs from the scenario (a bug in gust); nothing is printed")
    _load_named(flat, source, default_name=default_name)
    return flat


def to_toml(data: dict) -> str:
    """TOML for a table of strings, integers, booleans, arrays, and tables, in the shape of a hand-written scenario:
    plain keys first, small tables as dotted keys or inline tables, then [tables] and [[arrays of tables]].
    Any other value is a GustError."""
    out: list[str] = []
    _write_table(data, (), out, aot=False, tight=False)
    return "\n".join(out).lstrip("\n") + "\n"


def _write_table(table: dict, path: tuple, out: list[str], aot: bool, tight: bool) -> None:
    """Write a table: its header (left out when only sections follow), its plain keys, then its sections.

    tight: no blank line before a header, as inside an array of tables.
    """
    plain, sections = [], []
    for k, v in table.items():
        (sections if _is_section(v, path + (k,), bool(sections), in_step=aot or tight) else plain).append(k)
    if path and (plain or aot or not sections):
        if not tight:
            out.append("")
        name = ".".join(_key(p) for p in path)
        out.append(f"[[{name}]]" if aot else f"[{name}]")
    for k in plain:
        out.extend(_plain_lines((k,), table[k], path + (k,)))
    for k in sections:
        for element in table[k] if _is_aot(table[k]) else [table[k]]:
            _write_table(element, path + (k,), out, aot=_is_aot(table[k]), tight=tight or aot)


def _is_aot(v: object) -> bool:
    return isinstance(v, list) and bool(v) and all(isinstance(e, dict) for e in v)


def _is_section(v: object, path: tuple, after_section: bool, in_step: bool) -> bool:
    """Whether v is written under a header: an array of tables, a table of files, a table that holds a multiline string
    or one of those, a table after a section (so the key order stays), or outside a step a table too long for one line."""
    if _is_aot(v):
        return True
    if not isinstance(v, dict):
        return False
    return (after_section or path in FILE_TABLES or _holds_section(v, path)
            or not in_step and len(_plain_lines(path[-1:], v, path)) > 1)


def _holds_section(table: dict, path: tuple) -> bool:
    return any(isinstance(v, str) and "\n" in v or _is_aot(v)
               or isinstance(v, dict) and (path + (k,) in FILE_TABLES or _holds_section(v, path + (k,)))
               for k, v in table.items())


def _plain_lines(keys: tuple, v: object, path: tuple) -> list[str]:
    """`a.b = value` lines: one for a scalar, an array, or a table that fits inline, else one per entry."""
    lhs = ".".join(_key(k) for k in keys)
    if isinstance(v, dict) and len(v) == 1:
        (k, sub), = v.items()
        return _plain_lines(keys + (k,), sub, path + (k,))
    if isinstance(v, str) and "\n" in v and "'''" not in v and not re.search(r"[\x00-\x08\x0b-\x1f\x7f]", v):
        return [f"{lhs} = '''\n{v}'''"]      # the newline right after the opening ''' is not part of the string
    line = f"{lhs} = {_inline(v, path)}"
    if isinstance(v, dict) and len(line) > INLINE_WIDTH:
        return [entry for k, sub in v.items() for entry in _plain_lines(keys + (k,), sub, path + (k,))]
    return [line]


def _inline(v: object, path: tuple) -> str:
    if isinstance(v, str):
        return _string(v)
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_inline(e, path + (i,)) for i, e in enumerate(v)) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_inline(e, path + (k,))}" for k, e in v.items()) + " }" if v else "{}"
    where = ".".join(str(p) for p in path)
    raise GustError(f"{where}: a {type(v).__name__} value is not part of the scenario format and cannot be written")


def _string(s: str) -> str:
    """A basic string when nothing needs escaping, else a literal one when it can be, else an escaped basic one."""
    if not re.search(r'["\\\x00-\x08\x0a-\x1f\x7f]', s):
        return f'"{s}"'
    if not re.search(r"['\x00-\x08\x0a-\x1f\x7f]", s):
        return f"'{s}'"
    return '"' + "".join(_ESCAPES.get(c) or (f"\\u{ord(c):04x}" if c < " " or c == "\x7f" else c) for c in s) + '"'


def _key(k: str) -> str:
    return k if BARE_KEY_RE.fullmatch(k) else _string(k)


# --------------------------------------------------------------------------- cli


def _tail_arg(value: str) -> int:
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError("--tail must be 0 or more")
    return n


PRECEDENCE = f"default: setup.gradle, else the project's {WRAPPER}, else gradle from PATH"
BINARY = f"a Gradle binary, as a name on PATH or in the working directory, or as a path ({WRAPPER} is relative to the project)"
GRADLE_HELP = (f"{BINARY}, or a version such as 9.7.1, whose wrapper is installed first with gradle from PATH "
               f"(the layout needs a settings file); {PRECEDENCE}")
BINARY_HELP = f"{BINARY}; no version, since the project is set up already; {PRECEDENCE}"
TMP_HELP = (f"put the out dir at {TMP_SHAPE}; "
            "pass --out <printed path> to stop its daemons later")
SHOW_OUTPUT_HELP = "also print each step's output as it runs; the log is written either way"
SCENARIO_HELP = "the scenario file, or - to read it from stdin"
HOME_HELP = "the Gradle user home for every Gradle invocation (default: .gust/shared-gradle-user-home)"


def build_parser() -> argparse.ArgumentParser:
    invoked_as = Path(sys.argv[0]).name
    parser = argparse.ArgumentParser(
        prog=invoked_as if invoked_as in ("gust", "gust.py") else "gust.py",
        description="Gust, the Gradle User Scenario Tool: set up and run Gradle user scenarios.",
        epilog="Use 'help COMMAND' for a command's options and 'spec' for the scenario file format.",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    p_help = sub.add_parser("help", help="print usage, or a command's usage",
                            description="Print the usage, or one command's usage.")
    p_help.add_argument("topic", nargs="?", metavar="COMMAND", help="the command whose usage to print")
    sub.add_parser("version", help="print gust's version, the Python it runs on, and the script's path")
    sub.add_parser("spec", help="print the scenario file format")

    def scenario_args(p: argparse.ArgumentParser, lays_out: bool) -> None:
        """SCENARIO, --out, --gradle, --gradle-user-home; --tmp and a --gradle version only where the project is laid out."""
        p.add_argument("scenario", type=Path, help=SCENARIO_HELP)
        p.add_argument("--out", type=Path, metavar="DIR",
                       help="the out dir (default: <stem>.out in the working directory, or <name>.out for stdin)")
        if lays_out:
            p.add_argument("--tmp", action="store_true", help=TMP_HELP)
            p.add_argument("--gradle", metavar="BIN|VERSION", help=GRADLE_HELP)
        else:
            p.add_argument("--gradle", metavar="BIN", help=BINARY_HELP)
        p.add_argument("--gradle-user-home", type=Path, metavar="DIR", help=HOME_HELP)

    p_setup = sub.add_parser("setup", help="lay the project out, and install the wrapper for a version; run no step",
                             description="Lay the project out in the out dir and, with a version in play, install its wrapper. "
                                         "No step runs. Ends in 'result: set up' or 'result: error'.")
    scenario_args(p_setup, lays_out=True)
    p_setup.add_argument("--show-output", action="store_true", help=SHOW_OUTPUT_HELP)
    p_run = sub.add_parser(
        "run", help="setup, then stop daemons, run the steps, stop daemons",
        description="Set up, then stop daemons, run the steps, and stop daemons again. Everything after a literal -- "
                    "goes onto every run.gradle step's command, after the step's own arguments, so it wins on a "
                    "conflict. Shell steps get it as $GRADLE_ARGS. The daemon stops, the --version probe, and the "
                    "wrapper install do not get it.")
    scenario_args(p_run, lays_out=True)
    p_run.add_argument("--json", action="store_true", help="print the run summary as JSON on the last line")
    p_run.add_argument("--tail", type=_tail_arg, default=30, metavar="N",
                       help="how many log lines to show after a deviation or a failed wrapper install (default: 30)")
    p_run.add_argument("--show-output", action="store_true", help=SHOW_OUTPUT_HELP)
    p_run.add_argument("--keep-daemons", action="store_true", help="do not stop Gradle daemons before or after the steps")
    p_run.add_argument("--trace", action="store_true",
                       help="record a build operation trace for each run.gradle step, as step-NN-ops-log.txt in the "
                            "out dir; shell steps are not traced")
    p_run.usage = p_run.format_usage().removeprefix("usage: ").rstrip("\n") + " [-- GRADLE_ARGS ...]"
    p_check = sub.add_parser(
        "check", help="validate the scenario and settle the Gradle binary; run nothing",
        description="Validate the scenario (keys, file flow, setup.gradle) and settle the Gradle binary as run would, "
                    "--version probe included. Nothing is set up, installed, or fetched (an uncached remote is noted); "
                    "the only thing written is what the probe leaves in the Gradle user home. Worth running before a "
                    "long run.")
    p_check.add_argument("scenario", type=Path, help=SCENARIO_HELP)
    p_check.add_argument("--gradle", metavar="BIN|VERSION", help=GRADLE_HELP)
    p_check.add_argument("--gradle-user-home", type=Path, metavar="DIR", help=HOME_HELP)
    p_check.add_argument("--json", action="store_true", help="print the summary as JSON on the last line")
    scenario_args(sub.add_parser("stop-daemons", help="run '<gradle> --stop' in the set-up project",
                                 description="Run '<gradle> --stop' in the set-up project, stopping every daemon of that "
                                             "Gradle and user home. Needs <out>/project from 'setup' or an earlier 'run'."),
                  lays_out=False)
    p_flat = sub.add_parser(
        "flat", help="print the scenario with setup.layout.base inlined",
        description="Print the scenario as TOML on stdout with setup.layout.base inlined: the layout file's files are "
                    "merged into [setup.layout.project] as for setup, and base is dropped. A remote stays a link. The "
                    "comment and blank lines at the top of the file are kept; other comments are lost.")
    p_flat.add_argument("scenario", type=Path, help=SCENARIO_HELP)
    parser.set_defaults(_subparsers={name: sp for name, sp in sub.choices.items()})
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    gradle_args: list[str] = []
    if "--" in argv:                       # split before argparse: everything after -- is for the Gradle steps
        cut = argv.index("--")
        argv, gradle_args = argv[:cut], argv[cut + 1:]
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return EXIT_OK
    if gradle_args and args.command != "run":
        print(f"error: arguments after -- are accepted by run only ({args.command} runs no Gradle step)", file=sys.stderr)
        return EXIT_ERROR
    if args.command == "help":
        if args.topic is None:
            parser.print_help()
        elif args.topic in args._subparsers:
            args._subparsers[args.topic].print_help()
        else:
            parser.error(f"unknown command {args.topic!r} (choose from {', '.join(args._subparsers)})")
        return EXIT_OK
    if args.command == "version":
        print(f"gust {__version__}\n"
              f"python {sys.version.split()[0]} ({sys.executable})\n"
              f"script {Path(__file__).resolve()}", file=sys.stdout)
        return EXIT_OK
    if args.command == "spec":
        print(FORMAT_SPEC, end="")
        return EXIT_OK
    try:
        if args.command == "flat":
            text, source, kwargs = read_source(args)
            print(flatten(text, source, **kwargs), end="")
            return EXIT_OK
        scenario, out_dir = read_inputs(args)
        user_home = shared_home(args.gradle_user_home)
        if args.command == "check":
            return _cli_check(scenario, args.gradle, user_home, args.json)
        run = Run(scenario, out_dir, user_home, gradle=args.gradle, tail=getattr(args, "tail", 30),
                  show_output=getattr(args, "show_output", False), trace=getattr(args, "trace", False),
                  gradle_args=gradle_args)
        if args.command == "stop-daemons":
            return _cli_stop_daemons(run, str(args.scenario))
        if args.command == "setup":
            return _cli_setup(run)
        summary = run_scenario(run, not args.keep_daemons)
        if args.json:
            print(summary.to_json())
        return {"ok": EXIT_OK, "deviated": EXIT_DEVIATED}.get(summary.status, EXIT_ERROR)
    except (GustError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERROR


def read_inputs(args) -> tuple[Scenario, Path | None]:
    """Load the scenario from its file or stdin, and settle the out dir (None for check)."""
    tmp, out = getattr(args, "tmp", False), getattr(args, "out", None)
    if tmp and out:
        raise GustError(f"--tmp and --out cannot be combined: with --tmp the out dir is {TMP_SHAPE}")
    text, source, kwargs = read_source(args)
    scenario = _load_named(text, source, **kwargs)
    stem = kwargs.get("default_name", scenario.name)
    if args.command == "check":
        return scenario, None
    out_dir = tmp_out(stem) if tmp else out.resolve() if out else (Path.cwd() / (stem + ".out")).resolve()
    if str(args.scenario) != "-":
        check_out_dir(out_dir, args.scenario)
    return scenario, out_dir


def read_source(args) -> tuple[str, str, dict]:
    """The scenario's text from its file or stdin, its source for messages (the path as given, or 'stdin'), and the
    keywords for load_scenario: the directory a relative base resolves against, and a file's stem as the default name."""
    if str(args.scenario) == "-":
        if sys.stdin.isatty():
            raise GustError("SCENARIO is '-' but nothing is piped on stdin")
        return sys.stdin.read(), "stdin", {"base_dir": Path.cwd()}
    if not args.scenario.is_file():
        raise GustError(f"scenario file not found: {args.scenario}")
    return (args.scenario.read_text(encoding="utf-8"), str(args.scenario),
            {"base_dir": args.scenario.resolve().parent, "default_name": args.scenario.stem})


def _load_named(text: str, source: str, **kwargs) -> Scenario:
    """load_scenario, with the source (a path or 'stdin') prefixed to any load error."""
    try:
        return load_scenario(text, **kwargs)
    except GustError as e:
        raise GustError(f"{source}: {e}") from e


def _cli_check(scenario: Scenario, gradle: str | None, user_home: Path, as_json: bool) -> int:
    """Validate the scenario and settle the Gradle binary as run would, without setting anything up."""
    remote, root = scenario.remote, None
    if remote is not None:
        cached = (remote.dir / ".git").exists()
        if cached:
            root = remote.source
            check_file_flow(scenario, lambda rel: (remote.source / rel).is_file())
            print(f"remote:   {remote.link}\ncheckout: {remote.dir} (cached)")
        else:
            print(f"remote:   {remote.link} (not cached; fetched on run)")
    user_home.mkdir(parents=True, exist_ok=True)
    _check_shell(scenario)
    counts = scenario.counts()
    choice = settle_gradle(scenario, gradle, root, user_home, needed=counts["gradle"] > 0, may_install=True)
    print_header(scenario, None, user_home, choice, sys.stdout, description=True)
    print(f"steps: {len(scenario.steps)} ({', '.join(f'{n} {kind}' for kind, n in counts.items())})")
    print("result: ok")
    if as_json:
        summary = {"scenario": scenario.name, "description": scenario.description, "gradle": choice.binary,
                   "gradle_version": choice.version, "steps": len(scenario.steps), "step_counts": counts}
        if remote is not None:
            summary["remote"] = {"link": remote.link, "cached": cached}
        summary["status"] = "ok"
        print(json.dumps(summary))
    return EXIT_OK


def _cli_stop_daemons(run: Run, ref: str) -> int:
    """Run '<gradle> --stop' in a set-up project: the marker and <out>/project. ref, the scenario as given, is for the hint."""
    if not (run.out_dir / MARKER).is_file() or not run.project.is_dir():
        hint = "pipe the same scenario to 'gust setup -'" if ref == "-" else f"run 'gust setup {ref}'"
        raise GustError(f"no set-up project at {run.project}; {hint} first")
    run.choice = settle_gradle(run.scenario, run.gradle, run.project, run.user_home, needed=True, may_install=False)
    print_header(run.scenario, run.out_dir, run.user_home, run.choice, run.out)
    stopped = _stop_daemons(run, "[STOP]", "stop-daemons.log", full=True)["exit_code"] == 0
    print(f"result: {'ok' if stopped else 'error'}", file=run.out)
    print_out(run)
    return EXIT_OK if stopped else EXIT_ERROR


def _cli_setup(run: Run) -> int:
    ok = begin(run, needed=False).status != "error"
    print("result: set up" if ok else "result: error", file=run.out)
    print_out(run)
    return EXIT_OK if ok else EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
