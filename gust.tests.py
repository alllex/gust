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
from pathlib import Path

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


def gradle_env(binary):
    """$GRADLE for a binary given by its path: on Windows with forward slashes, for bash."""
    return Path(binary).as_posix() if gs.WINDOWS else str(binary)


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
            sys.stdin = stdin if isinstance(stdin, io.StringIO) else io.StringIO(stdin)
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
        self.assertEqual(s.counts(), {"gradle": 0, "shell": 1, "write": 0, "edit": 0})

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
        self.assertEqual(s.counts(), {"gradle": 2, "shell": 1, "write": 1, "edit": 1})

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
        self.assertEqual(s.steps[0].expect.to_json(), {"exit": "fail", "output": ["FAILED", "1 test"], "no_output": ["BUILD SUCCESSFUL"]})
        self.assertEqual(gs.Expect().to_json(), {"exit": "pass"})

    def test_expect_deviations(self):
        e = gs.Expect(exit="fail", output=("a", "b"), no_output=("z",))
        self.assertEqual(e.deviations(1, "a b"), [])
        self.assertEqual(e.deviations(0, "a b"), ["expected fail, got pass"])
        self.assertEqual(e.deviations(2, "a z"), ["output lacks 'b'", "output has 'z', which no_output forbids"])
        self.assertEqual(e.deviations(0, ""), ["expected fail, got pass", "output lacks 'a'", "output lacks 'b'"])   # all of them
        self.assertEqual(gs.Expect().deviations(0, ""), [])

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
        self.assert_rejects(S + '[[steps]]\nrun.shell = "x"\n[steps.write]\n"y" = "z"', "exactly one of 'run', 'write', or 'edit'")
        self.assert_rejects(S + '[[steps]]\nname = "n"', "exactly one of 'run', 'write', or 'edit'")
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
                            "steps[1].write: 'a' conflicts with 'a/b' (setup.layout.project)")
        # fine: edit after write, overwrite of a known file, sibling paths
        gs.load_scenario(S + '[setup.layout.project]\n"a/b" = "1"\n"a/c" = "2"\n[[steps]]\n[steps.write]\n"f" = "a"\n"a/b" = "x"\n'
                         '[[steps]]\n[[steps.edit]]\nfile = "f"\nreplace = "a"\nwith = "b"')


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
        self.assertEqual(summary.status, "ok", self.out.getvalue())
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
        self.assertEqual(summary.status, "ok")
        self.assertTrue((self.gust / "remotes" / scenario.remote.identity / ".git").is_dir())
        # the repository root has gradlew, so a relative --gradle is accepted, probed and works as a gradle step
        scenario = self.load(f'setup.layout.remote = "{self.url}#{self.sha}"', '[[steps]]\nrun.gradle = { args = "help", expect = { output = ["gradlew help"] } }\n')
        summary = self.run_scenario(scenario, self.tmp / "t.out", gradle=WRAPPER, stop_daemons=False)
        self.assertEqual(summary.status, "ok")
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

    def test_gradle_wrapper_field(self):
        scenario = self.load(f'setup.gradle = "wrapper"\nsetup.layout.remote = "{self.url}#{self.sha}"',
                             '[[steps]]\nrun.gradle = { args = "help", expect = { output = ["gradlew help"] } }\n')
        self.assertEqual(scenario.gradle, "wrapper")
        summary = self.run_scenario(scenario, self.tmp / "s.out", stop_daemons=False)   # no --gradle
        self.assertEqual(summary.status, "ok")
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
        self.assertIn(f'setup.gradle = "wrapper" but {WRAPPER_FILE} is not in the layout or the remote checkout', str(cm.exception))
        self.assertFalse((self.tmp / "s.out").exists())
        inline = gs.load_scenario(f'name = "i"\nsetup.gradle = "wrapper"\n[setup.layout.project]\n"{WRAPPER_FILE}" = "not run"\n[[steps]]\nrun.gradle = "help"')
        with self.assertRaises(gs.GustError) as cm:
            self.run_scenario(inline, self.tmp / "i.out", stop_daemons=False)
        self.assertIn("cannot come from an inline layout", str(cm.exception))
        with self.assertRaises(gs.GustError) as cm:
            gs.load_scenario('name = "s"\nsetup.gradle = "latest"\n[[steps]]\nrun.shell = "true"')
        self.assertIn("setup.gradle: must be 'wrapper' or a Gradle version such as '9.7.1', got 'latest'", str(cm.exception))
        with self.assertRaises(gs.GustError):
            gs.load_scenario('name = "s"\nsetup.gradle = 1\n[[steps]]\nrun.shell = "true"')

    def test_version_installs_wrapper_via_gradle_on_path(self):
        self.fake_gradle_on_path()
        scenario = gs.load_scenario('name = "v"\nsetup.gradle = "9.7.1"\n[setup.layout.project]\n"settings.gradle.kts" = ""\n[[steps]]\nrun.gradle = { args = "help", expect = { output = ["gradlew help"] } }', base_dir=self.tmp)
        out_dir = self.tmp / "v.out"
        summary = self.run_scenario(scenario, out_dir, stop_daemons=True)
        self.assertEqual(summary.status, "ok")
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
                                  r"ok: exit 0 in \d+\.\ds -> install-wrapper\.log\n"
                                  r"\[BEF\] Stop Gradle daemons -> stop-daemons-before\.log\n\[1/1\]")
        self.assertIn(f"home:     {self.gust / gs.SHARED_HOME}\n", printed)
        self.assertEqual((out_dir / "stop-daemons-before.log").read_text().strip(), "gradlew --stop")
        # a remote with its own wrapper still gets the requested version installed, with gradle from PATH
        scenario = self.load(f'setup.layout.remote = "{self.url}#{self.sha}"', '[[steps]]\nrun.gradle = "help"\n')
        self.out = io.StringIO()
        summary = self.run_scenario(scenario, self.tmp / "s.out", gradle="9.8.0-rc-2", stop_daemons=False)
        self.assertEqual(summary.status, "ok")
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
        self.assertEqual(str(cm.exception), "--gradle 'latest': not found on PATH and not a Gradle version such as 9.7.1")
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
        refused("./nope/gradle", "--gradle './nope/gradle' is relative to the project dir but no such file is in setup.layout.project or the remote checkout")
        (self.tmp / "plain").write_text("not executable")
        refused("./plain", "--gradle './plain' is relative to the project dir but no such file is in setup.layout.project or the remote checkout")
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
        refused("grdl", "--gradle 'grdl': not found on PATH and not a Gradle version such as 9.7.1")
        refused("9", "--gradle '9': not found on PATH and not a Gradle version such as 9.7.1")
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
        self.assertRegex(out, r"\[WRP\] Install Gradle wrapper 9\.7\.1\n\$ gradle wrapper --gradle-version 9\.7\.1 --no-daemon\nERROR: exit 7 in \d+\.\ds -> install-wrapper\.log\n")
        self.assertIn(f"{rule('last 1 of 1 lines of install-wrapper.log')}\nboom\n{CLOSING}\nresult: error (0/1 steps ran)\nout:      {self.tmp / 's.out'}\n", out)
        self.assertNotIn("[BEF]", out)                                                   # nothing ran after the failed install
        summary = json.loads(out.rstrip("\n").splitlines()[-1])
        self.assertEqual((summary["status"], summary["install"], summary["steps"]), ("error", {"version": "9.7.1", "log": "install-wrapper.log", "exit_code": 7}, []))
        self.assertEqual(json.loads((self.tmp / "s.out" / "summary.json").read_text()), summary)
        code, out, _ = self.cli("setup", str(s))
        self.assertEqual(code, gs.EXIT_ERROR)
        self.assertTrue(out.endswith(f"result: error\nout:      {self.tmp / 's.out'}\n"), out[-200:])

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
            "error: --gradle 'nope': not found on PATH and not a Gradle version such as 9.7.1",
            "error: --gradle './nope/gradle' is relative to the project dir but no such file is in setup.layout.project or the remote checkout",
            "error: --gradle '9.7.1' is a version, but nothing is installed here; pass a binary (a name or a path)"])   # the first run set the project up
        code, out, _ = self.cli("setup", str(s), "--gradle", "9.7.0")
        self.assertEqual(code, gs.EXIT_OK)
        self.assertIn("installed wrapper 9.7.0 via gradle", (self.tmp / "s.out" / "install-wrapper.log").read_text())
        self.assertRegex(out, rf"scenario: s\nout:      .*\nhome:     .*\ngradle:   {re.escape(WRAPPER)} 9\.7\.0 \(--gradle, installed by gradle\)\n"
                              r"\[WRP\] Install Gradle wrapper 9\.7\.0\n\$ gradle wrapper .*\nok: exit 0 in .* -> install-wrapper\.log\nresult: set up\nout:      .*\n")
        gs.rmtree(self.tmp / "s.out")                                             # without a project: the hint comes first
        code, _, err = self.cli("stop-daemons", str(s), "--gradle", "9.7.1")
        self.assertEqual((code, err.strip()), (gs.EXIT_ERROR, f"error: no set-up project at {self.tmp / 's.out' / 'project'}; run 'gust setup {s}' first"))
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
                              rf"\[STOP\] Stop Gradle daemons\n\$ {re.escape(WRAPPER)} --stop\nok: exit 0 in \d+\.\ds -> stop-daemons\.log\nresult: ok\n")
        gradlew = write_script(self.tmp / "s.out" / "project", "gradlew", 'print("not gradle")\n')   # the probe of a broken wrapper fails
        code, _, err = self.cli("stop-daemons", str(s))
        self.assertEqual(code, gs.EXIT_ERROR)
        self.assertEqual(err.strip(), f"error: {WRAPPER}: not a Gradle binary (no 'Gradle <version>' line in the output of --version)")
        gradlew.unlink()                                                                     # and a missing one is named
        code, _, err = self.cli("stop-daemons", str(s))
        self.assertEqual((code, err.strip()), (gs.EXIT_ERROR, f'error: setup.gradle = "wrapper" but {WRAPPER_FILE} is not in the layout or the remote checkout'))

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
            self.assertEqual(self.run_scenario(ok, self.tmp / "d.out").status, "ok", settings)
        # the remote checkout's settings file counts too
        remote = self.load(f'setup.gradle = "9.7.1"\nsetup.layout.remote = "{self.url}#{self.sha}"', '[[steps]]\nrun.gradle = "help"\n')
        self.assertEqual(self.run_scenario(remote, self.tmp / "e.out").status, "ok")
        code, _, err = self.cli("setup", "-", "--gradle", "9.7.1", "--out", str(self.tmp / "f.out"), stdin='[[steps]]\nrun.gradle = "help"\n')
        self.assertEqual(code, gs.EXIT_ERROR)
        self.assertEqual(err.strip(), "error: --gradle '9.7.1': a wrapper can only be installed into a Gradle build; add a settings file to the layout")

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
                          f"installed wrapper 9.7.1 via gradle\n{CLOSING}\nok: exit 0 in ", out, argv)

    def test_wrapper_in_the_project_is_the_default(self):
        self.fake_gradle_on_path()
        # a remote that brings its own gradlew, no setup.gradle: the wrapper runs, probed, noted (default)
        scenario = self.load(f'setup.layout.remote = "{self.url}#{self.sha}"', '[[steps]]\nrun.gradle = { args = "help", expect = { output = ["gradlew help"] } }\n')
        summary = self.run_scenario(scenario, self.tmp / "a.out")
        self.assertEqual((summary.status, summary.gradle, summary.gradle_version), ("ok", WRAPPER, "9.7.1"))
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

    def test_check_command(self):
        self.fake_gradle_on_path()
        s = self.write(f'description = "Checks the remote."\nsetup.gradle = "wrapper"\nsetup.layout.remote = "{self.url}#{self.sha}"',
                       '[[steps]]\nrun.gradle = "help"\n[[steps]]\nrun.shell = "true"\n[[steps]]\nedit = [{ file = "sub/note.txt", replace = "remote", with = "x" }]\n')
        # not cached: noted, nothing fetched or written
        code, out, _ = self.cli("check", str(s))
        self.assertEqual(code, gs.EXIT_OK, out)
        self.assertEqual(out, f"remote:   {self.url}#{self.sha} (not cached; fetched on run)\nscenario: s\n          Checks the remote.\n"
                              f"home:     {self.gust / gs.SHARED_HOME}\ngradle:   {WRAPPER} (scenario; not checked, remote not cached)\n"
                              f"steps: 3 (1 gradle, 1 shell, 0 write, 1 edit)\nresult: ok\n")          # no out dir, so no out: line
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
                          "steps": 3, "step_counts": {"gradle": 1, "shell": 1, "write": 0, "edit": 1},
                          "remote": {"link": f"{self.url}#{self.sha}", "cached": True}, "status": "ok"})
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
        self.assertFalse((self.gust / "remotes").exists())
        self.script("notgradle", NOT_GRADLE)
        code, _, err = self.cli("check", str(s), "--gradle", "notgradle")
        self.assertEqual(code, gs.EXIT_ERROR)
        self.assertIn("error: --gradle 'notgradle': not a Gradle binary", err)
        bad = self.scenario_file("name = ", name="bad.toml")
        code, _, err = self.cli("check", str(bad))
        self.assertEqual(code, gs.EXIT_ERROR)
        self.assertTrue(err.startswith(f"error: {bad}: not valid TOML"), err)
        code, out, _ = self.cli("check", "-", stdin='[[steps]]\nrun.shell = "true"\n')
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
        self.assertEqual((project / "lf.txt").read_bytes(), b"one\n2\n")                   # after an edit: LF, everywhere
        self.assertEqual((self.out_dir / gs.MARKER).read_text(),
                         "Marker for gust: this directory was produced by gust and may be replaced or removed by a later run.\n")

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
        self.assertEqual(summary.status, "ok")
        self.assertEqual([s.outcome for s in summary.steps], ["pass", "fail", "done", "pass", "done", "pass"])
        self.assertEqual([s.kind for s in summary.steps], ["shell", "shell", "edit", "shell", "write", "shell"])
        self.assertEqual((self.project / "f.txt").read_text(), "four")
        self.assertTrue((self.out_dir / "step-01.log").exists())
        self.assertFalse((self.out_dir / "step-03.log").exists())
        self.assertFalse((self.project / "step-01.log").exists())
        printed = self.out.getvalue()
        self.assertTrue(printed.startswith(f"scenario: loop\nout:      {self.out_dir}\nhome:     {self.gust / gs.SHARED_HOME}\ngradle:   gecho 9.7.1 (--gradle)\n"), printed)
        self.assertNotIn("narrative", printed)                                    # the description is for check and the summary only
        self.assertIn("[1/6] sees one\n$ grep -q one f.txt\nok: exit 0 in ", printed)
        self.assertIn("[2/6] Run: grep -q three f.txt\n$ grep -q three f.txt\nok: exit 1 in ", printed)   # expected fail: still ok
        self.assertIn("[3/6] Edit: f.txt\nok: 1 file edited\n[4/6]", printed)
        self.assertIn("[5/6] Write: f.txt, g.txt\nok: 2 files written\n[6/6]", printed)
        self.assertTrue(printed.endswith(f"result: ok (6/6 steps ran)\nout:      {self.out_dir}\n"), printed[-200:])   # the out dir once more, last
        self.assertNotIn("DEVIATION", printed)
        self.assertNotIn("pass", printed.split("gradle:")[1])                     # no pass/fail word on the console
        self.assertEqual(json.loads((self.out_dir / "summary.json").read_text())["description"], "Two lines of\nnarrative.")

    def test_stops_at_deviation_and_shows_tail(self):
        summary = self.run_text("""
name = "dev"
[[steps]]
run.shell = "echo line-a; echo line-b; exit 3"
[[steps]]
run.shell = "touch should-not-exist"
""")
        self.assertEqual(summary.status, "deviated")
        self.assertEqual(len(summary.steps), 1)
        self.assertEqual(summary.steps[0].exit_code, 3)
        self.assertTrue(summary.steps[0].deviated)
        self.assertFalse((self.project / "should-not-exist").exists())
        printed = self.out.getvalue()
        self.assertRegex(printed, r"\$ echo line-a; echo line-b; exit 3\nDEVIATION: exit 3 in \d+\.\ds -> step-01\.log\n  expected pass, got fail\n-{40}")
        self.assertEqual(summary.steps[0].deviations, ["expected pass, got fail"])
        self.assertIn(f"\n{rule('last 2 of 2 lines of step-01.log')}\nline-a\nline-b\n{CLOSING}\nresult: deviated (1/2 steps ran)", printed)

    def test_tail_length(self):
        text = 'name = "t"\n[[steps]]\nrun.shell = "echo a; echo b; echo c; exit 1"'
        self.run_text(text, tail=1)
        self.assertIn(f"\n{rule('last 1 of 3 lines of step-01.log')}\nc\n{CLOSING}\n", self.out.getvalue())
        self.out = io.StringIO()
        self.run_scenario(gs.load_scenario(text), self.tmp / "z.out", gradle="gecho", stop_daemons=False, tail=0)
        self.assertIn(f"\n{rule('last 0 of 3 lines of step-01.log')}\n{CLOSING}\n", self.out.getvalue())   # 0 shows nothing, not everything
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            gs.main(["run", str(self.scenario_file(MINIMAL)), "--tail", "-1"])
        self.assertEqual(cm.exception.code, 2)

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
        self.assertEqual(summary.status, "error")
        self.assertEqual(summary.steps[0].outcome, "error")
        self.assertEqual(summary.steps[0].error, "'a' occurs 2 times in f.txt; an edit needs exactly one")
        self.assertEqual((self.project / "f.txt").read_text(), "aa")
        self.assertFalse((self.project / "should-not-exist").exists())
        self.assertIn("[1/2] Edit: f.txt\nERROR: 'a' occurs 2 times in f.txt; an edit needs exactly one\nresult: error (1/2 steps ran)", self.out.getvalue())
        self.assertEqual(json.loads((self.out_dir / "summary.json").read_text())["status"], "error")

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
        self.assertEqual(summary.status, "error")
        self.assertEqual(summary.steps[1].outcome, "error")
        self.assertIn("cannot write f.txt: ", summary.steps[1].error)
        self.assertIn("[2/3] Write: f.txt\nERROR: cannot write f.txt: ", self.out.getvalue())
        self.assertIn("result: error (2/3 steps ran)", self.out.getvalue())
        self.assertTrue((self.out_dir / "summary.json").is_file())
        self.assertFalse((self.project / "should-not-exist").exists())

    def test_output_checks_read_crlf_as_lf(self):
        summary = self.run_text('name = "crlf"\n[[steps]]\nrun.shell = { command = "printf \'a\\\\r\\\\nb\\\\r\\\\n\'", expect = { output = ["a\\nb"] } }')
        self.assertEqual(summary.status, "ok", self.out.getvalue())
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
        self.assertEqual(summary.status, "deviated")
        self.assertEqual(len(summary.steps), 2)
        self.assertEqual(summary.steps[0].deviations, [])
        self.assertEqual(summary.steps[1].outcome, "fail")           # exit matched...
        self.assertEqual(summary.steps[1].deviations, ["output lacks 'delta'", "output has 'gamma', which no_output forbids"])
        printed = self.out.getvalue()
        self.assertIn("ok: exit 0, 3 output checks in", printed)
        self.assertRegex(printed, r"DEVIATION: exit 1, 3 output checks in \d+\.\ds -> step-02\.log\n  output lacks 'delta'\n  output has 'gamma', which no_output forbids\n-{40}")
        self.assertIn("\ngamma\n", printed)
        on_disk = json.loads((self.out_dir / "summary.json").read_text())
        self.assertEqual(on_disk["steps"][1]["expect"], {"exit": "fail", "output": ["alpha", "delta"], "no_output": ["gamma"]})
        self.assertEqual(on_disk["steps"][1]["deviations"], ["output lacks 'delta'", "output has 'gamma', which no_output forbids"])

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
        self.assertEqual(summary.status, "error")
        self.assertEqual(summary.steps[1].error, "no such file f; nothing to edit")

    def test_gradle_step_quotes_binary(self):
        fake = self.script("gradle", GECHO, where=(self.tmp / "g dir" / "bin"))
        summary = self.run_text('name = "g"\n[[steps]]\nrun.gradle = "help --quiet"', gradle=str(fake))
        self.assertEqual(summary.status, "ok")
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
        self.assertIn("no such file is in setup.layout.project", str(cm.exception))
        with self.assertRaises(gs.GustError) as cm:
            self.run_text(needs_gradle, gradle="../gradlew")
        self.assertIn("no executable at", str(cm.exception))
        self.assertFalse(self.out_dir.exists())
        # a shell-only scenario without the daemon stop needs no Gradle, but a --gradle value is still resolved
        summary = self.run_text('name = "s"\n[[steps]]\nrun.shell = "true"', gradle="gecho")
        self.assertEqual(summary.status, "ok")
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
        self.assertEqual((self.out_dir / summary.steps[0].log).read_text().strip(), f"using {gradle_env(gecho)}")
        self.assertNotIn("using /", self.out.getvalue())

    def test_gradle_step_via_dummy_binary(self):
        summary = self.run_text('name = "e"\n[[steps]]\nrun.gradle = { args = "test --info", expect = "pass" }')
        self.assertEqual(summary.status, "ok")
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

    def test_show_output_streams_between_step_and_outcome(self):
        text = 'name = "so"\n[[steps]]\nrun.shell = "echo alpha; echo beta >&2"\n[[steps]]\nrun.shell = "echo gamma; exit 3"\n'
        summary = self.run_text(text, show_output=True)
        self.assertEqual(summary.status, "deviated")
        printed = self.out.getvalue()
        block = f"[1/2] Run: echo alpha; echo beta >&2\n$ echo alpha; echo beta >&2\n{rule('step-01.log')}\nalpha\nbeta\n{CLOSING}\nok: exit 0 in "
        self.assertIn(block, printed)                                               # stderr is in the same stream, output untouched
        self.assertEqual((self.out_dir / "step-01.log").read_text(), "alpha\nbeta\n")   # the log is written as before
        self.assertRegex(printed, re.escape(f"[2/2] Run: echo gamma; exit 3\n$ echo gamma; exit 3\n{rule('step-02.log')}\ngamma\n{CLOSING}\nDEVIATION: exit 3 in ") + r"\d+\.\ds -> step-02\.log\n  expected pass, got fail\nresult: deviated")
        self.assertNotIn("last ", printed)                                          # no tail after the output was shown
        self.assertEqual((self.out_dir / "step-02.log").read_text(), "gamma\n")
        # no output at all, and output without a final newline, still get both rules
        self.out = io.StringIO()
        self.run_scenario(gs.load_scenario('name = "e"\n[[steps]]\nrun.shell = "true"\n[[steps]]\nrun.shell = "printf partial"'),
                        self.tmp / "e.out", gradle="gecho", stop_daemons=False, show_output=True)
        self.assertIn(f"\n{rule('step-01.log')}\n{CLOSING}\nok: exit 0", self.out.getvalue())
        self.assertIn(f"\n{rule('step-02.log')}\npartial\n{CLOSING}\nok: exit 0", self.out.getvalue())
        self.assertEqual((self.tmp / "e.out" / "step-02.log").read_text(), "partial")
        # without the flag nothing changes: no echo, the tail on deviation
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

    def test_stop_daemons_runs_before_and_after_steps_even_on_deviation(self):
        summary = self.run_text('name = "d"\n[[steps]]\nrun.shell = "false"', stop_daemons=True)
        self.assertEqual(summary.status, "deviated")
        self.assertEqual({k: {f: v for f, v in d.items() if f != "duration_s"} for k, d in summary.stop_daemons.items()}, {
            "before": {"exit_code": 0, "log": "stop-daemons-before.log"},
            "after": {"exit_code": 0, "log": "stop-daemons-after.log"},
        })
        self.assertIsInstance(summary.stop_daemons["before"]["duration_s"], float)
        self.assertEqual((self.out_dir / "stop-daemons-before.log").read_text().strip(), f"{home_arg()} --stop")
        self.assertEqual((self.out_dir / "stop-daemons-after.log").read_text().strip(), f"{home_arg()} --stop")
        printed = self.out.getvalue()
        self.assertIn("\n[BEF] Stop Gradle daemons -> stop-daemons-before.log\n[1/1] ", printed)     # one line each
        self.assertIn("\n[AFT] Stop Gradle daemons -> stop-daemons-after.log\nresult: deviated", printed)
        self.assertNotIn("--stop\n", printed)
        self.assertNotIn("ok: exit 0 in", printed)
        self.assertEqual(json.loads((self.out_dir / "summary.json").read_text())["stop_daemons"]["before"]["exit_code"], 0)
        # the stops are never framed, even with --show-output
        self.out = io.StringIO()
        self.run_scenario(gs.load_scenario('name = "v"\n[[steps]]\nrun.shell = "echo body"'), self.tmp / "b.out", gradle="gecho",
                        stop_daemons=True, show_output=True)
        printed = self.out.getvalue()
        self.assertIn(f"[BEF] Stop Gradle daemons -> stop-daemons-before.log\n[1/1] Run: echo body\n$ echo body\n{rule('step-01.log')}\nbody\n{CLOSING}\nok: exit 0", printed)
        self.assertNotIn(rule("stop-daemons-before.log"), printed)

    def test_failed_daemon_stop_is_reported(self):
        flaky = self.script("gradle", 'if ARGS[2:3] == ["--version"]:\n    print("Gradle 9.7.1")\n    sys.exit(0)\n'
                                      'if ARGS[2:3] == ["--stop"]:\n    print("cannot")\n    sys.exit(3)\nprint(*ARGS)\n')
        summary = self.run_scenario(gs.load_scenario('name = "f"\n[[steps]]\nrun.shell = "true"'), self.out_dir, gradle=str(flaky), stop_daemons=True)
        self.assertEqual(summary.status, "ok")                                              # a failed stop never changes the outcome
        self.assertEqual(summary.stop_daemons["before"]["exit_code"], 3)
        printed = self.out.getvalue()
        self.assertRegex(printed, r"\[BEF\] Stop Gradle daemons -> stop-daemons-before\.log\nERROR: exit 3 in \d+\.\ds -> stop-daemons-before\.log\n\[1/1\]")
        self.assertRegex(printed, r"\[AFT\] Stop Gradle daemons -> stop-daemons-after\.log\nERROR: exit 3 in \d+\.\ds -> stop-daemons-after\.log\nresult: ok")
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
        self.assertEqual(list(on_disk), ["scenario", "description", "out", "project", "gradle", "gradle_version", "gradle_user_home", "status", "steps"])
        self.assertEqual(on_disk["status"], "ok")
        self.assertEqual(on_disk["scenario"], "j")
        self.assertEqual(on_disk["description"], "one line")
        self.assertEqual(on_disk["out"], str(self.out_dir))
        self.assertEqual(on_disk["project"], str(self.project))
        self.assertEqual(on_disk["gradle"], "gecho")
        run_step, write_step, edit_step = on_disk["steps"]
        self.assertEqual(list(run_step), ["index", "kind", "name", "outcome", "expect", "deviations", "exit_code", "duration_s", "log", "command"])
        self.assertEqual(run_step["kind"], "shell")
        self.assertEqual(run_step["name"], "n")
        self.assertEqual(run_step["expect"], {"exit": "pass"})
        self.assertEqual(run_step["deviations"], [])
        self.assertEqual(run_step["exit_code"], 0)
        self.assertEqual(run_step["command"], "true")
        self.assertEqual(run_step["log"], "step-01.log")
        self.assertEqual(write_step, {"index": 2, "kind": "write", "name": "Write: w", "outcome": "done", "files": ["w"]})
        self.assertEqual(edit_step, {"index": 3, "kind": "edit", "name": "Edit: w", "outcome": "done", "files": ["w"]})
        # the full key set of a run with everything on
        summary = self.run_scenario(gs.load_scenario('name = "k"\n[[steps]]\nrun.gradle = "help"'), self.tmp / "k.out", gradle="gecho",
                           stop_daemons=True, gradle_args=["--offline"])
        self.assertEqual(list(json.loads(summary.to_json())),
                         ["scenario", "out", "project", "gradle", "gradle_version", "gradle_args", "gradle_user_home", "status", "steps", "stop_daemons"])


class CliTest(GustCase):
    def test_help(self):
        for argv in ([], ["help"]):
            code, out, _ = self.cli(*argv)
            self.assertEqual(code, gs.EXIT_OK, argv)
            self.assertIn("usage: gust.py", out)
            for command in ("run", "check", "setup", "stop-daemons", "flat", "spec", "version", "help"):
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
        self.assertEqual(gs.__version__, "10")
        self.assertEqual(lines[0], "gust 10")
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

    def test_any_scenario_file_name(self):
        s = self.scenario_file(MINIMAL, name="loop.scenario")
        code, out, _ = self.cli("run", str(s), "--keep-daemons")
        self.assertEqual(code, gs.EXIT_OK, out)
        self.assertIn(f"out:      {self.tmp / 'loop.out'}\n", out)                    # the stem of any file name
        code, _, err = self.cli("run", str(self.tmp / "nosuch.txt"))
        self.assertEqual(code, gs.EXIT_ERROR)
        self.assertEqual(err.strip(), f"error: scenario file not found: {self.tmp / 'nosuch.txt'}")   # the missing file comes first

    def test_run_exit_codes(self):
        self.assertEqual(self.cli("run", str(self.scenario_file(MINIMAL)), "--out", str(self.tmp / "a"), "--keep-daemons")[0], gs.EXIT_OK)
        bad = self.scenario_file('name = "b"\n[[steps]]\nrun.shell = "false"')
        self.assertEqual(self.cli("run", str(bad), "--out", str(self.tmp / "b"), "--keep-daemons")[0], gs.EXIT_DEVIATED)
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
        self.assertEqual(errors[0], f"error: {self.tmp / 's.toml'}: top level.name: must be str")   # a load error starts with its source
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
        self.assertEqual(last["status"], "ok")
        self.assertEqual(last["out"], str(out_dir))
        self.assertEqual(last["stop_daemons"]["after"]["exit_code"], 0)

    def test_stop_daemons_command(self):
        s = self.scenario_file(MINIMAL)
        out_dir = self.tmp / "s.out"
        code, out, err = self.cli("stop-daemons", str(s), "--gradle", "gecho")               # nothing set up yet
        self.assertEqual((code, out), (gs.EXIT_ERROR, ""))
        self.assertEqual(err.strip(), f"error: no set-up project at {out_dir / 'project'}; run 'gust setup {s}' first")
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
                              rf"\[STOP\] Stop Gradle daemons\n\$ gecho --stop\nok: exit 0 in \d+\.\ds -> stop-daemons\.log\nresult: ok\nout:      {re.escape(str(self.tmp / 's.out'))}\n$")
        self.assertEqual((self.tmp / "s.out" / "stop-daemons.log").read_text().strip(), f"{home_arg()} --stop")
        self.script("failing", FAILING)
        code, _, err = self.cli("stop-daemons", str(s), "--gradle", "failing")          # the probe: `failing --version` fails
        self.assertEqual(code, gs.EXIT_ERROR)
        self.assertIn("cannot run", err)

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
        self.assertEqual(json.loads(lines[-1])["status"], "ok")                 # the JSON line stays last
        self.assertEqual(lines[-2], f"out:      {self.tmp / 's.out'}")           # after result:, before the JSON
        self.assertTrue(lines[-3].startswith("result: ok"))
        self.assertEqual(lines[lines.index("hello") - 1], rule("step-02.log"))
        self.assertEqual(lines[lines.index("hello") + 1], CLOSING)
        self.assertLess(lines.index("[2/2] Run: echo hello"), lines.index("hello"))
        self.assertTrue(lines[lines.index("hello") + 2].startswith("ok: exit 0 in "))

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
        self.assertIn("error: --tmp and --out cannot be combined", err)
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
        self.assertEqual((code, err.strip()), (gs.EXIT_ERROR, "error: SCENARIO is '-' but nothing is piped on stdin"))
        code, _, err = self.cli("run", "-", stdin="")
        self.assertEqual((code, err.strip()), (gs.EXIT_ERROR, "error: stdin: scenario has no steps"))
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
        self.assertEqual(err.strip(), f"error: no set-up project at {self.tmp / 'nowhere' / 'project'}; pipe the same scenario to 'gust setup -' first")
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
            self.assertEqual(err.strip(), f"error: arguments after -- are accepted by run only ({name} runs no Gradle step)")


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

    def test_stdin_with_relative_base(self):
        code, out, err = self.cli("flat", "-", stdin='setup.layout.base = "layouts/java.toml"\n[[steps]]\nrun.shell = "true"\n')
        self.assertEqual((code, err), (gs.EXIT_OK, ""))
        self.assertIn('"src/A.java" = "base A"\n', out)
        self.assertNotIn("base =", out)
        code, out, err = self.cli("flat", "-", stdin='setup.layout.base = "nope.toml"\n[[steps]]\nrun.shell = "true"\n')
        self.assertEqual((code, out), (gs.EXIT_ERROR, ""))
        self.assertEqual(err, f"error: stdin: setup.layout.base: no such file {self.tmp / 'nope.toml'}\n")
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
                                           gradle="gecho").status, "ok")               # no shell step, no bash needed

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
                     "N [N ...]", "step through", "last full run"):
            for where, text in (("spec", gs.FORMAT_SPEC), ("README", readme), ("help", help_out.getvalue())):
                self.assertNotIn(gone, text, f"{gone!r} in {where}")
        for command in ("check SCENARIO", "stop-daemons SCENARIO [--out DIR] [--gradle BIN] [--gradle-user-home DIR]"):
            self.assertIn(command, gs.FORMAT_SPEC, command)


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
