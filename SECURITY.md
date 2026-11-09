# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 0.8.x | Yes |
| 0.7.x | Security fixes only |
| Older | No |

## Reporting a vulnerability

Please report vulnerabilities privately through [GitHub security advisories](https://github.com/piotrwozn/cairnmind/security/advisories/new). Do not open a public issue.

Include the affected version, the steps to reproduce and the impact you observed. You can expect an acknowledgement within one week.

## Scope notes

Cairnmind runs locally and keeps sensitive material on your machine. Reports are especially welcome for:

- ways a model or an archived source could write to memory, run a command or bypass a resume acknowledgement without the user's consent;
- prompt injection from indexed files or transcripts that changes stored decisions;
- leaks of API keys, tokens or conversation content into logs, snapshots or exports;
- access to the local service or broker from outside the loopback interface.

Portable memory snapshots in `.memory/devbrain/` can contain full conversation text. Review them before committing them to a shared repository.
