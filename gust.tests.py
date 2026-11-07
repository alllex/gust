"""Unit tests for gust.py. Run: python3 gust.tests.py

Offline, against a local git fixture repository and fake Gradle binaries, written in Python.
Needs on PATH: git, and the POSIX tools the shell steps use (/bin/sh, echo, grep, test, false, printf).
On Windows, shell steps run with the bash of Git for Windows, which comes with those tools.
Nothing is written outside per-test temporary directories: each test gets its own .gust and PATH.
"""

import contextlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import gust as gs


def rule(label):
    """An opening rule of the console frame, 80 wide, the label followed by five dashes."""
    return "-" * (80 - len(f" {label} -----")) + f" {label} -----"


CLOSING = "-" * 80


def home_arg():
    """The pair that goes first on every Gradle invocation, for the current GUST_DIR."""
    return f"--gradle-user-home {gs.gust_dir() / gs.SHARED_HOME}"


MINIMAL = """
name = "min"
[[steps]]
run.shell = "true"
"""

# The fake Gradle binaries are Python scripts, so the same ones run everywhere: NAME.py, run by a one-line
# launcher next to it, NAME on POSIX and NAME.bat on Windows (runnable(NAME)). Each script starts with PRELUDE:
# ARGS are the arguments, NAME is the name it runs as, and with copy_as(name) a copy of it is made that runs as name.
PRELUDE = """\
import os, shutil, sys
from pathlib import Path
sys.stdout.reconfigure(newline="\\n")                                 # LF on Windows too, as on POSIX
ARGS, NAME = sys.argv[1:], Path(__file__).stem
def copy_as(name):
    for source in (Path(__file__), Path(__file__).with_suffix(".bat" if os.name == "nt" else "")):
        shutil.copy(source, name + source.suffix)
        os.chmod(name + source.suffix, 0o755)
"""
LAUNCHER = '#!/bin/sh\nexec "{python}" "$0.py" "$@"\n'                 # a shebang cannot hold a path with a space
BAT = '@"{python}" "%~dpn0.py" %*\n'                                  # %~dpn0: this file's path without .bat


def runnable(name):
    """The file name that runs a fake binary called name: name on POSIX, name.bat on Windows."""
    return name + ".bat" if gs.WINDOWS else name


def write_script(where, name, code):
    """Write the fake binary name, PRELUDE and then code, into where. Returns the path to run it by."""
    (where / f"{name}.py").write_text(PRELUDE + code)
    launcher = where / runnable(name)
    launcher.write_text((BAT if gs.WINDOWS else LAUNCHER).format(python=sys.executable))
    launcher.chmod(0o755)
    return launcher


# A stand-in for Gradle. On `NAME wrapper --gradle-version V ...`, a copy of it becomes the project's wrapper,
# but only inside a Gradle build (with a settings file), as with Gradle's wrapper task. On `--version`, a
# Gradle banner is printed. Anything else is echoed, after its name.
FAKE_GRADLE = """\
if ARGS[:1] == ["--gradle-user-home"]:                                 # first on every invocation
    del ARGS[:2]
if ARGS[:1] == ["--version"]:
    print("\\nWelcome to Gradle 9.7.1!\\n\\n------------\\nGradle 9.7.1\\n------------")
    sys.exit(0)
if ARGS[:1] == ["wrapper"]:
    if not any(Path(f).is_file() for f in ("settings.gradle", "settings.gradle.kts", "settings.gradle.dcl")):
        print(f"Directory '{os.getcwd()}' does not contain a Gradle build.")
        sys.exit(1)
    copy_as("gradlew")
    print(f"installed wrapper {ARGS[2]} via {NAME}")
    sys.exit(0)
print(NAME, *ARGS)
"""

SETTINGS = '[setup.layout.project]\n"settings.gradle.kts" = ""\n'   # what a wrapper install needs: a Gradle build

# `gecho`: the tests' dummy Gradle on PATH, an echo with a Gradle-shaped --version. The arguments are
# echoed, the --gradle-user-home pair included, so a log shows the full command.
GECHO = """\
if ARGS[:1] == ["--gradle-user-home"] and ARGS[2:3] == ["--version"]:
    print("Gradle 9.7.1")
    sys.exit(0)
print(*ARGS)
"""

# `tracer`: gecho plus the file named by Gradle's trace property, <base>-log.txt, with one line in it.
TRACER = GECHO + """\
for arg in ARGS:
    if arg.startswith("-Dorg.gradle.internal.operations.trace="):
        Path(arg.split("=", 1)[1] + "-log.txt").write_text('{"id": 1}\\n')
"""

NOT_GRADLE = 'print("hello")\n'                   # runs, but is no Gradle
FAILING = "sys.exit(1)\n"                         # fails on every call
WRAPPER, WRAPPER_FILE = gs.WRAPPER, gs.WRAPPER_FILE   # ./gradlew and gradlew; ./gradlew.bat and gradlew.bat on Windows
GIT_DIR = [str(Path(shutil.which("git")).parent)] if gs.WINDOWS else []   # on a narrowed PATH: for the bash of shell steps


class GustCase(unittest.TestCase):
    """A temp dir as the working directory, its own .gust, gecho on PATH; everything restored afterwards."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.gust = self.tmp / ".gust"
        self._env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._env)))
        os.environ[gs.GUST_DIR_ENV] = str(self.gust)
        os.environ["GIT_CONFIG_GLOBAL"] = os.environ["GIT_CONFIG_SYSTEM"] = os.devnull   # keep this machine's git config out of gust's git calls too
        os.environ["NO_COLOR"] = "1"                                                     # plain argparse help (3.14+), whatever the shell sets
        for name in ("PYTHON_COLORS", "FORCE_COLOR"):                                    # PYTHON_COLORS=1 takes precedence over NO_COLOR
            os.environ.pop(name, None)
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.tmp)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.gecho = self.script("gecho", GECHO)
        os.environ["PATH"] = f"{self.bin}{os.pathsep}{self._env['PATH']}"
        self.out = io.StringIO()

    def script(self, name, code, where=None):
        """A fake binary: Python code after PRELUDE, in where (default: the bin dir on PATH). Returns its path."""
        return write_script(where or self.bin, name, code)

    def fake_gradle_on_path(self, name="gradle"):
        return self.script(name, FAKE_GRADLE)

    def patch(self, name, value):
        """Replace a module attribute for this test."""
        self.addCleanup(setattr, gs, name, getattr(gs, name))
        setattr(gs, name, value)

    def make_run(self, scenario, out_dir, gradle=None, gradle_user_home=None, **kw):
        return gs.Run(scenario, out_dir, gs.shared_home(gradle_user_home), gradle=gradle, out=self.out, **kw)

    def run_scenario(self, scenario, out_dir, gradle=None, stop_daemons=False, gradle_user_home=None, **kw):
        """gs.run_scenario on a Run built from keywords, the console into self.out."""
        return gs.run_scenario(self.make_run(scenario, out_dir, gradle, gradle_user_home, **kw), stop_daemons)

    def scenario_file(self, text, name="s.toml"):
        p = self.tmp / name
        p.write_text(text)
        return p

    def cli(self, *argv, stdin=None):
        """gs.main with stdout and stderr captured; stdin replaced when given. Returns (code, out, err)."""
        out, err = io.StringIO(), io.StringIO()
        real_stdin = sys.stdin
        if stdin is not None:
            sys.stdin = stdin if isinstance(stdin, io.TextIOBase) else io.TextIOWrapper(io.BytesIO(stdin.encode()), "utf-8")
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = gs.main(list(argv))
        finally:
            sys.stdin = real_stdin
        return code, out.getvalue(), err.getvalue()


class LoadScenarioTest(unittest.TestCase):
    def test_minimal(self):
        s = gs.load_scenario(MINIMAL)
        self.assertEqual(s.name, "min")
        self.assertIsNone(s.gradle)
        self.assertEqual(s.files, {})
        self.assertEqual(s.steps, [gs.RunStep(kind="shell", command="true")])
        self.assertEqual(s.steps[0].expect, gs.Expect())
        self.assertEqual(s.counts(), {"gradle": 0, "shell": 1, "write": 0, "edit": 0, "delete": 0, "clear": 0})

    def test_all_step_kinds(self):
        s = gs.load_scenario("""
name = "x"
description = "d"
[setup.layout.project]
"a/b.txt" = "hello"
[[steps]]
name = "fails"
run.gradle = { args = "build", expect = "fail" }
[[steps]]
run.shell = { command = "false", expect = "fail" }
[[steps]]
run.gradle = "help"
[[steps]]
[steps.write]
"a/b.txt" = "bye"
"c.txt" = "c"
[[steps]]
[[steps.edit]]
file = "a/b.txt"
replace = "b"
with = "B"
[[steps.edit]]
file = "a/b.txt"
replace = "y"
with = "Y"
""")
        self.assertEqual(s.files, {"a/b.txt": "hello"})
        self.assertEqual(s.description, "d")
        self.assertEqual(s.steps[0], gs.RunStep(kind="gradle", command="build", expect=gs.Expect(exit="fail"), name="fails"))
        self.assertEqual(s.steps[1], gs.RunStep(kind="shell", command="false", expect=gs.Expect(exit="fail")))
        self.assertEqual(s.steps[2], gs.RunStep(kind="gradle", command="help"))
        self.assertEqual(s.steps[3], gs.WriteStep(files={"a/b.txt": "bye", "c.txt": "c"}))
        self.assertEqual(s.steps[4].edits, [gs.Edit("a/b.txt", "b", "B"), gs.Edit("a/b.txt", "y", "Y")])
        self.assertEqual(s.counts(), {"gradle": 2, "shell": 1, "write": 1, "edit": 1, "delete": 0, "clear": 0})

    def test_delete_and_clear(self):
        s = gs.load_scenario('name = "x"\n[setup.layout.project]\n"build/a/b.txt" = "b"\n"out/c.txt" = "c"\n'
                             '[[steps]]\nclear = "build/"\n[[steps]]\nname = "n"\ndelete = "./out"\n')
        self.assertEqual(s.steps, [gs.DeleteStep(kind="clear", dir="build"), gs.DeleteStep(kind="delete", dir="out", name="n")])
        self.assertEqual([st.label() for st in s.steps], ["Clear: build", "n"])
        self.assertEqual(s.counts(), {"gradle": 0, "shell": 0, "write": 0, "edit": 0, "delete": 1, "clear": 1})
        self.assertEqual(gs.load_scenario('[[steps]]\ndelete = \'a\\b/c\'').steps[0].dir, "a/b/c")   # \ separates everywhere; unknown is fine
        S = 'name = "x"\n[[steps]]\n'
        for kind in ("delete", "clear"):                                                 # refused on every system, not only where they are roots
            for path in ("/abs", "C:\\abs", "C:/abs", "C:rel", "\\abs", "\\\\host\\share\\x", "//host/share/x",
                         "..", "../x", "a/../b", "a/..", "a\\..\\b", ""):
                self.assert_rejects(S + f"{kind} = '{path}'", f"steps[1].{kind}: path must be relative")
            for path in ("~", "~/x", "~user/x"):
                self.assert_rejects(S + f"{kind} = '{path}'", f"steps[1].{kind}: a leading '~' is refused, as a shell reads it as a home dir")
            for path in ("...", ".. ", "build.", "a/b /c", ".. /x"):                     # ".. " is ".." on Windows
                self.assert_rejects(S + f"{kind} = '{path}'", "is not a valid name on Windows: it ends in a dot or a space")
            for path in (".git", ".git/objects", "sub/.git", "sub/.GIT", ".Git\\hooks"):
                self.assert_rejects(S + f"{kind} = '{path}'", f"steps[1].{kind}: a git repository's .git is never changed by a scenario, got {path!r}")
            for path in (".", "./", ".\\", "./."):
                self.assert_rejects(S + f"{kind} = '{path}'", f"steps[1].{kind}: {path!r} is the project dir itself; name a directory in it")
            self.assert_rejects(S + f'{kind} = "a\\u0000b"', f"steps[1].{kind}: path must not contain a NUL character")
        self.assertEqual(gs.load_scenario(S + 'delete = ".github/x.git"').steps[0].dir, ".github/x.git")   # only a name .git itself
        self.assert_rejects(S + '[steps.write]\n"a\\u0000b" = "x"', "path must not contain a NUL character")                  # writes too
        self.assert_rejects(S + 'delete = ["a"]', "steps[1].delete: must be str")
        self.assert_rejects(S + 'delete = "a"\nclear = "a"', "exactly one of 'run', 'write', 'edit', 'delete', or 'clear'")

    def test_expect_table(self):
        s = gs.load_scenario("""
name = "x"
[[steps]]
run.gradle = { args = "test", expect = { exit = "fail", output = ["FAILED", "1 test"], no_output = ["BUILD SUCCESSFUL"] } }
[[steps]]
run.shell = { command = "true", expect = { output = ["x"] } }
""")
        self.assertEqual(s.steps[0].expect, gs.Expect(exit="fail", output=("FAILED", "1 test"), no_output=("BUILD SUCCESSFUL",)))
        self.assertEqual(s.steps[1].expect, gs.Expect(exit="pass", output=("x",)))

    def test_expect_checks(self):
        e = gs.Expect(exit="fail", output=("a", "b"), no_output=("z",))
        def results(exit_code, text):
            return [c["result"] for c in e.checks(exit_code, text)]
        self.assertEqual(e.checks(1, "a b"), [                                                    # every check, in order
            {"check": "exit", "expect": "fail", "got": "fail", "result": "expected"},
            {"check": "output", "text": "a", "result": "expected"},
            {"check": "output", "text": "b", "result": "expected"},
            {"check": "no_output", "text": "z", "result": "expected"}])
        self.assertEqual(results(0, "a b"), ["unexpected", "expected", "expected", "expected"])
        self.assertEqual(results(2, "a z"), ["expected", "expected", "unexpected", "unexpected"])
        self.assertEqual(results(0, ""), ["unexpected", "unexpected", "unexpected", "expected"])
        self.assertEqual(gs.Expect().checks(0, ""), [{"check": "exit", "expect": "pass", "got": "pass", "result": "expected"}])
        self.assertEqual([gs.describe_check(c) for c in e.checks(0, "z") if c["result"] == "unexpected"],
                         ["expected fail, got pass", "output lacks 'a'", "output lacks 'b'", "output has 'z', which no_output forbids"])

    def test_implied_labels(self):
        s = gs.load_scenario("""
name = "x"
[setup.layout.project]
"src/T.java" = "ac"
[[steps]]
run.gradle = "test --info"
[[steps]]
run.shell = "$GRADLE help"
[[steps]]
[steps.write]
"a/b.txt" = "1"
"c.kts" = "2"
[[steps]]
[[steps.edit]]
file = "src/T.java"
replace = "a"
with = "b"
[[steps.edit]]
file = "src/T.java"
replace = "c"
with = "d"
[[steps]]
name = "custom"
run.shell = "true"
""")
        self.assertEqual([st.label() for st in s.steps],
                         ["Run: gradle test --info", "Run: $GRADLE help", "Write: b.txt, c.kts", "Edit: T.java", "custom"])

    def assert_rejects(self, text, fragment):
        with self.assertRaises(gs.GustError) as cm:
            gs.load_scenario(text)
        self.assertIn(fragment, str(cm.exception))

    def test_rejections(self):
        S = 'name = "x"\n'
        self.assert_rejects("name = ", "not valid TOML")
        self.assert_rejects('name = 1\n[[steps]]\nrun.shell = "x"', "top level.name: must be str")
        self.assert_rejects(S + 'readme = "gone"\n[[steps]]\nrun.shell = "x"', "top level: unknown key(s) readme")
        self.assert_rejects(S, "no steps")
        self.assert_rejects(S + 'bogus = 1\n[[steps]]\nrun.shell = "x"', "unknown key(s) bogus")
        self.assert_rejects(S + '[files]\n"a" = "b"\n[[steps]]\nrun.shell = "x"', "unknown key(s) files")
        self.assert_rejects(S + '[layout.project]\n"a" = "b"\n[[steps]]\nrun.shell = "x"', "top level: unknown key(s) layout")
        self.assert_rejects(S + 'gradle = "wrapper"\n[[steps]]\nrun.shell = "x"', "top level: unknown key(s) gradle")
        self.assert_rejects(S + 'setup = 1\n[[steps]]\nrun.shell = "x"', "setup: must be a table")
        self.assert_rejects(S + '[setup]\nbogus = 1\n[[steps]]\nrun.shell = "x"', "setup: unknown key(s) bogus")
        self.assert_rejects(S + '[setup.layout.home]\n"a" = "b"\n[[steps]]\nrun.shell = "x"', "setup.layout: unknown key(s) home")
        self.assert_rejects(S + 'setup.layout.checkout = "x"\n[[steps]]\nrun.shell = "x"', "setup.layout: unknown key(s) checkout")
        self.assert_rejects(S + 'setup.layout = 1\n[[steps]]\nrun.shell = "x"', "setup.layout: must be a table")
        self.assert_rejects(S + '[setup.layout.project]\n"/abs" = "c"\n[[steps]]\nrun.shell = "x"', "must be relative")
        if gs.WINDOWS:                                                                    # a drive or a backslash root
            for path in ("C:/abs", "C:rel", "\\\\abs"):
                self.assert_rejects(S + f'[setup.layout.project]\n"{path}" = "c"\n[[steps]]\nrun.shell = "x"', "must be relative")
        self.assert_rejects(S + '[setup.layout.project]\n"a/../b" = "c"\n[[steps]]\nrun.shell = "x"', "must not contain '..'")
        self.assert_rejects(S + '[setup.layout.project]\n"a" = 1\n[[steps]]\nrun.shell = "x"', "content must be a string")
        self.assert_rejects(S + '[[steps]]\nrun.shell = "x"\n[steps.write]\n"y" = "z"', "exactly one of 'run', 'write', 'edit', 'delete', or 'clear'")
        self.assert_rejects(S + '[[steps]]\nname = "n"', "exactly one of 'run', 'write', 'edit', 'delete', or 'clear'")
        self.assert_rejects(S + '[[steps]]\nrun = "x"', "steps[1].run: must be run.gradle or run.shell")
        self.assert_rejects(S + '[[steps]]\nrun.gradle = "a"\nrun.shell = "b"', "exactly one of 'gradle' or 'shell'")
        self.assert_rejects(S + '[[steps]]\nrun.make = "a"', "steps[1].run: unknown key(s) make")
        self.assert_rejects(S + '[[steps]]\nrun.gradle = { args = "x", expect = "maybe" }', "steps[1].run.gradle.expect.exit: must be 'pass' or 'fail'")
        self.assert_rejects(S + '[[steps]]\nrun.gradle = { args = "x", expect = 1 }', "steps[1].run.gradle.expect: must be 'pass', 'fail', or a table")
        self.assert_rejects(S + '[[steps]]\nrun.gradle = { args = "x", expect = { exit = "pass", contains = ["a"] } }', "steps[1].run.gradle.expect: unknown key(s) contains")
        self.assert_rejects(S + '[[steps]]\nrun.gradle = { args = "x", expect = { output = "a" } }', "steps[1].run.gradle.expect.output: must be an array of non-empty strings")
        self.assert_rejects(S + '[[steps]]\nrun.gradle = { args = "x", expect = { no_output = [""] } }', "steps[1].run.gradle.expect.no_output: must be an array")
        self.assert_rejects(S + '[[steps]]\nrun.gradle = { command = "x" }', "steps[1].run.gradle: unknown key(s) command")
        self.assert_rejects(S + '[[steps]]\nrun.shell = { args = "x" }', "steps[1].run.shell: unknown key(s) args")
        self.assert_rejects(S + '[[steps]]\nrun.shell = { expect = "fail" }', "missing required key 'command'")
        self.assert_rejects(S + '[[steps]]\nexpect = "fail"\nrun.shell = "x"', "steps[1]: unknown key(s) expect")
        self.assert_rejects(S + '[[steps]]\n[steps.write]', "steps[1].write: no files")
        self.assert_rejects(S + '[[steps]]\nedit = []', "steps[1].edit: must be a non-empty array")
        self.assert_rejects(S + '[[steps]]\n[[steps.edit]]\nfile = "f"\nreplace = "a"', "missing required key 'with'")
        self.assert_rejects(S + '[[steps]]\n[[steps.edit]]\nfile = "f"\nreplace = ""\nwith = "b"', "replace: must not be empty")
        self.assert_rejects(S + '[[steps]]\n[[steps.edit]]\nfile = "../f"\nreplace = "a"\nwith = "b"', "steps[1].edit[1].file")
        self.assert_rejects('name = "has space"\n[[steps]]\nrun.shell = "x"', "name: must match")
        self.assert_rejects('name = "-lead"\n[[steps]]\nrun.shell = "x"', "name: must match")

    def test_gradle_args_that_cannot_be_split_are_refused(self):
        for run, where in (("run.gradle = 'help \"x'", "steps[1].run.gradle"),
                           ("run.gradle = { args = \"help 'x\" }", "steps[1].run.gradle.args")):
            with self.subTest(run=run):
                self.assert_rejects(f"[[steps]]\n{run}", f"{where}: cannot split")       # on every system, not only on Windows

    def test_a_nul_in_a_path_is_refused(self):
        for key in ("setup.project", "setup.layout.base"):
            with self.subTest(key=key):
                with self.assertRaises(gs.GustError) as cm:                             # not a ValueError from the OS
                    gs.load_scenario(f'{key} = "~\\u0000"\n[[steps]]\nrun.shell = "x"', base_dir=Path.cwd())
                self.assertEqual(str(cm.exception), f"{key}: path must not contain a NUL character")

    def test_name_inferred(self):
        nameless = '[[steps]]\nrun.shell = "x"\n'
        self.assertEqual(gs.load_scenario(nameless).name, "scenario")
        self.assertEqual(gs.load_scenario(nameless, default_name="testing-loop").name, "testing-loop")
        self.assertEqual(gs.load_scenario(nameless, default_name="has space").name, "has space")   # a stem is used as is
        self.assertEqual(gs.load_scenario('name = "own"\n' + nameless, default_name="file").name, "own")
        self.assert_rejects('name = "has space"\n' + nameless, "name: must match")                    # explicit: still a slug

    def test_static_file_flow(self):
        S = 'name = "x"\n'
        self.assert_rejects(S + '[[steps]]\n[[steps.edit]]\nfile = "f"\nreplace = "a"\nwith = "b"',
                            "steps[1].edit[1].file: 'f' is not in the layout and no earlier step writes it")
        self.assert_rejects(S + '[[steps]]\n[[steps.edit]]\nfile = "f"\nreplace = "a"\nwith = "b"\n[[steps]]\n[steps.write]\n"f" = "a"',
                            "steps[1].edit[1].file")
        self.assert_rejects(S + '[setup.layout.project]\n"a" = "1"\n"a/b" = "2"\n[[steps]]\nrun.shell = "x"',
                            "setup.layout.project: 'a/b' conflicts with 'a' (setup.layout.project)")
        self.assert_rejects(S + '[setup.layout.project]\n"a/b" = "2"\n[[steps]]\n[steps.write]\n"a" = "1"',
                            "steps[1].write: 'a' is a directory ('a/b' in setup.layout.project)")
        # fine: edit after write, overwrite of a known file, sibling paths
        gs.load_scenario(S + '[setup.layout.project]\n"a/b" = "1"\n"a/c" = "2"\n[[steps]]\n[steps.write]\n"f" = "a"\n"a/b" = "x"\n'
                         '[[steps]]\n[[steps.edit]]\nfile = "f"\nreplace = "a"\nwith = "b"')

    def test_static_file_flow_of_delete_and_clear(self):
        L = 'name = "x"\n[setup.layout.project]\n"build/a/b.txt" = "b"\n"f" = "x"\n'
        edit = '[[steps]]\nedit = [{ file = "build/a/b.txt", replace = "b", with = "c" }]\n'
        self.assert_rejects(L + '[[steps]]\ndelete = "build"\n' + edit, "steps[2].edit[1].file: 'build/a/b.txt' is not in the layout")
        self.assert_rejects(L + '[[steps]]\nclear = "build/a"\n' + edit, "steps[2].edit[1].file")
        self.assert_rejects(L + '[[steps]]\nclear = "f"', "steps[1].clear: 'f' is a file (setup.layout.project), not a directory")
        self.assert_rejects(L + '[[steps]]\nclear = "build"\n[[steps]]\nwrite.build = "x"',
                            "steps[2].write: 'build' is a directory (after steps[1].clear)")                  # a cleared dir stays
        self.assert_rejects(L + '[[steps]]\ndelete = "build/a"\n[[steps]]\nwrite.build = "x"',
                            "steps[2].write: 'build' is a directory (after steps[1].delete)")                 # and so do the dirs above
        # fine: unknown dirs, a file where a deleted dir was, a file written again into a cleared dir
        gs.load_scenario(L + '[[steps]]\nclear = "nope/libs"\n[[steps]]\ndelete = "build"\n[[steps]]\nclear = "build/a"\n'
                             '[[steps]]\ndelete = "build"\n[[steps]]\nwrite.build = "now a file"')
        gs.load_scenario(L + '[[steps]]\nclear = "build"\n[[steps]]\nwrite."build/a/b.txt" = "again"\n' + edit)

    # File paths, read by Windows rules on every system: regressions, one test each.

    def test_an_edit_finds_a_layout_file_spelled_with_a_leading_dot_slash(self):
        s = gs.load_scenario('[setup.layout.project]\n"./a.txt" = "a"\n[[steps]]\nedit = [{ file = "a.txt", replace = "a", with = "b" }]\n')
        self.assertEqual(list(s.files), ["a.txt"])

    def test_two_spellings_of_one_path_in_a_table_are_refused(self):
        self.assert_rejects('[setup.layout.project]\n"src/b.txt" = "1"\n"./src/b.txt" = "2"\n[[steps]]\nrun.shell = "x"',
                            "setup.layout.project: './src/b.txt' and 'src/b.txt' are the same path")

    def test_a_file_spelled_with_a_dot_slash_and_a_dir_of_the_same_name_are_refused(self):
        self.assert_rejects('[setup.layout.project]\n"./a" = "1"\n"a/b.txt" = "2"\n[[steps]]\nrun.shell = "x"',
                            "setup.layout.project: 'a/b.txt' conflicts with 'a' (setup.layout.project)")

    def test_a_write_of_the_project_dir_itself_is_refused(self):
        for path in (".", "./", ".\\", "./."):
            self.assert_rejects(f"[[steps]]\nwrite = {{ '{path}' = 'x' }}", f"steps[1].write.{path!r}: {path!r} is the project dir itself")

    def test_paths_that_differ_in_case_only_are_refused(self):
        L = '[setup.layout.project]\n"a/b.txt" = "1"\n"src/main/x.txt" = "2"\n'
        for more, message in (
                ('"A/b.txt" = "3"\n[[steps]]\nrun.shell = "x"', "setup.layout.project: 'A' and 'a' differ in case or Unicode form only, so they are one"),
                ('"Src/main/y.txt" = "3"\n[[steps]]\nrun.shell = "x"', "setup.layout.project: 'Src' and 'src' differ in case or Unicode form only"),
                ('[[steps]]\nwrite."A/B.txt" = "3"', "steps[1].write: 'A' and 'a' differ in case or Unicode form only"),
                ('[[steps]]\nwrite."a/B.txt" = "3"', "steps[1].write: 'a/B.txt' and 'a/b.txt' differ in case or Unicode form only"),
                ('[[steps]]\ndelete = "SRC/main"', "steps[1].delete: 'SRC' and 'src' differ in case or Unicode form only"),
                ('[[steps]]\nedit = [{ file = "a/B.txt", replace = "1", with = "2" }]', "steps[1].edit[1].file: 'a/B.txt' and 'a/b.txt' differ"),
                ('[[steps]]\nclear = "gen/out"\n[[steps]]\nwrite."GEN/x.txt" = "3"', "steps[2].write: 'GEN' and 'gen' differ")):
            with self.subTest(more=more):
                self.assert_rejects(L + more, message)
        gs.load_scenario(L + '[[steps]]\ndelete = "a"\n[[steps]]\nwrite."A/b.txt" = "3"')   # once a is gone, A is no variant

    def test_windows_paths_are_read_as_such_on_every_system(self):
        for path in (r"..\..\x.txt", r"a\..\x.txt"):
            self.assert_rejects(f"[setup.layout.project]\n'{path}' = 'x'\n[[steps]]\nrun.shell = 'x'", "must not contain '..'")
        for path in (r"C:\y.txt", "C:y.txt", r"\y.txt", r"\\host\share\y.txt", "C:/y.txt"):
            self.assert_rejects(f"[[steps]]\nwrite = {{ '{path}' = 'x' }}", "steps[1].write.")
            self.assert_rejects(f"[[steps]]\nedit = [{{ file = '{path}', replace = 'a', with = 'b' }}]", "steps[1].edit[1].file: path must be relative")
        s = gs.load_scenario("[setup.layout.project]\n'a\\b.txt' = 'x'\n[[steps]]\nedit = [{ file = 'a/b.txt', replace = 'x', with = 'y' }]\n")
        self.assertEqual(list(s.files), ["a/b.txt"])

    def test_names_that_are_not_valid_on_windows_are_refused(self):
        for path, name, why in (("a./b.txt", "a.", "it ends in a dot or a space"), ("b.txt ", "b.txt ", "it ends in a dot or a space"),
                                ("src/nul.txt", "nul.txt", "it is a device name there"), ("COM1", "COM1", "it is a device name there"),
                                ("a/b:c.txt", "b:c.txt", "it has the character ':'"), ("what?.txt", "what?.txt", "it has the character '?'"),
                                ("x\ty", "x\ty", "it has the character '\\t'")):
            with self.subTest(path=path):
                key = json.dumps(path)
                self.assert_rejects(f"[setup.layout.project]\n{key} = 'x'\n[[steps]]\nrun.shell = 'x'",
                                    f"setup.layout.project.{path!r}: {name!r} is not a valid name on Windows: {why}")
                self.assert_rejects(f"[[steps]]\ndelete = {key}", f"steps[1].delete: ")
        gs.load_scenario("[setup.layout.project]\n'console.txt' = 'x'\n'.hidden' = 'x'\n'a b/c.txt' = 'x'\n[[steps]]\nrun.shell = 'x'")

    def test_two_unicode_forms_of_one_name_are_refused(self):
        nfc, nfd = "caf\u00e9.txt", "cafe\u0301.txt"                                    # one name on macOS
        self.assert_rejects(f'[setup.layout.project]\n"{nfc}" = "1"\n"{nfd}" = "2"\n[[steps]]\nrun.shell = "x"',
                            f"setup.layout.project: {nfd!r} and {nfc!r} differ in case or Unicode form only")
        self.assert_rejects(f'[setup.layout.project]\n"{nfc}" = "1"\n[[steps]]\nwrite."CAFE\u0301.TXT" = "2"', "steps[1].write: ")

    def test_a_drive_is_named_in_the_refusal(self):
        for path, drive in (("a:b", "a:"), ("C:y.txt", "C:"), ("C:\\y.txt", "C:")):
            self.assert_rejects(f"[setup.layout.project]\n'{path}' = 'x'\n[[steps]]\nrun.shell = 'x'",
                                f"setup.layout.project.{path!r}: path must be relative, but {drive!r} is a drive on Windows")

    def test_a_file_path_that_ends_in_a_separator_is_refused(self):
        for path in ("src/", "src\\"):
            self.assert_rejects(f"[[steps]]\nwrite = {{ '{path}' = 'x' }}", f"steps[1].write.{path!r}: {path!r} ends in a separator; name a file")
        self.assertEqual(gs.load_scenario('[[steps]]\nclear = "build/"').steps[0].dir, "build")       # a dir may

    def test_dot_git_is_refused_in_every_file_path(self):
        for step in ('[setup.layout.project]\n".git/config" = "x"\n[[steps]]\nrun.shell = "x"', '[[steps]]\nwrite."sub/.GIT/hooks/pre-commit" = "x"',
                     '[[steps]]\nedit = [{ file = ".git/config", replace = "a", with = "b" }]'):
            self.assert_rejects(step, "a git repository's .git is never changed by a scenario, got ")
        gs.load_scenario('[[steps]]\nwrite.".github/x.git" = "x"')                         # only a name .git itself


class LayoutBaseTest(GustCase):
    def setUp(self):
        super().setUp()
        (self.tmp / "layouts").mkdir()
        self.base = self.tmp / "layouts" / "java.toml"
        self.base.write_text("""
[project]
"settings.gradle.kts" = "base settings"
"src/A.java" = "base A"
"src/B.java" = "base B"
""")

    def load(self, text, base_dir=None):
        return gs.load_scenario(text, base_dir=self.tmp if base_dir is None else base_dir)

    def test_a_base_that_is_not_utf8_is_refused(self):
        self.base.write_bytes('[project]\n"a.txt" = "caf\u00e9"\n'.encode("latin-1"))
        with self.assertRaises(gs.GustError) as cm:
            self.load('setup.layout.base = "layouts/java.toml"\n[[steps]]\nrun.shell = "true"\n')
        self.assertEqual(str(cm.exception), f"setup.layout.base: {self.base} is not UTF-8 text")

    def test_base_files_merged_and_overwritten(self):
        s = self.load("""
name = "x"
[setup.layout]
base = "layouts/java.toml"
[setup.layout.project]
"src/B.java" = "own B"
"src/C.java" = "own C"
[[steps]]
run.shell = "true"
""")
        self.assertEqual(s.files, {
            "settings.gradle.kts": "base settings", "src/A.java": "base A", "src/B.java": "own B", "src/C.java": "own C"})

    def test_base_alone_and_dotted_form(self):
        s = self.load('name = "x"\nsetup.layout.base = "layouts/java.toml"\n[[steps]]\nrun.shell = "true"')
        self.assertEqual(set(s.files), {"settings.gradle.kts", "src/A.java", "src/B.java"})

    def test_absolute_base(self):
        s = self.load(f'name = "x"\nsetup.layout.base = "{self.base.as_posix()}"\n[[steps]]\nrun.shell = "true"', base_dir=Path("/nonexistent"))
        self.assertIn("src/A.java", s.files)

    def test_base_satisfies_edit(self):
        s = self.load("""
name = "x"
setup.layout.base = "layouts/java.toml"
[[steps]]
edit = [{ file = "src/A.java", replace = "base", with = "edited" }]
""")
        self.assertEqual(s.steps[0].edits[0].file, "src/A.java")

    def assert_rejects(self, text, fragment, base_dir=None):
        with self.assertRaises(gs.GustError) as cm:
            self.load(text, base_dir)
        self.assertIn(fragment, str(cm.exception))

    def test_rejections(self):
        N, T = 'name = "x"\n', '\n[[steps]]\nrun.shell = "true"\n'
        self.assert_rejects(N + 'setup.layout.base = "layouts/nope.toml"' + T, "setup.layout.base: no such file")
        self.assert_rejects(N + 'setup.layout.base = 1' + T, "setup.layout.base: must be str")
        with self.assertRaises(gs.GustError) as cm:
            gs.load_scenario(N + 'setup.layout.base = "layouts/java.toml"' + T)
        self.assertIn("no directory to resolve it against", str(cm.exception))
        (self.tmp / "layouts" / "chain.toml").write_text('base = "java.toml"\n[project]\n"a" = "b"')
        self.assert_rejects(N + 'setup.layout.base = "layouts/chain.toml"' + T, "has a base of its own; only one level")
        (self.tmp / "layouts" / "bad-table.toml").write_text('[home]\n"a" = "b"')
        self.assert_rejects(N + 'setup.layout.base = "layouts/bad-table.toml"' + T, "setup.layout.base (bad-table.toml): unknown key(s) home")
        (self.tmp / "layouts" / "bad-content.toml").write_text('[project]\n"a" = 1')
        self.assert_rejects(N + 'setup.layout.base = "layouts/bad-content.toml"' + T, "setup.layout.base (bad-content.toml).project.'a': content must be a string")
        (self.tmp / "layouts" / "broken.toml").write_text('[project')
        self.assert_rejects(N + 'setup.layout.base = "layouts/broken.toml"' + T, "is not valid TOML")
        self.assert_rejects(N + 'setup.layout.base = "layouts/java.toml"\n[setup.layout.project]\n"src/A.java/x" = "c"' + T,
                            "'src/A.java/x' conflicts with 'src/A.java'")

    def test_cli_resolves_base_against_scenario_dir(self):
        scenario = self.scenario_file('name = "s"\nsetup.layout.base = "layouts/java.toml"\n[[steps]]\nrun.shell = "test -f src/A.java"')
        code, out, _ = self.cli("run", str(scenario), "--keep-daemons")
        self.assertEqual(code, gs.EXIT_OK, out)
        self.assertEqual((self.tmp / "s.out" / "project" / "src" / "A.java").read_text(), "base A")

    def test_readme_quick_start_scenario_parses(self):
        readme = Path(gs.__file__).with_name("README.md").read_text(encoding="utf-8")
        toml = re.search(r"```toml\n(.*?)```", readme, re.S).group(1)
        s = gs.load_scenario(toml, default_name="loop")
        self.assertEqual(s.name, "loop")
        self.assertEqual([type(st).__name__ for st in s.steps], ["RunStep", "EditStep", "RunStep"])
        self.assertIn("build.gradle.kts", s.files)


class LayoutRemoteTest(GustCase):
    """Uses a local git repository as the remote; no network."""

    @classmethod
    def setUpClass(cls):
        cls._repo_dir = tempfile.TemporaryDirectory()
        cls.repo = Path(cls._repo_dir.name) / "repo"
        cls.repo.mkdir()
        (cls.repo / "settings.gradle.kts").write_text('rootProject.name = "remote"\n')
        write_script(cls.repo, "gradlew", FAKE_GRADLE)
        (cls.repo / "sub").mkdir()
        (cls.repo / "sub" / "build.gradle.kts").write_text("// sub\n")
        (cls.repo / "sub" / "note.txt").write_text("from remote\n")
        cls.git("init", "-q")
        cls.git("-c", "user.name=t", "-c", "user.email=t@t", "add", ".")
        cls.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "one")
        cls.sha = cls.git("rev-parse", "HEAD")
        cls.url = cls.repo.as_posix()        # as written in a scenario: a backslash would start a TOML escape

    @classmethod
    def tearDownClass(cls):
        cls._repo_dir.cleanup()

    @classmethod
    def git(cls, *a):
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}   # no signing, hooks, templates
        return subprocess.run(["git", *a], cwd=cls.repo, check=True, capture_output=True, text=True, env=env).stdout.strip()

    def setUp(self):
        super().setUp()
        self.scenario = self.tmp / "s.toml"

    def write(self, layout_lines, steps='[[steps]]\nrun.shell = "true"\n'):
        self.scenario.write_text('name = "s"\n' + layout_lines + "\n" + steps)
        return self.scenario

    def load(self, layout_lines, steps='[[steps]]\nrun.shell = "true"\n'):
        return gs.load_scenario(self.write(layout_lines, steps).read_text(), base_dir=self.tmp)

    def test_link_forms(self):
        r = gs.load_scenario('name = "s"\nsetup.layout.remote = "https://github.com/gradle/gradle/tree/0123abcd/platforms/core"\n[[steps]]\nrun.shell = "true"', base_dir=self.tmp).remote
        self.assertEqual((r.url, r.sha, r.subdir), ("https://github.com/gradle/gradle.git", "0123abcd", "platforms/core"))
        self.assertEqual(r.dir, self.gust / "remotes" / "github.com-gradle-gradle-0123abcd")
        self.assertEqual(r.identity, "github.com-gradle-gradle-0123abcd")
        r = gs.load_scenario('name = "s"\nsetup.layout.remote = "https://github.com/o/r/commit/0123abcd"\n[[steps]]\nrun.shell = "true"', base_dir=self.tmp).remote
        self.assertEqual((r.url, r.sha, r.subdir), ("https://github.com/o/r.git", "0123abcd", ""))
        r = gs.load_scenario('name = "s"\nsetup.layout.remote = "git@gitlab.com:o/r.git#0123abcd/sub/dir"\n[[steps]]\nrun.shell = "true"', base_dir=None).remote
        self.assertEqual((r.url, r.sha, r.subdir), ("git@gitlab.com:o/r.git", "0123abcd", "sub/dir"))
        self.assertEqual(r.identity, "git-gitlab.com-o-r-0123abcd")

    def assert_rejects(self, text, fragment):
        with self.assertRaises(gs.GustError) as cm:
            gs.load_scenario('name = "s"\n' + text + '\n[[steps]]\nrun.shell = "true"', base_dir=self.tmp)
        self.assertIn(fragment, str(cm.exception))

    def test_rejections(self):
        self.assert_rejects('setup.layout.remote = "https://example.com/x"', "unsupported link")
        self.assert_rejects('setup.layout.remote = "https://github.com/o/r/tree/main"', "'main' is not a commit hash")
        self.assert_rejects('setup.layout.remote = "https://github.com/o/r#v1.0"', "'v1.0' is not a commit hash")
        self.assert_rejects('setup.layout.remote = "https://github.com/o/r/tree/0123abcd/../x"', "setup.layout.remote subdirectory")
        self.assert_rejects('setup.layout.remote = "https://github.com/o/r/tree/0123abcd"\nsetup.layout.base = "b.toml"', "base and remote cannot be combined")
        self.assert_rejects('setup.layout.remote = "https://github.com/o/r/tree/0123abcd"\nsetup.layout.checkout = "x"', "unknown key(s) checkout")

    def test_fetch_copy_reuse_and_layering(self):
        layout = f'setup.layout.remote = "{self.url}#{self.sha}"\n[setup.layout.project]\n"sub/note.txt" = "own"\n"extra.txt" = "x"'
        steps = '[[steps]]\nedit = [{ file = "settings.gradle.kts", replace = "remote", with = "edited" }]\n[[steps]]\nrun.shell = "test ' + ("-f " if gs.WINDOWS else "-x ") + WRAPPER_FILE + '"\n'   # Windows has no mode bits
        scenario = gs.load_scenario(self.write(layout, steps).read_text(), base_dir=self.tmp)
        out_dir = self.tmp / "s.out"
        summary = self.run_scenario(scenario, out_dir, gradle="gecho", stop_daemons=False)
        self.assertEqual(summary.result, "success", self.out.getvalue())
        checkout = self.gust / "remotes" / scenario.remote.identity
        self.assertTrue((checkout / ".git").is_dir())
        self.assertEqual(summary.remote, {"link": f"{self.url}#{self.sha}", "checkout": str(checkout), "fetched": True})
        printed = self.out.getvalue()
        self.assertTrue(printed.startswith(f"remote:   {self.url}#{self.sha}\ncheckout: {checkout}\n          fetching {self.sha} from {self.url}\nscenario: s\n"))
        self.assertLess(printed.index("checkout: "), printed.index("out:      "))
        project = out_dir / "project"
        self.assertFalse((project / ".git").exists())
        self.assertEqual((project / "sub" / "note.txt").read_text(), "own")            # own file wins
        self.assertEqual((project / "sub" / "build.gradle.kts").read_text(), "// sub\n")  # remote file kept
        self.assertEqual((project / "extra.txt").read_text(), "x")
        self.assertIn("edited", (project / "settings.gradle.kts").read_text())          # edit on a remote file
        self.assertTrue(os.access(project / WRAPPER_FILE, os.X_OK))                     # mode preserved
        # second run reuses the checkout
        self.out = io.StringIO()
        summary = self.run_scenario(scenario, out_dir, gradle="gecho", stop_daemons=False)
        self.assertFalse(summary.remote["fetched"])
        self.assertIn(f"checkout: {checkout} (cached)\n", self.out.getvalue())
        self.assertNotIn("fetching", self.out.getvalue())

    def test_subdir_and_relative_gradle(self):
        scenario = self.load(f'setup.layout.remote = "{self.url}#{self.sha}/sub"', '[[steps]]\nrun.shell = "test -f build.gradle.kts && test ! -e sub"\n')
        summary = self.run_scenario(scenario, self.tmp / "s.out", gradle="gecho", stop_daemons=False)
        self.assertEqual(summary.result, "success")
        self.assertTrue((self.gust / "remotes" / scenario.remote.identity / ".git").is_dir())
        # the repository root has gradlew, so a relative --gradle is accepted, probed and works as a gradle step
        scenario = self.load(f'setup.layout.remote = "{self.url}#{self.sha}"', '[[steps]]\nrun.gradle = { args = "help", expect = { output = ["gradlew help"] } }\n')
        summary = self.run_scenario(scenario, self.tmp / "t.out", gradle=WRAPPER, stop_daemons=False)
        self.assertEqual(summary.result, "success")
        self.assertIn(f"gradle:   {WRAPPER} 9.7.1 (--gradle)", self.out.getvalue())

    def test_checks_after_resolve(self):
        scenario = self.load(f'setup.layout.remote = "{self.url}#{self.sha}"', '[[steps]]\nedit = [{ file = "nope.txt", replace = "a", with = "b" }]\n')
        with self.assertRaises(gs.GustError) as cm:
            self.run_scenario(scenario, self.tmp / "s.out", gradle="gecho", stop_daemons=False)
        self.assertIn("'nope.txt' is not in the layout", str(cm.exception))
        self.assertFalse((self.tmp / "s.out").exists())
        scenario = self.load(f'setup.layout.remote = "{self.url}#{self.sha}"\n[setup.layout.project]\n"sub/build.gradle.kts/x" = "c"')
        with self.assertRaises(gs.GustError) as cm:
            self.run_scenario(scenario, self.tmp / "s.out", gradle="gecho", stop_daemons=False)
        self.assertIn("conflicts with 'sub/build.gradle.kts' (setup.layout.remote)", str(cm.exception))
        scenario = self.load(f'setup.layout.remote = "{self.url}#{self.sha}/missing"')
        with self.assertRaises(gs.GustError) as cm:
            self.run_scenario(scenario, self.tmp / "s.out", gradle="gecho", stop_daemons=False)
        self.assertIn("'missing' is not a directory", str(cm.exception))
        scenario = self.load(f'setup.layout.remote = "{self.url}#{"f" * 40}"')
        with self.assertRaises(gs.GustError) as cm:
            self.run_scenario(scenario, self.tmp / "s.out", gradle="gecho", stop_daemons=False)
        self.assertIn("git ", str(cm.exception))
        self.assertFalse((self.gust / "remotes" / scenario.remote.identity).exists())

    def test_delete_and_clear_with_remote(self):
        layout = f'setup.layout.remote = "{self.url}#{self.sha}"'
        scenario = self.load(layout, '[[steps]]\nclear = "sub"\n[[steps]]\nrun.shell = "test -d sub && test ! -e sub/note.txt"\n')
        summary = self.run_scenario(scenario, self.tmp / "s.out", gradle="gecho")
        self.assertEqual(summary.result, "success", self.out.getvalue())
        self.assertTrue((scenario.remote.dir / "sub" / "note.txt").is_file())                 # the checkout is left as it is
        for steps, message in (
                ('[[steps]]\ndelete = "sub"\n[[steps]]\nedit = [{ file = "sub/note.txt", replace = "remote", with = "x" }]\n',
                 "steps[2].edit[1].file: 'sub/note.txt' is not in the layout"),
                ('[[steps]]\ndelete = "settings.gradle.kts"\n', "steps[1].delete: 'settings.gradle.kts' is a file (setup.layout.remote), not a directory")):
            with self.assertRaises(gs.GustError) as cm:
                self.run_scenario(self.load(layout, steps), self.tmp / "t.out", gradle="gecho")
            self.assertIn(message, str(cm.exception))
        self.assertFalse((self.tmp / "t.out").exists())

    def test_mismatched_checkout_refused(self):
        import dataclasses
        scenario = self.load(f'setup.layout.remote = "{self.url}#{self.sha}"')
        self.assertTrue(gs.fetch_remote(scenario, out=self.out)["fetched"])
        short = self.load(f'setup.layout.remote = "{self.url}#{self.sha[:12]}"')
        self.assertNotEqual(short.remote.dir, scenario.remote.dir)                  # identity is the link as written
        self.assertTrue(gs.fetch_remote(short, out=self.out)["fetched"])            # so a short sha is its own checkout
        self.assertFalse(gs.fetch_remote(short, out=self.out)["fetched"])           # and HEAD (full sha) satisfies the prefix
        self.assertIsNone(gs.fetch_remote(gs.load_scenario(MINIMAL)))               # no remote: nothing to fetch
        # a checkout dir whose HEAD is some other commit is refused, never refetched over
        other = dataclasses.replace(scenario, remote=gs.Remote(link="x", url=str(self.repo), sha="0123abcd", subdir=""))
        shutil.copytree(scenario.remote.dir, other.remote.dir, symlinks=True)
        with self.assertRaises(gs.GustError) as cm:
            gs.fetch_remote(other, out=self.out)
        self.assertIn("not 0123abcd; delete it to refetch", str(cm.exception))
        # a non-git directory in the way is refused too
        junk = dataclasses.replace(scenario, remote=gs.Remote(link="x", url=str(self.repo), sha="0123abce", subdir=""))
        junk.remote.dir.mkdir(parents=True)
        (junk.remote.dir / "file").write_text("")
        with self.assertRaises(gs.GustError) as cm:
            gs.fetch_remote(junk, out=self.out)
        self.assertIn("not a git checkout, so it is left alone", str(cm.exception))

    def test_a_bad_cached_checkout_leaves_nothing_on_stdout(self):
        good = self.write(f'setup.layout.remote = "{self.url}#{self.sha}"')
        self.assertEqual(self.cli("run", str(good), "--keep-daemons")[0], gs.EXIT_OK)    # cached now
        checkout = gs.load_scenario(good.read_text(), base_dir=self.tmp).remote.dir
        # check verifies the cached checkout's HEAD, as run does
        other = self.scenario_file(f'setup.layout.remote = "{self.url}#0123abcd"\n[[steps]]\nrun.shell = "true"\n', "other.toml")
        shutil.copytree(checkout, gs.load_scenario(other.read_text(), base_dir=self.tmp).remote.dir, symlinks=True)
        for command in ("check", "run"):
            with self.subTest(command=command):
                code, out, err = self.cli(command, str(other))
                self.assertEqual((code, out), (gs.EXIT_ERROR, ""))
                self.assertIn(", not 0123abcd; delete it to refetch", err)
        # a file flow that fails against the checkout prints no remote: line first
        missing = self.write(f'setup.layout.remote = "{self.url}#{self.sha}"',
                             '[[steps]]\nedit = [{ file = "missing.txt", replace = "a", with = "b" }]\n')
        for command in ("check", "run"):
            with self.subTest(command=command):
                code, out, err = self.cli(command, str(missing))
                self.assertEqual((code, out), (gs.EXIT_ERROR, ""))
                self.assertIn("'missing.txt' is not in the layout", err)

    def test_a_cached_remote_prints_nothing_before_a_failed_gradle_check(self):
        s = self.write(f'setup.layout.remote = "{self.url}#{self.sha}"')
        self.assertEqual(self.cli("run", str(s), "--keep-daemons")[0], gs.EXIT_OK)       # cached now
        for command in ("check", "run"):
            with self.subTest(command=command):
                code, out, err = self.cli(command, str(s), "--gradle", "nosuch")
                self.assertEqual((code, out), (gs.EXIT_ERROR, ""))                     # the remote lines come with the header
                self.assertIn("--gradle 'nosuch': not found on PATH", err)

    def test_a_long_url_gets_a_checkout_dir_name_within_the_limit(self):
        url = "https://example.com/" + "a" * 300 + ".git"                               # longer than any file name may be
        os.environ.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0=f"url.{self.url}.insteadOf",   # fetched from the fixture
                          GIT_CONFIG_VALUE_0=url)
        s = self.write(f'setup.layout.remote = "{url}#{self.sha}"')
        code, out, err = self.cli("run", str(s), "--keep-daemons")
        self.assertEqual((code, err), (gs.EXIT_OK, ""), out)
        checkout = gs.load_scenario(s.read_text(), base_dir=self.tmp).remote.dir
        self.assertEqual(checkout.parent, self.gust / "remotes")
        self.assertRegex(checkout.name, rf"^example\.com-a+-[0-9a-f]{{12}}-{self.sha}$")    # a readable prefix, a hash of the URL
        self.assertLessEqual(len(checkout.name), gs.REMOTE_NAME_MAX)
        self.assertIn(f"checkout: {checkout}\n", out)
        code, out, _ = self.cli("run", str(s), "--keep-daemons")                         # the same URL, the same dir
        self.assertIn(f"checkout: {checkout} (cached)\n", out)

    def test_a_checkout_dir_that_cannot_be_made_is_one_error_line(self):
        s = self.write(f'setup.layout.remote = "{self.url}#{self.sha}"')
        self.gust.mkdir()
        (self.gust / "remotes").write_text("")                                          # a file where the remotes dir goes
        code, out, err = self.cli("run", str(s), "--keep-daemons")
        self.assertEqual((code, out), (gs.EXIT_ERROR, ""))                               # no progress lines ahead of it
        self.assertTrue(err.startswith(f"ERROR: cannot use {self.gust / 'remotes'}"), err)
        self.assertIn("as the checkout dir: ", err)
        self.assertEqual(len(err.splitlines()), 1, err)

    def test_check_refuses_a_checkout_dir_that_is_no_checkout(self):
        s = self.write(f'setup.layout.remote = "{self.url}#{self.sha}"')
        junk = gs.load_scenario(s.read_text(), base_dir=self.tmp).remote.dir
        junk.mkdir(parents=True)
        (junk / "file").write_text("")
        for command in ("check", "run"):                                                 # as run refuses it
            with self.subTest(command=command):
                code, out, err = self.cli(command, str(s))
                self.assertEqual((code, out), (gs.EXIT_ERROR, ""))
                self.assertIn("is not a git checkout, so it is left alone", err)

    def test_check_verifies_a_cached_checkout_has_the_subdir(self):
        s = self.write(f'setup.layout.remote = "{self.url}#{self.sha}"')
        self.assertEqual(self.cli("run", str(s), "--keep-daemons")[0], gs.EXIT_OK)       # cached now
        checkout = gs.load_scenario(s.read_text(), base_dir=self.tmp).remote.dir
        s = self.write(f'setup.layout.remote = "{self.url}#{self.sha}/nosuch"')            # the same checkout
        code, out, err = self.cli("check", str(s))
        self.assertEqual((code, out), (gs.EXIT_ERROR, ""))
        self.assertIn(f"setup.layout.remote: 'nosuch' is not a directory in {checkout}", err)

    def test_check_takes_a_project_binary_from_an_uncached_remote(self):
        s = self.write(f'setup.layout.remote = "{self.url}#{self.sha}"', '[[steps]]\nrun.gradle = "help"\n')
        code, out, err = self.cli("check", str(s), "--gradle", WRAPPER)              # in the commit, not fetched yet
        self.assertEqual((code, err), (gs.EXIT_OK, ""))
        self.assertIn(f"gradle:   {WRAPPER} (--gradle; not checked, remote not cached)\n", out)
        self.assertEqual(self.cli("run", str(s), "--gradle", WRAPPER, "--keep-daemons")[0], gs.EXIT_OK)   # as run takes it

    def test_gradle_wrapper_field(self):
        scenario = self.load(f'setup.gradle = "wrapper"\nsetup.layout.remote = "{self.url}#{self.sha}"',
                             '[[steps]]\nrun.gradle = { args = "help", expect = { output = ["gradlew help"] } }\n')
        self.assertEqual(scenario.gradle, "wrapper")
        summary = self.run_scenario(scenario, self.tmp / "s.out", stop_daemons=False)   # no --gradle
        self.assertEqual(summary.result, "success")
        self.assertEqual(summary.gradle, WRAPPER)
        self.assertEqual(summary.gradle_version, "9.7.1")                              # from the probe, like any binary
        self.assertIsNone(summary.install)
        printed = self.out.getvalue()
        self.assertIn(f"gradle:   {WRAPPER} 9.7.1 (scenario)\n[1/1]", printed)
        self.assertNotIn("[WRP]", printed)
        summary = self.run_scenario(scenario, self.tmp / "t.out", gradle="gecho", stop_daemons=False)   # CLI wins
        self.assertEqual(summary.gradle, "gecho")
        self.assertEqual(summary.steps[0].command, "gecho help")                                # shown without the home pair
        self.assertEqual((self.tmp / "t.out" / "step-01.log").read_text().strip(), f"{home_arg()} help")   # run with it
        self.assertIn("gradle:   gecho 9.7.1 (--gradle)", self.out.getvalue())

    def test_gradle_wrapper_field_without_gradlew(self):
        scenario = self.load(f'setup.gradle = "wrapper"\nsetup.layout.remote = "{self.url}#{self.sha}/sub"', '[[steps]]\nrun.gradle = "help"\n')
        with self.assertRaises(gs.GustError) as cm:
            self.run_scenario(scenario, self.tmp / "s.out", stop_daemons=False)
        self.assertIn(f'setup.gradle = "wrapper" but {WRAPPER_FILE} is not among the project\'s files', str(cm.exception))
        self.assertFalse((self.tmp / "s.out").exists())
        inline = gs.load_scenario(f'name = "i"\nsetup.gradle = "wrapper"\n[setup.layout.project]\n"{WRAPPER_FILE}" = "not run"\n[[steps]]\nrun.gradle = "help"')
        with self.assertRaises(gs.GustError) as cm:
            self.run_scenario(inline, self.tmp / "i.out", stop_daemons=False)
        self.assertIn("cannot come from an inline layout", str(cm.exception))
        tabled = self.load(f'setup.gradle = "wrapper"\nsetup.layout.remote = "{self.url}#{self.sha}/sub"\n'
                           f'[setup.layout.project]\n"{WRAPPER_FILE}" = "not run"', '[[steps]]\nrun.gradle = "help"\n')
        with self.assertRaises(gs.GustError) as cm:                                      # in the table, not in the checkout
            self.run_scenario(tabled, self.tmp / "t.out", stop_daemons=False)
        self.assertEqual(str(cm.exception), f'setup.gradle "wrapper": {WRAPPER_FILE} is in the project table alone, which is laid '
                                            f'out only after Gradle is probed; the wrapper must be in the remote checkout')
        with self.assertRaises(gs.GustError) as cm:
            gs.load_scenario('name = "s"\nsetup.gradle = "latest"\n[[steps]]\nrun.shell = "true"')
        self.assertIn("setup.gradle: must be 'wrapper' or a Gradle version such as '9.9.0', got 'latest'", str(cm.exception))
        with self.assertRaises(gs.GustError):
            gs.load_scenario('name = "s"\nsetup.gradle = 1\n[[steps]]\nrun.shell = "true"')

    def test_version_installs_wrapper_via_gradle_on_path(self):
        self.fake_gradle_on_path()
        scenario = gs.load_scenario('name = "v"\nsetup.gradle = "9.7.1"\n[setup.layout.project]\n"settings.gradle.kts" = ""\n[[steps]]\nrun.gradle = { args = "help", expect = { output = ["gradlew help"] } }', base_dir=self.tmp)
        out_dir = self.tmp / "v.out"
        summary = self.run_scenario(scenario, out_dir, stop_daemons=True)
        self.assertEqual(summary.result, "success")
        self.assertEqual(summary.gradle, WRAPPER)
        self.assertEqual(summary.gradle_version, "9.7.1")
        self.assertEqual(summary.install, {"version": "9.7.1", "log": "install-wrapper.log", "exit_code": 0})
        self.assertTrue(os.access(out_dir / "project" / WRAPPER_FILE, os.X_OK))
        self.assertIn("installed wrapper 9.7.1 via gradle", (out_dir / "install-wrapper.log").read_text())
        self.assertEqual(sorted(p.name for p in self.gust.iterdir()), ["shared-gradle-user-home"])   # no remote: no remotes dir
        printed = self.out.getvalue()
        self.assertNotIn("--gradle-user-home", printed)                                          # implied everywhere a command is shown
        self.assertRegex(printed, rf"gradle:   {re.escape(WRAPPER)} 9\.7\.1 \(scenario, installed by gradle\)\n"
                                  r"\[WRP\] Install Gradle wrapper 9\.7\.1\n\$ gradle wrapper --gradle-version 9\.7\.1 --no-daemon\n"
                                  r"exit=0 in \d+\.\ds -> install-wrapper\.log\n"
                                  r"\[BEF\] Stop Gradle daemons -> stop-daemons-before\.log\n\[1/1\]")
        self.assertIn(f"home:     {self.gust / gs.SHARED_HOME}\n", printed)
        self.assertEqual((out_dir / "stop-daemons-before.log").read_text().strip(), "gradlew --stop")
        # a remote with its own wrapper still gets the requested version installed, with gradle from PATH
        scenario = self.load(f'setup.layout.remote = "{self.url}#{self.sha}"', '[[steps]]\nrun.gradle = "help"\n')
        self.out = io.StringIO()
        summary = self.run_scenario(scenario, self.tmp / "s.out", gradle="9.8.0-rc-2", stop_daemons=False)
        self.assertEqual(summary.result, "success")
        self.assertEqual(summary.install, {"version": "9.8.0-rc-2", "log": "install-wrapper.log", "exit_code": 0})
        self.assertIn("installed wrapper 9.8.0-rc-2 via gradle", (self.tmp / "s.out" / "install-wrapper.log").read_text())
        self.assertIn(f"gradle:   {WRAPPER} 9.8.0-rc-2 (--gradle, installed by gradle)\n[WRP]", self.out.getvalue())
        self.assertIn("$ gradle wrapper --gradle-version 9.8.0-rc-2 --no-daemon\n", self.out.getvalue())

    def test_version_precedence_and_rejections(self):
        self.fake_gradle_on_path()
        scenario = gs.load_scenario('name = "v"\nsetup.gradle = "9.7.1"\n' + SETTINGS + '[[steps]]\nrun.gradle = "help"', base_dir=self.tmp)
        # --gradle with a binary overrides the scenario's version: no install
        summary = self.run_scenario(scenario, self.tmp / "a.out", gradle="gecho", stop_daemons=False)
        self.assertIsNone(summary.install)
        self.assertEqual(summary.gradle, "gecho")
        # a --gradle version overrides the scenario's version
        summary = self.run_scenario(scenario, self.tmp / "b.out", gradle="9.7.0", stop_daemons=False)
        self.assertEqual(summary.install["version"], "9.7.0")
        self.assertIn(f"gradle:   {WRAPPER} 9.7.0 (--gradle, installed by gradle)", self.out.getvalue())
        with self.assertRaises(gs.GustError) as cm:
            self.run_scenario(scenario, self.tmp / "d.out", gradle="latest", stop_daemons=False)
        self.assertEqual(str(cm.exception), "--gradle 'latest': not found on PATH and not a Gradle version such as 9.9.0")
        self.assertFalse((self.tmp / "d.out").exists())

    def test_gradle_value_resolution(self):
        self.fake_gradle_on_path()
        scenario = gs.load_scenario('name = "v"\n' + SETTINGS + '[[steps]]\nrun.gradle = "help"', base_dir=self.tmp)

        def refused(gradle, message, **kw):
            with self.assertRaises(gs.GustError) as cm:
                self.run_scenario(scenario, self.tmp / "x.out", gradle=gradle, stop_daemons=False, **kw)
            self.assertEqual(str(cm.exception), message)
            self.assertFalse((self.tmp / "x.out").exists())
        # 1. a "/" is a binary given as a path, never a version
        refused("foo/bar", f"--gradle 'foo/bar': no executable at {self.tmp / 'foo' / 'bar'}")
        refused("/no/such/gradle", f"--gradle '/no/such/gradle': no executable at {Path('/no/such/gradle').resolve()}")   # on Windows, with the drive
        refused("./nope/gradle", "--gradle './nope/gradle' is relative to the project dir but no such file is in the project")
        (self.tmp / "plain").write_text("not executable")
        refused("./plain", "--gradle './plain' is relative to the project dir but no such file is in the project")
        refused(str(self.tmp / "plain"), f"--gradle {str(self.tmp / 'plain')!r}: no executable at {self.tmp / 'plain'}")
        gradle = runnable("gradle")
        summary = self.run_scenario(scenario, self.tmp / "a.out", gradle=f"bin/{gradle}", stop_daemons=False)
        self.assertEqual(summary.gradle, str(self.bin / gradle))            # a relative filesystem path, made absolute
        self.assertIsNone(summary.install)
        self.assertIn(f"gradle:   {self.bin / gradle} 9.7.1 (--gradle)", self.out.getvalue())
        # a leading ~ is expanded before anything else
        os.environ["HOME"] = os.environ["USERPROFILE"] = str(self.tmp)     # USERPROFILE on Windows
        summary = self.run_scenario(scenario, self.tmp / "h.out", gradle=f"~/bin/{gradle}", stop_daemons=False)
        self.assertEqual(Path(summary.gradle), self.bin / gradle)                # C:\...\tmp/bin/gradle.bat on Windows
        refused("~/nope/gradle", f"--gradle {str(self.tmp) + '/nope/gradle'!r}: no executable at {self.tmp / 'nope' / 'gradle'}")
        # 2. no "/": an executable on PATH or in the working directory is a binary
        summary = self.run_scenario(scenario, self.tmp / "b.out", gradle="gradle", stop_daemons=False)
        self.assertEqual(summary.gradle, "gradle")
        self.assertIsNone(summary.install)
        self.assertEqual(summary.steps[0].command, "gradle help")
        self.assertIn("gradle:   gradle 9.7.1 (--gradle)", self.out.getvalue())
        mygradle = self.script("mygradle", FAKE_GRADLE, where=self.tmp)
        summary = self.run_scenario(scenario, self.tmp / "c.out", gradle="mygradle", stop_daemons=False)
        self.assertEqual(summary.gradle, str(mygradle))
        self.assertEqual((self.tmp / "c.out" / "step-01.log").read_text().strip(), "mygradle help")
        # a binary that runs but is no Gradle, or does not run at all, is refused by the --version probe
        self.script("notgradle", NOT_GRADLE)
        refused("notgradle", "--gradle 'notgradle': not a Gradle binary (no 'Gradle <version>' line in the output of --version)")
        broken = self.script("broken", 'print("first", file=sys.stderr)\nprint("boom", file=sys.stderr)\nsys.exit(5)\n', where=self.tmp)
        refused("broken", f"--gradle 'broken': cannot run '{shlex.quote(str(broken))} --version' (first; boom)")
        self.script("gradle", 'print("no version here")\n')
        with self.assertRaises(gs.GustError) as cm:
            self.run_scenario(scenario, self.tmp / "x.out", stop_daemons=False)   # the default is probed too
        self.assertEqual(str(cm.exception), "gradle: not a Gradle binary (no 'Gradle <version>' line in the output of --version)")
        self.fake_gradle_on_path()
        # 3. anything else is a version, installed with gradle from PATH whatever the version string says
        refused("grdl", "--gradle 'grdl': not found on PATH and not a Gradle version such as 9.9.0")
        refused("9", "--gradle '9': not found on PATH and not a Gradle version such as 9.9.0")
        summary = self.run_scenario(scenario, self.tmp / "d.out", gradle="9.99.0", stop_daemons=False)
        self.assertEqual(summary.install, {"version": "9.99.0", "log": "install-wrapper.log", "exit_code": 0})

    def test_version_without_gradle_on_path(self):
        os.environ["PATH"] = str(self.tmp / "empty-bin")
        scenario = gs.load_scenario('name = "v"\nsetup.gradle = "9.7.1"\n' + SETTINGS + '[[steps]]\nrun.gradle = "help"', base_dir=self.tmp)
        with self.assertRaises(gs.GustError) as cm:
            self.run_scenario(scenario, self.tmp / "s.out", stop_daemons=False)
        self.assertEqual(str(cm.exception), "a Gradle version was requested but no 'gradle' is on PATH to install its wrapper")
        self.assertFalse((self.tmp / "s.out").exists())

    def test_version_install_failure(self):
        self.script("gradle", 'if ARGS[2:3] == ["--version"]:\n    print("Gradle 9.7.1")\n    sys.exit(0)\nprint("boom")\nsys.exit(7)\n')
        s = self.scenario_file('name = "v"\nsetup.gradle = "9.7.1"\n' + SETTINGS + '[[steps]]\nrun.gradle = "help"')
        code, out, err = self.cli("run", str(s), "--json")
        self.assertEqual((code, err), (gs.EXIT_ERROR, ""))                                # a failed install: result error, exit 2
        self.assertRegex(out, r"\[WRP\] Install Gradle wrapper 9\.7\.1\n\$ gradle wrapper --gradle-version 9\.7\.1 --no-daemon\nERROR: exit=7 in \d+\.\ds -> install-wrapper\.log\n")
        self.assertIn(f"{rule('last 1 of 1 lines of install-wrapper.log')}\nboom\n{CLOSING}\nresult: ERROR (0/1 steps ran)\nout:      {self.tmp / 's.out'}\n", out)
        self.assertNotIn("[BEF]", out)                                                   # nothing ran after the failed install
        summary = json.loads(out.rstrip("\n").splitlines()[-1])
        self.assertEqual((summary["result"], summary["install"], summary["steps"]), ("error", {"version": "9.7.1", "log": "install-wrapper.log", "exit_code": 7}, []))
        self.assertEqual(summary["error"], "wrapper install failed: exit=7 -> install-wrapper.log")
        self.assertEqual(json.loads((self.tmp / "s.out" / "summary.json").read_text()), summary)
        code, out, _ = self.cli("setup", str(s))
        self.assertEqual(code, gs.EXIT_ERROR)
        self.assertTrue(out.endswith(f"result: ERROR\nout:      {self.tmp / 's.out'}\n"), out[-200:])

    def test_an_install_that_cannot_start_ends_the_run_as_an_error(self):
        self.fake_gradle_on_path()
        s = self.scenario_file('name = "v"\nsetup.gradle = "9.7.1"\n' + SETTINGS + '[[steps]]\nrun.gradle = "help"')
        with mock.patch.object(gs, "_run_logged", side_effect=OSError(2, "No such file or directory", "gradle")):
            code, out, err = self.cli("run", str(s), "--json")
        self.assertEqual((code, err), (gs.EXIT_ERROR, ""))                                # after the header: on stdout
        message = "cannot run gradle wrapper --gradle-version 9.7.1 --no-daemon: No such file or directory: gradle"
        self.assertIn(f"ERROR: {message}\nresult: ERROR (0/1 steps ran)\nout:      {self.tmp / 's.out'}\n", out)
        summary = json.loads((self.tmp / "s.out" / "summary.json").read_text())
        self.assertEqual((summary["result"], summary["error"]), ("error", message))
        with mock.patch.object(gs, "_run_logged", side_effect=OSError(2, "No such file or directory", "gradle")):
            code, out, err = self.cli("setup", str(s))
        self.assertEqual((code, err), (gs.EXIT_ERROR, ""))
        self.assertTrue(out.endswith(f"ERROR: {message}\nresult: ERROR\n"
                                     f"out:      {self.tmp / 's.out'}\n"), out[-200:])

    def test_cli_version_values(self):
        self.fake_gradle_on_path()
        s = self.write(SETTINGS, '[[steps]]\nrun.gradle = "help"\n')
        code, out, _ = self.cli("run", str(s), "--gradle", "9.7.1", "--keep-daemons", "--json")
        self.assertEqual(code, gs.EXIT_OK)
        self.assertEqual(json.loads(out.splitlines()[-1])["install"], {"version": "9.7.1", "log": "install-wrapper.log", "exit_code": 0})
        errors = []
        for argv in (["run", str(s), "--gradle", "nope"], ["run", str(s), "--gradle", "./nope/gradle"],
                     ["stop-daemons", str(s), "--gradle", "9.7.1"]):
            code, _, err = self.cli(*argv)
            self.assertEqual(code, gs.EXIT_ERROR, argv)
            errors.append(err.strip())
        self.assertEqual(errors, [
            "ERROR: --gradle 'nope': not found on PATH and not a Gradle version such as 9.9.0",
            "ERROR: --gradle './nope/gradle' is relative to the project dir but no such file is in the project",
            "ERROR: --gradle '9.7.1' is a version, but nothing is installed here; pass a binary (a name or a path)"])   # the first run set the project up
        code, out, _ = self.cli("setup", str(s), "--gradle", "9.7.0")
        self.assertEqual(code, gs.EXIT_OK)
        self.assertIn("installed wrapper 9.7.0 via gradle", (self.tmp / "s.out" / "install-wrapper.log").read_text())
        self.assertRegex(out, rf"scenario: s\nout:      .*\nhome:     .*\ngradle:   {re.escape(WRAPPER)} 9\.7\.0 \(--gradle, installed by gradle\)\n"
                              r"\[WRP\] Install Gradle wrapper 9\.7\.0\n\$ gradle wrapper .*\nexit=0 in .* -> install-wrapper\.log\nresult: set up\nout:      .*\n")
        gs.rmtree(self.tmp / "s.out")                                             # without a project: the hint comes first
        code, _, err = self.cli("stop-daemons", str(s), "--gradle", "9.7.1")
        self.assertEqual((code, err.strip()), (gs.EXIT_ERROR, f"ERROR: no set-up project at {self.tmp / 's.out' / 'project'}; run 'gust setup {s}' first"))
        code, out, _ = self.cli("setup", str(s), "--gradle", "gecho")            # a binary: nothing to install, but probed
        self.assertEqual(code, gs.EXIT_OK)
        self.assertIn("gradle:   gecho 9.7.1 (--gradle)\n", out)
        self.assertNotIn("[WRP]", out)
        self.assertFalse((self.tmp / "s.out" / "project" / WRAPPER_FILE).exists())
        self.script("notgradle", NOT_GRADLE)
        code, _, err = self.cli("setup", str(s), "--gradle", "notgradle")
        self.assertEqual(code, gs.EXIT_ERROR)
        self.assertIn("not a Gradle binary", err)

    def test_cli_setup_with_remote(self):
        s = self.write(f'setup.layout.remote = "{self.url}#{self.sha}"')
        code, out, _ = self.cli("setup", str(s))
        self.assertEqual(code, gs.EXIT_OK)
        self.assertTrue((self.tmp / "s.out" / "project" / "settings.gradle.kts").is_file())
        self.assertTrue((self.gust / "remotes").is_dir())
        self.assertTrue(out.startswith(f"remote:   {self.url}#{self.sha}\ncheckout: "))
        self.assertTrue(out.endswith(f"result: set up\nout:      {self.tmp / 's.out'}\n"), out[-200:])

    def test_stop_daemons_probes_the_wrapper(self):
        s = self.write(f'setup.gradle = "wrapper"\nsetup.layout.remote = "{self.url}#{self.sha}"', '[[steps]]\nrun.gradle = "help"\n')
        self.assertEqual(self.cli("setup", str(s))[0], gs.EXIT_OK)
        code, out, _ = self.cli("stop-daemons", str(s))
        self.assertEqual(code, gs.EXIT_OK, out)
        self.assertRegex(out, rf"home:     .*\ngradle:   {re.escape(WRAPPER)} 9\.7\.1 \(scenario\)\n"
                              rf"\[STOP\] Stop Gradle daemons\n\$ {re.escape(WRAPPER)} --stop\nexit=0 in \d+\.\ds -> stop-daemons\.log\nout:      ")
        gradlew = write_script(self.tmp / "s.out" / "project", "gradlew", 'print("not gradle")\n')   # the probe of a broken wrapper fails
        code, _, err = self.cli("stop-daemons", str(s))
        self.assertEqual(code, gs.EXIT_ERROR)
        self.assertEqual(err.strip(), f"ERROR: {WRAPPER}: not a Gradle binary (no 'Gradle <version>' line in the output of --version)")
        gradlew.unlink()                                                                     # and a missing one is named
        code, _, err = self.cli("stop-daemons", str(s))
        self.assertEqual((code, err.strip()), (gs.EXIT_ERROR, f'ERROR: setup.gradle = "wrapper" but {WRAPPER_FILE} is not among the project\'s files'))

    def test_version_needs_a_gradle_build(self):
        self.fake_gradle_on_path()
        bare = gs.load_scenario('name = "b"\nsetup.gradle = "9.7.1"\n[[steps]]\nrun.gradle = "help"', base_dir=self.tmp)
        with self.assertRaises(gs.GustError) as cm:
            self.run_scenario(bare, self.tmp / "b.out")
        self.assertEqual(str(cm.exception), "setup.gradle '9.7.1': a wrapper can only be installed into a Gradle build; add a settings file to the layout")
        self.assertFalse((self.tmp / "b.out").exists())
        with self.assertRaises(gs.GustError) as cm:
            self.run_scenario(gs.load_scenario(MINIMAL), self.tmp / "c.out", gradle="9.7.1")
        self.assertEqual(str(cm.exception), "--gradle '9.7.1': a wrapper can only be installed into a Gradle build; add a settings file to the layout")
        for settings in ("settings.gradle", "settings.gradle.kts", "settings.gradle.dcl"):
            ok = gs.load_scenario(f'name = "b"\nsetup.gradle = "9.7.1"\n[setup.layout.project]\n"{settings}" = ""\n[[steps]]\nrun.gradle = "help"', base_dir=self.tmp)
            self.assertEqual(self.run_scenario(ok, self.tmp / "d.out").result, "success", settings)
        # the remote checkout's settings file counts too
        remote = self.load(f'setup.gradle = "9.7.1"\nsetup.layout.remote = "{self.url}#{self.sha}"', '[[steps]]\nrun.gradle = "help"\n')
        self.assertEqual(self.run_scenario(remote, self.tmp / "e.out").result, "success")
        code, _, err = self.cli("setup", "-", "--gradle", "9.7.1", "--out", str(self.tmp / "f.out"), stdin='[[steps]]\nrun.gradle = "help"\n')
        self.assertEqual(code, gs.EXIT_ERROR)
        self.assertEqual(err.strip(), "ERROR: --gradle '9.7.1': a wrapper can only be installed into a Gradle build; add a settings file to the layout")

    def test_scenario_version_is_probed_once_set_up(self):
        self.fake_gradle_on_path()                                                    # its --version says 9.7.1 whatever was requested
        s = self.scenario_file('setup.gradle = "9.7.0"\n' + SETTINGS + '[[steps]]\nrun.gradle = "help"\n')
        code, out, _ = self.cli("setup", str(s))
        self.assertEqual(code, gs.EXIT_OK, out)
        self.assertIn(f"gradle:   {WRAPPER} 9.7.0 (scenario, installed by gradle)\n", out)      # setup: the requested version, as given
        code, out, _ = self.cli("stop-daemons", str(s))
        self.assertEqual(code, gs.EXIT_OK, out)
        self.assertIn(f"gradle:   {WRAPPER} 9.7.1 (scenario)\n", out)                           # once set up: the wrapper, probed

    def test_show_output_frames_the_install_block(self):
        self.fake_gradle_on_path()
        s = self.scenario_file('setup.gradle = "9.7.1"\n' + SETTINGS + '[[steps]]\nrun.shell = "true"\n')
        for argv in (["run", str(s), "--keep-daemons", "--show-output"], ["setup", str(s), "--show-output"]):
            code, out, _ = self.cli(*argv)
            self.assertEqual(code, gs.EXIT_OK, out)
            self.assertIn(f"[WRP] Install Gradle wrapper 9.7.1\n$ gradle wrapper --gradle-version 9.7.1 --no-daemon\n{rule('install-wrapper.log')}\n"
                          f"installed wrapper 9.7.1 via gradle\n{CLOSING}\nexit=0 in ", out, argv)

    def test_wrapper_in_the_project_is_the_default(self):
        self.fake_gradle_on_path()
        # a remote that brings its own gradlew, no setup.gradle: the wrapper runs, probed, noted (default)
        scenario = self.load(f'setup.layout.remote = "{self.url}#{self.sha}"', '[[steps]]\nrun.gradle = { args = "help", expect = { output = ["gradlew help"] } }\n')
        summary = self.run_scenario(scenario, self.tmp / "a.out")
        self.assertEqual((summary.result, summary.gradle, summary.gradle_version), ("success", WRAPPER, "9.7.1"))
        self.assertIn(f"gradle:   {WRAPPER} 9.7.1 (default)\n", self.out.getvalue())
        # --gradle gradle overrides the wrapper
        self.out = io.StringIO()
        summary = self.run_scenario(scenario, self.tmp / "b.out", gradle="gradle")
        self.assertEqual(summary.gradle, "gradle")
        self.assertIn("gradle:   gradle 9.7.1 (--gradle)\n", self.out.getvalue())
        self.assertEqual((self.tmp / "b.out" / "step-01.log").read_text().strip(), "gradle help")
        # a layout without gradlew falls to PATH
        self.out = io.StringIO()
        summary = self.run_scenario(gs.load_scenario('name = "p"\n' + SETTINGS + '[[steps]]\nrun.gradle = "help"'), self.tmp / "c.out")
        self.assertEqual(summary.gradle, "gradle")
        self.assertIn("gradle:   gradle 9.7.1 (default)\n", self.out.getvalue())
        # once a wrapper is installed by a run (--gradle 9.7.1), stop-daemons uses it with no flag
        s = self.scenario_file('name = "w"\n' + SETTINGS + '[[steps]]\nrun.gradle = "help"\n')
        code, out, _ = self.cli("run", str(s), "--gradle", "9.7.1", "--keep-daemons")
        self.assertEqual(code, gs.EXIT_OK, out)
        code, out, _ = self.cli("stop-daemons", str(s))
        self.assertEqual(code, gs.EXIT_OK, out)
        self.assertIn(f"gradle:   {WRAPPER} 9.7.1 (default)\n[STOP] Stop Gradle daemons\n$ {WRAPPER} --stop\n", out)
        # check on an uncached remote without setup.gradle: the default is unknown, so noted, not probed
        gs.rmtree(self.gust / "remotes")
        code, out, _ = self.cli("check", str(self.write(f'setup.layout.remote = "{self.url}#{self.sha}"', '[[steps]]\nrun.gradle = "help"\n')))
        self.assertEqual(code, gs.EXIT_OK, out)
        self.assertIn("gradle:   gradle (default; not checked, remote not cached)\n", out)

    def test_check_json_has_no_null_keys(self):
        s = self.scenario_file('[[steps]]\nrun.shell = "true"\n', "plain.toml")        # no description, nothing to probe
        code, out, _ = self.cli("check", str(s), "--json", "--keep-daemons")
        self.assertEqual(code, gs.EXIT_OK)
        self.assertEqual(json.loads(out.rstrip("\n").splitlines()[-1]),               # empty keys left out, as in run's summary
                         {"scenario": "plain", "gradle": "gradle", "steps": 1,
                          "step_counts": {"gradle": 0, "shell": 1, "write": 0, "edit": 0, "delete": 0, "clear": 0},
                          "result": "success"})

    def test_check_command(self):
        self.fake_gradle_on_path()
        s = self.write(f'description = "Checks the remote."\nsetup.gradle = "wrapper"\nsetup.layout.remote = "{self.url}#{self.sha}"',
                       '[[steps]]\nrun.gradle = "help"\n[[steps]]\nrun.shell = "true"\n[[steps]]\nedit = [{ file = "sub/note.txt", replace = "remote", with = "x" }]\n')
        # not cached: noted, nothing fetched or written
        code, out, _ = self.cli("check", str(s))
        self.assertEqual(code, gs.EXIT_OK, out)
        self.assertEqual(out, f"remote:   {self.url}#{self.sha} (not cached; fetched on run)\nscenario: s\n          Checks the remote.\n"
                              f"home:     {self.gust / gs.SHARED_HOME}\ngradle:   {WRAPPER} (scenario; not checked, remote not cached)\n"
                              f"steps: 3 (1 gradle, 1 shell, 0 write, 1 edit, 0 delete, 0 clear)\n")          # no result: line on success, and no out dir, so no out: line
        self.assertFalse((self.gust / "remotes").exists())
        self.assertFalse((self.tmp / "s.out").exists())
        # cached by a run: the wrapper is probed like any binary
        self.assertEqual(self.cli("run", str(s), "--keep-daemons")[0], gs.EXIT_OK)
        gs.rmtree(self.tmp / "s.out")
        code, out, _ = self.cli("check", str(s), "--json")
        self.assertEqual(code, gs.EXIT_OK, out)
        self.assertIn(f"checkout: {self.gust / 'remotes' / gs.load_scenario(s.read_text(), base_dir=self.tmp).remote.identity} (cached)\n", out)
        self.assertIn(f"gradle:   {WRAPPER} 9.7.1 (scenario)\nsteps: 3", out)
        self.assertEqual(json.loads(out.rstrip("\n").splitlines()[-1]),
                         {"scenario": "s", "description": "Checks the remote.", "gradle": WRAPPER, "gradle_version": "9.7.1",
                          "steps": 3, "step_counts": {"gradle": 1, "shell": 1, "write": 0, "edit": 1, "delete": 0, "clear": 0},
                          "remote": {"link": f"{self.url}#{self.sha}", "cached": True}, "result": "success"})
        self.assertFalse((self.tmp / "s.out").exists())
        # a binary is probed, a bad one refused; a version is settled without installing; a load error starts with the file
        code, out, _ = self.cli("check", str(s), "--gradle", "gradle")
        self.assertEqual(code, gs.EXIT_OK)
        self.assertIn("gradle:   gradle 9.7.1 (--gradle)\n", out)
        code, out, _ = self.cli("check", str(s), "--gradle", "9.7.0")
        self.assertEqual(code, gs.EXIT_OK)
        self.assertIn(f"gradle:   {WRAPPER} 9.7.0 (--gradle, installed by gradle)\n", out)
        self.assertFalse((self.tmp / "s.out").exists())
        # an uncached remote: a binary from PATH is still probed, a version is noted as not checked
        gs.rmtree(self.gust / "remotes")
        code, out, _ = self.cli("check", str(s), "--gradle", "gradle", "--json")
        self.assertEqual(code, gs.EXIT_OK)
        self.assertIn("gradle:   gradle 9.7.1 (--gradle)\n", out)
        self.assertEqual(json.loads(out.rstrip("\n").splitlines()[-1])["remote"], {"link": f"{self.url}#{self.sha}", "cached": False})
        code, out, _ = self.cli("check", str(s), "--gradle", "9.7.0")
        self.assertEqual(code, gs.EXIT_OK)
        self.assertIn(f"gradle:   {WRAPPER} 9.7.0 (--gradle, installed by gradle; not checked, remote not cached)\n", out)
        local = self.scenario_file(f'setup.layout.remote = "{self.url}#{self.sha}"\n[setup.layout.project]\n"mygradle" = ""\n'
                                   '[[steps]]\nrun.gradle = "help"\n', "local.toml")            # a project binary, not on disk yet
        code, out, _ = self.cli("check", str(local), "--gradle", "./mygradle")
        self.assertEqual(code, gs.EXIT_OK, out)
        self.assertIn("gradle:   ./mygradle (--gradle; not checked, remote not cached)\n", out)
        self.assertFalse((self.gust / "remotes").exists())
        self.script("notgradle", NOT_GRADLE)
        code, _, err = self.cli("check", str(s), "--gradle", "notgradle")
        self.assertEqual(code, gs.EXIT_ERROR)
        self.assertIn("ERROR: --gradle 'notgradle': not a Gradle binary", err)
        bad = self.scenario_file("name = ", name="bad.toml")
        code, _, err = self.cli("check", str(bad))
        self.assertEqual(code, gs.EXIT_ERROR)
        self.assertTrue(err.startswith(f"ERROR: {bad}: not valid TOML"), err)
        code, out, _ = self.cli("check", "-", "--keep-daemons", stdin='[[steps]]\nrun.shell = "true"\n')
        self.assertEqual(code, gs.EXIT_OK)
        self.assertTrue(out.startswith(f"scenario: scenario\nhome:     {self.gust / gs.SHARED_HOME}\ngradle:   gradle (default)\n"), out)   # a shell-only scenario: nothing to probe


class SetupTest(GustCase):
    def setUp(self):
        super().setUp()
        self.out_dir = self.tmp / "s.out"

    def test_writes_layout_under_project(self):
        s = gs.load_scenario('name = "x"\n[setup.layout.project]\n"a/b/c.txt" = "hi"\n"top.txt" = "t"\n"lf.txt" = "1\\n2\\r\\n"\n'
                             '[[steps]]\nedit = [{ file = "lf.txt", replace = "1", with = "one" }]')
        project = gs.lay_out(s, self.out_dir)
        self.assertEqual(project, self.out_dir / "project")
        self.assertEqual((project / "a/b/c.txt").read_text(), "hi")
        self.assertEqual((project / "top.txt").read_text(), "t")
        self.assertEqual((project / "lf.txt").read_bytes(), b"1\n2\r\n")                  # as written, on Windows too
        gs._apply_edit(project, s.steps[0].edits[0])
        self.assertEqual((project / "lf.txt").read_bytes(), b"one\n2\r\n")                 # after an edit: line ends kept, everywhere
        self.assertEqual((self.out_dir / gs.MARKER).read_text(),
                         "Marker for gust: this directory was produced by gust and may be replaced or removed by a later run.\n")

    def test_edit_keeps_line_ends(self):
        path = self.tmp / "f.txt"
        for before, replace, with_, after in [
            (b"a\r\nb\r\nc\r\n", "a\nb", "x\ny\nz", b"x\r\ny\r\nz\r\nc\r\n"),      # CRLF: a bare \n in with stands for CRLF
            (b"a\r\nb\r\nc\r\n", "c", "d", b"a\r\nb\r\nd\r\n"),                       # the rest of the file is left as it is
            (b"a\r\nb\r\n", "a\r\nb", "x\r\ny", b"x\r\ny\r\n"),                       # \r\n written out means the same
            (b"a\r\nb", "b", "b\nc", b"a\r\nb\r\nc"),                                 # no final line end
            (b"a\rb\r\nc\r\n", "c", "d", b"a\rb\r\nd\r\n"),                           # a lone \r is kept
            (b"a\rb\n", "a\nb", "x", b"x\n"),                                       # a line end matches a lone \r too
            (b"a\r\nb\r\n", "a\r", "Z", b"Zb\r\n"),                                # CRLF is one line end, never split
            (b"a\r\nb\r\n", "\nb", "\nc", b"a\r\nc\r\n"),                         # replace starting with a line end
            ("\u00e9\r\n\u20ac\r\n".encode(), "\u00e9\n\u20ac", "\u20ac\n\u00e9", "\u20ac\r\n\u00e9\r\n".encode()),   # multi-byte UTF-8
            (b"a\nb\nc\n", "b\nc", "y\n", b"a\ny\n\n"),                               # LF
            (b"a\nb\n", "a\r\nb", "x\r\ny", b"x\r\ny\n"),                             # LF: \r\n in with is written as it is
            (b"a\nb", "a\nb", "c", b"c"),                                             # no final line end
            (b"abc", "b", "1\n2", b"a1\n2c"),                                         # no line end at all: as written
            (b"abc", "b", "1\r\n2", b"a1\r\n2c"),
            (b"\r  \r", "  ", "\n", b"\r\r\r"),                                       # CR: a bare \n in with stands for CR
            (b"a\rb\rc", "a\nb", "x\ny\r\nz", b"x\ry\r\nz\rc"),                       # CR: \r\n in with is written as it is
            (b"a\nb\r\nc\n", "b\nc", "B\nC", b"a\nB\nC\n"),                           # both kinds: with as written
            (b"a\rb\nc", "c", "x\ny", b"a\rb\nx\ny"),                                   # CR and LF: with as written
            (b"a\rb\r\nc", "b\nc", "x\ny", b"a\rx\ny"),                                 # CR and CRLF: with as written
            (b"a\r\nb\nc\n", "a\r\nb", "x\r\ny", b"x\r\ny\nc\n"),
        ]:
            path.write_bytes(before)
            gs._apply_edit(self.tmp, gs.Edit(file="f.txt", replace=replace, with_=with_))
            self.assertEqual(path.read_bytes(), after, (before, replace, with_))
        path.write_bytes(b"a\r\nb\na\nb\n")
        with self.assertRaisesRegex(gs.StepError, r"'a\\nb' occurs 2 times in f.txt"):       # a line end matches either kind
            gs._apply_edit(self.tmp, gs.Edit(file="f.txt", replace="a\nb", with_="x"))
        path.write_bytes(b"a\r\na\r\n")
        with self.assertRaisesRegex(gs.StepError, r"'a' occurs 2 times"):
            gs._apply_edit(self.tmp, gs.Edit(file="f.txt", replace="a", with_="x"))
        self.assertEqual(path.read_bytes(), b"a\r\na\r\n")                                   # untouched on an error
        path.write_bytes(b"a\xff\r\n")
        with self.assertRaisesRegex(gs.StepError, r"^f.txt is not UTF-8 text; nothing to edit$"):
            gs._apply_edit(self.tmp, gs.Edit(file="f.txt", replace="a", with_="x"))
        self.assertEqual(path.read_bytes(), b"a\xff\r\n")

    def test_edit_counts_overlapping_occurrences(self):
        path = self.tmp / "f.txt"
        for before, replace, n in [
            (b"1.0.0.0", "0.0", 2),
            (b"}\n}\n}\n", "}\n}", 2),
            (b"}\r\n}\r\n}\r\n", "}\n}", 2),                                         # with a line end of any kind
            (b"}\r}\r}\r", "}\n}", 2),
            (b"aaa", "aa", 2),
            ("\u00e9.\u00e9.\u00e9".encode(), "\u00e9.\u00e9", 2),                       # multi-byte UTF-8
            (b"a\rb\r\na\rb", "a\nb", 2),                                              # CR and CRLF in one file
        ]:
            path.write_bytes(before)
            with self.assertRaises(gs.StepError) as cm:
                gs._apply_edit(self.tmp, gs.Edit(file="f.txt", replace=replace, with_="X"))
            self.assertEqual(str(cm.exception), f"{replace!r} occurs {n} times in f.txt; an edit needs exactly one")
            self.assertEqual(path.read_bytes(), before)
        for before, replace, after in [
            (b"1.0.0", "0.0", b"1.X"),
            (b"a\r\nb\r\n", "\nb", b"aX\r\n"),                                          # a CRLF is one line end: no second start at its LF
            (b"a\r\n\r\nb", "\n\n", b"aXb"),
        ]:
            path.write_bytes(before)
            gs._apply_edit(self.tmp, gs.Edit(file="f.txt", replace=replace, with_="X"))
            self.assertEqual(path.read_bytes(), after, (before, replace))

    def test_edit_of_a_large_file_is_fast(self):
        started = time.monotonic()
        with self.assertRaisesRegex(gs.StepError, r"occurs 0 times"):          # a search that tried each position took seconds
            gs._edited_text("a" * 4_000_000, gs.Edit(file="f.txt", replace="a" * 1000 + "b", with_="X"))
        self.assertEqual(gs._edited_text("x\r\n" * 1_000_000 + "MARK\r\n", gs.Edit(file="f.txt", replace="MARK", with_="M"))[-3:], "M\r\n")
        self.assertLess(time.monotonic() - started, 1)

    def delete_dir(self, kind, rel, **kw):
        """_delete_dir in <out>/project, laid out empty on the first call; the error message, or what was done."""
        run = self.make_run(gs.load_scenario(MINIMAL), self.out_dir, **kw)
        if not run.project.is_dir():
            gs.lay_out(run.scenario, self.out_dir)
        try:
            return gs._delete_dir(run, gs.DeleteStep(kind=kind, dir=rel))
        except gs.StepError as e:
            return str(e)

    def test_delete_and_clear_read_only_files(self):
        project = gs.lay_out(gs.load_scenario(MINIMAL), self.out_dir)
        for kind in ("clear", "delete"):
            obj = project / "repo" / ".git" / "objects" / "ab" / "cdef"                      # read-only, as git's objects are
            obj.parent.mkdir(parents=True)
            obj.write_text("")
            obj.chmod(0o444)
            self.assertEqual(self.delete_dir(kind, "repo"), {"clear": "cleared", "delete": "deleted"}[kind])
            self.assertEqual([p.name for p in project.rglob("*")], ["repo"] if kind == "clear" else [], kind)   # kept and empty, or gone
        self.assertTrue(project.is_dir())

    def test_end_state_of_a_missing_dir(self):
        project = gs.lay_out(gs.load_scenario(MINIMAL), self.out_dir)
        self.assertEqual(self.delete_dir("delete", "a/b"), "was not there")
        self.assertEqual(self.delete_dir("clear", "a/b"), "no such directory a, so a/b cannot be made")
        self.assertEqual(self.delete_dir("clear", "a"), "created")
        self.assertEqual(self.delete_dir("clear", "a/b"), "created")
        self.assertEqual([p.relative_to(project).as_posix() for p in sorted(project.rglob("*"))], ["a", "a/b"])
        (project / "f").write_text("")
        self.assertEqual([self.delete_dir(kind, "f") for kind in ("delete", "clear")], ["f is a file, not a directory"] * 2)

    def test_delete_and_clear_never_leave_the_project(self):
        project = gs.lay_out(gs.load_scenario(MINIMAL), self.out_dir)
        outside = self.tmp / "outside"
        (outside / "sub").mkdir(parents=True)
        (outside / "sub" / "precious").write_text("")
        (project / "inner" / "x").mkdir(parents=True)
        try:
            (project / "link").symlink_to(outside, target_is_directory=True)
            (project / "to-inner").symlink_to(project / "inner", target_is_directory=True)
            (project / "up").symlink_to(project.parent, target_is_directory=True)
        except OSError:                                                                 # Windows without the privilege
            self.skipTest("symbolic links cannot be made here")
        for kind in ("delete", "clear"):
            for rel, error in (("link", f"link is a link, not a real directory; nothing to {kind}"),
                               ("to-inner", f"to-inner is a link, not a real directory; nothing to {kind}"),
                               ("link/sub", "link/sub is not inside the project dir once links are followed, so it is left alone"),
                               ("up/project", "up/project is not inside the project dir once links are followed, so it is left alone")):
                self.assertEqual(self.delete_dir(kind, rel), error)
        self.assertTrue((outside / "sub" / "precious").is_file())
        self.assertEqual(self.delete_dir("delete", "to-inner/x"), "deleted")           # a link above that stays inside is fine
        self.assertEqual(sorted(p.name for p in project.iterdir()), ["inner", "link", "to-inner", "up"])
        self.assertEqual(list((project / "inner").iterdir()), [])

    @unittest.skipUnless(gs.WINDOWS, "junctions are Windows only")
    def test_a_junction_is_refused(self):
        project = gs.lay_out(gs.load_scenario(MINIMAL), self.out_dir)
        (project / "inner" / "x").mkdir(parents=True)
        subprocess.run(["cmd", "/c", "mklink", "/J", str(project / "j"), str(project / "inner")], check=True, capture_output=True)
        for kind in ("delete", "clear"):
            self.assertEqual(self.delete_dir(kind, "j"), f"j is a link, not a real directory; nothing to {kind}")
        with self.assertRaises(OSError):
            gs.rmtree(project / "j")                                                    # never reported as removed
        self.assertTrue((project / "inner" / "x").is_dir())
        gs.rmtree(project)                                                              # a junction inside a tree: the link goes, its target stays
        self.assertFalse(project.exists())

    def test_rmtree_never_goes_through_a_link(self):
        self.patch("WINDOWS", True)                                                     # the handler for read-only files, on any system
        target = self.tmp / "target"
        (target / "sub").mkdir(parents=True)
        (target / "sub" / "f").write_text("x")
        link = self.tmp / "link"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("symbolic links cannot be made here")
        with self.assertRaises(OSError):
            gs.rmtree(link)
        self.assertEqual(sorted(p.name for p in target.rglob("*")), ["f", "sub"])

    def test_gusts_own_dirs_are_refused(self):
        project = gs.lay_out(gs.load_scenario(MINIMAL), self.out_dir)
        (project / "a" / ".gust").mkdir(parents=True)
        (project / "home").mkdir()
        os.environ[gs.GUST_DIR_ENV] = str(project / "a" / ".gust")
        for rel in ("a", "a/.gust"):
            self.assertEqual(self.delete_dir("delete", rel), f"{rel} overlaps {project / 'a' / '.gust'}, one of gust's own dirs, so it is left alone")
        self.assertEqual(self.delete_dir("clear", "home", gradle_user_home=project / "home"),
                         f"home overlaps {project / 'home'}, one of gust's own dirs, so it is left alone")
        self.assertTrue((project / "a" / ".gust").is_dir())

    def test_empty_layout_still_makes_project_dir(self):
        project = gs.lay_out(gs.load_scenario(MINIMAL), self.out_dir)
        self.assertTrue(project.is_dir())

    def test_existing_marked_out_dir_is_recreated(self):
        gs.prepare_out(self.out_dir)
        (self.out_dir / "step-01.log").write_text("first")
        (self.out_dir / "project").mkdir()
        gs.prepare_out(self.out_dir)                                        # removed and recreated, silently
        self.assertEqual(sorted(p.name for p in self.out_dir.iterdir()), [gs.MARKER])
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["bin", "s.out"])   # no sibling

    def test_empty_out_is_reused(self):
        self.out_dir.mkdir()
        gs.prepare_out(self.out_dir)
        self.assertEqual(sorted(p.name for p in self.out_dir.iterdir()), [gs.MARKER])

    def test_refuses_foreign_non_empty_out(self):
        self.out_dir.mkdir()
        (self.out_dir / "precious").write_text("")
        with self.assertRaises(gs.GustError) as cm:
            gs.lay_out(gs.load_scenario(MINIMAL), self.out_dir)
        self.assertEqual(str(cm.exception), f"out dir is not empty and was not created by gust, so it is left alone: {self.out_dir}; pass another --out or remove it")
        self.assertTrue((self.out_dir / "precious").exists())

    def test_refuses_file_as_out(self):
        self.out_dir.write_text("")
        with self.assertRaises(gs.GustError):
            gs.lay_out(gs.load_scenario(MINIMAL), self.out_dir)

    def test_out_dir_must_not_contain_scenario(self):
        scenario = self.tmp / "s.toml"
        scenario.write_text(MINIMAL)
        with self.assertRaises(gs.GustError) as cm:
            gs.check_out_dir(self.tmp, scenario)
        self.assertIn("would be removed with the earlier run", str(cm.exception))
        gs.check_out_dir(self.tmp / "s.out", scenario)


class RunTest(GustCase):
    def setUp(self):
        super().setUp()
        self.out_dir = self.tmp / "s.out"
        self.project = self.out_dir / "project"

    def run_text(self, text, gradle="gecho", stop_daemons=False, **kw):
        return self.run_scenario(gs.load_scenario(text), self.out_dir, gradle=gradle, stop_daemons=stop_daemons, **kw)

    def test_all_as_expected(self):
        summary = self.run_text("""
name = "loop"
description = "Two lines of\\nnarrative."
[setup.layout.project]
"f.txt" = "one two"
[[steps]]
name = "sees one"
run.shell = "grep -q one f.txt"
[[steps]]
run.shell = { command = "grep -q three f.txt", expect = "fail" }
[[steps]]
[[steps.edit]]
file = "f.txt"
replace = "two"
with = "three"
[[steps]]
run.shell = "grep -q three f.txt"
[[steps]]
[steps.write]
"f.txt" = "four"
"g.txt" = "five"
[[steps]]
run.shell = "grep -q four f.txt && grep -q five g.txt"
""")
        self.assertEqual(summary.result, "success")
        self.assertEqual([s.result for s in summary.steps], ["expected", "expected", "done", "expected", "done", "expected"])
        self.assertEqual([s.kind for s in summary.steps], ["shell", "shell", "edit", "shell", "write", "shell"])
        self.assertEqual((self.project / "f.txt").read_text(), "four")
        self.assertTrue((self.out_dir / "step-01.log").exists())
        self.assertFalse((self.out_dir / "step-03.log").exists())
        self.assertFalse((self.project / "step-01.log").exists())
        printed = self.out.getvalue()
        self.assertTrue(printed.startswith(f"scenario: loop\nout:      {self.out_dir}\nhome:     {self.gust / gs.SHARED_HOME}\ngradle:   gecho 9.7.1 (--gradle)\n"), printed)
        self.assertNotIn("narrative", printed)                                    # the description is for check and the summary only
        self.assertIn("[1/6] sees one\n$ grep -q one f.txt\nEXPECTED: exit=0 in ", printed)
        self.assertIn("[2/6] Run: grep -q three f.txt\n$ grep -q three f.txt\nEXPECTED: exit=1 in ", printed)   # an expected fail
        self.assertIn("[3/6] Edit: f.txt\n1 file edited\n[4/6]", printed)
        self.assertIn("[5/6] Write: f.txt, g.txt\n2 files written\n[6/6]", printed)
        self.assertTrue(printed.endswith(f"result: SUCCESS (6/6 steps ran)\nout:      {self.out_dir}\n"), printed[-200:])   # the out dir once more, last
        for gone in ("UNEXPECTED", "ok:", "ok "):
            self.assertNotIn(gone, printed)
        self.assertNotIn("pass", printed.split("gradle:")[1])                     # no pass/fail word on the console
        self.assertEqual(json.loads((self.out_dir / "summary.json").read_text())["description"], "Two lines of\nnarrative.")

    def test_an_error_inside_the_steps_still_stops_the_daemons_after(self):
        with mock.patch.object(gs, "_change_files", side_effect=gs.GustError("boom")):
            summary = self.run_text('name = "e"\n[[steps]]\nwrite."a.txt" = "a"\n', stop_daemons=True)
        self.assertEqual((summary.result, summary.error), ("error", "boom"))
        self.assertIn("[1/1] Write: a.txt\nERROR: boom\n[AFT] Stop Gradle daemons -> stop-daemons-after.log\n"
                      "result: ERROR (0/1 steps ran)\n", self.out.getvalue())             # the error first, then the stop

    def test_stops_at_unexpected_check_and_shows_tail(self):
        summary = self.run_text("""
name = "dev"
[[steps]]
run.shell = "echo line-a; echo line-b; exit 3"
[[steps]]
run.shell = "touch should-not-exist"
""")
        self.assertEqual(summary.result, "failure")
        self.assertEqual(len(summary.steps), 1)
        self.assertEqual(summary.steps[0].exit_code, 3)
        self.assertEqual(summary.steps[0].result, "unexpected")
        self.assertFalse((self.project / "should-not-exist").exists())
        printed = self.out.getvalue()
        self.assertRegex(printed, r"\$ echo line-a; echo line-b; exit 3\nUNEXPECTED: exit=3 in \d+\.\ds -> step-01\.log\n  expected pass, got fail\n-{40}")
        self.assertEqual(summary.steps[0].checks, [{"check": "exit", "expect": "pass", "got": "fail", "result": "unexpected"}])
        self.assertIn(f"\n{rule('last 2 of 2 lines of step-01.log')}\nline-a\nline-b\n{CLOSING}\nresult: FAILURE (1/2 steps ran)", printed)

    def test_tail_length(self):
        text = 'name = "t"\n[[steps]]\nrun.shell = "echo a; echo b; echo c; exit 1"'
        self.run_text(text, tail=1)
        self.assertIn(f"\n{rule('last 1 of 3 lines of step-01.log')}\nc\n{CLOSING}\n", self.out.getvalue())
        self.out = io.StringIO()
        self.run_scenario(gs.load_scenario(text), self.tmp / "z.out", gradle="gecho", stop_daemons=False, tail=0)
        self.assertIn(f"\n{rule('last 0 of 3 lines of step-01.log')}\n{CLOSING}\n", self.out.getvalue())   # 0 shows nothing, not everything
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            gs.main(["run", str(self.scenario_file(MINIMAL)), "--tail", "-1"])
        self.assertEqual(cm.exception.code, 2)
        self.assertTrue(err.getvalue().startswith("usage: "), err.getvalue())                # a usage error ends in an ERROR: line too
        self.assertRegex(err.getvalue(), r"\nERROR: argument --tail: .*\n$")

    def test_edit_error_stops_run(self):
        summary = self.run_text("""
name = "bad-edit"
[setup.layout.project]
"f.txt" = "aa"
[[steps]]
[[steps.edit]]
file = "f.txt"
replace = "a"
with = "b"
[[steps]]
run.shell = "touch should-not-exist"
""")
        self.assertEqual(summary.result, "error")
        self.assertEqual(summary.steps[0].result, "error")
        self.assertEqual(summary.steps[0].error, "'a' occurs 2 times in f.txt; an edit needs exactly one")
        self.assertEqual((self.project / "f.txt").read_text(), "aa")
        self.assertFalse((self.project / "should-not-exist").exists())
        self.assertIn("[1/2] Edit: f.txt\nERROR: 'a' occurs 2 times in f.txt; an edit needs exactly one\nresult: ERROR (1/2 steps ran)", self.out.getvalue())
        on_disk = json.loads((self.out_dir / "summary.json").read_text())
        self.assertEqual((on_disk["result"], on_disk["error"]), ("error", "'a' occurs 2 times in f.txt; an edit needs exactly one"))
        self.assertEqual(on_disk["steps"], [{"index": 1, "kind": "edit", "name": "Edit: f.txt", "result": "error", "files": ["f.txt"],
                                             "error": "'a' occurs 2 times in f.txt; an edit needs exactly one"}])   # no checks
        self.assertEqual(list(on_disk)[-3:], ["result", "error", "steps"])

    def test_run_step_that_cannot_start_is_an_error(self):
        if gs.WINDOWS:   # the dir a process runs in cannot be removed there, but a Gradle step runs without a shell, so a gone binary fails to start
            gone, gradle = 'run.shell = \'rm -f "$GRADLE"\'\n[[steps]]\nrun.gradle = "help"', str(self.script("goner", GECHO))
        else:            # through sh, a gone binary is exit 127, but a gone project dir fails to start
            gone, gradle = 'run.shell = "cd .. && rm -rf project"\n[[steps]]\nrun.shell = "true"', "gecho"
        text = f'name = "gone"\n[[steps]]\n{gone}\n[[steps]]\nrun.shell = "true"\n'
        summary = self.run_text(text, gradle=gradle, stop_daemons=True)
        self.assertEqual((summary.result, len(summary.steps)), ("error", 2), self.out.getvalue())
        step = summary.steps[1]
        self.assertEqual((step.result, step.checks, step.exit_code, step.log), ("error", None, None, None))
        self.assertTrue(step.error.startswith(f"cannot run {step.command}: "), step.error)
        self.assertEqual(summary.error, step.error)
        self.assertFalse((self.out_dir / "step-02.log").exists())                       # no log for a step that never started
        after = summary.stop_daemons["after"]                                           # the stop after it cannot start either
        self.assertEqual(list(after), ["error"])
        printed = self.out.getvalue()
        self.assertIn(f"\n$ {step.command}\nERROR: {step.error}\n[AFT] Stop Gradle daemons -> stop-daemons-after.log\n"
                      f"ERROR: {after['error']}\nresult: ERROR (2/3 steps ran)\nout:      {self.out_dir}\n", printed)
        on_disk = json.loads((self.out_dir / "summary.json").read_text())
        self.assertEqual((on_disk["error"], on_disk["stop_daemons"]["after"]), (step.error, after))
        # with --show-output: no rule is opened for a step that never started
        self.out = io.StringIO()
        if gs.WINDOWS:
            gradle = str(self.script("goner", GECHO))
        summary = self.run_scenario(gs.load_scenario(text), self.tmp / "so.out", gradle=gradle, show_output=True)
        printed = self.out.getvalue()
        self.assertEqual(summary.result, "error", printed)
        self.assertNotIn(gs.opening_rule("step-02.log"), printed)
        self.assertIn(f"\n$ {summary.steps[1].command}\nERROR: ", printed)

    def test_write_onto_a_directory_is_a_step_error(self):
        summary = self.run_text("""
name = "w"
[[steps]]
run.shell = "mkdir -p f.txt"
[[steps]]
[steps.write]
"f.txt" = "content"
[[steps]]
run.shell = "touch should-not-exist"
""")
        self.assertEqual(summary.result, "error")
        self.assertEqual(summary.steps[1].result, "error")
        self.assertIn("cannot write f.txt: ", summary.steps[1].error)
        self.assertIn("[2/3] Write: f.txt\nERROR: cannot write f.txt: ", self.out.getvalue())
        self.assertIn("result: ERROR (2/3 steps ran)", self.out.getvalue())
        self.assertTrue((self.out_dir / "summary.json").is_file())
        self.assertFalse((self.project / "should-not-exist").exists())

    def test_delete_and_clear(self):
        summary = self.run_text("""
name = "d"
[setup.layout.project]
"build/a.txt" = "a"
"build/sub/b.txt" = "b"
"out/c.txt" = "c"
"keep.txt" = "k"
[[steps]]
clear = "build"
[[steps]]
name = "gone"
delete = "out"
[[steps]]
run.shell = 'test -d build && test -z "$(ls -A build)" && test ! -e out'
""")
        self.assertEqual(summary.result, "success", self.out.getvalue())
        self.assertIn("[1/3] Clear: build\nbuild cleared\n[2/3] gone\nout deleted\n[3/3]", self.out.getvalue())
        self.assertEqual(sorted(p.name for p in self.project.rglob("*")), ["build", "keep.txt"])
        self.assertEqual(json.loads((self.out_dir / "summary.json").read_text())["steps"][:2], [
            {"index": 1, "kind": "clear", "name": "Clear: build", "result": "done", "files": ["build"]},
            {"index": 2, "kind": "delete", "name": "gone", "result": "done", "files": ["out"]}])

    def test_a_missing_dir_is_fine_and_a_file_is_a_step_error(self):
        s = self.scenario_file('name = "m"\n[[steps]]\ndelete = "d"\n[[steps]]\nclear = "d"\n[[steps]]\nrun.shell = "rm -r d && touch d"\n'
                               '[[steps]]\nclear = "d"\n[[steps]]\nrun.shell = "touch should-not-exist"\n')
        code, out, _ = self.cli("run", str(s), "--keep-daemons", "--json")
        self.assertEqual(code, gs.EXIT_ERROR, out)
        self.assertIn("[1/5] Delete: d\nd was not there\n[2/5] Clear: d\nd created\n[3/5]", out)
        self.assertIn("[4/5] Clear: d\nERROR: d is a file, not a directory\nresult: ERROR (4/5 steps ran)", out)
        steps = json.loads(out.splitlines()[-1])["steps"]
        self.assertEqual([steps[0], steps[1], steps[3]], [
            {"index": 1, "kind": "delete", "name": "Delete: d", "result": "done", "files": []},          # nothing was removed
            {"index": 2, "kind": "clear", "name": "Clear: d", "result": "done", "files": ["d"]},
            {"index": 4, "kind": "clear", "name": "Clear: d", "result": "error", "files": ["d"], "error": "d is a file, not a directory"}])
        self.assertFalse((self.out_dir / "project" / "should-not-exist").exists())
        code, out, _ = self.cli("check", str(s), "--keep-daemons")
        self.assertEqual(code, gs.EXIT_OK, out)
        self.assertIn("steps: 5 (0 gradle, 2 shell, 0 write, 0 edit, 1 delete, 2 clear)\n", out)

    @unittest.skipIf(gs.WINDOWS, "ln -s in the bash of Git for Windows copies by default")
    def test_a_link_to_the_project_dir_is_refused_when_the_step_runs(self):
        summary = self.run_text('name = "l"\n[setup.layout.project]\n"f" = "x"\n[[steps]]\nrun.shell = "ln -s .. up"\n'
                                '[[steps]]\nclear = "up/project"\n')
        self.assertEqual(summary.result, "error")
        self.assertEqual(summary.steps[1].error, "up/project is not inside the project dir once links are followed, so it is left alone")
        self.assertEqual(sorted(p.name for p in self.project.iterdir()), ["f", "up"])

    def test_output_checks_read_crlf_as_lf(self):
        summary = self.run_text('name = "crlf"\n[[steps]]\nrun.shell = { command = "printf \'a\\\\r\\\\nb\\\\r\\\\n\'", expect = { output = ["a\\nb"] } }')
        self.assertEqual(summary.result, "success", self.out.getvalue())
        self.assertEqual((self.out_dir / "step-01.log").read_bytes(), b"a\r\nb\r\n")          # the log as printed

    def test_output_assertions(self):
        summary = self.run_text("""
name = "o"
[[steps]]
run.shell = { command = "echo alpha; echo beta", expect = { output = ["alpha", "beta"], no_output = ["gamma"] } }
[[steps]]
run.shell = { command = "echo alpha; echo gamma; exit 1", expect = { exit = "fail", output = ["alpha", "delta"], no_output = ["gamma"] } }
[[steps]]
run.shell = "true"
""")
        self.assertEqual(summary.result, "failure")
        self.assertEqual(len(summary.steps), 2)
        self.assertEqual([c["result"] for c in summary.steps[0].checks], ["expected"] * 4)
        self.assertEqual(summary.steps[1].result, "unexpected")      # the exit check came out as expected, two others did not
        printed = self.out.getvalue()
        self.assertIn("EXPECTED: exit=0, 3 output checks in", printed)
        self.assertRegex(printed, r"UNEXPECTED: exit=1, 3 output checks in \d+\.\ds -> step-02\.log\n  output lacks 'delta'\n  output has 'gamma', which no_output forbids\n-{40}")
        self.assertIn("\ngamma\n", printed)
        on_disk = json.loads((self.out_dir / "summary.json").read_text())
        self.assertEqual(on_disk["steps"][1]["checks"], [                                          # one record per check kind
            {"check": "exit", "expect": "fail", "got": "fail", "result": "expected"},
            {"check": "output", "text": "alpha", "result": "expected"},
            {"check": "output", "text": "delta", "result": "unexpected"},
            {"check": "no_output", "text": "gamma", "result": "unexpected"}])
        self.assertNotIn("expect", on_disk["steps"][1])
        self.assertEqual(on_disk["result"], "failure")
        self.assertNotIn("error", on_disk)                                                            # only with result "error"

    def test_edit_of_file_removed_at_runtime(self):
        summary = self.run_text("""
name = "m"
[setup.layout.project]
"f" = "a"
[[steps]]
run.shell = "rm f"
[[steps]]
[[steps.edit]]
file = "f"
replace = "a"
with = "b"
""")
        self.assertEqual(summary.result, "error")
        self.assertEqual(summary.steps[1].error, "no such file f; nothing to edit")

    def test_gradle_step_quotes_binary(self):
        fake = self.script("gradle", GECHO, where=(self.tmp / "g dir" / "bin"))
        summary = self.run_text('name = "g"\n[[steps]]\nrun.gradle = "help --quiet"', gradle=str(fake))
        self.assertEqual(summary.result, "success")
        self.assertEqual(summary.steps[0].command, f"'{fake}' help --quiet")
        self.assertEqual((self.out_dir / "step-01.log").read_text().strip(), f"{home_arg()} help --quiet")
        self.assertIn(f"gradle:   {fake} 9.7.1 (--gradle)", self.out.getvalue())

    def script(self, name, text, where=None):
        if where is not None:
            where.mkdir(parents=True, exist_ok=True)
        return super().script(name, text, where)

    def test_gradle_preconditions_run_before_out_dir_is_touched(self):
        needs_gradle = 'name = "g"\n[[steps]]\nrun.gradle = "help"'
        with self.assertRaises(gs.GustError) as cm:
            self.run_text(needs_gradle, gradle="no-such-gradle-binary")
        self.assertIn("not found on PATH and not a Gradle version", str(cm.exception))
        with self.assertRaises(gs.GustError) as cm:
            self.run_text(needs_gradle, gradle="/no/such/gradle")
        self.assertIn(f"no executable at {Path('/no/such/gradle').resolve()}", str(cm.exception))
        with self.assertRaises(gs.GustError) as cm:
            self.run_text(needs_gradle, gradle=WRAPPER)
        self.assertIn("no such file is in the project", str(cm.exception))
        with self.assertRaises(gs.GustError) as cm:
            self.run_text(needs_gradle, gradle="../gradlew")
        self.assertIn("no executable at", str(cm.exception))
        self.assertFalse(self.out_dir.exists())
        # a shell-only scenario without the daemon stop needs no Gradle, but a --gradle value is still resolved
        summary = self.run_text('name = "s"\n[[steps]]\nrun.shell = "true"', gradle="gecho")
        self.assertEqual(summary.result, "success")
        with self.assertRaises(gs.GustError):
            self.run_scenario(gs.load_scenario('name = "s"\n[[steps]]\nrun.shell = "true"'), self.out_dir / "x",
                            gradle="no-such-gradle-binary", stop_daemons=False)
        # the scenario's own default is checked only when the daemon stop or a Gradle step needs it
        os.environ["PATH"] = os.pathsep.join([str(self.bin)] + GIT_DIR)                # no gradle here
        summary = self.run_scenario(gs.load_scenario('name = "s"\n[[steps]]\nrun.shell = "true"'), self.out_dir / "y", stop_daemons=False)
        self.assertEqual(summary.gradle, "gradle")
        self.assertIn("gradle:   gradle (default)\n", self.out.getvalue())
        with self.assertRaises(gs.GustError) as cm:
            self.run_scenario(gs.load_scenario('name = "s"\n[[steps]]\nrun.shell = "true"'), self.out_dir / "z", stop_daemons=True)
        self.assertEqual(str(cm.exception), "'gradle' is not on PATH")

    def test_shell_step_sees_gradle_env(self):
        gecho = self.gecho
        summary = self.run_text('name = "g2"\n[[steps]]\nrun.shell = "echo using $GRADLE"', gradle=str(gecho))
        self.assertEqual((self.out_dir / summary.steps[0].log).read_text().strip(), f"using {Path(gecho).as_posix() if gs.WINDOWS else gecho}")   # forward slashes for bash
        self.assertNotIn("using /", self.out.getvalue())

    def test_gradle_step_via_dummy_binary(self):
        summary = self.run_text('name = "e"\n[[steps]]\nrun.gradle = { args = "test --info", expect = "pass" }')
        self.assertEqual(summary.result, "success")
        self.assertEqual(summary.steps[0].kind, "gradle")
        self.assertEqual((self.out_dir / summary.steps[0].log).read_text().strip(), f"{home_arg()} test --info")
        self.assertIsNone(summary.stop_daemons)
        self.assertEqual(summary.gradle_user_home, str(self.gust / gs.SHARED_HOME))
        self.assertTrue((self.gust / gs.SHARED_HOME).is_dir())

    def test_shell_step_sees_gradle_user_home_and_cli_override_wins(self):
        self.run_text('name = "h"\n[[steps]]\nrun.shell = "echo home=$GRADLE_USER_HOME"')
        self.assertEqual((self.out_dir / "step-01.log").read_text().strip(), f"home={self.gust / gs.SHARED_HOME}")
        custom = self.tmp / "my-home"
        summary = self.run_scenario(gs.load_scenario('name = "h"\n[[steps]]\nrun.gradle = "help"\n[[steps]]\nrun.shell = "echo $GRADLE_USER_HOME"'),
                                  self.tmp / "c.out", gradle="gecho", stop_daemons=False, gradle_user_home=custom)
        self.assertEqual(summary.gradle_user_home, str(custom))
        self.assertEqual(summary.steps[0].command, "gecho help")
        self.assertEqual((self.tmp / "c.out" / "step-01.log").read_text().strip(), f"--gradle-user-home {custom} help")
        self.assertEqual((self.tmp / "c.out" / "step-02.log").read_text().strip(), str(custom))
        self.assertTrue(custom.is_dir())

    def test_show_output_streams_between_step_and_its_result(self):
        text = 'name = "so"\n[[steps]]\nrun.shell = "echo alpha; echo beta >&2"\n[[steps]]\nrun.shell = "echo gamma; exit 3"\n'
        summary = self.run_text(text, show_output=True)
        self.assertEqual(summary.result, "failure")
        printed = self.out.getvalue()
        block = f"[1/2] Run: echo alpha; echo beta >&2\n$ echo alpha; echo beta >&2\n{rule('step-01.log')}\nalpha\nbeta\n{CLOSING}\nEXPECTED: exit=0 in "
        self.assertIn(block, printed)                                               # stderr is in the same stream, output untouched
        self.assertEqual((self.out_dir / "step-01.log").read_text(), "alpha\nbeta\n")   # the log is written as before
        self.assertRegex(printed, re.escape(f"[2/2] Run: echo gamma; exit 3\n$ echo gamma; exit 3\n{rule('step-02.log')}\ngamma\n{CLOSING}\nUNEXPECTED: exit=3 in ") + r"\d+\.\ds -> step-02\.log\n  expected pass, got fail\nresult: FAILURE")
        self.assertNotIn("last ", printed)                                          # no tail after the output was shown
        self.assertEqual((self.out_dir / "step-02.log").read_text(), "gamma\n")
        # no output at all, and output without a final newline, still get both rules
        self.out = io.StringIO()
        self.run_scenario(gs.load_scenario('name = "e"\n[[steps]]\nrun.shell = "true"\n[[steps]]\nrun.shell = "printf partial"'),
                        self.tmp / "e.out", gradle="gecho", stop_daemons=False, show_output=True)
        self.assertIn(f"\n{rule('step-01.log')}\n{CLOSING}\nEXPECTED: exit=0", self.out.getvalue())
        self.assertIn(f"\n{rule('step-02.log')}\npartial\n{CLOSING}\nEXPECTED: exit=0", self.out.getvalue())
        self.assertEqual((self.tmp / "e.out" / "step-02.log").read_text(), "partial")
        # without the flag nothing changes: no echo, the tail on an unexpected check
        self.out = io.StringIO()
        self.run_scenario(gs.load_scenario(text), self.tmp / "q.out", gradle="gecho", stop_daemons=False)
        printed = self.out.getvalue()
        self.assertNotIn("alpha\nbeta", printed)
        self.assertIn(f"\n{rule('last 1 of 1 lines of step-02.log')}\ngamma\n{CLOSING}\n", printed)

    def test_rerun_recreates_the_out_dir(self):
        self.run_text(MINIMAL)
        (self.project / "leftover").write_text("from the first run")
        self.out = io.StringIO()
        self.run_scenario(gs.load_scenario(MINIMAL), self.out_dir, gradle="gecho", stop_daemons=False)
        self.assertFalse((self.project / "leftover").exists())                      # recreated from scratch
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), [".gust", "bin", "s.out"])   # no sibling kept

    def test_stop_daemons_runs_before_and_after_steps_even_on_failure(self):
        summary = self.run_text('name = "d"\n[[steps]]\nrun.shell = "false"', stop_daemons=True)
        self.assertEqual(summary.result, "failure")
        self.assertEqual({k: {f: v for f, v in d.items() if f != "duration_s"} for k, d in summary.stop_daemons.items()}, {
            "before": {"exit_code": 0, "log": "stop-daemons-before.log"},
            "after": {"exit_code": 0, "log": "stop-daemons-after.log"},
        })
        self.assertIsInstance(summary.stop_daemons["before"]["duration_s"], float)
        for duration in (summary.stop_daemons["before"]["duration_s"], summary.stop_daemons["after"]["duration_s"],
                         summary.steps[0].duration_s):
            self.assertEqual(duration, round(duration, 1))                         # to 0.1 s, as on the console
        self.assertEqual((self.out_dir / "stop-daemons-before.log").read_text().strip(), f"{home_arg()} --stop")
        self.assertEqual((self.out_dir / "stop-daemons-after.log").read_text().strip(), f"{home_arg()} --stop")
        printed = self.out.getvalue()
        self.assertIn("\n[BEF] Stop Gradle daemons -> stop-daemons-before.log\n[1/1] ", printed)     # one line each
        self.assertIn("\n[AFT] Stop Gradle daemons -> stop-daemons-after.log\nresult: FAILURE", printed)
        self.assertNotIn("--stop\n", printed)
        self.assertNotIn("exit=0 in", printed)                                     # a stop that works has no line of its own
        self.assertEqual(json.loads((self.out_dir / "summary.json").read_text())["stop_daemons"]["before"]["exit_code"], 0)
        # the stops are never framed, even with --show-output
        self.out = io.StringIO()
        self.run_scenario(gs.load_scenario('name = "v"\n[[steps]]\nrun.shell = "echo body"'), self.tmp / "b.out", gradle="gecho",
                        stop_daemons=True, show_output=True)
        printed = self.out.getvalue()
        self.assertIn(f"[BEF] Stop Gradle daemons -> stop-daemons-before.log\n[1/1] Run: echo body\n$ echo body\n{rule('step-01.log')}\nbody\n{CLOSING}\nEXPECTED: exit=0", printed)
        self.assertNotIn(rule("stop-daemons-before.log"), printed)

    def test_failed_daemon_stop_is_reported(self):
        flaky = self.script("gradle", 'if ARGS[2:3] == ["--version"]:\n    print("Gradle 9.7.1")\n    sys.exit(0)\n'
                                      'if ARGS[2:3] == ["--stop"]:\n    print("cannot")\n    sys.exit(3)\nprint(*ARGS)\n')
        summary = self.run_scenario(gs.load_scenario('name = "f"\n[[steps]]\nrun.shell = "true"'), self.out_dir, gradle=str(flaky), stop_daemons=True)
        self.assertEqual(summary.result, "success")                                              # a failed stop never changes the result
        self.assertEqual(summary.stop_daemons["before"]["exit_code"], 3)
        printed = self.out.getvalue()
        self.assertRegex(printed, r"\[BEF\] Stop Gradle daemons -> stop-daemons-before\.log\nERROR: exit=3 in \d+\.\ds -> stop-daemons-before\.log\n\[1/1\]")
        self.assertRegex(printed, r"\[AFT\] Stop Gradle daemons -> stop-daemons-after\.log\nERROR: exit=3 in \d+\.\ds -> stop-daemons-after\.log\nresult: SUCCESS")
        self.assertNotIn("FAILED", printed)

    def test_summary_json(self):
        summary = self.run_text("""
name = "j"
description = "one line"
[setup.layout.project]
"w" = "x"
[[steps]]
name = "n"
run.shell = "true"
[[steps]]
[steps.write]
"w" = "c"
[[steps]]
[[steps.edit]]
file = "w"
replace = "c"
with = "d"
""")
        on_disk = json.loads((self.out_dir / "summary.json").read_text())
        self.assertEqual(on_disk, json.loads(summary.to_json()))
        self.assertEqual(list(on_disk), ["scenario", "description", "out", "project", "gradle", "gradle_version", "gradle_user_home", "result", "steps"])
        self.assertEqual(on_disk["result"], "success")
        self.assertEqual(on_disk["scenario"], "j")
        self.assertEqual(on_disk["description"], "one line")
        self.assertEqual(on_disk["out"], str(self.out_dir))
        self.assertEqual(on_disk["project"], str(self.project))
        self.assertEqual(on_disk["gradle"], "gecho")
        run_step, write_step, edit_step = on_disk["steps"]
        self.assertEqual(list(run_step), ["index", "kind", "name", "result", "checks", "exit_code", "duration_s", "log", "command"])
        self.assertEqual(run_step["kind"], "shell")
        self.assertEqual(run_step["name"], "n")
        self.assertEqual(run_step["result"], "expected")
        self.assertEqual(run_step["checks"], [{"check": "exit", "expect": "pass", "got": "pass", "result": "expected"}])
        self.assertEqual(run_step["exit_code"], 0)
        self.assertEqual(run_step["command"], "true")
        self.assertEqual(run_step["log"], "step-01.log")
        self.assertEqual(write_step, {"index": 2, "kind": "write", "name": "Write: w", "result": "done", "files": ["w"]})
        self.assertEqual(edit_step, {"index": 3, "kind": "edit", "name": "Edit: w", "result": "done", "files": ["w"]})
        # the full key set of a run with everything on
        summary = self.run_scenario(gs.load_scenario('name = "k"\n[[steps]]\nrun.gradle = "help"'), self.tmp / "k.out", gradle="gecho",
                           stop_daemons=True, gradle_args=["--offline"])
        self.assertEqual(list(json.loads(summary.to_json())),
                         ["scenario", "out", "project", "gradle", "gradle_version", "gradle_args", "gradle_user_home", "result", "steps", "stop_daemons"])


class CliTest(GustCase):
    def test_help(self):
        for argv in ([], ["help"]):
            code, out, _ = self.cli(*argv)
            self.assertEqual(code, gs.EXIT_OK, argv)
            self.assertIn("usage: gust.py", out)
            for command in ("run", "check", "setup", "stop-daemons", "flat", "zip", "spec", "version", "help"):
                self.assertIn(f" {command} ", out.replace("\n", " ") + " ", command)
        code, out, _ = self.cli("help", "run")
        self.assertEqual(code, gs.EXIT_OK)
        self.assertIn("usage: gust.py run", out)
        self.assertIn("--keep-daemons", out)
        self.assertIn("--show-output", out)
        self.assertIn("[-- GRADLE_ARGS ...]", out)
        self.assertIn("--tail N", out)
        self.assertIn("--gradle BIN|VERSION", out)
        code, out, _ = self.cli("help", "setup")
        self.assertIn("Lay the project out", out)
        self.assertIn("--show-output", out)
        code, out, _ = self.cli("help", "stop-daemons")
        self.assertIn("Run '<gradle> --stop' in the set-up project", out)
        self.assertIn("--gradle BIN]", out)                                           # a binary only there
        self.assertNotIn("BIN|VERSION", out)
        code, out, _ = self.cli("help", "help")
        self.assertIn("the command whose usage to print", out)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            gs.main(["help", "bogus"])
        self.assertEqual(cm.exception.code, 2)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            gs.main(["--version"])                                                       # the flag is gone; the command stays
        self.assertEqual(cm.exception.code, 2)

    def test_version(self):
        code, out, _ = self.cli("version")
        self.assertEqual(code, gs.EXIT_OK)
        lines = out.splitlines()
        self.assertRegex(gs.__version__, r"^[1-9][0-9]*(-dev)?$")
        self.assertEqual(lines[0], f"gust {gs.__version__}")
        self.assertTrue(lines[1].startswith("python 3."))
        self.assertEqual(lines[2], f"script {Path(gs.__file__).resolve()}")

    def test_spec(self):
        code, out, _ = self.cli("spec")
        self.assertEqual(code, gs.EXIT_OK)
        self.assertIn("[setup.layout.project]", out)
        self.assertIn("steps[].run.gradle", out)
        self.assertIn("<out>/project/", out)

    def test_double_dash_without_a_command(self):
        code, out, err = self.cli("--", "--offline")
        self.assertEqual((code, err), (gs.EXIT_OK, ""))
        self.assertIn("usage: gust.py", out)                                          # the general help, not a "None runs no Gradle step" error

    def test_a_scenario_that_is_not_utf8_is_refused(self):
        text = '[[steps]]\nwrite."a.txt" = "caf\u00e9"\n'
        for encoding in ("latin-1", "utf-16"):                                          # utf-16: PowerShell 5's >
            path = self.tmp / f"{encoding}.toml"
            path.write_bytes(text.encode(encoding))
            for command in ("check", "run", "zip", "flat"):
                with self.subTest(encoding=encoding, command=command):
                    self.assertEqual(self.cli(command, str(path)), (gs.EXIT_ERROR, "", f"ERROR: {path} is not UTF-8 text\n"))

    def test_stdin_is_read_as_utf8_bytes(self):
        text = '[[steps]]\nwrite."a.txt" = "caf\u00e9"\n'
        latin = io.TextIOWrapper(io.BytesIO(text.encode("latin-1")), encoding="utf-8")   # strict, as on a UTF-8 locale
        self.assertEqual(self.cli("run", "-", stdin=latin), (gs.EXIT_ERROR, "", "ERROR: stdin is not UTF-8 text\n"))
        cp1252 = io.TextIOWrapper(io.BytesIO(text.encode("utf-8")), encoding="cp1252")   # a Windows code page
        code, _, err = self.cli("run", "-", "--keep-daemons", stdin=cp1252)
        self.assertEqual((code, err), (gs.EXIT_OK, ""))
        self.assertEqual((self.tmp / "scenario.out" / "project" / "a.txt").read_bytes(), "caf\u00e9".encode("utf-8"))

    def test_a_console_that_cannot_show_a_character_gets_an_escape(self):
        s = self.tmp / "u.toml"
        s.write_bytes('[[steps]]\nrun.shell = "echo \u65e5"\n'.encode("utf-8"))
        console = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")                   # a redirected stdout on Windows
        with contextlib.redirect_stdout(console), contextlib.redirect_stderr(io.StringIO()):
            code = gs.main(["run", str(s), "--keep-daemons"])
        console.flush()
        self.assertEqual(code, gs.EXIT_OK)
        self.assertIn(b"[1/1] Run: echo \\u65e5", console.buffer.getvalue())
        self.assertTrue((self.tmp / "u.out" / "summary.json").is_file())

    def test_any_scenario_file_name(self):
        s = self.scenario_file(MINIMAL, name="loop.scenario")
        code, out, _ = self.cli("run", str(s), "--keep-daemons")
        self.assertEqual(code, gs.EXIT_OK, out)
        self.assertIn(f"out:      {self.tmp / 'loop.out'}\n", out)                    # the stem of any file name
        code, _, err = self.cli("run", str(self.tmp / "nosuch.txt"))
        self.assertEqual(code, gs.EXIT_ERROR)
        self.assertEqual(err.strip(), f"ERROR: scenario file not found: {self.tmp / 'nosuch.txt'}")   # the missing file comes first

    def test_run_exit_codes(self):
        self.assertEqual(self.cli("run", str(self.scenario_file(MINIMAL)), "--out", str(self.tmp / "a"), "--keep-daemons")[0], gs.EXIT_OK)
        bad = self.scenario_file('name = "b"\n[[steps]]\nrun.shell = "false"')
        self.assertEqual(self.cli("run", str(bad), "--out", str(self.tmp / "b"), "--keep-daemons")[0], gs.EXIT_FAILURE)
        bad_edit = self.scenario_file('name = "c"\n[setup.layout.project]\n"f" = "aa"\n[[steps]]\n[[steps.edit]]\nfile = "f"\nreplace = "a"\nwith = "b"')
        self.assertEqual(self.cli("run", str(bad_edit), "--out", str(self.tmp / "c"), "--keep-daemons")[0], gs.EXIT_ERROR)
        self.assertTrue((self.tmp / "c" / "summary.json").is_file())                    # a step that could not be carried out: summary present
        errors = []
        for argv in (lambda: ["run", str(self.scenario_file('name = 1')), "--keep-daemons"],
                     lambda: ["run", str(self.tmp / "missing.toml"), "--keep-daemons"],
                     lambda: ["run", str(self.scenario_file(MINIMAL)), "--out", str(self.tmp), "--keep-daemons"],
                     lambda: ["run", str(self.scenario_file(MINIMAL)), "--gradle", "no-such-gradle-binary"]):
            argv = argv()
            code, out, err = self.cli(*argv)
            self.assertEqual(code, gs.EXIT_ERROR, argv)
            self.assertEqual(out, "", argv)                                                # no stray header before a failed precondition
            errors.append(err.strip())
        self.assertEqual(errors[0], f"ERROR: {self.tmp / 's.toml'}: top level.name: must be str")   # a load error starts with its source
        self.assertIn("scenario file not found", errors[1])
        self.assertIn("would be removed with the earlier run", errors[2])
        self.assertIn("not found on PATH and not a Gradle version", errors[3])
        self.assertFalse((self.tmp / "s.out").exists())

    def test_run_defaults_to_out_in_working_dir_and_stops_daemons(self):
        code, out, _ = self.cli("run", str(self.scenario_file(MINIMAL)), "--gradle", "gecho", "--json")
        self.assertEqual(code, gs.EXIT_OK)
        out_dir = self.tmp / "s.out"
        self.assertTrue((out_dir / "project").is_dir())
        self.assertTrue((out_dir / "step-01.log").is_file())
        self.assertEqual((out_dir / "stop-daemons-before.log").read_text().strip(), f"{home_arg()} --stop")
        self.assertEqual((out_dir / "stop-daemons-after.log").read_text().strip(), f"{home_arg()} --stop")
        last = json.loads(out.rstrip("\n").splitlines()[-1])
        self.assertEqual(last["result"], "success")
        self.assertEqual(last["out"], str(out_dir))
        self.assertEqual(last["stop_daemons"]["after"]["exit_code"], 0)

    def test_stop_daemons_command(self):
        s = self.scenario_file(MINIMAL)
        out_dir = self.tmp / "s.out"
        code, out, err = self.cli("stop-daemons", str(s), "--gradle", "gecho")               # nothing set up yet
        self.assertEqual((code, out), (gs.EXIT_ERROR, ""))
        self.assertEqual(err.strip(), f"ERROR: no set-up project at {out_dir / 'project'}; run 'gust setup {s}' first")
        self.assertFalse(out_dir.exists())                                                   # nothing was set up on the way
        (out_dir / "project").mkdir(parents=True)                                            # a project dir without the marker is not ours
        self.assertEqual(self.cli("stop-daemons", str(s), "--gradle", "gecho")[0], gs.EXIT_ERROR)
        (out_dir / gs.MARKER).write_text("")
        (out_dir / "project").rmdir()                                                        # the marker alone is not enough either
        self.assertEqual(self.cli("stop-daemons", str(s), "--gradle", "gecho")[0], gs.EXIT_ERROR)
        self.assertEqual(self.cli("setup", str(s))[0], gs.EXIT_OK)
        code, out, _ = self.cli("stop-daemons", str(s), "--gradle", "gecho")
        self.assertEqual(code, gs.EXIT_OK)
        self.assertRegex(out, rf"^scenario: min\nout:      {re.escape(str(self.tmp / 's.out'))}\nhome:     .*\ngradle:   gecho 9\.7\.1 \(--gradle\)\n"
                              rf"\[STOP\] Stop Gradle daemons\n\$ gecho --stop\nexit=0 in \d+\.\ds -> stop-daemons\.log\nout:      {re.escape(str(self.tmp / 's.out'))}\n$")
        self.assertEqual((self.tmp / "s.out" / "stop-daemons.log").read_text().strip(), f"{home_arg()} --stop")
        self.script("failing", FAILING)
        code, _, err = self.cli("stop-daemons", str(s), "--gradle", "failing")          # the probe: `failing --version` fails
        self.assertEqual(code, gs.EXIT_ERROR)
        self.assertIn("cannot run", err)
        self.script("nostop", 'if ARGS[2:3] == ["--version"]:\n    print("Gradle 9.7.1")\n    sys.exit(0)\nsys.exit(3)\n')
        code, out, _ = self.cli("stop-daemons", str(s), "--gradle", "nostop")             # the stop itself fails
        self.assertEqual(code, gs.EXIT_ERROR)
        self.assertRegex(out, r"\$ nostop --stop\nERROR: exit=3 in \d+\.\ds -> stop-daemons\.log\nresult: ERROR\nout:      ")

    def test_a_project_table_binary_is_refused_until_set_up(self):
        g = runnable("g")
        content = "@echo Gradle 9.7.1" if gs.WINDOWS else "#!/bin/sh\necho 'Gradle 9.7.1'"
        s = self.scenario_file(f"[setup.layout.project]\n\"tools/{g}\" = '''{content}\n'''\n[[steps]]\nrun.gradle = \"help\"\n")
        for command in ("check", "run", "setup"):                                       # probed before anything is laid out
            with self.subTest(command=command):
                code, out, err = self.cli(command, str(s), "--gradle", f"./tools/{g}")
                self.assertEqual((code, out), (gs.EXIT_ERROR, ""))
                self.assertEqual(err.strip(), f"ERROR: --gradle './tools/{g}': tools/{g} is in the project table alone, which is "
                                              f"laid out only after Gradle is probed; use a binary outside the project, or one in "
                                              f"the remote checkout")
        self.assertEqual(self.cli("setup", str(s))[0], gs.EXIT_OK)                      # on disk now, so stop-daemons takes it
        (self.tmp / "s.out" / "project" / "tools" / g).chmod(0o755)                     # the project table's files are not executable
        code, out, _ = self.cli("stop-daemons", str(s), "--gradle", f"./tools/{g}")
        self.assertEqual(code, gs.EXIT_OK, out)
        self.assertIn(f"gradle:   ./tools/{g} 9.7.1 (--gradle)\n", out)

    def test_stop_daemons_probes_a_project_table_binary_gone_from_the_set_up_project(self):
        s = self.scenario_file('[setup.layout.project]\n"tools/g" = "x"\n[[steps]]\nrun.gradle = "help"\n')
        self.assertEqual(self.cli("setup", str(s))[0], gs.EXIT_OK)
        (self.tmp / "s.out" / "project" / "tools" / "g").unlink()                       # gone, as a step could make it
        code, out, err = self.cli("stop-daemons", str(s), "--gradle", "./tools/g")
        self.assertEqual((code, out), (gs.EXIT_ERROR, ""))
        self.assertTrue(err.startswith("ERROR: --gradle './tools/g': cannot run './tools/g --version'"), err)   # the probe's message

    def test_check_needs_gradle_where_run_does(self):
        s = self.scenario_file(MINIMAL)                                                 # shell only
        os.environ["PATH"] = os.pathsep.join([str(self.bin)] + GIT_DIR)                 # no gradle here
        for command in ("run", "check"):                                                # the daemon stops need it
            with self.subTest(command=command):
                code, out, err = self.cli(command, str(s))
                self.assertEqual((code, out, err.strip()), (gs.EXIT_ERROR, "", "ERROR: 'gradle' is not on PATH"))
        for command in ("run", "check"):                                                # without them, nothing does
            with self.subTest(command=command, keep_daemons=True):
                code, out, _ = self.cli(command, str(s), "--keep-daemons")
                self.assertEqual(code, gs.EXIT_OK, out)
                self.assertIn("gradle:   gradle (default)\n", out)

    def test_setup_only(self):
        s = self.scenario_file('name = "m"\ndescription = "Set up only."\n[setup.layout.project]\n"x" = "y"\n[[steps]]\nrun.shell = "false"')
        code, out, _ = self.cli("setup", str(s), "--out", str(self.tmp / "m"))
        self.assertEqual(code, gs.EXIT_OK)
        self.assertEqual((self.tmp / "m" / "project" / "x").read_text(), "y")
        self.assertEqual(sorted(p.name for p in (self.tmp / "m").iterdir()), [gs.MARKER, "project"])
        self.assertEqual(out, f"scenario: m\nout:      {self.tmp / 'm'}\nhome:     {self.gust / gs.SHARED_HOME}\ngradle:   gradle (default)\nresult: set up\nout:      {self.tmp / 'm'}\n")
        (self.tmp / "m" / "project" / "extra").write_text("")
        code, out, _ = self.cli("setup", str(s), "--out", str(self.tmp / "m"))
        self.assertNotIn("previous", out)
        self.assertFalse((self.tmp / "m" / "project" / "extra").exists())            # recreated
        self.assertFalse((self.tmp / "m.prev").exists())

    def test_run_header_shows_the_inferred_name(self):
        s = self.scenario_file('[[steps]]\nrun.shell = "true"')
        code, out, _ = self.cli("run", str(s), "--keep-daemons", "--json")
        self.assertEqual(code, gs.EXIT_OK)
        self.assertTrue(out.startswith("scenario: s\nout:      "))
        last = json.loads(out.rstrip("\n").splitlines()[-1])
        self.assertEqual(last["scenario"], "s")
        self.assertNotIn("description", last)

    def test_show_output_with_json_keeps_json_last(self):
        s = self.scenario_file('name = "j"\n[[steps]]\nrun.gradle = "help"\n[[steps]]\nrun.shell = "echo hello"')
        code, out, _ = self.cli("run", str(s), "--gradle", "gecho", "--keep-daemons", "--show-output", "--json")
        self.assertEqual(code, gs.EXIT_OK)
        lines = out.rstrip("\n").splitlines()
        self.assertIn(f"{home_arg()} help", lines)
        self.assertEqual(json.loads(lines[-1])["result"], "success")                 # the JSON line stays last
        self.assertEqual(lines[-2], f"out:      {self.tmp / 's.out'}")           # after result:, before the JSON
        self.assertTrue(lines[-3].startswith("result: SUCCESS"))
        self.assertEqual(lines[lines.index("hello") - 1], rule("step-02.log"))
        self.assertEqual(lines[lines.index("hello") + 1], CLOSING)
        self.assertLess(lines.index("[2/2] Run: echo hello"), lines.index("hello"))
        self.assertTrue(lines[lines.index("hello") + 2].startswith("EXPECTED: exit=0 in "))

    def test_tmp_out_dir(self):
        root = self.tmp / "fake-tmp"
        self.patch("TMP_ROOT", root)                                                     # nothing lands under the real /tmp
        s = self.scenario_file('[[steps]]\nrun.shell = "echo body"')
        # the path shape, from a fixed clock: 2026-09-18 21:05:07 local
        fixed = time.mktime((2026, 9, 18, 21, 5, 7, 0, 0, -1))
        self.assertEqual(gs.tmp_out("s", fixed), root / "gust-260918" / "s.210507.out")
        self.assertRegex(gs.tmp_out("testing-loop").as_posix(), r"/fake-tmp/gust-\d{6}/testing-loop\.\d{6}\.out$")
        # a run: the dir under the tmp root, summary.json there, no output shown (--tmp implies nothing)
        code, out, _ = self.cli("run", str(s), "--tmp", "--keep-daemons", "--json")
        self.assertEqual(code, gs.EXIT_OK)
        out_dir = Path(re.search(r"^out:      (.*)$", out, re.M).group(1))
        self.assertRegex(out_dir.as_posix(), r"/fake-tmp/gust-\d{6}/s\.\d{6}\.out$")
        self.assertTrue((out_dir / gs.MARKER).is_file())
        self.assertNotIn("-----", out)
        self.assertEqual(json.loads(out.rstrip("\n").splitlines()[-1])["out"], str(out_dir))
        self.assertFalse((self.tmp / "s.out").exists())
        code, out, _ = self.cli("run", str(s), "--tmp", "--keep-daemons", "--show-output")
        self.assertIn(f"{rule('step-01.log')}\nbody\n{CLOSING}\n", out)
        # the same name already there from an earlier gust run: the common rule, removed and recreated
        fixed_dir = root / "gust-260918" / "s.210507.out"
        self.patch("tmp_out", lambda stem, now=None: fixed_dir)
        code, out, _ = self.cli("setup", str(s), "--tmp")
        self.assertEqual(code, gs.EXIT_OK)
        (fixed_dir / "junk").write_text("x")
        code, out, _ = self.cli("setup", str(s), "--tmp")
        self.assertEqual(code, gs.EXIT_OK)
        self.assertFalse((fixed_dir / "junk").exists())
        self.assertTrue((fixed_dir / "project").is_dir())
        self.assertEqual([p.name for p in fixed_dir.parent.iterdir()], ["s.210507.out"])
        # and, like anywhere else, a foreign non-empty dir at that name is refused
        gs.rmtree(fixed_dir)
        fixed_dir.mkdir()
        (fixed_dir / "precious").write_text("")
        code, _, err = self.cli("setup", str(s), "--tmp")
        self.assertEqual(code, gs.EXIT_ERROR)
        self.assertIn("was not created by gust, so it is left alone", err)
        # usage errors
        code, _, err = self.cli("run", str(s), "--tmp", "--out", str(self.tmp / "o"))
        self.assertEqual(code, gs.EXIT_ERROR)
        self.assertIn("ERROR: --tmp and --out cannot be combined", err)
        for argv in (["stop-daemons", str(s), "--tmp"], ["check", str(s), "--tmp"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                gs.main(argv)                                                            # no --tmp there

    def test_scenario_from_stdin(self):
        class Tty(io.StringIO):
            def isatty(self):
                return True
        # named "scenario", out dir scenario.out in the working directory, relative base against the cwd
        (self.tmp / "base.toml").write_text('[project]\n"a.txt" = "from base"\n')
        code, out, _ = self.cli("run", "-", "--keep-daemons", "--json", stdin='setup.layout.base = "base.toml"\n[[steps]]\nrun.shell = "test -f a.txt"\n')
        self.assertEqual(code, gs.EXIT_OK)
        self.assertTrue(out.startswith(f"scenario: scenario\nout:      {self.tmp / 'scenario.out'}\n"))
        self.assertEqual(json.loads(out.rstrip("\n").splitlines()[-1])["scenario"], "scenario")
        self.assertEqual((self.tmp / "scenario.out" / "project" / "a.txt").read_text(), "from base")
        # with a name in the TOML, the out dir is named after it
        code, out, _ = self.cli("run", "-", "--keep-daemons", stdin='name = "piped"\n[[steps]]\nrun.shell = "true"\n')
        self.assertEqual(code, gs.EXIT_OK)
        self.assertIn(f"scenario: piped\nout:      {self.tmp / 'piped.out'}\n", out)
        # a TTY, and empty input
        code, _, err = self.cli("run", "-", stdin=Tty(""))
        self.assertEqual((code, err.strip()), (gs.EXIT_ERROR, "ERROR: SCENARIO is '-' but nothing is piped on stdin"))
        code, _, err = self.cli("run", "-", stdin="")
        self.assertEqual((code, err.strip()), (gs.EXIT_ERROR, "ERROR: stdin: scenario has no steps"))
        # setup -, stop-daemons -, and its hint
        gradle = '[[steps]]\nrun.gradle = "help"\n'
        code, out, _ = self.cli("setup", "-", "--out", str(self.tmp / "piped-setup"), stdin=gradle)
        self.assertEqual(code, gs.EXIT_OK)
        self.assertTrue(out.endswith(f"result: set up\nout:      {self.tmp / 'piped-setup'}\n"), out[-200:])
        code, out, _ = self.cli("stop-daemons", "-", "--out", str(self.tmp / "piped-setup"), "--gradle", "gecho", stdin=gradle)
        self.assertEqual(code, gs.EXIT_OK)
        self.assertIn("scenario: scenario\n", out)
        self.assertIn("[STOP] Stop Gradle daemons", out)
        code, _, err = self.cli("stop-daemons", "-", "--out", str(self.tmp / "nowhere"), stdin=gradle)
        self.assertEqual(code, gs.EXIT_ERROR)
        self.assertEqual(err.strip(), f"ERROR: no set-up project at {self.tmp / 'nowhere' / 'project'}; pipe the same scenario to 'gust setup -' first")
        # --tmp: the name is the stem
        self.patch("TMP_ROOT", self.tmp / "fake-tmp")
        code, out, _ = self.cli("run", "-", "--tmp", "--keep-daemons", stdin='[[steps]]\nrun.shell = "true"\n')
        self.assertEqual(code, gs.EXIT_OK)
        self.assertRegex(Path(re.search(r"^out:      (.*)$", out, re.M).group(1)).as_posix(), r"/fake-tmp/gust-\d{6}/scenario\.\d{6}\.out$")

    def test_gradle_args_after_double_dash(self):
        s = self.scenario_file('name = "a"\n[[steps]]\nrun.gradle = "help --quiet"\n[[steps]]\nrun.shell = "echo args=$GRADLE_ARGS"')
        code, out, _ = self.cli("run", str(s), "--gradle", "gecho", "--json", "--", "--isolated-projects", "-Dfoo=a b")
        self.assertEqual(code, gs.EXIT_OK)
        self.assertIn("[1/2] Run: gradle help --quiet\n$ gecho help --quiet --isolated-projects '-Dfoo=a b'\n", out)   # label unchanged, args last
        out_dir = self.tmp / "s.out"                                                        # named after the file, not `name`
        self.assertEqual((out_dir / "step-01.log").read_text().strip(), f"{home_arg()} help --quiet --isolated-projects -Dfoo=a b")
        self.assertEqual((out_dir / "step-02.log").read_text().strip(), "args=--isolated-projects '-Dfoo=a b'")
        self.assertEqual((out_dir / "stop-daemons-before.log").read_text().strip(), f"{home_arg()} --stop")    # the stops: without
        self.assertEqual((out_dir / "stop-daemons-after.log").read_text().strip(), f"{home_arg()} --stop")
        last = json.loads(out.rstrip("\n").splitlines()[-1])
        self.assertEqual(last["gradle_args"], ["--isolated-projects", "-Dfoo=a b"])
        self.assertEqual(last["steps"][0]["command"], "gecho help --quiet --isolated-projects '-Dfoo=a b'")
        self.assertEqual(last["steps"][0]["name"], "Run: gradle help --quiet")
        # an empty -- is a no-op
        code, out, _ = self.cli("run", str(s), "--gradle", "gecho", "--keep-daemons", "--json", "--")
        self.assertEqual(code, gs.EXIT_OK)
        last = json.loads(out.rstrip("\n").splitlines()[-1])
        self.assertNotIn("gradle_args", last)
        self.assertEqual(last["steps"][0]["command"], "gecho help --quiet")
        self.assertEqual((out_dir / "step-02.log").read_text().strip(), "args=")
        # run only
        for argv, name in (((["setup", str(s), "--", "--offline"]), "setup"), ((["stop-daemons", str(s), "--gradle", "gecho", "--", "--offline"]), "stop-daemons")):
            code, _, err = self.cli(*argv)
            self.assertEqual(code, gs.EXIT_ERROR)
            self.assertEqual(err.strip(), f"ERROR: arguments after -- are accepted by run only ({name} runs no Gradle step)")

    def test_trace(self):
        self.script("tracer", TRACER)
        s = self.scenario_file('name = "t"\n[[steps]]\nrun.gradle = "help"\n[[steps]]\nrun.shell = "echo args=$GRADLE_ARGS"\n'
                               '[[steps]]\nrun.gradle = "build"')
        out_dir, prop = self.tmp / "s.out", "-Dorg.gradle.internal.operations.trace"
        code, out, _ = self.cli("run", str(s), "--gradle", "tracer", "--trace", "--keep-daemons", "--json", "--", "--offline")
        self.assertEqual(code, gs.EXIT_OK)
        self.assertEqual((out_dir / "step-01.log").read_text().strip(),
                         f"{home_arg()} {prop}={out_dir / 'step-01-ops'} {prop}.tree=false help --offline")   # first, so args win
        self.assertEqual((out_dir / "step-02.log").read_text().strip(), "args=--offline")                     # shell steps: untraced
        self.assertEqual((out_dir / "step-03-ops-log.txt").read_text(), '{"id": 1}\n')
        self.assertIn("[1/3] Run: gradle help\n$ tracer help --offline\nEXPECTED: exit=0 in ", out)                  # shown without it
        self.assertRegex(out, r"-> step-01\.log, step-01-ops-log\.txt\n\[2/3\].*\n.*\nEXPECTED: .* -> step-02\.log\n")
        steps = json.loads(out.rstrip("\n").splitlines()[-1])["steps"]
        self.assertEqual([step.get("trace") for step in steps], ["step-01-ops-log.txt", None, "step-03-ops-log.txt"])
        self.assertEqual(steps[0]["command"], "tracer help --offline")
        # no file, no trace: gecho has no trace support
        code, out, _ = self.cli("run", str(s), "--gradle", "gecho", "--trace", "--keep-daemons", "--json")
        self.assertIn(f"{prop}={out_dir / 'step-01-ops'}", (out_dir / "step-01.log").read_text())
        self.assertNotIn("ops-log", out)
        # off by default
        code, out, _ = self.cli("run", str(s), "--gradle", "tracer", "--keep-daemons", "--json")
        self.assertEqual((out_dir / "step-01.log").read_text().strip(), f"{home_arg()} help")
        self.assertEqual(sorted(p.name for p in out_dir.glob("*ops*")), [])
        self.assertNotIn("trace", json.loads(out.rstrip("\n").splitlines()[-1])["steps"][0])


class InPlaceTest(GustCase):
    """setup.project and --project: the steps run in an existing directory, with nothing laid out."""

    STEPS = '[[steps]]\nrun.shell = "true"\n'

    def setUp(self):
        super().setUp()
        self.proj = self.tmp / "my proj"
        self.proj.mkdir()
        (self.proj / "settings.gradle.kts").write_text("")
        (self.proj / "a.txt").write_text("hello\n")

    def files(self):
        return sorted(p.relative_to(self.proj).as_posix() for p in self.proj.rglob("*"))

    def assert_rejects(self, text, message, **kw):
        with self.assertRaises(gs.GustError) as cm:
            gs.load_scenario(text, **kw)
        self.assertEqual(str(cm.exception), message)

    def test_key_and_flag(self):
        S = self.STEPS
        self.assertEqual(gs.load_scenario('setup.project = "my proj"\n' + S, base_dir=self.tmp).project, self.proj)
        self.assertEqual(gs.load_scenario(f'setup.project = "{self.proj.as_posix()}"\n' + S).project, self.proj)   # absolute
        self.assertIsNone(gs.load_scenario(S).project)
        other = self.tmp / "other"
        other.mkdir()
        # --project takes precedence, even over a missing dir, and a relative one resolves against the working directory
        self.assertEqual(gs.load_scenario('setup.project = "nope"\n' + S, base_dir=self.tmp, project=other).project, other)
        self.assertEqual(gs.load_scenario(S, base_dir=other, project=Path("my proj")).project, self.proj)
        self.assert_rejects('setup.project = "nope"\n' + S, f"setup.project: no such directory {self.tmp / 'nope'}", base_dir=self.tmp)
        self.assert_rejects('setup.project = "a.txt"\n' + S, f"setup.project: no such directory {self.proj / 'a.txt'}", base_dir=self.proj)
        self.assert_rejects('setup.project = "my proj"\n' + S, "setup.project: 'my proj' is relative but the scenario has no directory to resolve it against")
        self.assert_rejects('setup.project = 1\n' + S, "setup.project: must be str")
        self.assert_rejects(S, f"--project: no such directory {self.tmp / 'nope'}", project=Path("nope"))
        self.assert_rejects('setup.project = ""\n' + S, "setup.project: must not be empty", base_dir=self.tmp)   # "." says that
        # a leading ~ is expanded in both, as for --gradle; the path is kept as given, normalized, a symlink included
        os.environ["HOME"] = os.environ["USERPROFILE"] = str(self.tmp)                 # USERPROFILE on Windows
        self.assertEqual(gs.load_scenario('setup.project = "~/my proj"\n' + S).project, self.proj)
        self.assertEqual(gs.load_scenario(S, project=Path("~/my proj")).project, self.proj)
        self.assertEqual(gs.load_scenario('setup.project = "other/../my proj"\n' + S, base_dir=self.tmp).project, self.proj)
        if not gs.WINDOWS:                                                               # symlinks need privileges there
            (self.tmp / "link").symlink_to(self.proj)
            self.assertEqual(gs.load_scenario(S, project=self.tmp / "link").project, self.tmp / "link")
        # in place means the directory as it is: no setup.layout key at all, empty table included
        for layout in ('[setup.layout.project]\n"x" = "y"\n', 'setup.layout.remote = "https://github.com/o/r/commit/0123abcd"\n',
                       '[setup.layout.project]\n'):
            self.assert_rejects('setup.project = "my proj"\n' + layout + S,
                                "setup.project and setup.layout cannot be combined: in place, the project is used as it is", base_dir=self.tmp)
            self.assert_rejects(layout + S, "--project and setup.layout cannot be combined: in place, the project is used as it is",
                                project=self.proj)

    def test_edit_targets_are_checked_against_the_dir(self):
        edit = '[[steps]]\nedit = [{ file = "FILE", replace = "hello", with = "bye" }]\n'
        gs.load_scenario(edit.replace("FILE", "a.txt"), project=self.proj)
        self.assert_rejects(edit.replace("FILE", "b.txt"), f"steps[1].edit[1].file: 'b.txt' is not in {self.proj} and no earlier step writes it",
                            project=self.proj)
        self.assert_rejects('[[steps]]\nwrite."a.txt/x" = ""\n', f"steps[1].write: 'a.txt/x' conflicts with 'a.txt' ({self.proj}); one is a directory of the other",
                            project=self.proj)

    def test_case_variants_of_files_in_place_are_refused(self):
        (self.proj / "src").mkdir()
        (self.proj / "src" / "a.txt").write_text("a\n")
        for step, message in (('write."SRC/b.txt" = "b"', "steps[1].write: 'SRC' and 'src'"),
                              ('write."A.TXT" = "b"', "steps[1].write: 'A.TXT' and 'a.txt'"),
                              ('edit = [{ file = "src/A.txt", replace = "a", with = "b" }]', "steps[1].edit[1].file: 'src/A.txt' and 'src/a.txt'")):
            self.assert_rejects(f"[[steps]]\n{step}\n", f"{message} ({self.proj}) differ in case or Unicode form only, so they are one path on "
                                                      f"macOS or Windows", project=self.proj)
        gs.load_scenario('[[steps]]\nwrite."src/b.txt" = "b"\n', project=self.proj)

    def test_delete_and_clear_in_place(self):
        (self.proj / "build" / "libs").mkdir(parents=True)
        (self.proj / "build" / "libs" / "app.jar").write_text("")
        (self.proj / ".git" / "objects").mkdir(parents=True)
        self.assert_rejects('[[steps]]\nclear = ".GIT/objects"\n', "steps[1].clear: a git repository's .git is never changed by a scenario, "
                                                                  "got '.GIT/objects'", project=self.proj)
        self.assert_rejects('[[steps]]\ndelete = "a.txt"\n', f"steps[1].delete: 'a.txt' is a file ({self.proj}), not a directory", project=self.proj)
        out_dir = self.proj / "s.out"                                                   # the out dir and .gust inside the project
        os.environ[gs.GUST_DIR_ENV] = str(self.proj / ".gust")
        steps = '[[steps]]\nclear = "build"\n[[steps]]\ndelete = "nope"\n[[steps]]\nclear = "gen"\n[[steps]]\nrun.shell = "true"\n'
        summary = self.run_scenario(gs.load_scenario(steps, project=self.proj), out_dir)
        self.assertEqual(summary.result, "success", self.out.getvalue())
        self.assertIn("build cleared\n[2/4] Delete: nope\nnope was not there\n[3/4] Clear: gen\ngen created\n", self.out.getvalue())
        self.assertEqual(self.files(), [".git", ".git/objects", ".gust", ".gust/shared-gradle-user-home", "a.txt", "build", "gen",
                                        "s.out", "s.out/.gust-run", "s.out/step-04.log", "s.out/summary.json", "settings.gradle.kts"])
        for rel, own in (("s.out", out_dir), ("s.out/x", out_dir), (".gust", self.proj / ".gust"),
                         (".gust/shared-gradle-user-home", self.proj / ".gust")):
            summary = self.run_scenario(gs.load_scenario(f'[[steps]]\ndelete = "{rel}"\n', project=self.proj), out_dir)
            self.assertEqual(summary.steps[0].error, f"{rel} overlaps {own}, one of gust's own dirs, so it is left alone")
        self.assertTrue((self.proj / ".gust" / "shared-gradle-user-home").is_dir())

    def test_run_changes_the_real_dir(self):
        s = self.scenario_file('setup.project = "my proj"\n[[steps]]\nwrite."b.txt" = "written"\n'
                               '[[steps]]\nedit = [{ file = "a.txt", replace = "hello", with = "edited" }]\n'
                               '[[steps]]\nrun.shell = "grep -q edited a.txt && test -f b.txt"\n')
        out_dir = self.tmp / "s.out"
        shutil.copytree(self.proj, self.tmp / "other")                              # for a second run, below
        code, out, _ = self.cli("run", str(s), "--keep-daemons", "--json")
        self.assertEqual(code, gs.EXIT_OK, out)
        self.assertTrue(out.startswith(f"scenario: s\nout:      {out_dir}\nproject:  {self.proj} (in place)\n"
                                       f"home:     {self.gust / gs.SHARED_HOME}\ngradle:   gradle (default)\n[1/3]"), out)
        self.assertEqual(((self.proj / "a.txt").read_text(), (self.proj / "b.txt").read_text()), ("edited\n", "written"))
        self.assertEqual(sorted(p.name for p in out_dir.iterdir()), [gs.MARKER, "step-03.log", "summary.json"])   # no project/
        summary = json.loads(out.rstrip("\n").splitlines()[-1])
        self.assertEqual((summary["project"], summary["out"]), (str(self.proj), str(out_dir)))
        self.assertEqual(json.loads((out_dir / "summary.json").read_text()), summary)
        # on a rerun only the out dir is recreated; --project takes precedence over setup.project
        (out_dir / "junk").write_text("")
        before = self.files()
        code, out, _ = self.cli("run", str(s), "--keep-daemons", "--project", "other")
        self.assertEqual(code, gs.EXIT_OK, out)
        self.assertIn(f"project:  {self.tmp / 'other'} (in place)\n", out)
        self.assertFalse((out_dir / "junk").exists())
        self.assertEqual(self.files(), before)
        self.assertEqual((self.tmp / "other" / "b.txt").read_text(), "written")
        # from stdin, a relative setup.project resolves against the working directory; the same out dir again
        code, out, _ = self.cli("run", "-", "--keep-daemons", "--out", str(out_dir),
                                stdin='setup.project = "my proj"\n[[steps]]\nrun.shell = "test -f b.txt"\n')
        self.assertEqual(code, gs.EXIT_OK, out)
        self.assertIn(f"project:  {self.proj} (in place)\n", out)
        self.assertEqual(self.files(), before)                                      # b.txt from the first run still there
        # an out dir that holds the project dir is refused, as it would be removed on a rerun
        code, out, err = self.cli("run", str(s), "--project", "my proj", "--out", str(self.proj))
        self.assertEqual((code, out), (gs.EXIT_ERROR, ""))
        self.assertEqual(err.strip(), f"ERROR: out dir {self.proj} contains the project dir {self.proj}; it would be removed with the earlier run")
        self.assertEqual(self.files(), before)

    def test_gradle_default_is_the_dirs_wrapper(self):
        self.fake_gradle_on_path()
        write_script(self.proj, "gradlew", FAKE_GRADLE)
        scenario = gs.load_scenario('[[steps]]\nrun.gradle = { args = "help", expect = { output = ["gradlew help"] } }\n', project=self.proj)
        summary = self.run_scenario(scenario, self.tmp / "s.out", stop_daemons=True)
        self.assertEqual((summary.result, summary.gradle, summary.project), ("success", WRAPPER, str(self.proj)))
        self.assertIn(f"gradle:   {WRAPPER} 9.7.1 (default)\n", self.out.getvalue())
        self.assertEqual((self.tmp / "s.out" / "stop-daemons-before.log").read_text().strip(), "gradlew --stop")
        # a version is refused, before the out dir or the project is touched
        before = self.files()
        for gradle, text, label in ((None, 'setup.gradle = "9.7.1"\n', "setup.gradle '9.7.1'"), ("9.7.1", "", "--gradle '9.7.1'")):
            with self.assertRaises(gs.GustError) as cm:
                self.run_scenario(gs.load_scenario(text + self.STEPS, project=self.proj), self.tmp / "v.out", gradle=gradle)
            self.assertEqual(str(cm.exception), f"{label} is a version, but no wrapper is installed into a project in place; pass a binary")
        self.assertFalse((self.tmp / "v.out").exists())
        self.assertEqual(self.files(), before)
        # the wrapper by name: --gradle ./gradlew and setup.gradle = "wrapper"; stop-daemons takes it by default too
        help_step = '[[steps]]\nrun.gradle = "help"\n'
        self.out = io.StringIO()
        self.assertEqual(self.run_scenario(scenario, self.tmp / "s.out", gradle=WRAPPER).gradle, WRAPPER)
        self.assertEqual(self.run_scenario(gs.load_scenario('setup.gradle = "wrapper"\n' + help_step, project=self.proj),
                                           self.tmp / "s.out").gradle, WRAPPER)
        self.assertIn(f"gradle:   {WRAPPER} 9.7.1 (--gradle)\n", self.out.getvalue())
        self.assertIn(f"gradle:   {WRAPPER} 9.7.1 (scenario)\n", self.out.getvalue())
        code, out, _ = self.cli("stop-daemons", str(self.scenario_file(help_step)), "--project", "my proj")
        self.assertEqual(code, gs.EXIT_OK, out)
        self.assertIn(f"gradle:   {WRAPPER} 9.7.1 (default)\n[STOP] Stop Gradle daemons\n$ {WRAPPER} --stop\n", out)
        # without the wrapper: gradle from PATH, and naming the wrapper is refused
        for name in ("gradlew.py", runnable("gradlew")):
            (self.proj / name).unlink()
        self.out = io.StringIO()
        summary = self.run_scenario(gs.load_scenario(help_step, project=self.proj), self.tmp / "s.out")
        self.assertEqual(summary.gradle, "gradle")
        self.assertIn("gradle:   gradle 9.7.1 (default)\n", self.out.getvalue())
        for text, gradle, message in (('setup.gradle = "wrapper"\n', None, f'setup.gradle = "wrapper" but {WRAPPER_FILE} is not among the project\'s files'),
                                      ("", WRAPPER, f"--gradle {WRAPPER!r} is relative to the project dir but no such file is in the project")):
            with self.assertRaises(gs.GustError) as cm:
                self.run_scenario(gs.load_scenario(text + help_step, project=self.proj), self.tmp / "s.out", gradle=gradle)
            self.assertEqual(str(cm.exception), message)

    def test_out_dir_inside_the_project(self):
        s = self.scenario_file('setup.project = "my proj"\n' + self.STEPS)
        before = self.files()
        for _ in range(2):                                                               # on the second run, the first one's out dir is cleared
            code, out, _ = self.cli("run", str(s), "--keep-daemons", "--out", str(self.proj / "s.out"))
            self.assertEqual(code, gs.EXIT_OK, out)
        self.assertEqual(self.files(), sorted(before + ["s.out", f"s.out/{gs.MARKER}", "s.out/step-01.log", "s.out/summary.json"]))
        self.assertEqual((self.proj / "a.txt").read_text(), "hello\n")

    def test_check_setup_and_stop_daemons(self):
        s = self.scenario_file('setup.project = "my proj"\n[[steps]]\nedit = [{ file = "a.txt", replace = "hello", with = "bye" }]\n')
        out_dir = self.tmp / "s.out"
        code, out, _ = self.cli("check", str(s), "--json", "--keep-daemons")
        self.assertEqual(code, gs.EXIT_OK, out)
        self.assertTrue(out.startswith(f"scenario: s\nproject:  {self.proj} (in place)\nhome:     "), out)
        self.assertEqual(json.loads(out.rstrip("\n").splitlines()[-1])["project"], str(self.proj))
        bare = self.scenario_file('[[steps]]\nedit = [{ file = "a.txt", replace = "hello", with = "bye" }]\n', name="t.toml")
        code, _, err = self.cli("check", str(bare))
        self.assertEqual((code, err.strip()), (gs.EXIT_ERROR, f"ERROR: {bare}: steps[1].edit[1].file: 'a.txt' is not in the layout and no earlier step writes it"))
        self.assertEqual(self.cli("check", str(bare), "--project", "my proj", "--keep-daemons")[0], gs.EXIT_OK)
        # setup has nothing to lay out, and no --project
        code, out, err = self.cli("setup", str(s))
        self.assertEqual((code, out, err.strip()), (gs.EXIT_ERROR, "", f"ERROR: nothing to lay out: the project {self.proj} is used in place, as it is"))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            gs.main(["setup", str(bare), "--project", "my proj"])
        self.assertNotIn("--project", self.cli("help", "setup")[1])
        self.assertFalse(out_dir.exists())
        # stop-daemons needs no earlier run: the out dir is made for its log, and kept when it is gust's
        code, out, _ = self.cli("stop-daemons", str(s), "--gradle", "gecho")
        self.assertEqual(code, gs.EXIT_OK, out)
        self.assertIn(f"project:  {self.proj} (in place)\nhome:     ", out)
        self.assertEqual(sorted(p.name for p in out_dir.iterdir()), [gs.MARKER, "stop-daemons.log"])
        self.assertEqual((out_dir / "stop-daemons.log").read_text().strip(), f"{home_arg()} --stop")
        (out_dir / "step-01.log").write_text("from a run")
        self.assertEqual(self.cli("stop-daemons", str(bare), "--project", "my proj", "--out", str(out_dir), "--gradle", "gecho")[0], gs.EXIT_OK)
        self.assertTrue((out_dir / "step-01.log").is_file())
        (self.tmp / "foreign").mkdir()
        (self.tmp / "foreign" / "precious").write_text("")
        code, _, err = self.cli("stop-daemons", str(s), "--out", str(self.tmp / "foreign"), "--gradle", "gecho")
        self.assertEqual(code, gs.EXIT_ERROR)
        self.assertIn("was not created by gust, so it is left alone", err)
        self.assertEqual(self.files(), ["a.txt", "settings.gradle.kts"])
        code, out, _ = self.cli("help", "run")
        self.assertIn("--project DIR", out)

    def test_flat_keeps_the_key(self):
        code, out, err = self.cli("flat", str(self.scenario_file('[setup]\nproject = "my proj"\n' + self.STEPS)))
        self.assertEqual((code, out, err), (gs.EXIT_OK, 'setup.project = "my proj"\n\n' + self.STEPS, ""))


class FlatTest(GustCase):
    def setUp(self):
        super().setUp()
        (self.tmp / "layouts").mkdir()
        (self.tmp / "layouts" / "java.toml").write_text('[project]\n"settings.gradle.kts" = "base settings"\n"src/A.java" = "base A"\n"src/B.java" = "base B"\n')

    def flat(self, text, name="s.toml"):
        """flat of a scenario file in the temp dir: (exit code, stdout, stderr)."""
        return self.cli("flat", str(self.scenario_file(text, name)))

    def test_base_only(self):
        code, out, err = self.flat('setup.layout.base = "layouts/java.toml"\n[[steps]]\nrun.shell = "true"\n')
        self.assertEqual((code, err), (gs.EXIT_OK, ""))
        self.assertEqual(out, '[setup.layout.project]\n"settings.gradle.kts" = "base settings"\n"src/A.java" = "base A"\n'
                              '"src/B.java" = "base B"\n\n[[steps]]\nrun.shell = "true"\n')

    def test_inline_files_win_over_base(self):
        code, out, _ = self.flat("""
name = "x"
[setup]
gradle = "9.7.1"
layout.base = "layouts/java.toml"
[setup.layout.project]
"src/B.java" = "own B"
"src/C.java" = "own C"
[[steps]]
run.shell = "true"
""")
        self.assertEqual(code, gs.EXIT_OK)
        self.assertEqual(out, 'name = "x"\n\n[setup]\ngradle = "9.7.1"\n\n[setup.layout.project]\n'
                              '"settings.gradle.kts" = "base settings"\n"src/A.java" = "base A"\n"src/B.java" = "own B"\n'
                              '"src/C.java" = "own C"\n\n[[steps]]\nrun.shell = "true"\n')
        source = gs.load_scenario((self.tmp / "s.toml").read_text(), base_dir=self.tmp)
        self.assertEqual(gs.load_scenario(out), source)
        self.assertEqual(list(gs.load_scenario(out).files), list(source.files))           # laid out in the same order as well

    def test_without_base_the_scenario_is_normalized(self):
        code, out, _ = self.flat('[[steps]]\nname = "n"\nwrite.a = "x"\n[[steps]]\nedit = [{ file = "a", replace = "x", with = "y" }]\n'
                                 '[[steps]]\nrun.gradle = { args = "test", expect = "fail" }\n')
        self.assertEqual(code, gs.EXIT_OK)
        self.assertEqual(out, '[[steps]]\nname = "n"\n[steps.write]\na = "x"\n\n[[steps]]\n[[steps.edit]]\nfile = "a"\n'
                              'replace = "x"\nwith = "y"\n\n[[steps]]\nrun.gradle = { args = "test", expect = "fail" }\n')   # no name added

    def test_remote_left_as_a_link(self):
        link = "https://github.com/o/r/tree/0123456789abcdef/sub"
        code, out, _ = self.flat(f'[[steps]]\nrun.gradle = "test"\n[setup]\ngradle = "wrapper"\nlayout.remote = "{link}"\n')
        self.assertEqual((code, out), (gs.EXIT_OK, f'[[steps]]\nrun.gradle = "test"\n\n[setup]\ngradle = "wrapper"\nlayout.remote = "{link}"\n'))
        code, out, _ = self.flat(f'[[steps]]\nrun.gradle = "test"\n[setup]\nlayout.remote = "{link}"\n[setup.layout.project]\n"a.txt" = "a"\n')
        self.assertEqual(out, f'[[steps]]\nrun.gradle = "test"\n\n[setup.layout]\nremote = "{link}"\n\n[setup.layout.project]\n"a.txt" = "a"\n')

    def test_contents_that_need_escaping(self):
        files = {"q.txt": "has ''' inside\n", "bs.txt": "C:\\path\\n\n", "quotes.txt": "say \"hi\" and 'bye'", "cr.txt": "a\r\nb\n",
                 "one.txt": 'x = "y"', "lead.txt": "\nstarts with a newline\n", "tab.txt": "a\tb\n", "end.txt": "ends in ''"}
        text = "[[steps]]\nrun.shell = \"true\"\n[setup.layout.project]\n" + "".join(
            f"{gs._key(p)} = {gs._string(c)}\n" for p, c in files.items())
        out = gs.flatten(text, "t")
        self.assertEqual(gs.load_scenario(out).files, files)
        self.assertIn('"q.txt" = "has \'\'\' inside\\n"\n', out)                          # ''' inside: an escaped basic string
        self.assertIn('"bs.txt" = \'\'\'\nC:\\path\\n\n\'\'\'\n', out)                    # backslashes as they are, in '''
        self.assertIn('"quotes.txt" = "say \\"hi\\" and \'bye\'"\n', out)
        self.assertIn('"cr.txt" = "a\\r\\nb\\n"\n', out)
        self.assertIn('"one.txt" = \'x = "y"\'\n', out)
        self.assertIn('"lead.txt" = \'\'\'\n\nstarts with a newline\n\'\'\'\n', out)     # the trimmed newline, then the content's own
        self.assertIn('"end.txt" = "ends in \'\'"', out)
        self.assertEqual(gs.flatten(out, "t"), out)

    def test_leading_comment_block_kept(self):
        code, out, _ = self.flat('# A scenario.\n#   indented, kept as is\n\n# more\nname = "x" # lost\n'
                                 '[[steps]]\n# lost too\nrun.shell = "true"\n')
        self.assertEqual(out, '# A scenario.\n#   indented, kept as is\n\n# more\nname = "x"\n\n[[steps]]\nrun.shell = "true"\n')
        code, out, _ = self.flat('\n\nname = "x"\n[[steps]]\nrun.shell = "true"\n')
        self.assertEqual(out, 'name = "x"\n\n[[steps]]\nrun.shell = "true"\n')          # blank lines alone are not a comment block

    def test_a_comment_with_a_unicode_line_separator_is_kept(self):
        for sep in ("\u2028", "\u2029", "\u0085"):                                    # line breaks for Python, not for TOML
            with self.subTest(sep=repr(sep)):
                path = self.tmp / "sep.toml"
                path.write_bytes(f'# a{sep}b\n[[steps]]\nrun.shell = "true"\n'.encode("utf-8"))   # UTF-8 on Windows too
                code, out, err = self.cli("flat", str(path))
                self.assertEqual((code, err), (gs.EXIT_OK, ""))
                self.assertEqual(out, f'# a{sep}b\n[[steps]]\nrun.shell = "true"\n')

    def test_stdin_with_relative_base(self):
        code, out, err = self.cli("flat", "-", stdin='setup.layout.base = "layouts/java.toml"\n[[steps]]\nrun.shell = "true"\n')
        self.assertEqual((code, err), (gs.EXIT_OK, ""))
        self.assertIn('"src/A.java" = "base A"\n', out)
        self.assertNotIn("base =", out)
        code, out, err = self.cli("flat", "-", stdin='setup.layout.base = "nope.toml"\n[[steps]]\nrun.shell = "true"\n')
        self.assertEqual((code, out), (gs.EXIT_ERROR, ""))
        self.assertEqual(err, f"ERROR: stdin: setup.layout.base: no such file {self.tmp / 'nope.toml'}\n")
        code, out, err = self.cli("flat", "-", "--", "x", stdin="")
        self.assertEqual((code, out), (gs.EXIT_ERROR, ""))

    def test_values_outside_the_format_refused(self):
        for value in (1.5, __import__("datetime").date(2026, 9, 23)):
            with self.assertRaises(gs.GustError) as cm:
                gs.to_toml({"steps": [{"run": {"shell": value}}]})
            self.assertIn(f"steps.run.shell: a {type(value).__name__} value is not part of the scenario format", str(cm.exception))
        self.assertEqual(gs.to_toml({"a": [1, True, "s"], "t": {}}), 'a = [1, true, "s"]\nt = {}\n')

    def test_output_that_does_not_parse_back_is_not_printed(self):
        self.patch("to_toml", lambda data: 'name = "other"\n[[steps]]\nrun.shell = "true"\n')
        code, out, err = self.flat(MINIMAL)
        self.assertEqual((code, out), (gs.EXIT_ERROR, ""))
        self.assertIn("the flattened TOML, once parsed, differs from the scenario", err)

    def test_round_trip(self):
        readme = Path(gs.__file__).with_name("README.md").read_text(encoding="utf-8")
        toml = re.search(r"```toml\n(.*?)```", readme, re.S).group(1)
        self.assertEqual(gs.flatten(toml, "README"), toml)
        long = ('[[steps]]\nrun.gradle = { args = "assemble", expect = { output = ["' + "x" * 40 + '", "' + "y" * 40 + '"] } }\n'
                '[setup]\nlayout.base = "layouts/java.toml"\n')
        once = gs.flatten(long, "t", base_dir=self.tmp)
        self.assertIn('run.gradle.args = "assemble"\nrun.gradle.expect.output = ["', once)       # too long for one inline table
        self.assertEqual(gs.flatten(once, "t"), once)


class ZipTest(GustCase):
    """gust zip: the project's starting state and a SCENARIO.md in a zip, with no Gradle run."""

    def remote(self, files, name="repo"):
        """A local git repository with one commit of files, path -> (git mode, bytes), made with git's plumbing, so
        that any name and mode is possible on any file system. Returns its setup.layout.remote link."""
        repo = self.tmp / name                     # a new one for each name: git's objects are read-only on Windows
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}

        def git(*args, data=None):
            return subprocess.run(["git", *args], cwd=repo, input=data, check=True, capture_output=True, env=env).stdout.decode().strip()
        repo.mkdir()
        git("init", "-q")
        for rel, (mode, data) in files.items():
            git("update-index", "--add", "--cacheinfo", f"{mode},{git('hash-object', '-w', '--stdin', data=data)},{rel}")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "one")
        return f"{repo.as_posix()}#{git('rev-parse', 'HEAD')}"

    def readme(self, text, gradle_version=None):
        return gs.zip_files(gs.load_scenario(text, base_dir=self.tmp), gradle_version)[gs.SCENARIO_MD][1].decode("utf-8")

    def entries(self, zip_path):
        """name -> (mode, date_time, bytes) of every entry, in the zip's order."""
        with zipfile.ZipFile(zip_path) as zf:
            return {i.filename: (i.external_attr >> 16, i.date_time, zf.read(i)) for i in zf.infolist()}

    def run_snippets(self, readme, cwd, shell=None):
        """Run each sh block of a SCENARIO.md in cwd, in order, as a reader would; with shell, run by that one."""
        for _, body in re.findall(r"^(`{3,})sh\n(.*?)\n\1$", readme, re.M | re.S):
            subprocess.run([shell, "-c", body] if shell else gs.shell_argv(body), cwd=cwd, check=True, capture_output=True)

    def test_each_step_kind(self):
        text = (
            'name = "kinds"\ndescription = "Every kind of step."\nsetup.gradle = "9.7.1"\n'
            '[setup.layout.project]\n"settings.gradle.kts" = "rootProject.name = \\"kinds\\"\\n"\n'
            '"build.gradle.kts" = "plugins { java }\\n// one\\n// two\\n// three\\n// four\\n"\n'
            '[[steps]]\nrun.gradle = { args = "build --info", expect = { exit = "fail", output = ["BUILD FAILED", "two\\nlines"], no_output = ["BUILD SUCCESSFUL"] } }\n'
            '[[steps]]\nname = "shell"\nrun.shell = "./gradlew help && \\"$GRADLE\\" help $GRADLE_ARGS && echo $GRADLE_USER_HOME ./gradlew.bat"\n'
            '[[steps]]\nwrite = { "src/main/java/A.java" = "class A {}\\n", "notes.txt" = "x\\n" }\n'
            '[[steps]]\nname = """edit\nthe build"""\nedit = [{ file = "build.gradle.kts", replace = "java", with = "java-library" }]\n'
            '[[steps]]\nclear = "src/main"\n'
            '[[steps]]\nwrite."src/main/java/B.java" = "class B {}\\n"\n'
            '[[steps]]\ndelete = "build"\n'
        )
        self.assertEqual(self.readme(text), """\
# kinds

Every kind of step.

Needs Gradle 9.7.1, as `gradle` on PATH.

## Steps

Run each `sh` block from the directory this file is in, in order. What to expect from a block is in the text above it.

In the shell steps, Gradle is `$GRADLE`, and any extra arguments are in `$GRADLE_ARGS`. Set them first:

```sh
GRADLE=gradle
GRADLE_ARGS=
```

### 1. Run: gradle build --info

Expected: fails, and the output contains `BUILD FAILED` and text 1 below, but not `BUILD SUCCESSFUL`.

Text 1:

```text
two
lines
```

```sh
gradle build --info
```

### 2. shell

```sh
gradle help && "$GRADLE" help $GRADLE_ARGS && echo $GRADLE_USER_HOME gradle
```

### 3. Write: A.java, notes.txt

```sh
mkdir -p src/main/java &&
cat > src/main/java/A.java <<'EOF' &&
class A {}
EOF
cat > notes.txt <<'EOF'
x
EOF
```

### 4. edit ...

```sh
patch -p1 <<'EOF'
--- a/build.gradle.kts
+++ b/build.gradle.kts
@@ -1,4 +1,4 @@
-plugins { java }
+plugins { java-library }
 // one
 // two
 // three
EOF
```

### 5. Clear: src/main

```sh
rm -rf src/main && mkdir src/main
```

### 6. Write: B.java

```sh
mkdir -p src/main/java &&
cat > src/main/java/B.java <<'EOF'
class B {}
EOF
```

### 7. Delete: build

```sh
rm -rf build
```
""")
        self.assertIn("Needs Gradle 8.0, as", self.readme(text, "8.0"))                    # --gradle over the scenario
        self.assertIn("Needs Gradle, as `gradle` on PATH.", self.readme(text.replace('setup.gradle = "9.7.1"\n', "")))

    def test_writes_that_a_plain_heredoc_cannot_hold(self):
        contents = {
            "eof.txt": "line\nEOF\n'EOF'\nEOF2\n",                   # a line that is the delimiter: EOF3
            "crlf.txt": "one\r\ntwo\r\n",                             # carriage returns: printf, with \r
            "open.txt": "no final newline, it's $HOME and \\n",      # no final newline: printf '%s'
            "dash.txt": "-n 100% \\ \x01\r",                          # a leading -, %, a backslash, a control character
            "empty.txt": "",
            "tab.txt": "\tindented\n",
            "fence.md": "```kotlin\nx\n```\n",                        # a longer fence around the block
        }
        table = "\n".join(f"{json.dumps(k)} = {json.dumps(v)}" for k, v in contents.items())
        readme = self.readme(f'[[steps]]\n[steps.write]\n{table}\n')
        self.assertIn("cat > eof.txt <<'EOF3' &&\nline\nEOF\n'EOF'\nEOF2\nEOF3\n", readme)
        self.assertIn("printf 'one\\r\ntwo\\r\n' > crlf.txt &&\n", readme)
        self.assertIn("""printf '%s' 'no final newline, it'"'"'s $HOME and \\n' > open.txt &&\n""", readme)
        self.assertIn("printf '\\055n 100%% \\\\ \\001\\r' > dash.txt &&\n", readme)
        self.assertIn("printf '%s' '' > empty.txt &&\n", readme)
        self.assertIn("````sh\n", readme)
        self.assertNotIn("\r", readme)
        project = self.tmp / "p"
        project.mkdir()
        self.run_snippets(readme, project)
        for rel, content in contents.items():
            self.assertEqual((project / rel).read_bytes(), content.encode(), rel)

    def test_edits(self):
        text = ('[setup.layout.project]\n"open.txt" = "a\\nb"\n"crlf.txt" = "one\\r\\ntwo\\r\\n"\n"same.txt" = "x\\n"\n'
                '"my file.txt" = "x\\n"\n'
                '[[steps]]\nedit = [{ file = "open.txt", replace = "b", with = "c" }, { file = "crlf.txt", replace = "two", with = "deux" },'
                ' { file = "same.txt", replace = "x", with = "x" }, { file = "my file.txt", replace = "x", with = "y" }]\n')
        readme = self.readme(text)
        self.assertIn("patch -p1 <<'EOF' &&\n--- a/open.txt\n+++ b/open.txt\n@@ -1,2 +1,2 @@\n a\n-b\n\\ No newline at end of file\n"
                      "+c\n\\ No newline at end of file\nEOF\n", readme)
        self.assertIn("\nThe file `crlf.txt` is written anew as a whole, instead of with a diff for patch.\n", readme)
        self.assertIn("\nprintf 'one\\r\ndeux\\r\n' > crlf.txt &&\n", readme)
        self.assertIn("\nThe file `same.txt` stays as it is: its text is replaced with the same text.\n", readme)
        self.assertIn("cat > 'my file.txt' <<'EOF'\ny\nEOF\n", readme)
        if shutil.which("patch"):
            project = self.tmp / "p"
            project.mkdir()
            for rel, content in gs.load_scenario(text).files.items():
                (project / rel).write_bytes(content.encode())
            self.run_snippets(readme, project)
            self.assertEqual([(project / f).read_bytes() for f in ("open.txt", "crlf.txt", "same.txt", "my file.txt")],
                             [b"a\nc", b"one\r\ndeux\r\n", b"x\n", b"y\n"])
        with self.assertRaises(gs.GustError) as cm:            # a multi-line replace that fits only with CRLF read as LF
            self.readme('[setup.layout.project]\n"c.txt" = "a\\r\\nb\\r\\n"\n[[steps]]\nedit = [{ file = "c.txt", replace = "a\\nb\\nc", with = "" }]\n')
        self.assertEqual(str(cm.exception), "steps[1].edit[1].replace: 'a\\nb\\nc' occurs 0 times in c.txt; an edit needs exactly one")
        crlf = '[setup.layout.project]\n"c.txt" = "a\\r\\nb\\r\\n"\n[[steps]]\nedit = [{ file = "c.txt", replace = "a\\nb", with = "x\\ny" }]\n'
        self.assertIn("printf 'x\\r\ny\\r\n' > c.txt\n", self.readme(crlf))       # LF matches CRLF, and is written as CRLF there

    def test_edits_split_lines_at_lf_only(self):
        text = '[setup.layout.project]\n"u.txt" = "one\u2028two\\u0085\\ntwo\\nthree\\n"\n[[steps]]\nedit = [{ file = "u.txt", replace = "three", with = "3" }]\n'
        readme = self.readme(text)
        self.assertIn("@@ -1,3 +1,3 @@\n one\u2028two\u0085\n two\n-three\n+3\nEOF\n", readme)
        if shutil.which("patch"):
            project = self.tmp / "p"
            project.mkdir()
            (project / "u.txt").write_bytes("one\u2028two\u0085\ntwo\nthree\n".encode())
            self.run_snippets(readme, project)
            self.assertEqual((project / "u.txt").read_bytes(), "one\u2028two\u0085\ntwo\n3\n".encode())

    def test_paths_with_a_leading_dash(self):
        readme = self.readme('[[steps]]\nwrite = { "-d/-f.txt" = "f\\n", "-g.txt" = "g" }\n[[steps]]\nclear = "-d"\n'
                             '[[steps]]\nwrite."-e/x.txt" = "x\\n"\n[[steps]]\ndelete = "-e"\n')
        self.assertIn("mkdir -p ./-d &&\ncat > ./-d/-f.txt <<'EOF' &&\n", readme)
        self.assertIn("rm -rf ./-d && mkdir ./-d\n", readme)
        project = self.tmp / "p"
        project.mkdir()
        self.run_snippets(readme, project)
        self.assertEqual(sorted(p.relative_to(project).as_posix() for p in project.rglob("*")), ["-d", "-g.txt"])
        self.assertEqual((project / "-g.txt").read_text(), "g")

    def test_byte_identical_across_runs_and_dirs(self):
        scenario = self.scenario_file('description = "d"\n[setup.layout.project]\n"b.txt" = "b\\n"\n"a/x.txt" = "x\\n"\n'
                                      '[[steps]]\nrun.gradle = "help"\n', "same.toml")
        (self.tmp / "one").mkdir()
        (self.tmp / "two").mkdir()
        self.assertEqual(self.cli("zip", str(scenario), "-o", "one/same.zip")[0], gs.EXIT_OK)
        os.chdir(self.tmp / "two")
        time.sleep(1.1)                                      # a different second, which a timestamp would show
        self.assertEqual(self.cli("zip", str(scenario))[0], gs.EXIT_OK)
        first, second = (self.tmp / "one" / "same.zip").read_bytes(), (self.tmp / "two" / "same.zip").read_bytes()
        self.assertEqual(first, second)
        entries = self.entries(self.tmp / "one" / "same.zip")
        self.assertEqual(list(entries), ["same/", "same/SCENARIO.md", "same/a/", "same/a/x.txt", "same/b.txt"])
        self.assertEqual({e[1] for e in entries.values()}, {(1980, 1, 1, 0, 0, 0)})
        self.assertEqual([e[0] for e in entries.values()], [0o40755, 0o100644, 0o40755, 0o100644, 0o100644])
        readme = entries["same/SCENARIO.md"][2].decode()
        self.assertTrue(readme.startswith("# same\n\nd\n\nNeeds Gradle, as `gradle` on PATH.\n"))
        self.assertNotIn(str(self.tmp), readme)
        self.assertNotIn(gs.__version__, readme)

    def test_top_folder_and_stdout(self):
        scenario = self.scenario_file('[[steps]]\nrun.gradle = "help"\n', "loop.toml")
        code, out, err = self.cli("zip", str(scenario))
        self.assertEqual((code, out, err), (gs.EXIT_OK, f"{self.tmp / 'loop.zip'}\n", ""))
        self.assertEqual(list(self.entries(self.tmp / "loop.zip")), ["loop/", "loop/SCENARIO.md"])
        (self.tmp / "desk top").mkdir()
        code, out, err = self.cli("zip", str(scenario), "-o", str(Path("desk top") / "repro-39193.zip"))
        self.assertEqual((code, out, err), (gs.EXIT_OK, f"{self.tmp / 'desk top' / 'repro-39193.zip'}\n", ""))
        self.assertEqual(list(self.entries(self.tmp / "desk top" / "repro-39193.zip")), ["repro-39193/", "repro-39193/SCENARIO.md"])
        code, out, err = self.cli("zip", "-", stdin='name = "piped"\n[[steps]]\nrun.gradle = "help"\n')
        self.assertEqual((code, out, err), (gs.EXIT_OK, f"{self.tmp / 'piped.zip'}\n", ""))
        self.assertFalse((self.gust / gs.SHARED_HOME).exists())    # no Gradle user home, since no Gradle runs
        for name in ("...zip", ".zip", "a b.zip"):                     # the top folder: a name, as a scenario's
            code, out, err = self.cli("zip", str(scenario), "-o", name)
            self.assertEqual((code, out), (gs.EXIT_ERROR, ""), name)
            self.assertTrue(err.startswith("ERROR: the zip's top folder would be "), err)
            self.assertFalse((self.tmp / name).exists())

    def test_wrapper_left_out_and_remote_files_from_the_commit(self):
        link = self.remote({
            "settings.gradle": ("100644", b"// remote\n"),
            "keep.txt": ("100644", b"line\r\n"),                      # committed as CRLF, so kept as CRLF
            "gradlew": ("100755", b"#!/bin/sh\n"),
            "gradlew.bat": ("100644", b"@rem\n"),
            "gradle/wrapper/gradle-wrapper.jar": ("100644", b"PK\0\0"),
            "gradle/wrapper/gradle-wrapper.properties": ("100644", b"distributionUrl=https\\://services.gradle.org/distributions/gradle-8.14.3-bin.zip\n"),
            "gradle/libs.versions.toml": ("100644", b"[versions]\n"),
            "tools/run.sh": ("100755", b"#!/bin/sh\n"),
            "na\u00efve.txt": ("100644", b"utf-8 name\n"),
        })
        scenario = self.scenario_file(f'setup.gradle = "wrapper"\nsetup.layout.remote = "{link}"\n'
                                      '[setup.layout.project]\n"tools/run.sh" = "#!/bin/sh\\necho own\\n"\n'
                                      '[[steps]]\nrun.shell = "./gradlew test && ../gradlew help"\n', "remote.toml")
        code, out, err = self.cli("zip", str(scenario))
        self.assertEqual((code, err), (gs.EXIT_OK, ""), err)
        entries = self.entries(self.tmp / "remote.zip")
        self.assertEqual(list(entries), ["remote/", "remote/SCENARIO.md", "remote/gradle/", "remote/gradle/libs.versions.toml",
                                         "remote/keep.txt", "remote/na\u00efve.txt", "remote/settings.gradle", "remote/tools/",
                                         "remote/tools/run.sh"])
        self.assertEqual(entries["remote/tools/run.sh"][0], 0o100755)            # the executable bit of the commit
        self.assertEqual(entries["remote/tools/run.sh"][2], b"#!/bin/sh\necho own\n")   # the layout's file, over the remote's
        self.assertEqual(entries["remote/keep.txt"][2], b"line\r\n")
        readme = entries["remote/SCENARIO.md"][2].decode()
        self.assertIn("Needs Gradle 8.14.3, as `gradle` on PATH.\n", readme)    # from the wrapper's distributionUrl
        self.assertIn("```sh\ngradle test && gradle help\n```", readme)
        self.assertNotIn(link.partition("#")[0], readme)
        self.assertNotIn(link.partition("#")[2], readme)
        scenario.write_text(scenario.read_text().replace('setup.gradle = "wrapper"\n', ""))   # gust runs the project's gradlew then too
        self.assertEqual(self.cli("zip", str(scenario), "-o", "default.zip")[0], gs.EXIT_OK)
        self.assertIn("Needs Gradle 8.14.3, as", self.entries(self.tmp / "default.zip")["default/SCENARIO.md"][2].decode())
        for step, message in (
                ('edit = [{ file = "gradle/wrapper/gradle-wrapper.properties", replace = "8", with = "9" }]',
                 "steps[2].edit[1].file: 'gradle/wrapper/gradle-wrapper.properties' is left out of the zip with the wrapper, so it cannot be edited there"),
                ('run.shell = "test -x gradlew"', "steps[2].run.shell: the command uses the wrapper ('gradlew'), which is left out of the zip"),
                ('run.shell = "cat gradle/wrapper/gradle-wrapper.properties"', "steps[2].run.shell: the command uses the wrapper ('gradle/wrapper'), which is left out of the zip")):
            (self.tmp / "more.toml").write_text(scenario.read_text() + f"[[steps]]\n{step}\n")
            code, out, err = self.cli("zip", str(self.tmp / "more.toml"))
            self.assertEqual((code, out, err), (gs.EXIT_ERROR, "", f"ERROR: {message}\n"))
            self.assertFalse((self.tmp / "more.zip").exists())

    def test_names_that_differ_in_case_only(self):
        link = self.remote({"A.txt": ("100644", b"upper\n"), "a.txt": ("100644", b"lower\n")})
        scenario = self.scenario_file(f'setup.layout.remote = "{link}"\n[[steps]]\nrun.gradle = "help"\n', "case.toml")
        self.assertEqual(self.cli("zip", str(scenario))[0], gs.EXIT_OK)
        entries = self.entries(self.tmp / "case.zip")
        self.assertEqual((entries["case/A.txt"][2], entries["case/a.txt"][2]), (b"upper\n", b"lower\n"))

    def test_links_that_leave_the_project_are_refused(self):
        for i, target in enumerate(("/etc/passwd", "../../outside", "x/../../../y", "C:/x")):   # from sub/: ../a.txt is inside
            with self.subTest(target=target):
                link = self.remote({"sub/l": ("120000", target.encode()), "a.txt": ("100644", b"a\n")}, f"repo{i}")
                scenario = self.scenario_file(f'setup.layout.remote = "{link}"\n[[steps]]\nrun.gradle = "help"\n', "links.toml")
                code, out, err = self.cli("zip", str(scenario))
                self.assertEqual((code, out, err), (gs.EXIT_ERROR, "", f"ERROR: setup.layout.remote: the link 'sub/l' points to {target!r}, outside the project\n"))
        link = self.remote({"sub/l": ("120000", b"../a.txt"), "a.txt": ("100644", b"a\n")}, "inside")
        scenario = self.scenario_file(f'setup.layout.remote = "{link}"\n[[steps]]\nrun.gradle = "help"\n', "inside.toml")
        self.assertEqual(self.cli("zip", str(scenario))[0], gs.EXIT_OK)
        self.assertEqual(self.entries(self.tmp / "inside.zip")["inside/sub/l"][:3:2], (0o120777, b"../a.txt"))

    def test_refusals(self):
        scenario = self.scenario_file('[[steps]]\nrun.gradle = "help"\n', "s.toml")
        (self.tmp / "s.zip").write_text("mine")
        code, out, err = self.cli("zip", str(scenario))
        self.assertEqual((code, out, err), (gs.EXIT_ERROR, "", f"ERROR: {self.tmp / 's.zip'} exists\n"))
        self.assertEqual((self.tmp / "s.zip").read_text(), "mine")
        code, out, err = self.cli("zip", str(scenario), "-o", "missing/s.zip")
        self.assertEqual((code, out, err), (gs.EXIT_ERROR, "", f"ERROR: no such directory {self.tmp / 'missing'} for the zip\n"))
        (self.tmp / "app").mkdir()
        in_place = self.scenario_file('setup.project = "app"\n[[steps]]\nrun.gradle = "help"\n', "in-place.toml")
        code, out, err = self.cli("zip", str(in_place))
        self.assertEqual((code, out, err), (gs.EXIT_ERROR, "", f"ERROR: nothing to zip: the project {self.tmp / 'app'} is used in place, as it is\n"))
        for value in ("./gradlew", "gecho", str(self.gecho)):
            code, out, err = self.cli("zip", str(scenario), "-o", "v.zip", "--gradle", value)
            self.assertEqual((code, out), (gs.EXIT_ERROR, ""), value)
            self.assertTrue(err.startswith(f"ERROR: --gradle {value!r}: a Gradle version such as 9.9.0 is expected here"), err)
        self.assertEqual(self.cli("zip", str(scenario), "-o", "v.zip", "--gradle", "9.8.0-rc-2")[0], gs.EXIT_OK)
        own = self.scenario_file('[setup.layout.project]\n"SCENARIO.md" = "x"\n[[steps]]\nrun.gradle = "help"\n', "own.toml")
        self.assertEqual(self.cli("zip", str(own)), (gs.EXIT_ERROR, "", "ERROR: the project has 'SCENARIO.md', where the zip's SCENARIO.md goes\n"))
        self.assertEqual(sorted(p.name for p in self.tmp.glob("*.zip")), ["s.zip", "v.zip"])

    def test_a_failure_leaves_no_zip(self):
        scenario = self.scenario_file('[setup.layout.project]\n"a.txt" = "a\\n"\n[[steps]]\nrun.gradle = "help"\n', "s.toml")
        real_writestr = zipfile.ZipFile.writestr

        def failing_writestr(zf, info, data, **kw):
            real_writestr(zf, info, data, **kw)
            raise OSError(28, "No space left on device")
        with mock.patch.object(zipfile.ZipFile, "writestr", failing_writestr):
            code, out, err = self.cli("zip", str(scenario))
        self.assertEqual((code, out, err), (gs.EXIT_ERROR, "", "ERROR: [Errno 28] No space left on device\n"))
        self.assertFalse((self.tmp / "s.zip").exists())

    def test_a_failed_git_call_is_one_error_line(self):
        link = self.remote({"sub/a.txt": ("100644", b"a\n")})
        fetched = self.scenario_file(f'setup.layout.remote = "{link}/sub"\n[[steps]]\nrun.gradle = "help"\n', "fetched.toml")
        self.assertEqual(self.cli("zip", str(fetched))[0], gs.EXIT_OK)
        checkout = gs.load_scenario(fetched.read_text()).remote.dir
        (checkout / "ghost").mkdir()                       # on disk, so the cached checkout passes, but not in the commit
        ghost = self.scenario_file(f'setup.layout.remote = "{link}/ghost"\n[[steps]]\nrun.gradle = "help"\n', "ghost.toml")
        os.environ["GIT_TRACE"] = "1"                      # trace lines on git's stderr, ahead of the one that counts
        code, out, err = self.cli("zip", str(ghost))
        self.assertEqual((code, out, err), (gs.EXIT_ERROR, "", f"ERROR: git ls-tree failed in {checkout}: fatal: Not a valid object name HEAD:ghost\n"))
        self.assertFalse((self.tmp / "ghost.zip").exists())


    # Regressions in SCENARIO.md, one test each.

    def layout_project(self, text):
        """A dir with the scenario's layout files, as a reader has it after unzipping."""
        project = self.tmp / "p"
        project.mkdir()
        for rel, content in gs.load_scenario(text).files.items():
            (project / rel).parent.mkdir(parents=True, exist_ok=True)
            (project / rel).write_bytes(content.encode())
        return project

    def test_diff_lines_that_look_like_headers_are_content(self):
        text = ('[setup.layout.project]\n"a.txt" = "x\\n"\n"b.txt" = "-- one\\t\\nkeep\\n"\n'
                '[[steps]]\nedit = [{ file = "a.txt", replace = "x\\n", with = "x\\n++ y" }, { file = "b.txt", replace = "-- one\\t\\n", with = "" }]\n')
        readme = self.readme(text)
        self.assertIn("\n+++ y\n\\ No newline at end of file\nEOF\n", readme)       # an added "++ y", without a final newline
        self.assertIn("\n--- one\t\n keep\nEOF\n", readme)                         # a removed "-- one<tab>", tab kept
        if shutil.which("patch"):
            project = self.layout_project(text)
            self.run_snippets(readme, project)
            self.assertEqual([(project / f).read_bytes() for f in ("a.txt", "b.txt")], [b"x\n++ y", b"keep\n"])

    def test_edit_of_a_file_named_dash(self):
        text = '[setup.layout.project]\n"-" = "one\\n"\n[[steps]]\nedit = [{ file = "-", replace = "one", with = "two" }]\n'
        readme = self.readme(text)
        self.assertNotIn("--- a/-\n", readme)                 # BSD patch reads - as stdout and leaves the file as it is
        self.assertIn("cat > ./- <<'EOF'\ntwo\nEOF\n", readme)
        project = self.layout_project(text)
        self.run_snippets(readme, project)
        self.assertEqual((project / "-").read_bytes(), b"two\n")

    def test_heredocs_are_safe_from_old_dash(self):
        contents = {"a.txt": "Eé\n", "b.txt": "EOé x\n", "c.txt": "EOFé\n", "d.txt": "EOxé\nE é\né\n"}
        readme = self.readme("[[steps]]\n[steps.write]\n" + "\n".join(f"{json.dumps(k)} = {json.dumps(v)}" for k, v in contents.items()) + "\n")
        heredocs = re.findall(r"<<'(\w+)'(?: &&)?\n(.*?)\n\1$", readme, re.M | re.S)
        self.assertEqual([d for d, _ in heredocs], ["EOF"])   # d.txt only; the others need printf
        for delimiter, body in heredocs:                      # dash before 0.5.13 drops a byte after a part of the delimiter
            for line in body.split("\n"):
                for k in range(1, len(delimiter) + 1):
                    self.assertFalse(line.startswith(delimiter[:k]) and len(line) > k and not line[k].isascii(), line)
        if not gs.WINDOWS:                                    # non-ASCII in bash -c there depends on the code page
            project = self.tmp / "p"
            project.mkdir()
            self.run_snippets(readme, project, shutil.which("dash"))   # dash where there is one, such as an old one on macOS
            for rel, content in contents.items():
                self.assertEqual((project / rel).read_bytes(), content.encode(), rel)

    def test_a_fence_in_the_description_is_escaped(self):
        readme = self.readme('description = """```kotlin\nval x = 1"""\n[[steps]]\nrun.gradle = "help"\n')
        self.assertIn("\n\\```kotlin\nval x = 1\n", readme)
        self.assertEqual(len(re.findall(r"^ {0,3}```", readme, re.M)) % 2, 0)

    def test_a_step_that_writes_scenario_md_is_refused(self):
        for rel in ("SCENARIO.md", "SCENARIO.md/x"):
            with self.assertRaises(gs.GustError) as cm:
                self.readme(f'[[steps]]\nwrite."{rel}" = "x\\n"\n')
            self.assertEqual(str(cm.exception), f"steps[1].write: {rel!r} is where the zip's SCENARIO.md goes")

    def test_a_file_step_block_stops_at_its_first_failure(self):
        readme = self.readme('[[steps]]\nwrite = { "a/b.txt" = "b\\n", "c.txt" = "c\\n" }\n')
        body = re.findall(r"^```sh\n(.*?)\n```$", readme, re.M | re.S)[0]
        project = self.tmp / "p"
        project.mkdir()
        (project / "a").write_text("a file where the dir goes")   # so mkdir -p a fails
        proc = subprocess.run(gs.shell_argv(body), cwd=project, capture_output=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse((project / "c.txt").exists())

    def test_remote_names_get_the_layout_checks(self):
        craft = self.tmp / "craft"
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}

        def git(*args, data=None):
            return subprocess.run(["git", *args], cwd=craft, input=data, check=True, capture_output=True, env=env).stdout.decode().strip()
        craft.mkdir()
        git("init", "-q")
        blob = git("hash-object", "-w", "--stdin", data=b"x\n")
        inner = git("mktree", data=f"100644 blob {blob}\tx.txt\n".encode())
        tree = git("mktree", data=f"040000 tree {inner}\t..\n100644 blob {blob}\tok.txt\n".encode())   # git checkout refuses that
        sha = git("-c", "user.name=t", "-c", "user.email=t@t", "commit-tree", tree, "-m", "one")
        git("update-ref", "HEAD", sha)
        scenario = gs.load_scenario(f'setup.layout.remote = "{craft.as_posix()}#{sha}"\n[[steps]]\nrun.gradle = "help"\n')
        scenario.remote.dir.parent.mkdir(parents=True)
        shutil.move(str(craft), str(scenario.remote.dir))      # as if fetched
        with self.assertRaises(gs.GustError) as cm:
            gs.project_files(scenario)
        self.assertEqual(str(cm.exception), "setup.layout.remote '../x.txt': path must be relative and must not contain '..'")

    def test_a_backslash_in_a_layout_path_zips_as_a_slash_on_every_system(self):
        scenario = self.scenario_file("[setup.layout.project]\n'a\\b.txt' = 'b'\n[[steps]]\nwrite.'c\\d.txt' = \"d\\n\"\n", "bs.toml")
        self.assertEqual(self.cli("zip", str(scenario))[0], gs.EXIT_OK)
        entries = self.entries(self.tmp / "bs.zip")
        self.assertEqual(list(entries), ["bs/", "bs/SCENARIO.md", "bs/a/", "bs/a/b.txt"])
        self.assertIn("mkdir -p c &&\ncat > c/d.txt", entries["bs/SCENARIO.md"][2].decode())

    def test_case_variants_of_remote_files_are_refused(self):
        link = self.remote({"readme.md": ("100644", b"r\n"), "src/a.txt": ("100644", b"a\n")})
        for i, (more, message) in enumerate((
                ('[setup.layout.project]\n"README.md" = "x"\n', "setup.layout.project: 'README.md' and 'readme.md'"),
                ('[[steps]]\nwrite."SRC/b.txt" = "b"\n', "steps[1].write: 'SRC' and 'src'"),
                ('[[steps]]\ndelete = "Src"\n', "steps[1].delete: 'Src' and 'src'"))):
            scenario = self.scenario_file(f'setup.layout.remote = "{link}"\n{more}[[steps]]\nrun.gradle = "help"\n', f"v{i}.toml")
            self.assertEqual(self.cli("zip", str(scenario)), (gs.EXIT_ERROR, "", f"ERROR: {message} (setup.layout.remote) differ in case or "
                                                                               f"Unicode form only, so they are one path on macOS or Windows\n"))
        scenario = self.scenario_file(f'setup.layout.remote = "{link}"\n[[steps]]\ndelete = "src"\n[[steps]]\nwrite."SRC/b.txt" = "b"\n'
                                      '[[steps]]\nrun.gradle = "help"\n', "gone.toml")
        self.assertEqual(self.cli("zip", str(scenario))[0], gs.EXIT_OK)                   # once src is gone, SRC is no variant

    def test_remote_names_get_the_windows_checks_of_a_layout(self):
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
        for i, (name, message) in enumerate((("a\\b.txt", "on Windows a backslash is a separator, so the name is another path there"),
                                             ("x.", "'x.' is not a valid name on Windows: it ends in a dot or a space"),
                                             ("aux.txt", "'aux.txt' is not a valid name on Windows: it is a device name there"),
                                             ("a:b", "path must be relative, but 'a:' is a drive on Windows"))):
            craft = self.tmp / f"craft{i}"

            def git(*args, data=None):
                return subprocess.run(["git", *args], cwd=craft, input=data, check=True, capture_output=True, env=env).stdout.decode().strip()
            craft.mkdir()
            git("init", "-q")
            blob = git("hash-object", "-w", "--stdin", data=b"x\n")
            tree = git("mktree", data=f"100644 blob {blob}\t{name}\n100644 blob {blob}\tok.txt\n".encode())   # git checkout refuses some
            sha = git("-c", "user.name=t", "-c", "user.email=t@t", "commit-tree", tree, "-m", "one")
            git("update-ref", "HEAD", sha)
            scenario = gs.load_scenario(f'setup.layout.remote = "{craft.as_posix()}#{sha}"\n[[steps]]\nrun.gradle = "help"\n')
            scenario.remote.dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(craft), str(scenario.remote.dir))      # as if fetched
            with self.subTest(name=name):
                with self.assertRaises(gs.GustError) as cm:
                    gs.project_files(scenario)
                self.assertEqual(str(cm.exception), f"setup.layout.remote {name!r}: {message}")

    def test_scenario_md_in_any_case_is_refused(self):
        for rel in ("scenario.md", "Scenario.MD/x"):               # one file on macOS and Windows: the replay would overwrite it
            with self.assertRaises(gs.GustError) as cm:
                self.readme(f'[[steps]]\nwrite."{rel}" = "x\\n"\n')
            self.assertEqual(str(cm.exception), f"steps[1].write: {rel!r} is where the zip's SCENARIO.md goes")
        scenario = self.scenario_file('[setup.layout.project]\n"Scenario.md" = "x\\n"\n[[steps]]\nrun.gradle = "help"\n', "own.toml")
        self.assertEqual(self.cli("zip", str(scenario))[0], gs.EXIT_ERROR)

    def test_delete_or_clear_of_scenario_md_is_refused(self):
        for kind, rel in (("delete", "SCENARIO.md"), ("clear", "scenario.md")):
            with self.assertRaises(gs.GustError) as cm:
                self.readme(f'[[steps]]\n{kind} = "{rel}"\n')
            self.assertEqual(str(cm.exception), f"steps[1].{kind}: {rel!r} is where the zip's SCENARIO.md goes")

    def test_html_at_the_start_of_a_description_line_is_escaped(self):
        readme = self.readme('description = """<!-- not a comment\n  <pre>x"""\n[[steps]]\nrun.gradle = "help"\n')
        self.assertIn("\n\\<!-- not a comment\n  \\<pre>x\n", readme)

    def test_sh_blocks_hold_commands_only(self):
        text = ('[setup.layout.project]\n"a.txt" = "a\\n"\n"c.txt" = "x\\r\\n"\n"s.txt" = "s\\n"\n'
                '[[steps]]\nrun.gradle = { args = "help", expect = { exit = "fail", output = ["> Task :help", "has `tick`"], '
                'no_output = ["x\\n# y"] } }\n'
                '[[steps]]\nedit = [{ file = "a.txt", replace = "a", with = "b" }, { file = "c.txt", replace = "x", with = "y" },'
                ' { file = "s.txt", replace = "s", with = "s" }, { file = "a.txt", replace = "b", with = "c" }]\n')
        readme = self.readme(text)
        blocks = [body for _, body in re.findall(r"^(`{3,})sh\n(.*?)\n\1$", readme, re.M | re.S)]
        self.assertEqual(len(blocks), 2)
        for body in blocks:                                   # an interactive zsh runs a pasted comment as a command
            self.assertEqual([line for line in body.split("\n") if line.lstrip().startswith("#")], [], body)
        self.assertIn("\nExpected: fails, and the output contains `> Task :help` and `` has `tick` ``, but not text 1 below.\n\n"
                      "Text 1:\n\n```text\nx\n# y\n```\n\n```sh\ngradle help\n```\n", readme)
        self.assertIn("\nThe file `c.txt` is written anew as a whole, instead of with a diff for patch.\n\n"
                      "The file `s.txt` stays as it is: its text is replaced with the same text.\n\n```sh\npatch -p1", readme)

    def test_forbidden_output_texts_are_joined_with_or(self):
        readme = self.readme('[[steps]]\nrun.gradle = { args = "help", expect = { output = ["a"], no_output = ["b", "c", "d"] } }\n'
                             '[[steps]]\nrun.gradle = { args = "help", expect = { no_output = ["x", "y"] } }\n')
        self.assertIn("\nExpected: passes, and the output contains `a`, but not `b`, `c` or `d`.\n", readme)   # any one fails it
        self.assertIn("\nExpected: passes, and the output does not contain `x` or `y`.\n", readme)

    def test_an_edit_step_of_no_op_edits_has_no_empty_block(self):
        readme = self.readme('[setup.layout.project]\n"s.txt" = "s\\n"\n[[steps]]\nedit = [{ file = "s.txt", replace = "s", with = "s" }]\n')
        self.assertTrue(readme.endswith("### 1. Edit: s.txt\n\nThe file `s.txt` stays as it is: its text is replaced with the same text.\n"),
                        readme)
        self.assertNotIn("```sh\n```", readme)

    def test_a_shell_command_with_a_comment_gets_a_note(self):
        readme = self.readme('[[steps]]\nrun.shell = "echo hi # say hi"\n[[steps]]\nrun.shell = "echo \'#1\' a#b $#"\n')
        note = "The command has a `#` comment; in zsh, run `setopt interactive_comments` first."
        first, second = readme.split("### 2.")
        self.assertIn(f"\n{note}\n\n```sh\necho hi # say hi\n```\n", first)
        self.assertEqual(second, " Run: echo '#1' a#b $#\n\n```sh\necho '#1' a#b $#\n```\n")   # no comment there: no note

    def test_the_intro_names_the_sh_blocks(self):
        self.assertIn("\nRun each `sh` block from the directory this file is in, in order.", self.readme('[[steps]]\nrun.gradle = "help"\n'))

    def test_spec_names_when_dash_forces_printf(self):
        self.assertIn("starts with E, EO, or EOF and then a non-ASCII", " ".join(gs.FORMAT_SPEC.split()))
        for content, heredoc in (("E\u00e9\n", False), ("EO\u00e9\n", False), ("EOF\u00e9\n", False), ("EOF2\u00e9\n", True), ("x\u00e9\n", True)):
            self.assertEqual(gs._sh_write("f", content)[0][0].startswith("cat "), heredoc, content)

    def test_html_after_list_or_quote_markers_is_escaped_but_not_an_autolink(self):
        readme = self.readme('description = """- <!-- a\n> <pre>\n  1. > <!-- b\n<https://github.com/gradle/gradle/issues/1>\n'
                             '<me@example.com> and <b>"""\n[[steps]]\nrun.gradle = "help"\n')
        self.assertIn("\n- \\<!-- a\n> \\<pre>\n  1. > \\<!-- b\n<https://github.com/gradle/gradle/issues/1>\n<me@example.com> and <b>\n", readme)

    def test_remote_links_to_scenario_md_or_the_project_dir_are_refused(self):
        for name, rel, to in (("r1", "notes.md", "SCENARIO.md"), ("r2", "here", "."), ("r3", "sub/up", ".."), ("r4", "sub/s", "../scenario.md")):
            link = self.remote({rel: ("120000", to.encode()), "a.txt": ("100644", b"a\n")}, name)
            scenario = self.scenario_file(f'setup.layout.remote = "{link}"\n[[steps]]\nrun.gradle = "help"\n', f"{name}.toml")
            self.assertEqual(self.cli("zip", str(scenario)), (gs.EXIT_ERROR, "", f"ERROR: setup.layout.remote: the link {rel!r} points to "
                                                                               f"{to!r}, the project dir or its SCENARIO.md\n"))

    def test_the_refusal_of_an_own_scenario_md_names_the_file(self):
        for rel in ("Scenario.md", "scenario.md/notes.txt"):
            scenario = self.scenario_file(f'[setup.layout.project]\n"{rel}" = "x\\n"\n[[steps]]\nrun.gradle = "help"\n', "own.toml")
            self.assertEqual(self.cli("zip", str(scenario)), (gs.EXIT_ERROR, "", f"ERROR: the project has {rel!r}, where the zip's SCENARIO.md goes\n"))

    def test_no_line_of_gust_py_is_longer_than_140(self):
        long = [n for n, line in enumerate(Path(gs.__file__).read_text(encoding="utf-8").splitlines(), start=1) if len(line) > 140]
        self.assertEqual(long, [])

class WindowsTest(GustCase):
    """The Windows helpers, run on any OS, some with gs.WINDOWS patched."""

    def fake_git(self, exec_path):
        """A git on PATH whose --exec-path is exec_path; git_bash() asks it afresh."""
        gs.git_bash.cache_clear()
        self.addCleanup(gs.git_bash.cache_clear)
        self.script("git", f"print({str(exec_path)!r})\n")

    def test_git_bash_is_found_above_the_exec_path(self):
        for root in ("Git", "scoop/apps/git/current"):                                 # an installer's layout, and scoop's
            root = self.tmp / root
            (root / "bin").mkdir(parents=True)
            (root / "bin" / "bash.exe").write_text("")
            self.fake_git(root / "mingw64" / "libexec" / "git-core")
            self.assertEqual(gs.git_bash(), str(root / "bin" / "bash.exe"), root)
        self.fake_git(self.tmp / "wsl" / "libexec" / "git-core")                       # a git with no bash above it
        with self.assertRaises(gs.GustError) as cm:
            gs.git_bash()
        self.assertRegex(str(cm.exception), "^shell steps on Windows run with the bash of Git for Windows, but there is no "
                                            r"bin\\bash\.exe above .*git.*'s exec path " + re.escape(repr(str(self.tmp / "wsl" / "libexec" / "git-core"))) + "$")
        gs.git_bash.cache_clear()
        os.environ["PATH"] = str(self.bin / "none")
        with self.assertRaises(gs.GustError) as cm:
            gs.git_bash()
        self.assertEqual(str(cm.exception), "shell steps on Windows run with the bash of Git for Windows, but no git is on PATH")

    def test_no_bash_is_refused_before_the_out_dir_is_touched(self):
        self.patch("WINDOWS", True)
        gs.git_bash.cache_clear()
        self.addCleanup(gs.git_bash.cache_clear)
        os.environ["PATH"] = str(self.bin)                                             # no git
        with self.assertRaises(gs.GustError) as cm:
            self.run_scenario(gs.load_scenario(MINIMAL), self.tmp / "s.out")
        self.assertIn("no git is on PATH", str(cm.exception))
        self.assertFalse((self.tmp / "s.out").exists())
        self.assertEqual(self.run_scenario(gs.load_scenario('name = "g"\n[[steps]]\nrun.gradle = "help"'), self.tmp / "g.out",
                                           gradle="gecho").result, "success")               # no shell step, no bash needed

    def test_gradle_command_is_split_and_its_binary_found(self):
        line = "./gradlew.bat --gradle-user-home '/a home' help '-Dx=a b'"
        if not gs.WINDOWS:
            self.assertEqual(gs.gradle_argv(line, self.tmp), ["/bin/sh", "-c", line])          # split by sh
        self.patch("WINDOWS", True)
        self.assertEqual(gs.gradle_argv(line, self.tmp), [str(self.tmp / "gradlew.bat"), "--gradle-user-home", "/a home", "help", "-Dx=a b"])
        binary, *args = gs.gradle_argv("gecho help", self.tmp)                                 # from PATH
        self.assertEqual((os.path.normcase(binary), args), (os.path.normcase(self.gecho), ["help"]))   # .BAT from which(), maybe
        with self.assertRaises(gs.GustError) as cm:
            gs.gradle_argv("gecho 'help", self.tmp)
        self.assertIn("cannot split \"gecho 'help\" into arguments", str(cm.exception))

    def test_a_failed_removal_keeps_the_marker(self):
        out_dir = self.tmp / "s.out"
        gs.prepare_out(out_dir)
        for name in ("project/f", "step-01.log", "a", "z"):                            # names around the marker's
            (out_dir / name).parent.mkdir(exist_ok=True)
            (out_dir / name).write_text("")
        real = gs.rmtree

        def held(path, ignore_errors=False):                                           # as with a daemon's open file
            raise PermissionError(13, "in use", str(path / "f"))
        self.patch("rmtree", held)
        with self.assertRaises(gs.GustError) as cm:
            gs.prepare_out(out_dir)
        self.assertIn("stop it with 'gust stop-daemons' and try again", str(cm.exception))
        self.assertIn(str(out_dir / "project" / "f"), str(cm.exception))
        self.assertTrue((out_dir / gs.MARKER).is_file())                               # so the dir is still gust's
        gs.rmtree = real
        gs.prepare_out(out_dir)                                                         # and the next setup goes through
        self.assertEqual([p.name for p in out_dir.iterdir()], [gs.MARKER])

    def test_echo_on_windows_has_lf_line_ends(self):
        self.patch("WINDOWS", True)
        echo = io.StringIO()
        gs._run_logged([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'a\\r\\nb\\n')"], self.tmp, dict(os.environ),
                       self.tmp / "x.log", echo)
        self.assertEqual(echo.getvalue(), f"{rule('x.log')}\na\nb\n{CLOSING}\n")
        self.assertEqual((self.tmp / "x.log").read_bytes(), b"a\r\nb\n")          # the log as printed

    def test_gradle_env_runs_in_the_shell(self):
        self.fake_gradle_on_path()
        shell = '[[steps]]\nrun.shell = { command = \'"$GRADLE" help\', expect = { output = ["NAME help"] } }\n'
        s = self.scenario_file('name = "e"\n' + SETTINGS + shell.replace("NAME", "gradle"))
        code, out, _ = self.cli("run", str(s), "--keep-daemons")                           # gradle from PATH, by name
        self.assertEqual(code, gs.EXIT_OK, out)
        s.write_text('name = "e"\n' + SETTINGS + shell.replace("NAME", "gradlew"))
        code, out, _ = self.cli("run", str(s), "--gradle", "9.7.1", "--keep-daemons")     # the installed wrapper
        self.assertEqual(code, gs.EXIT_OK, out)
        self.assertIn(f"gradle:   {WRAPPER} 9.7.1", out)

    def test_executable_by_extension(self):
        self.patch("WINDOWS", True)
        names = ("gradlew.bat", "gradle.CMD", "java.exe", "gradlew", "gradle.py")
        for name in names:
            (self.tmp / name).write_text("")
        self.assertEqual([gs._executable(self.tmp / n) for n in names], [True, True, True, False, False])
        self.assertFalse(gs._executable(self.tmp / "missing.bat"))                    # it must exist, as on POSIX

    @unittest.skipUnless(gs.WINDOWS, "Windows paths")
    def test_windows_paths(self):
        self.assertEqual(gs.TMP_ROOT, Path(tempfile.gettempdir()))
        self.assertEqual(gs.WRAPPER, "./gradlew.bat")
        self.assertEqual(gs._settle_binary(".\\gradlew.bat", lambda rel: rel == "gradlew.bat"), ("./gradlew.bat", None))   # the form bash accepts too
        self.assertEqual(gs._settle_binary(str(self.gecho), lambda rel: False), (str(self.gecho), None))                  # C:\...\gecho.bat


class SpecTest(unittest.TestCase):
    def test_example_is_a_valid_scenario(self):
        scenario = gs.load_scenario(gs.EXAMPLE)
        self.assertEqual([type(s).__name__ for s in scenario.steps], ["RunStep", "EditStep", "RunStep"])
        self.assertIn(gs.EXAMPLE, Path(__file__).with_name("README.md").read_text(encoding="utf-8"))      # the README quick start, word for word
        self.assertIn(gs.EXAMPLE, gs.FORMAT_SPEC)

    def test_spec_names_every_accepted_key_and_value(self):
        words = set(re.findall(r"[A-Za-z_][A-Za-z0-9_.-]*", gs.FORMAT_SPEC))
        expected = set(gs.TOP_KEYS) | set(gs.SETUP_KEYS) | set(gs.LAYOUT_KEYS) | set(gs.STEP_KEYS) | set(gs.EXPECT_KEYS) | {"wrapper"} | set(gs.RUN_KINDS) | set(gs.EDIT_KEYS) | set(gs.EXPECT_VALUES)
        for keys in gs.RUN_KEYS.values():
            expected |= keys
        missing = sorted(expected - words)
        self.assertEqual(missing, [], f"spec does not mention: {missing}")
        for table in ("top level", "setup", "setup.layout", "setup.layout.project", "steps[]", "steps[].run.gradle", "steps[].run.shell", "steps[].run.*.expect", "steps[].edit[]"):
            self.assertIn(f"\n{table}", gs.FORMAT_SPEC)
        readme = Path(gs.__file__).with_name("README.md").read_text(encoding="utf-8")
        help_out = io.StringIO()
        with contextlib.redirect_stdout(help_out):
            for command in gs.build_parser().parse_args(["help"])._subparsers:
                gs.main(["help", command])
        for gone in ("anchor", "layout.checkout", "checkout      string", "readme", "--verbose", "--step", "mechanical", "location", "launcher",
                     "out directory", "\u2014", "--trust-wrapper", "sha256", "checksum", "versions feed", "bootstrap", "verif", ".prev",
                     "previous:", "(per ", "(reused)", "must end in .toml", "three meanings", "step SCENARIO", "gust step",
                     "N [N ...]", "step through", "last full run", "deviat", "DEVIATION", "status", "ok: ", "result: ok",
                     "result: error", "exit 0 in", "exit N", "`error: "):
            for where, text in (("spec", gs.FORMAT_SPEC), ("README", readme), ("help", help_out.getvalue())):
                self.assertNotIn(gone, text, f"{gone!r} in {where}")
        for command in ("check SCENARIO", "stop-daemons SCENARIO [--out DIR] [--gradle BIN] [--gradle-user-home DIR]"):
            self.assertIn(command, gs.FORMAT_SPEC, command)


class Tty(io.StringIO):
    def isatty(self):
        return True


class ColorTest(GustCase):
    """When the console words are colored, how, and that the out dir and the JSON stay plain."""

    def test_color_on(self):
        del os.environ["NO_COLOR"]
        os.environ.pop("TERM", None)
        for env, stream, want in [({}, io.StringIO(), False), ({}, Tty(), True), ({"TERM": "dumb"}, Tty(), False),
                                  ({"NO_COLOR": "1"}, Tty(), False), ({"FORCE_COLOR": "1"}, io.StringIO(), True),
                                  ({"FORCE_COLOR": "1", "TERM": "dumb"}, Tty(), True),
                                  ({"NO_COLOR": "1", "FORCE_COLOR": "1"}, Tty(), False), ({"NO_COLOR": ""}, Tty(), True)]:
            with mock.patch.dict(os.environ, env), mock.patch.object(gs, "WINDOWS", False):   # Tty has no console
                self.assertEqual(gs.color_on(stream), want, env)
        with mock.patch.object(gs, "WINDOWS", True):
            self.assertFalse(gs.color_on(Tty()))                                         # no console handle behind it
            with open(os.devnull, "w") as nul:                                           # NUL, a tty to Windows, but no console
                self.assertFalse(gs.color_on(nul))
            for vt, env, want, calls in [(True, {}, True, 1), (False, {}, False, 1), (True, {"TERM": "dumb"}, False, 0),
                                         (False, {"FORCE_COLOR": "1"}, True, 1)]:
                enable_vt = mock.Mock(return_value=vt)
                with mock.patch.object(gs, "_enable_vt", enable_vt), mock.patch.dict(os.environ, env):
                    self.assertEqual(gs.color_on(Tty()), want, (vt, env))
                self.assertEqual(enable_vt.call_count, calls, (vt, env))                 # the console mode is left alone for TERM=dumb

    def test_each_word_alone_is_colored(self):
        for word, sgr in [(gs.EXPECTED, "32"), (gs.UNEXPECTED, "1;31"), ("SUCCESS", "1"), ("FAILURE", "33"), (gs.ERROR, "31")]:
            self.assertEqual(gs.paint(word, True), f"\x1b[{sgr}m{word}\x1b[0m")
            self.assertEqual(gs.paint(word, False), word)

    def test_console_colored_files_and_json_plain(self):
        del os.environ["NO_COLOR"]
        os.environ["FORCE_COLOR"] = "1"                                                  # through cli's StringIO, a pipe in effect
        on = {word: gs.paint(word, True) for word in gs.STYLES}
        for name, text, lines in [
            ("pass", '[[steps]]\nrun.shell = "true"\n', [f"{on['EXPECTED']}: exit=0", f"result: {on['SUCCESS']} (1/1"]),
            ("fail", '[[steps]]\nrun.shell = "true"\n[[steps]]\nrun.shell = "false"\n',
             [f"{on['EXPECTED']}: exit=0", f"{on['UNEXPECTED']}: exit=1", f"result: {on['FAILURE']} (2/2"]),
            ("err", '[setup.layout.project]\n"f.txt" = "aa"\n[[steps]]\n[[steps.edit]]\nfile = "f.txt"\nreplace = "a"\nwith = "b"\n',
             [f"{on['ERROR']}: 'a' occurs", f"result: {on['ERROR']} (1/1"]),
        ]:
            _, out, _ = self.cli("run", str(self.scenario_file(text, f"{name}.toml")), "--keep-daemons", "--json")
            for line in lines:
                self.assertIn(line, out, name)
            self.assertEqual(out.count("\x1b["), 2 * len(lines), name)                   # the words, and nothing else
            self.assertNotIn("\x1b", out.rstrip("\n").splitlines()[-1], name)            # the --json line
            for f in (self.tmp / f"{name}.out").rglob("*"):
                if f.is_file():
                    self.assertNotIn(b"\x1b", f.read_bytes(), f)

    def test_stderr_error_colored(self):
        del os.environ["NO_COLOR"]
        s = self.scenario_file('[[steps]]\nrun.shell = "true"\n')
        for env, word in [({"FORCE_COLOR": "1"}, gs.paint(gs.ERROR, True)), ({"FORCE_COLOR": "1", "NO_COLOR": "1"}, gs.ERROR), ({}, gs.ERROR)]:
            with mock.patch.dict(os.environ, env):
                _, _, err = self.cli("run", "missing.toml")                              # a precondition
                self.assertTrue(err.startswith(f"{word}: "), (env, err))
                _, _, err = self.cli("check", str(s), "--", "-q")                        # arguments after --
                self.assertEqual(err, f"{word}: arguments after -- are accepted by run only (check runs no Gradle step)\n", env)
                usage = io.StringIO()                                                    # an argparse usage error
                with contextlib.redirect_stderr(usage), self.assertRaises(SystemExit):
                    gs.main(["run", str(s), "--tail", "x"])
                self.assertIn(f"\n{word}: argument --tail", usage.getvalue(), env)
                code, out, err = self.cli("zip", str(s), "--gradle", "bin/gradle")         # zip's error, through main
                self.assertEqual((code, out), (gs.EXIT_ERROR, ""), env)
                self.assertTrue(err.startswith(f"{word}: --gradle "), (env, err))
        with mock.patch.dict(os.environ, {"FORCE_COLOR": "1"}):
            code, out, _ = self.cli("zip", str(s), "-o", str(self.tmp / "r.zip"))
        self.assertEqual((code, out), (gs.EXIT_OK, f"{self.tmp / 'r.zip'}\n"))                   # the zip's path stays plain


class VersionGateTest(unittest.TestCase):
    def test_gate_precedes_every_version_dependent_import(self):
        source = Path(gs.__file__).read_text(encoding="utf-8").splitlines()
        gate = next(i for i, l in enumerate(source) if l.startswith("if sys.version_info < (3, 11)"))
        tomllib = next(i for i, l in enumerate(source) if l == "import tomllib")
        first_def = next(i for i, l in enumerate(source) if l.startswith(("def ", "class ", "@dataclass")))
        self.assertLess(gate, tomllib)
        self.assertLess(gate, first_def)
        for line in source[:gate]:
            self.assertFalse(line.startswith("import ") and line != "import sys", line)


if __name__ == "__main__":
    unittest.main()
