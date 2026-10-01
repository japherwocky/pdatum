# AGENTS.md

This is the file to edit. `CLAUDE.md` is a one-line shim that imports it;
opencode, Codex and Cursor never read `CLAUDE.md`.

## What this is

The `pdatum` command line client, published to PyPI. It was `cli/` in
[japherwocky/jobwolverine](https://github.com/japherwocky/jobwolverine) until
October 2026 and moved here so an open-source client has a public home for its
issues and its release provenance.

**The server is not here.** The keyed API (`/api/v1/`), its keys and usage
accounting live in jobwolverine: `api/pdatum.py`, `api/keyauth.py`,
`models/apikey.py`, and the contract itself, `docs/pdatum/api.md`, served at
`/api/v1/docs`. This client imports nothing from it and must keep working on
a machine that has never seen it. When the API changes, the change lands there
first and the matching client change lands here; `pdatum guide` prints
whatever contract the server in use is serving.

Work is tracked on the **pdatum** kanban board (`19`) at
https://pkanban.pearachute.com, not in this repo's issues alone; see
jobwolverine's AGENTS.md for how to drive `pkanban` safely on Windows.

## Develop

```bash
pip install -e .
python -m unittest discover -s tests
```

CI installs the package non-editable, on Python 3.9 and 3.12. 3.9 is the floor
`pyproject.toml` claims, so do not use syntax or stdlib newer than that.
**No test may reach a server**: `tests/test_pdatum.py` replaces the HTTP
session.

## Release

Bump the version in `pyproject.toml` **and** `pdatum/__init__.py`, then push a
tag `vX.Y.Z`. `publish-pypi.yml` refuses a tag that disagrees with either,
runs the tests, publishes, and cuts a GitHub release. PyPI versions can never
be reused, so check the number before tagging.

Publishing is trusted publishing: PyPI's publisher for `pdatum` names this
repository and `publish-pypi.yml`. Renaming either breaks releases.

## Conventions

`pkanban` (https://github.com/japherwocky/pkanban) is the model this CLI
copies. Fix a shortcoming there first, then port it. These are its fixes, and
all three have regressed before:

- Click's Windows `~`/glob expansion is off (`windows_expand_args=False`), so
  `~` and `*` in an argument arrive untouched.
- `--json` and `--api-key` are never taken from another option's value or
  from after `--`.
- A closed pipe (`| head`) exits quietly. On Windows it arrives as EINVAL,
  not EPIPE.

Streams (`pull`, `changes`) are JSON lines on stdout and progress goes to
stderr, so a pipe can be trusted to carry data only.
