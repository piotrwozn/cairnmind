"""The sentence for a service that refuses this data directory's token (#267, #291): the command line, the
terminal workspace and the MCP bridge say it from here. Neither the token nor anything of it is printed."""
from __future__ import annotations

PORT_ADVICE = "Give this data directory its own port: `port` in {}/config.json, or CAIRNMIND_PORT for one command."
# A template: the workspace translates it before the URL and the directory are put in (see broker_error_text).
FOREIGN_SERVICE = ("The service at {url} does not accept the token of {data_dir}, so it most likely belongs to "
                   "another data directory. " + PORT_ADVICE.format("{data_dir}"))


def refused_token(status: int | None, detail) -> bool:
    return status == 401 and "Invalid local service token" in str(detail)


def foreign_service(settings) -> dict[str, str]:
    return {"url": settings.url, "data_dir": str(settings.data_dir)}


def foreign_service_text(settings) -> str:
    return FOREIGN_SERVICE.format(**foreign_service(settings))
