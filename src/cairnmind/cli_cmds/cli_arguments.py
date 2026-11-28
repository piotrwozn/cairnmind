"""The positional arguments of the command line, each with a name and a description in its help (#244).

A command declares an argument with one of these instead of a bare type, so that `--help` says what it is and
not only its type and [required]. The names are what the usage line shows.
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

ProjectArg = Annotated[str, typer.Argument(metavar="PROJECT", help="The project's name, id or folder.")]
QuestionArg = Annotated[str, typer.Argument(metavar="QUESTION", help="The question, in quotes.")]
ClientArg = Annotated[str, typer.Argument(metavar="CLIENT", help="The coding client: codex or claude.")]
SessionClientArg = Annotated[
    str, typer.Argument(metavar="CLIENT", help="The client: codex, claude, external, api or copilot.")
]
SessionIdArg = Annotated[
    str, typer.Argument(metavar="SESSION_ID", help="The session's id, as `session prepare` printed it.")
]
PlanIdArg = Annotated[
    str, typer.Argument(metavar="PLAN_ID", help="The plan's id, as `plan list` prints it.")
]
JobIdArg = Annotated[str, typer.Argument(metavar="JOB_ID", help="The job's id, as `jobs list` prints it.")]
TaskIdArg = Annotated[
    str, typer.Argument(metavar="TASK_ID", help="The task's id, as `task list` prints it.")
]
RecordIdArg = Annotated[
    str, typer.Argument(metavar="RECORD_ID", help="The record's id, as `impact` lists it.")
]
RecordArg = Annotated[str, typer.Argument(metavar="RECORD", help="The record's id.")]
SourceIdArg = Annotated[
    str, typer.Argument(metavar="SOURCE_ID", help="The source's id, as a citation or `records` names it.")
]
AnswerIdArg = Annotated[
    str, typer.Argument(metavar="ANSWER_ID", help="The logged answer's id, as `answers list` prints it.")
]
ProjectNameArg = Annotated[str, typer.Argument(metavar="NAME", help="The name to give the project.")]
ProjectRootArg = Annotated[Path, typer.Argument(metavar="ROOT", help="The project's folder.")]
OptionalProjectNameArg = Annotated[
    str | None,
    typer.Argument(
        metavar="NAME",
        help="The name to give the project, or a remote Git URL; with --clone defaults to the repository name.",
    ),
]
OptionalProjectRootArg = Annotated[
    str | None,
    typer.Argument(
        metavar="ROOT",
        help="The project's folder, or a remote Git URL; with --clone defaults to ./<repo>.",
    ),
]
FolderArg = Annotated[
    Path, typer.Argument(metavar="PATH", help="Another folder or worktree of the project.")
]
TitleArg = Annotated[str, typer.Argument(metavar="TITLE", help="What the task is, in a few words.")]
StateArg = Annotated[
    str, typer.Argument(metavar="STATE", help="todo, in_progress, needs_test, needs_review, done or blocked.")
]
GroupArg = Annotated[
    str, typer.Argument(metavar="GROUP", help="The old metadata.plan_id label whose tasks become the plan.")
]
ProfileArg = Annotated[
    str, typer.Argument(metavar="PROFILE", help="The check profile, as `check profiles` lists it.")
]
PacketIdArg = Annotated[
    str, typer.Argument(metavar="PACKET_ID", help="The resume packet's id, as `session context` printed it.")
]
PacketHashArg = Annotated[
    str, typer.Argument(metavar="PACKET_HASH", help="The resume packet's hash, as `session context` printed it.")
]
ReadbackArg = Annotated[
    Path,
    typer.Argument(
        metavar="READBACK_JSON",
        help="A JSON file with the plan and task, each its id and version, as read from the packet.",
    ),
]
StateFileArg = Annotated[
    Path, typer.Argument(metavar="STATE_FILE", help="A JSON file with where work stands.")
]
TranscriptArg = Annotated[
    Path, typer.Argument(metavar="PATH", help="The client's JSONL transcript file.")
]
SnapshotArg = Annotated[Path, typer.Argument(metavar="PATH", help="The memory snapshot's folder.")]
OursSnapshotArg = Annotated[
    Path, typer.Argument(metavar="OURS", help="First snapshot folder or manifest.json to merge.")
]
TheirsSnapshotArg = Annotated[
    Path, typer.Argument(metavar="THEIRS", help="Second snapshot folder or manifest.json to merge.")
]
PublishedArg = Annotated[Path, typer.Argument(metavar="PATH", help="The published memory directory.")]
AuditPathArg = Annotated[
    Path,
    typer.Argument(metavar="PATH", help="Repository root, snapshot folder or published memory directory to audit."),
]
RepositoryArg = Annotated[
    Path, typer.Argument(metavar="ROOT", help="The repository's folder; it is copied, not changed.")
]
ExportArg = Annotated[Path, typer.Argument(metavar="DESTINATION", help="The file to write.")]
RevisionArg = Annotated[
    str, typer.Argument(metavar="REVISION", help="The Git commit, branch or tag.")
]
BaseArg = Annotated[str, typer.Argument(metavar="BASE", help="The earlier Git commit, branch or tag.")]
HeadArg = Annotated[str, typer.Argument(metavar="HEAD", help="The later Git commit, branch or tag.")]
PlanArg = Annotated[str, typer.Argument(metavar="PLAN", help="The plan's id, as `plan list` prints it.")]
KindClientArg = Annotated[
    str,
    typer.Argument(metavar="CLIENT", help="The client whose transcripts have the kind: claude, codex or unknown."),
]
KindArg = Annotated[
    str, typer.Argument(metavar="KIND", help="The record kind, as `record-kind list` names it.")
]
ImporterNameArg = Annotated[
    str, typer.Argument(metavar="NAME", help="The external converter's name.")
]
ImporterCommandArg = Annotated[
    str, typer.Argument(metavar="COMMAND", help="Absolute path to the converter executable.")
]
ImporterArgsArg = Annotated[
    list[str] | None,
    typer.Argument(metavar="[ARGS...]", help="Fixed arguments passed to the converter before the transcript path."),
]
