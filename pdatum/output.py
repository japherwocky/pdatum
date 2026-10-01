"""
How a command reports: formatted for a person, or JSON for a script.

Copied in shape from pkanban. A command hands emit() the API's payload and a
callable that renders it for a person; the mode decides which is printed. In
JSON mode stdout carries only the result and errors go to stderr as JSON, so a
script can parse stdout without first working out whether it holds an error.

Streams (`jobs pull`, `jobs changes`, `employers pull`) are JSON lines in
either mode: one record per line on stdout, progress on stderr.
"""

import json
import os
import sys

from rich.console import Console

_json_output = None

console = Console(highlight=False)
errors = Console(stderr=True, highlight=False)


def configure_streams():
    """
    UTF-8 on stdout and stderr. On Windows they default to the console code
    page, and one company name with an accent in it would otherwise end a
    listing with a UnicodeEncodeError. An explicit PYTHONIOENCODING is left
    alone.
    """
    chosen = bool(os.environ.get("PYTHONIOENCODING", "").strip())
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace") if chosen else reconfigure(
                encoding="utf-8", errors="replace"
            )
        except (ValueError, OSError):
            pass


def set_json_output(enabled):
    global _json_output
    _json_output = bool(enabled)


def json_output():
    if _json_output is not None:
        return _json_output
    return os.environ.get("PDATUM_OUTPUT", "").strip().lower() == "json"


def emit(payload, render):
    if json_output():
        # Plain print: rich reflows long lines and reads [brackets] as markup,
        # and either would corrupt JSON.
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        render()


def emit_error(message, **extra):
    if json_output():
        print(json.dumps({"error": message, **extra}), file=sys.stderr)
    else:
        errors.print(f"[red]error:[/red] {message}", markup=True)


def line(record):
    """One JSON-lines record on stdout."""
    sys.stdout.write(json.dumps(record, ensure_ascii=False) + "\n")


def progress(message):
    """A status line on stderr, where it never mixes with the data."""
    print(message, file=sys.stderr, flush=True)
