#!/bin/sh
# Release gust from main: .github/release.sh
#
# Releasing, for maintainers: on main at version "<N>-dev", run this script.
# Once v<N> is pushed, the Release workflow (.github/workflows/release.yml)
# runs and a draft release for v<N> appears on GitHub, with gust.py attached.
# Write the notes on that draft and publish it.
#
# The steps, each printed as it begins:
#   - checks: on main, a clean tree, main in sync with origin/main, gust.py at
#     "<N>-dev", and no tag v<N> here or on origin
#   - version "<N>", the unit tests, commit "Release v<N>", signed tag v<N>
#   - main and v<N> pushed together, so both reach origin or neither does
#   - version "<N+1>-dev", commit "Start v<N+1>-dev", main pushed
# At the first failure the run stops, with a note on the state of the checkout
# and how to go on. The GitHub release is not created here; that is the job of
# the Release workflow.
#
# Needs git and python3 on PATH, with signing set up for commits and tags, and
# push access to origin.

set -eu
cd "$(dirname "$0")/.."

step() { printf '\n== %s\n' "$1"; }
fail() { printf 'error: %s\n' "$1" >&2; exit 1; }

# Print a command as `$ ...`, quoting any argument that needs it, then run it.
run() {
    line='$'
    for arg in "$@"; do
        case $arg in
            '' | *[!A-Za-z0-9._/:=@+-]*) line="$line '$arg'" ;;
            *) line="$line $arg" ;;
        esac
    done
    printf '%s\n' "$line"
    "$@"
}

# Set the version line of gust.py to $1; the line must change, and no other.
# The file is overwritten in place, so its mode stays as it is.
set_version() {
    sed "s/^__version__ = \"[^\"]*\"\$/__version__ = \"$1\"/" gust.py > gust.py.new
    changed=$(diff gust.py gust.py.new | grep -c '^>' || true)
    if [ "$changed" != 1 ] || ! grep -qx "__version__ = \"$1\"" gust.py.new; then
        rm -f gust.py.new
        fail "the version line of gust.py did not change to \"$1\" exactly once ($changed lines changed); gust.py is unchanged"
    fi
    cat gust.py.new > gust.py
    rm gust.py.new
}

step "Checking the checkout"
branch=$(git branch --show-current)
[ "$branch" = main ] || fail "on ${branch:-a detached HEAD}, not main; nothing is changed"
status=$(git status --porcelain) || fail "git status failed; nothing is changed"
[ -z "$status" ] || fail "the working tree has changes; commit or stash them first. Nothing is changed"
git fetch --quiet origin main || fail "git fetch failed; nothing is changed"
head=$(git rev-parse --short HEAD)
upstream=$(git rev-parse --short origin/main)
[ "$head" = "$upstream" ] || fail "main is at $head and origin/main at $upstream; bring them in sync first. Nothing is changed"

current=$(sed -n 's/^__version__ = "\([^"]*\)"$/\1/p' gust.py)
n=${current%-dev}
case $current in *-dev) ;; *) n= ;; esac
case $n in
    '' | 0* | *[!0-9]*) fail "gust.py is at version \"$current\", not \"<N>-dev\"; only a dev version can be released" ;;
esac
tag=v$n
next=$((n + 1))-dev
[ -z "$(git tag --list "$tag")" ] || fail "tag $tag exists here already; nothing is changed"
remote_tag=$(git ls-remote --tags origin "refs/tags/$tag") || fail "git ls-remote failed; nothing is changed"
[ -z "$remote_tag" ] || fail "tag $tag exists on origin already; nothing is changed"
printf 'main at %s, gust.py at "%s": releasing %s\n' "$head" "$current" "$tag"

step "Setting the version to \"$n\""
set_version "$n"

step "Running the unit tests"
if ! PYTHONDONTWRITEBYTECODE=1 python3 gust.tests.py; then
    git checkout HEAD -- gust.py
    fail "the unit tests failed; gust.py is back at \"$current\" and nothing is committed"
fi

step "Committing and tagging $tag"
if ! run git commit -S -m "Release $tag" gust.py; then
    git checkout HEAD -- gust.py
    fail "git commit failed; gust.py is back at \"$current\" and nothing is committed"
fi
run git tag -s "$tag" -m "$tag" ||
    fail "git tag failed; the commit 'Release $tag' is local only. To undo it: git reset --hard origin/main"

step "Pushing main and $tag together"
run git push --atomic origin main "refs/tags/$tag" ||
    fail "git push failed; nothing reached origin. To try again: git push --atomic origin main refs/tags/$tag. To undo: git tag -d $tag, then git reset --hard origin/main"

step "Setting the version to \"$next\""
set_version "$next"
run git commit -S -m "Start v$next" gust.py ||
    fail "git commit failed; $tag is on origin, and gust.py is at \"$next\" but not committed. To finish: commit it as 'Start v$next' and push main"
run git push origin main ||
    fail "git push failed; $tag is on origin, and the commit 'Start v$next' is local only. To finish: git push origin main"

printf '\n%s is on origin, and main is at "%s". Once the Release workflow is done, write the notes on the draft release and publish it: https://github.com/alllex/gust/releases\n' "$tag" "$next"
