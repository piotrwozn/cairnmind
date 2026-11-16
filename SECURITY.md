# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 0.9.x | Yes |
| 0.8.x | Security fixes only |
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

## Database access

The managed local engine listens on loopback and Cairnmind signs in to it as `root`, with a password generated at installation and kept in `config.json` (mode `0600`) in the data directory. An installation from an earlier version keeps working this way; `cairnmind doctor` says where the password comes from.

A database on another host must be reached with `https://`; the certificate and the host name are always verified, against a private authority named by `arcade_ca_file` (an absolute path to a PEM file) or else the system's authorities; as everywhere OpenSSL is used, the variables `SSL_CERT_FILE` and `SSL_CERT_DIR` replace the system's authorities when they are set. Cairnmind never starts a server for such an address and never writes its password into the data directory; it requires the same pinned ArcadeDB release there as on this machine. Such an installation is connect-only, and so is one whose address is named by `arcade_connect_url`, also on this machine: it never installs, starts or stops an engine, never creates a database and gets no generated password ([docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#connect-only-installations)). One installation writes a database and any number read it; the rights of the database user are the same for both, so a reader is kept from writing by Cairnmind, not by the server. Cairnmind refuses plain `http://`, the user `root` and a password from `config.json` for such a host, and says which rule was broken. Create a user with rights on the Cairnmind database only, once, with `cairnmind db create-user --admin-password-file FILE`, which signs in as `root` for that step alone; each database gets its own user, and a user of another database is refused, never given rights on a second one. After several failed sign-ins ArcadeDB refuses a user for about 30 seconds, even with the right password, so a refused password that worked before can be this lockout: wait and try again. Give the user's password through the variable `CAIRNMIND_ARCADE_PASSWORD` or a file named by `arcade_password_file` (a regular file of your user with mode `0600`, not a link; the one exception is a Kubernetes Secret mounted as a volume, whose links are followed only in the kubelet's exact layout, to a file of your user that group and others cannot read: see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#kubernetes)). Such a password is never written to `config.json`, and it is masked in the agent journal and in database error messages.

Portable memory snapshots in `.memory/cairnmind/` (and `.memory/devbrain/`, written by versions up to 0.9.0) can contain full conversation text. Review them before committing them to a shared repository.
