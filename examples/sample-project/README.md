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

And the proof that the package is whole, every file matching the hash in its manifest:

```bash
cairnmind memory inspect examples/sample-project/.memory/cairnmind
```

## What this view does not show

`memory show` reads the package and prints it. It starts nothing and writes nothing. It also leaves things out:

- **The text of a source.** You see its address, such as `note://trail-notes/2026-03-19-identifiers-revisited`. The text is in the package, one JSON file per row under `objects/`:

  ```
  grep -l "Two phones wrote note 14" examples/sample-project/.memory/cairnmind/objects/*.json
  ```

- **Acceptance criteria and the check each one is linked to.** The first step has two: "The unit check passes.", linked to the check `unit`, and "A note written before tags existed still loads.", which only a person can confirm. They are in the task's row under `objects/`.
- **Which decision replaced which.** The view shows the word `superseded`; the link itself is in the row of the newer decision.

Searching, asking questions, the workspace and the briefing all need the full installation, where this package can be imported into a project. See the [main README](../../README.md).

## How the sample was made

`scripts/build_sample_memory.py` builds it with the same code that makes any snapshot: the store, the plan manager, the check runner and `cairnmind memory snapshot`. It needs the database engine, so only a maintainer runs it. Identifiers and times change with every build; `tests/test_sample_memory.py` keeps the committed package readable.
