# Cairnmind pilot repository

A single task handed over between fresh sessions: Codex implements, Claude tests
and reviews, and another Codex restores the result from the shared memory.

The task definition and criteria are in `pilot-plan.json`. The `unit` profile is
in `.devbrain/checks.toml`. The implementation and tests will be created by the
first Codex session; the initial directory intentionally does not contain them.

Target API: `token_auth.validate_token(token) -> bool`. Only the string `valid`
returns `True`. Other strings and values of other types return `False`, without
raising an exception. This is a small pilot example used to check the
environment.

`tests/test_token_auth.py` must contain at least 12 unittest test methods: a
valid token, an empty string, a wrong token, different letter case, a leading
space, a trailing space, `None`, integers, bool, bytes, a list and a dict. Each
method must check the function's actual result. Exit code 0 from an empty test
suite does not meet the criteria; the reviewer reads the test code and the
stdout/stderr summary.

Reports, file copies and pilot notes go outside the repository, into the
`DEVBRAIN_PILOT_REPORT_DIR` directory. Changing the code, tests or profile after
a check means it must be verified again; the `done` state and the freshness of
the evidence are separate.
