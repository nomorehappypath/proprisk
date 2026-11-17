# Contributing to PropRisk

Contributions are accepted under the Apache License 2.0, the same licence as PropRisk.
No contributor agreement (CLA) is needed.

The most useful contribution is **your county**. Adding one is filling in one
data file, and the recipe in [`docs/EXTENDING.md`](docs/EXTENDING.md) is written so
that an AI build tool can do it for you. You need no programming.

## With NoMoreHappyPath (the way PropRisk was built)

[NoMoreHappyPath](https://nomorehappypath.com) is free and open source.

1. On GitHub, **fork** `nomorehappypath/proprisk`, then clone your fork to your
   computer.
2. In NoMoreHappyPath, choose **Adopt existing** and pick that folder.
3. Set NoMoreHappyPath's upload to a new branch in **your fork**, for example
   `add-comal-county`.
4. Give it the task, in your own words: "Add Comal County, Texas to PropRisk."
   It reads the recipe, shows you the table of what it found for the county, and
   asks you to approve it before it writes the file.
5. When you accept the finished work, NoMoreHappyPath pushes it to your fork.
6. On GitHub, press **Compare & pull request** to `nomorehappypath/proprisk`, and
   paste the output of `scripts/check_region.sh tx <county>` into the pull request.

## With another AI tool, or by hand

Claude Code, Codex, Cursor and others read [`AGENTS.md`](AGENTS.md) and follow the
same recipe. By hand: follow [`docs/EXTENDING.md`](docs/EXTENDING.md) yourself.
Either way, fork, branch, run the check, and open a pull request with its output.

## What a pull request needs

- The check ends with PASS: `scripts/check_region.sh tx <county>`.
- The filled requirements table from `docs/EXTENDING.md`.
- No Python change for a county. If the recipe cannot express your county, open an
  issue instead, describing what the county publishes.

Other changes (bugs, new sources): see [`docs/MAINTENANCE.md`](docs/MAINTENANCE.md)
and run `scripts/test.sh` before opening a pull request.
