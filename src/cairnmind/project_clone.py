"""Clone a Git repository (via `gh` or `git`) before registering it as a Cairnmind project.

People work differently with GitHub: some authenticate through GitHub CLI (`gh`) and expect private
repositories and `gh`'s configured protocol to work out of the box; others use plain `git` with SSH
keys (including custom `~/.ssh/config` host aliases) or credential helpers and may not have `gh`
installed or signed in.

With ``--via auto`` (the default):
- An explicit SSH URL (`git@host:...` or `ssh://...`) always uses `git`, so `gh` never rewrites the
  requested SSH transport or rejects a custom SSH host alias.
- A GitHub shorthand (`owner/repo`) or `https://github.com/...` URL uses `gh` when `gh` is on PATH
  and signed in to `github.com` (`gh auth status`), and falls back to `git` otherwise.
- Any other HTTPS Git host uses `git`.

All validation and store preflight checks run before any network call or filesystem write.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import urlsplit

from .config import is_own_variable
from .first_start import PROJECT_NAME_TAKEN, PROJECT_ROOT_TAKEN
from .memory_paths import PUBLISHED_RELATIVE, snapshot_directory
from .path_spelling import lies_in
from .process_control import quote_arg
from .store_writer import StoreNotWriter
from .terminal_safe import safe_text

CLONE_TOOLS = ("auto", "gh", "git")
DEFAULT_TIMEOUT = 600
GH_AUTH_TIMEOUT = 5

_GH_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REPO_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,252}[A-Za-z0-9])?$")
_USER = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$")
_SEGMENT = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_SCP_SSH = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9._-]{0,63})@([A-Za-z0-9][A-Za-z0-9._-]{0,253}):([^?#@]+)$")
_EXPLICIT_SSH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}@[A-Za-z0-9][A-Za-z0-9._-]{0,253}:.+$")
_SECRET_PATTERNS = (
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{12,255}|github_pat_[A-Za-z0-9_]{12,255})\b"),
    re.compile(r"(https?://)([^/\s@]+)@"),
)

# English message templates; Polish translations live in MESSAGES below and are merged into terminal_i18n.
GIT_MISSING = "git was not found on PATH. Install git first."
GH_MISSING = "gh was not found on PATH. Install GitHub CLI (gh), or clone with `--via git`."
INVALID_VIA = "Clone tool must be auto, gh or git."
INVALID_REPO = ("Give a repository as owner/repo, https://github.com/owner/repo, "
                "git@github.com:owner/repo.git or an ssh:// URL.")
UNSAFE_SCHEME = "Only HTTPS and SSH repository URLs (or GitHub owner/repo) are allowed; {scheme} is refused."
URL_CREDENTIALS_REFUSED = ("Repository URLs must not contain embedded credentials or tokens. "
                           "Use `gh auth login`, SSH keys or a Git credential helper instead.")
GITHUB_SUBPATH_REFUSED = ("Give the GitHub repository URL (`https://github.com/owner/repo`), "
                          "not a page or path inside it.")
INVALID_BRANCH = "Branch or tag must be a valid Git ref name, without spaces, control characters or a leading '-'."
DEST_SYMLINK = "Destination {root} is a symbolic link; choose a new folder to clone into."
DEST_NOT_DIR = "Destination {root} already exists and is a file, not a folder."
DEST_NOT_EMPTY = ("Destination {root} already exists and is not empty. Choose an empty or new folder, "
                  "or register it directly with `cairnmind project add NAME {quoted}`.")
CLONING_NOW = "Cloning {source} into {root} with {tool}…"
CLONE_FAILED = "Cloning {source} with {tool} failed: {detail}"
CLONE_FAILED_ABOVE = "Cloning {source} with {tool} failed (exit status {code}); its output is above."
CLONE_TIMED_OUT = "Cloning {source} with {tool} did not finish within {seconds} seconds."
HINT_GIT_AUTH_WITH_GH = ("For a private repository, sign in with `gh auth login` and try again, "
                         "or use an SSH URL (`git@github.com:{owner}/{repo}.git`).")
HINT_GIT_AUTH_NO_GH = ("For a private repository, configure Git credentials, use an SSH URL "
                       "(`git@github.com:{owner}/{repo}.git`), or install and sign in to `gh`.")
HINT_GH_FALLBACK = "Check `gh auth status`, or retry with `--via git` (for example with an SSH URL)."
CLONE_ONLY_OPTION = "{option} can be used only when cloning a repository."
DATA_DIR_REFUSED = ("Project folder cannot be inside the data directory: Cairnmind never indexes "
                    "it; choose a folder outside it")
CREDENTIAL_STORE_REFUSED = "Project root cannot contain Cairnmind's private credential store"

MESSAGES = {
    GIT_MISSING: "Nie znaleziono polecenia git w PATH. Najpierw zainstaluj git.",
    GH_MISSING: "Nie znaleziono polecenia gh w PATH. Zainstaluj GitHub CLI (gh) albo użyj `--via git`.",
    INVALID_VIA: "Narzędzie klonowania musi mieć wartość auto, gh albo git.",
    INVALID_REPO: ("Podaj repozytorium jako owner/repo, https://github.com/owner/repo, "
                   "git@github.com:owner/repo.git albo URL ssh://."),
    UNSAFE_SCHEME: "Dozwolone są tylko adresy HTTPS i SSH (lub GitHub owner/repo); schemat {scheme} został odrzucony.",
    URL_CREDENTIALS_REFUSED: ("Adres repozytorium nie może zawierać poświadczeń ani tokenów. "
                              "Użyj `gh auth login`, kluczy SSH albo Git credential helpera."),
    GITHUB_SUBPATH_REFUSED: ("Podaj główny adres repozytorium GitHub (`https://github.com/owner/repo`), "
                             "a nie podstronę wewnątrz niego."),
    INVALID_BRANCH: "Gałąź lub tag musi być poprawną nazwą referencji Git bez spacji, znaków sterujących i wiodącego '-'.",
    DEST_SYMLINK: "Cel {root} jest dowiązaniem symbolicznym; wybierz nowy folder do sklonowania.",
    DEST_NOT_DIR: "Cel {root} już istnieje i jest plikiem, a nie folderem.",
    DEST_NOT_EMPTY: ("Cel {root} już istnieje i nie jest pusty. Wybierz pusty lub nowy folder, "
                     "albo zarejestruj go bezpośrednio poleceniem `cairnmind project add NAZWA {quoted}`."),
    CLONING_NOW: "Klonowanie {source} do {root} przez {tool}…",
    CLONE_FAILED: "Klonowanie {source} przez {tool} nie powiodło się: {detail}",
    CLONE_FAILED_ABOVE: "Klonowanie {source} przez {tool} nie powiodło się (kod wyjścia {code}); wynik jest powyżej.",
    CLONE_TIMED_OUT: "Klonowanie {source} przez {tool} nie zakończyło się w ciągu {seconds} sekund.",
    HINT_GIT_AUTH_WITH_GH: ("Dla prywatnego repozytorium zaloguj się przez `gh auth login` i spróbuj ponownie, "
                            "albo użyj adresu SSH (`git@github.com:{owner}/{repo}.git`)."),
    HINT_GIT_AUTH_NO_GH: ("Dla prywatnego repozytorium skonfiguruj poświadczenia Git, użyj adresu SSH "
                          "(`git@github.com:{owner}/{repo}.git`) albo zainstaluj i zaloguj `gh`."),
    HINT_GH_FALLBACK: "Sprawdź `gh auth status` albo spróbuj ponownie z `--via git` (na przykład z adresem SSH).",
    CLONE_ONLY_OPTION: "Opcji {option} można użyć tylko podczas klonowania repozytorium.",
}


class CloneError(ValueError):
    """An operational clone refusal or failure."""


@dataclass(frozen=True)
class CloneSource:
    raw: str
    kind: str  # "github_shorthand" | "https" | "ssh"
    host: str
    owner: str
    repo: str
    default_name: str
    git_url: str
    gh_target: str


def is_explicit_remote_url(value: str | Path | None) -> bool:
    """True only for an unambiguous remote Git URL (HTTPS or SSH), never for `owner/repo` or a local path."""
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text or any(ch.isspace() or ord(ch) < 32 for ch in text):
        return False
    if text.startswith(("https://", "ssh://")) or _EXPLICIT_SSH.fullmatch(text):
        try:
            return not Path(text).exists()
        except OSError:
            return True
    return False


def _validate_segments(segments: list[str], *, github: bool) -> tuple[str, str]:
    if len(segments) < 2:
        raise ValueError(INVALID_REPO)
    if github and len(segments) > 2:
        raise ValueError(GITHUB_SUBPATH_REFUSED)
    for part in segments:
        if not part or part in {".", ".."} or part.startswith("-") or not _SEGMENT.fullmatch(part):
            raise ValueError(INVALID_REPO)
    owner = "/".join(segments[:-1])
    raw_repo = segments[-1]
    repo = raw_repo[:-4] if raw_repo.endswith(".git") else raw_repo
    if not repo or repo in {".", ".."} or repo.startswith("-") or not _REPO_NAME.fullmatch(repo):
        raise ValueError(INVALID_REPO)
    if github and not _GH_OWNER.fullmatch(owner):
        raise ValueError(INVALID_REPO)
    return owner, repo


def parse_clone_source(spec: str) -> CloneSource:
    """Validate and classify a repository specifier (`owner/repo`, HTTPS URL or SSH URL)."""
    if not isinstance(spec, str):
        raise ValueError(INVALID_REPO)
    text = spec.strip()
    if not text or len(text) > 512 or text.startswith("-") or any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise ValueError(INVALID_REPO)
    if text.casefold().startswith("ext::"):
        raise ValueError(UNSAFE_SCHEME.format(scheme="ext::"))
    if any(ch.isspace() for ch in text):
        raise ValueError(INVALID_REPO)
    if "://" in text:
        scheme = text.split("://", 1)[0].lower()
        if scheme not in {"https", "ssh"}:
            raise ValueError(UNSAFE_SCHEME.format(scheme=f"{scheme}://"))
        parsed = urlsplit(text)
        if parsed.query or parsed.fragment:
            raise ValueError(INVALID_REPO)
        if scheme == "https" and (parsed.username is not None or parsed.password is not None):
            raise ValueError(URL_CREDENTIALS_REFUSED)
        if scheme == "ssh" and parsed.password is not None:
            raise ValueError(URL_CREDENTIALS_REFUSED)
        if scheme == "ssh" and parsed.username is not None and not _USER.fullmatch(parsed.username):
            raise ValueError(INVALID_REPO)
        try:
            _ = parsed.port
        except ValueError:
            raise ValueError(INVALID_REPO) from None
        host = parsed.hostname or ""
        if not host or not _HOST.fullmatch(host):
            raise ValueError(INVALID_REPO)
        github = host.lower() == "github.com"
        segments = [part for part in parsed.path.split("/") if part != ""]
        owner, repo = _validate_segments(segments, github=github)
        cleaned = text.rstrip("/")
        gh_target = f"{owner}/{repo}" if github else cleaned
        return CloneSource(raw=text, kind=scheme, host=host, owner=owner, repo=repo, default_name=repo,
                           git_url=cleaned, gh_target=gh_target)

    scp = _SCP_SSH.fullmatch(text)
    if scp:
        _user, host, path_part = scp.groups()
        if not _HOST.fullmatch(host) or path_part.startswith(("/", "-")) or "\\" in path_part:
            raise ValueError(INVALID_REPO)
        github = host.lower() == "github.com"
        segments = path_part.rstrip("/").split("/")
        owner, repo = _validate_segments(segments, github=github)
        cleaned = text.rstrip("/")
        gh_target = f"{owner}/{repo}" if github else cleaned
        return CloneSource(raw=text, kind="ssh", host=host, owner=owner, repo=repo, default_name=repo,
                           git_url=cleaned, gh_target=gh_target)

    if ":" in text or "@" in text or "\\" in text:
        raise ValueError(INVALID_REPO)

    segments = text.strip("/").split("/")
    if text.startswith("/") or text.endswith("/") or len(segments) != 2:
        raise ValueError(INVALID_REPO)
    owner, repo = _validate_segments(segments, github=True)
    return CloneSource(raw=text, kind="github_shorthand", host="github.com", owner=owner, repo=repo,
                       default_name=repo, git_url=f"https://github.com/{owner}/{repo}.git",
                       gh_target=f"{owner}/{repo}")


def validate_branch(branch: str) -> str:
    """Validate an optional branch or tag name passed to clone."""
    if not isinstance(branch, str):
        raise ValueError(INVALID_BRANCH)
    text = branch.strip()
    if (not text or len(text) > 256 or text.startswith("-") or ".." in text
            or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in text)):
        raise ValueError(INVALID_BRANCH)
    return text


def clone_environment(*, interactive: bool, environ: dict[str, str] | None = None) -> dict[str, str]:
    """Build a child-process environment without Cairnmind's own variables or credentials."""
    base = os.environ if environ is None else environ
    env = {key: value for key, value in base.items() if not is_own_variable(key)}
    if not interactive:
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GH_PROMPT_DISABLED"] = "1"
    return env


def gh_authenticated(host: str = "github.com", *, runner=subprocess.run, environ=None) -> bool:
    """True when `gh` reports a valid sign-in for `host`; never asks for or prints a token."""
    try:
        done = runner(["gh", "auth", "status", "--hostname", host], stdin=subprocess.DEVNULL,
                      capture_output=True, text=True, timeout=GH_AUTH_TIMEOUT,
                      env=clone_environment(interactive=False, environ=environ))
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def choose_tool(source: CloneSource, via: str = "auto", *, which=shutil.which,
                is_gh_authenticated=gh_authenticated) -> str:
    """Select `gh` or `git` according to `--via`, input transport and local tool availability."""
    tool = (via or "auto").strip().lower()
    if tool not in CLONE_TOOLS:
        raise ValueError(INVALID_VIA)
    if not which("git"):
        raise CloneError(GIT_MISSING)
    if tool == "git":
        return "git"
    if tool == "gh":
        if not which("gh"):
            raise CloneError(GH_MISSING)
        return "gh"
    # auto: an explicit SSH URL always goes straight to git so gh never rewrites protocol or rejects ~/.ssh/config aliases.
    if source.kind == "ssh":
        return "git"
    if source.host.lower() == "github.com" and which("gh") and is_gh_authenticated(source.host):
        return "gh"
    return "git"


def prepare_destination(destination: Path, data_dir: Path) -> Path:
    """Resolve and validate the target directory before any network clone starts."""
    expanded = destination.expanduser()
    resolved = expanded.resolve(strict=False)
    data_root = data_dir.expanduser().resolve(strict=False)
    if lies_in(str(resolved), str(data_root)):
        raise ValueError(DATA_DIR_REFUSED)
    if (data_root / "credentials" / "api-keys.json").exists() and lies_in(str(data_root), str(resolved)):
        raise ValueError(CREDENTIAL_STORE_REFUSED)
    shown = safe_text(str(resolved))
    if expanded.is_symlink() or resolved.is_symlink():
        raise ValueError(DEST_SYMLINK.format(root=shown))
    if resolved.exists():
        if not resolved.is_dir():
            raise ValueError(DEST_NOT_DIR.format(root=shown))
        try:
            has_entries = any(resolved.iterdir())
        except OSError as exc:
            raise ValueError(f"{exc.strerror or 'Destination cannot be read'}: {shown}") from None
        if has_entries:
            raise ValueError(DEST_NOT_EMPTY.format(root=shown, quoted=quote_arg(shown)))
    return resolved


def check_store_preflight(store, name: str, destination: Path) -> str:
    """Check writer role, name limits and active project conflicts before cloning."""
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Project name must not be empty")
    if len(clean_name) > 256:
        raise ValueError("Project name must be at most 256 characters")
    if getattr(store, "not_writer", None):
        raise StoreNotWriter(store.not_writer)
    if hasattr(store, "require_safe_project_root"):
        path = store.require_safe_project_root(destination)
    else:
        path = str(destination.expanduser().resolve(strict=False))
    resolved = str(Path(path).resolve(strict=False))
    projects_by_id = {}
    for row in store.list_projects():
        projects_by_id[row.get("id")] = row
        row_name = row.get("name")
        row_root = str(Path(row["root"]).resolve(strict=False)) if row.get("root") else ""
        if row_name == clean_name:
            raise ValueError(PROJECT_NAME_TAKEN.format(name=safe_text(clean_name), root=safe_text(row["root"])))
        if row_root == resolved:
            raise ValueError(PROJECT_ROOT_TAKEN.format(name=safe_text(row_name or ""), root=safe_text(resolved)))
    if hasattr(store, "query"):
        for root_row in store.query("SELECT project_id FROM project_roots WHERE path=:path", {"path": path}):
            owner = projects_by_id.get(root_row.get("project_id"))
            if owner:
                raise ValueError(PROJECT_ROOT_TAKEN.format(name=safe_text(owner["name"]), root=safe_text(path)))
    return clean_name


def inspect_cloned_memory(root: Path) -> dict[str, str | None]:
    """Detect portable snapshot and published team memory directories without reading or importing them."""
    snapshot = snapshot_directory(root)
    published = root / PUBLISHED_RELATIVE
    return {
        "memory_snapshot": str(snapshot) if snapshot is not None else None,
        "published_memory": str(published) if published.is_dir() and not published.is_symlink() else None,
    }


def _scrub_secrets(text: str) -> str:
    cleaned = safe_text(text, limit=300)
    for pattern in _SECRET_PATTERNS:
        cleaned = pattern.sub(lambda match: (match.group(1) + "[REDACTED]@") if match.lastindex else "[REDACTED]",
                              cleaned)
    return cleaned


def _error_detail(stderr: str | None, stdout: str | None, code: int) -> str:
    lines = [line.strip() for line in ((stderr or "") + "\n" + (stdout or "")).splitlines() if line.strip()]
    for line in reversed(lines):
        if line.lower().startswith(("fatal:", "error:", "remote: ")):
            return _scrub_secrets(line)
    return _scrub_secrets(lines[-1]) if lines else f"exit status {code}"


def _failure_hint(source: CloneSource, tool: str, detail: str, *, which=shutil.which) -> str:
    lowered = detail.casefold()
    if tool == "gh":
        return " " + HINT_GH_FALLBACK
    if source.host.lower() == "github.com" and any(marker in lowered for marker in (
            "authentication", "could not read username", "repository not found", "permission denied",
            "access rights", "terminal prompts disabled", "403", "401", "not found")):
        template = HINT_GIT_AUTH_WITH_GH if which("gh") else HINT_GIT_AUTH_NO_GH
        return " " + template.format(owner=source.owner, repo=source.repo)
    return ""


def clone_command(source: CloneSource, destination: Path, tool: str, branch: str | None = None) -> list[str]:
    """Build the argv list for `git clone` or `gh repo clone` without a shell."""
    dest = str(destination)
    if tool == "gh":
        extra = ["--", "--branch", branch] if branch else []
        return ["gh", "repo", "clone", source.gh_target, dest, *extra]
    extra = ["--branch", branch] if branch else []
    return ["git", "clone", *extra, "--", source.git_url, dest]


def clone_repository(source: CloneSource, destination: Path, *, via: str = "auto", branch: str | None = None,
                     interactive: bool = False, timeout: int = DEFAULT_TIMEOUT, which=shutil.which,
                     is_gh_authenticated=gh_authenticated, runner=subprocess.run,
                     status_out=None) -> dict:
    """Clone `source` into `destination` with `gh` or `git`, returning clone metadata."""
    chosen_branch = validate_branch(branch) if branch is not None else None
    tool = choose_tool(source, via, which=which, is_gh_authenticated=is_gh_authenticated)
    argv = clone_command(source, destination, tool, chosen_branch)
    env = clone_environment(interactive=interactive)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if interactive:
        stream = sys.stderr if status_out is None else status_out
        print(CLONING_NOW.format(source=safe_text(source.raw), root=safe_text(str(destination)), tool=tool),
              file=stream, flush=True)
        try:
            stream.fileno()
        except (AttributeError, OSError, ValueError):
            io_kwargs = {"stdin": subprocess.DEVNULL, "capture_output": True, "text": True}
            streamed = False
        else:
            io_kwargs = {"stdout": stream, "stderr": stream}
            streamed = True
    else:
        io_kwargs = {"stdin": subprocess.DEVNULL, "capture_output": True, "text": True}
        streamed = False
    try:
        done = runner(argv, env=env, timeout=timeout, **io_kwargs)
    except FileNotFoundError:
        raise CloneError(GH_MISSING if tool == "gh" else GIT_MISSING) from None
    except subprocess.TimeoutExpired:
        raise CloneError(CLONE_TIMED_OUT.format(source=safe_text(source.raw), tool=tool, seconds=timeout)) from None
    except OSError as exc:
        raise CloneError(CLONE_FAILED.format(source=safe_text(source.raw), tool=tool,
                                             detail=safe_text(exc.strerror or type(exc).__name__))) from None
    if done.returncode != 0:
        if streamed:
            hint = (" " + HINT_GH_FALLBACK) if tool == "gh" else ""
            raise CloneError(CLONE_FAILED_ABOVE.format(source=safe_text(source.raw), tool=tool,
                                                       code=done.returncode) + hint)
        detail = _error_detail(done.stderr, done.stdout, done.returncode)
        hint = _failure_hint(source, tool, detail, which=which)
        raise CloneError(CLONE_FAILED.format(source=safe_text(source.raw), tool=tool, detail=detail) + hint)
    info = {"source": source.raw, "url": source.git_url, "tool": tool, "destination": str(destination)}
    if chosen_branch is not None:
        info["branch"] = chosen_branch
    return info
