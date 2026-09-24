# Gust

Write a Gradle user scenario as one TOML file, then run it.

**G**radle **U**ser **S**cenario **T**ool: one Python file, no dependencies.

A scenario holds the files of a Gradle project and the steps to run against it:
Gradle invocations, shell commands, and file edits in between. The project is
written to disk and the steps run in order, until the first one whose outcome
differs from what the scenario expects. Gradle output goes to log files, and
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
dependencies { testImplementation("org.junit.jupiter:junit-jupiter:5.11.4") }
tasks.test { useJUnitPlatform() }
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
gust step loop.toml 2 3      # run steps 2 and 3 against the set-up project
gust stop-daemons loop.toml  # stop the daemons of the project's Gradle
gust flat loop.toml          # print it with setup.layout.base inlined
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

To go one step at a time, set the project up and then name the steps:
`gust setup loop.toml`, then `gust step loop.toml 2` (several numbers run in
the order given). The steps run against the project as it is, with no daemon
stops, and `summary.json` stays as the last full run left it. A plain `run` is
setup plus all the steps, with daemon stops before and after.

## Scenarios

| Key | What it holds |
| --- | --- |
| `name` | optional; defaults to the file's stem (`loop` above) |
| `description` | one line, recorded in the summary and shown by `check`; a run header has only the name |
| `steps` | each one of `run.gradle`, `run.shell`, `write`, or `edit` |
| `expect`, on a run step | `"pass"`, `"fail"`, or a table of `exit`, `output`, and `no_output` substring checks |
| `setup.layout.project` | the project's files, inline |
| `setup.layout.base` | the project's files from a layout file |
| `setup.layout.remote` | the project's files from a git commit, pinned to a hash, optionally a subdirectory |

Every key is in `gust spec`.

A scenario built on a layout file becomes one self-contained file with
`gust flat loop.toml > loop.flat.toml`: the layout file's files are merged into
`[setup.layout.project]` as for a run, and `setup.layout.remote` stays a link.
The comments at the top of the file are kept, the others are lost.

The Gradle the steps run with is the first of:

1. `--gradle <bin>`: a distribution or a local build, as a name on PATH or in
   the working directory, or as a path. A value with a `/` is always a path.
2. `--gradle 9.7.1`: a version, when the value is not a binary on PATH or in
   the working directory. A wrapper for it is installed into the project first,
   with `gradle` from PATH, so a local Gradle installation is assumed and the
   layout needs a settings file.
3. The scenario's `setup.gradle`: `"wrapper"` for the project's own wrapper,
   which must exist, or a version.
4. The project's own `./gradlew` (`gradlew.bat` on Windows), when the checkout
   or the set-up project has one.
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

Everything goes into the out dir:

| Invocation | Out dir |
| --- | --- |
| default | `loop.out/` in the working directory, after the scenario file's stem (the scenario's name for stdin) |
| `--out DIR` | `DIR` |
| `--tmp` | a throwaway dir, `/tmp/gust-<yymmdd>/loop.<HHMMSS>.out/` (under the temp dir on Windows) |

It holds:

- `project/`
- one log per run step
- the daemon-stop logs
- `summary.json`, also on stdout with `--json`

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

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | every step went as expected |
| `1` | a run step deviated from its expectation |
| `2` | bad input (a failed precondition, a malformed scenario, bad arguments), a step that could not be carried out, or a failed wrapper install |

The preconditions (Python version, scenario shape, Gradle binary, git) are all
checked before the out dir is touched, and `summary.json` is there whenever the
header was printed.

## Install

Gust is one file, `gust.py`, with no dependencies beyond the Python standard
library. It needs Python 3.11 or newer, and runs on macOS, Linux, and Windows. Download it from the latest release as
`gust`, make it executable, and move it into any directory on your PATH, such
as `~/.local/bin` or `/usr/local/bin`:

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
