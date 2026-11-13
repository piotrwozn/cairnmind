# Sample project memory

A small project with its memory committed next to it, so that you can see what Cairnmind keeps before you install a database engine, a model or anything else.

The project, Trail Notes, is a few lines of Python that store hiking notes as files. Its memory holds what a team decided while building it, the plan it is working on and the result of a check. All of it was written for this sample: it is no real project history.

## What you need

- Linux, or Windows with WSL2. Cairnmind does not run on native Windows, and macOS is untested.
- [uv](https://docs.astral.sh/uv/). It fetches Python 3.12 if you do not have it.
- This repository.

No Java, no database, no tmux, no Ollama and no model.

## Look at it

```bash
git clone https://github.com/piotrwozn/cairnmind.git
cd cairnmind
uv tool install --python 3.12 .
```

Stop there: do not run the installer script or `cairnmind init`, which set up the full system. If the shell cannot find `cairnmind` afterwards, run `uv tool update-shell` and open a new terminal.

The decisions, each with the addresses of the sources it rests on:

```bash
cairnmind memory show examples/sample-project/.memory/cairnmind --kind decision
```

You will see three. Two are `confirmed`. One is `superseded`: the team first numbered its notes, then two phones wrote the same number on one day, and the decision was replaced by the one above it with the same key, `note-identifiers`. Nothing was deleted; the earlier decision stays readable with its own source.

The plan and its steps:

```bash
cairnmind memory show examples/sample-project/.memory/cairnmind --kind plan
```

The tasks behind those steps, with the check that ran for the first one:

```bash
cairnmind memory show examples/sample-project/.memory/cairnmind --kind task
```

Under the finished step you see `unit: passed` among the *historical check runs*, and `acceptance passed` under *reported by an agent, not verified by this view*. That wording is the point: Cairnmind keeps apart what a machine measured and what somebody claimed, and a check that ran elsewhere is history, not proof about your checkout.

Everything at once, or only what mentions a word:

```bash
cairnmind memory show examples/sample-project/.memory/cairnmind
cairnmind memory show examples/sample-project/.memory/cairnmind --grep network
```

And the proof that what you just read is intact: the manifest and every row it lists match their hashes.

```bash
cairnmind memory inspect examples/sample-project/.memory/cairnmind
```

## What this view shows, and what it leaves out

`memory show` reads the package and prints it. It starts nothing and writes nothing. Some things are always shown:

- **Acceptance criteria and the check each one is linked to.** The first step has two: "The unit check passes.", linked to the check `unit`, and "A note written before tags existed still loads.", which says `no check` because only a person can confirm it.
- **Which decision replaced which.** The newer decision says `replaces:` and the old one, which is `superseded`, says `replaced by:`, each with the other's identifier and key.

And one thing only when you ask for it. The text of a source is in the package, one JSON file per row under `objects/`; by default you see only its address, such as `note://trail-notes/2026-03-19-identifiers-revisited`. Add `--sources` to print the text of each source under the record it supports:

```bash
cairnmind memory show examples/sample-project/.memory/cairnmind --kind decision --sources
```

You now read the meeting note of 2 March under the storage decision. A source that supports two decisions is printed under both. Each text is cut after 1,200 characters, with a note of how many were left out, and a run prints at most 60,000 characters of source text. A source that is not text, is empty or is missing from the package is named and not printed. With `--sources`, `--grep` also searches the text you see, and only that.

What it leaves out even then: the sources of a plan (a plan is listed with their addresses only) and the earlier versions of a record.

Searching, asking questions, the workspace and the briefing all need the full installation, where this package can be imported into a project. See the [main README](../../README.md).

## How the sample was made

`scripts/build_sample_memory.py` builds it with the same code that makes any snapshot: the store, the plan manager, the check runner and `cairnmind memory snapshot`. It needs the database engine, so only a maintainer runs it. Identifiers and times change with every build; `tests/test_sample_memory.py` keeps the committed package readable.
