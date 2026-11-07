# Gust

Write a Gradle user scenario as one TOML file, then run it.

**G**radle **U**ser **S**cenario **T**ool: one Python file, no dependencies.

A scenario holds the files of a Gradle project and the steps to run against it:
Gradle invocations, shell commands, and file edits in between. The project is
written to disk and the steps run in order, until the first check that comes
out unexpected. Gradle output goes to log files, and
with `--show-output` to your terminal too. For agents there are fixed exit
codes, a JSON summary, and `spec` for the whole file format.

## Quick start

Save this as `loop.toml`:

```toml
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
```

In the commands here, `gust` is `gust.py` on your PATH as `gust`; see
[Install](#install).

Then run it:

```
gust run loop.toml
```

Other commands:

```
gust check loop.toml         # validate it and settle the Gradle binary, run nothing
gust setup loop.toml         # set the project up, run nothing
gust stop-daemons loop.toml  # stop the daemons of the project's Gradle
gust flat loop.toml          # print it with setup.layout.base inlined
gust zip loop.toml           # write loop.zip: the project, and the steps to run by hand
gust spec                    # the whole file format, with an example
gust help run                # a command's options
gust version
```

Run `check` before a long run. A throwaway scenario needs no file: pipe it to
`-` (it is named `scenario` unless it sets `name`), here with a `/tmp` out dir:

```
gust run - --tmp <<'EOF'
[[steps]]
run.gradle = "help"
EOF
```

A plain `run` is `setup` plus all the steps, with daemon stops before and
after. With `setup` alone, the project is laid out in the out dir and no step
runs, so you can look around in it or run Gradle there by hand.

## Scenarios

| Key | What it holds |
| --- | --- |
| `name` | optional; defaults to the file's stem (`loop` above) |
| `description` | one line, recorded in the summary and shown by `check`; a run header has only the name |
| `steps` | each one of `run.gradle`, `run.shell`, `write`, `edit`, `delete` (a directory, gone afterwards), or `clear` (a directory, there and empty afterwards) |
| `expect`, on a run step | `"pass"`, `"fail"`, or a table of `exit`, `output`, and `no_output` substring checks |
| `setup.layout.project` | the project's files, inline |
| `setup.layout.base` | the project's files from a layout file |
| `setup.layout.remote` | the project's files from a git commit, pinned to a hash, optionally a subdirectory |
| `setup.project` | an existing project dir, used in place instead of a layout |

Every key is in `gust spec`.

With `setup.project = "<dir>"`, or `gust run loop.toml --project <dir>`, the
steps run in that directory as it is, with no copy, so their writes, edits,
deletes, and clears land in your real files. Nothing outside the project dir,
no `.git`, and neither the out dir nor `.gust` can be deleted or cleared there.
The console header has a `project:  <dir> (in place)` line. The logs and
`summary.json` still go to the out dir, and on a rerun only the out dir is
cleared. A rerun starts from the files as the last run left them; restore them
between runs yourself, for example with git. `setup.layout` cannot be combined
with it, and no wrapper is installed there, so a Gradle version is refused;
pass a binary.

With a run from inside the project, `<stem>.out/` and `.gust/` (a whole Gradle
user home) are made there. To avoid that, pass `--out` or `--tmp` and set
`GUST_DIR`, or have git ignore them. Both are removed by a reset step such as
`git clean -fdx`.

A scenario built on a layout file becomes one self-contained file with
`gust flat loop.toml > loop.flat.toml`: the layout file's files are merged into
`[setup.layout.project]` as for a run, and `setup.layout.remote` stays a link.
The comments at the top of the file are kept, the others are lost.

The Gradle the steps run with is the first of:

1. `--gradle <bin>`: a distribution or a local build, as a name on PATH or in
   the working directory, or as a path. A value with a `/` is always a path.
2. `--gradle 9.9.0`: a version, when the value is not a binary on PATH or in
   the working directory. A wrapper for it is installed into the project first,
   with `gradle` from PATH, so a local Gradle installation is assumed and the
   layout needs a settings file.
3. The scenario's `setup.gradle`: `"wrapper"` for the project's own wrapper,
   which must exist, or a version.
4. The project's own `./gradlew` (`gradlew.bat` on Windows), when the checkout,
   the project dir in place, or the set-up project has one.
5. `gradle` from PATH.

About that choice:

- Nothing is persisted; the choice is made again on every command.
- The `gradle:` header line shows the binary and its version from `--version`,
  the wrapper included, so a wrong path or a non-Gradle binary fails before the
  out dir is touched. A version still to be installed is shown as given.
- Shell steps get the binary as `$GRADLE`.

Arguments after `--` go onto every Gradle step, last, so they take precedence
over the scenario's: `gust run loop.toml -- --isolated-projects`. Shell steps
get them as `$GRADLE_ARGS`.

## What a run leaves behind

Everything goes into the out dir, apart from the changes to a project in place:

| Invocation | Out dir |
| --- | --- |
| default | `loop.out/` in the working directory, after the scenario file's stem (the scenario's name for stdin) |
| `--out DIR` | `DIR` |
| `--tmp` | a throwaway dir, `/tmp/gust-<yymmdd>/loop.<HHMMSS>.out/` (under the temp dir on Windows) |

It holds:

- `project/`, unless the project is in place
- one log per run step
- with `run --trace`, Gradle's build operation trace for each Gradle step, as
  `step-NN-ops-log.txt`
- the daemon-stop logs
- `summary.json`, also on stdout with `--json`: the run's `result` (`success`,
  `failure`, or `error`), and each step's, with every check of a run step as
  `expected` or `unexpected`

The out dir is named in the console header and again on the last line. On a
rerun it is removed and recreated, but a directory without gust's marker is
left alone.

Daemons of the chosen Gradle are stopped before the first step and after the
last, so every run starts cold. These are the `[BEF]` and `[AFT]` lines around
the numbered steps; pass `--keep-daemons` to skip them.

The working directory also gets `.gust/`, a cache that is safe to delete (set
`GUST_DIR` to put it elsewhere):

- `remotes/`: one git checkout per `setup.layout.remote`, shallow where
  possible.
- `shared-gradle-user-home/`: the Gradle user home for every Gradle invocation,
  so distributions, dependency caches, and daemons stay here instead of in
  `~/.gradle`.
  - It goes first on each invocation as `--gradle-user-home`, and is left out
    of the commands shown on the console and in `summary.json`.
  - Shell steps get it as `GRADLE_USER_HOME`.
  - A `--gradle-user-home` in a step, or on the command line, takes precedence.

## Scenario zip

To hand a scenario to someone without Gust, such as a reproducer to attach to an
issue:

```
gust zip loop.toml                        # writes loop.zip, which unzips to loop/
gust zip loop.toml -o ~/repro-39193.zip   # unzips to repro-39193/
```

The zip holds the project's starting state, as `setup` lays it out, and a
`SCENARIO.md`: the scenario written out as steps to run by hand. It has the
scenario's name, description, and Gradle version, and each step as a shell
snippet: `gradle <args>` for a Gradle step, a heredoc for a write, a diff for
`patch -p1` for an edit, and `rm -rf` for a delete or clear. A snippet holds
commands only, so it can be pasted into any shell, zsh included; what to expect
from it is in the text above it. The commands of a file step are joined with
`&&`, so its block stops at the first failure. The wrapper is left out, and so is the scenario
file. In a shell step, `./gradlew` becomes `gradle`, and a step that still uses
the wrapper, such as `test -x gradlew`, is refused.

- No Gradle runs, and nothing is installed. The Gradle version comes from
  `setup.gradle`, or from the wrapper's properties when the steps run with the
  project's own wrapper; pass `--gradle 9.9.0` to name another.
- The zip's path is the only line on stdout. An existing zip is never replaced,
  and a scenario with `setup.project` is refused, since there is nothing to lay
  out.
- For the same scenario, `SCENARIO.md` is the same, byte for byte, on every
  machine: no timestamps, no absolute paths, no Gust version. The zip is the
  same too wherever Python's zlib is the same.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | success: every check came out as expected |
| `1` | failure: a check came out unexpected |
| `2` | error: gust could not do something: bad input (a failed precondition, a malformed scenario, bad arguments), a step that could not be carried out, or a failed wrapper install |

The preconditions (Python version, scenario shape, Gradle binary, git) are all
checked before the out dir is touched. `summary.json` is written for `gust run`
only, and there it is present whenever the header was printed.

On a terminal, the console's verdict words are colored in the terminal's own
palette: `EXPECTED` green, `UNEXPECTED` bold red, `ERROR` red (on stderr too),
`SUCCESS` bold, `FAILURE` yellow. Set [`NO_COLOR`](https://no-color.org) for a plain console,
or `FORCE_COLOR` for color through a pipe; the steps get `FORCE_COLOR` in
their environment too. Logs and `summary.json` stay plain.

## Install

Gust is one file, `gust.py`, with no dependencies beyond the Python standard
library. It needs Python 3.11 or newer, and runs on macOS, Linux, and Windows.
Download it from the latest release as `gust`, make it executable, and move it
into any directory on your PATH, such as `~/.local/bin` or `/usr/local/bin`:

```
curl -fLo gust https://github.com/alllex/gust/releases/latest/download/gust.py
chmod +x gust
mv gust <dir-on-your-PATH>/
```

Or, from a checkout of this repository, make `gust.py` executable and link or
copy it as `gust` into a directory on your PATH:

```
chmod +x gust.py
ln -s "$PWD/gust.py" <dir-on-your-PATH>/gust
```

Without `gust` on your PATH, run `python3 gust.py` wherever `gust` appears here.

On Windows, run `python gust.py`. Shell steps there run with the bash of
[Git for Windows](https://gitforwindows.org), so a scenario file works the same
on every system, and the project's wrapper is `gradlew.bat`. `gust spec` has
the details.

## Tests

```
python3 gust.tests.py
```

They run offline against a local git repository and fake Gradle binaries, and
need `git` on PATH. On Windows, run `python gust.tests.py`, with Git for
Windows installed.

## License

Distributed under the MIT License. See `LICENSE` for more information.
