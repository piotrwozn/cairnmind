"""The positional arguments of the command line, each with a name and a description in its help (#244).

A command declares an argument with one of these instead of a bare type, so that `--help` says what it is and
not only its type and [required]. The names are what the usage line shows.
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer


def _text(metavar: str, description: str):
    return Annotated[str, typer.Argument(metavar=metavar, help=description)]


def _path(metavar: str, description: str):
    return Annotated[Path, typer.Argument(metavar=metavar, help=description)]


ProjectArg = _text("PROJECT", "The project's name, id or folder.")
QuestionArg = _text("QUESTION", "The question, in quotes.")
ClientArg = _text("CLIENT", "The coding client: codex or claude.")
SessionClientArg = _text("CLIENT", "The client: codex, claude, external, api or copilot.")
SessionIdArg = _text("SESSION_ID", "The session's id, as `session prepare` printed it.")
PlanIdArg = _text("PLAN_ID", "The plan's id, as `plan list` prints it.")
JobIdArg = _text("JOB_ID", "The job's id, as `jobs list` prints it.")
TaskIdArg = _text("TASK_ID", "The task's id, as `task list` prints it.")
RecordIdArg = _text("RECORD_ID", "The record's id, as `impact` lists it.")
RecordArg = _text("RECORD", "The record's id.")
SourceIdArg = _text("SOURCE_ID", "The source's id, as a citation or `records` names it.")
AnswerIdArg = _text("ANSWER_ID", "The logged answer's id, as `answers list` prints it.")
ProjectNameArg = _text("NAME", "The name to give the project.")
ProjectRootArg = _path("ROOT", "The project's folder.")
OptionalProjectNameArg = Annotated[str | None, typer.Argument(
    metavar="NAME", help="The name to give the project, or a remote Git URL; with --clone defaults to the repository name.")]
OptionalProjectRootArg = Annotated[str | None, typer.Argument(
    metavar="ROOT", help="The project's folder, or a remote Git URL; with --clone defaults to ./<repo>.")]
FolderArg = _path("PATH", "Another folder or worktree of the project.")
TitleArg = _text("TITLE", "What the task is, in a few words.")
StateArg = _text("STATE", "todo, in_progress, needs_test, needs_review, done or blocked.")
GroupArg = _text("GROUP", "The old metadata.plan_id label whose tasks become the plan.")
ProfileArg = _text("PROFILE", "The check profile, as `check profiles` lists it.")
PacketIdArg = _text("PACKET_ID", "The resume packet's id, as `session context` printed it.")
PacketHashArg = _text("PACKET_HASH", "The resume packet's hash, as `session context` printed it.")
ReadbackArg = _path("READBACK_JSON", "A JSON file with the plan and task, each its id and version, as read "
                                     "from the packet.")
StateFileArg = _path("STATE_FILE", "A JSON file with where work stands.")
TranscriptArg = _path("PATH", "The client's JSONL transcript file.")
SnapshotArg = _path("PATH", "The memory snapshot's folder.")
PublishedArg = _path("PATH", "The published memory directory.")
RepositoryArg = _path("ROOT", "The repository's folder; it is copied, not changed.")
ExportArg = _path("DESTINATION", "The file to write.")
RevisionArg = _text("REVISION", "The Git commit, branch or tag.")
BaseArg = _text("BASE", "The earlier Git commit, branch or tag.")
HeadArg = _text("HEAD", "The later Git commit, branch or tag.")
PlanArg = _text("PLAN", "The plan's id, as `plan list` prints it.")
KindClientArg = _text("CLIENT", "The client whose transcripts have the kind: claude, codex or unknown.")
KindArg = _text("KIND", "The record kind, as `record-kind list` names it.")
ImporterNameArg = _text("NAME", "The external converter's name.")
ImporterCommandArg = _text("COMMAND", "Absolute path to the converter executable.")
ImporterArgsArg = Annotated[
    list[str] | None,
    typer.Argument(metavar="[ARGS...]", help="Fixed arguments passed to the converter before the transcript path."),
]
