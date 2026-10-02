"""
How a command reports: formatted for a person, or JSON for a script.

Copied in shape from pkanban. A command hands emit() the API's payload and a
callable that renders it for a person; the mode decides which is printed. In
JSON mode stdout carries only the result and errors go to stderr as JSON, so a
script can parse stdout without first working out whether it holds an error.

Streams (`jobs pull`, `jobs changes`, `employers pull`) are JSON lines in
either mode: one record per line on stdout, progress on stderr.
"""

import errno
import json
import os
import sys

from rich.console import Console
from rich.markup import escape

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
        # Escaped: a message that echoes user text ("[/x]") is not markup. And
        # soft-wrapped, or rich breaks a long line at the terminal width, in the
        # middle of a command someone means to copy.
        errors.print(f"[red]error:[/red] {esc(message)}", markup=True, soft_wrap=True)


def line(record):
    """One JSON-lines record on stdout."""
    sys.stdout.write(json.dumps(record, ensure_ascii=False) + "\n")


def progress(message):
    """A status line on stderr, where it never mixes with the data."""
    print(message, file=sys.stderr, flush=True)


def esc(value):
    """Text from the server or the user, made safe to put inside rich markup.

    rich reads square brackets as style tags: "Nurse [per diem]" would print as
    "Nurse ", and a title containing "[/x]" raises MarkupError and aborts the
    whole listing. Everything that did not come from this file's own source
    belongs inside esc() on its way into an f-string for console.print, and
    into any cell of a Table, which parses markup too.
    """
    return escape(str(value))


def reader_left(error):
    """
    Whether an OSError means whoever was reading our output has gone. POSIX says
    so with EPIPE; Windows says EINVAL when the far end of a pipe is closed.
    """
    if isinstance(error, BrokenPipeError):
        return True
    return os.name == "nt" and getattr(error, "errno", None) == errno.EINVAL


def _sever(stream):
    """
    Point a stream's file descriptor at nothing. After a failed write the stream
    still holds the bytes it could not send, and Python flushes it once more as
    it exits: the same error again, as an "Exception ignored" traceback, and the
    exit status replaced with 120.
    """
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, stream.fileno())
        finally:
            os.close(devnull)
    except (OSError, ValueError, AttributeError):
        pass


def _flush(stream):
    """Flush a stream. False when its reader has gone, which is then handled."""
    try:
        stream.flush()
        return True
    except OSError as error:
        if not reader_left(error):
            raise
        _sever(stream)
        return False
    except ValueError:  # already closed
        return True


def _status_of(exit_):
    code = exit_.code
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    print(code, file=sys.stderr)
    return 1


def run_quietly(run):
    """
    Run a command and return its exit status. A reader that left is not a crash.

    `pdatum jobs pull | head`, `... 2>&1 | head`, a terminal closed under a long
    listing: the reader goes, and the next write raises. Left alone that is a
    traceback, and 120 as the exit status. It is not a failure of ours, so it
    ends the command quietly.

    Which stream the reader was on decides the status. Output cut short on
    stdout is the reader's choice, so 0. If it was stderr, the command had
    something to report, so the status it was going to exit with stands: Click's
    own for a usage error, 1 for the rest. (Only stdout was handled before, so a
    usage error piped through `2>&1 | head` exited 120.)

    The flushes at the end matter as much as the except: a short output sits in
    the buffer until exit, so the error often arrives there, outside any
    handler, and can only be met by flushing here, deliberately.

    POSIX needs less of this. rich and Click already turn EPIPE into a quiet
    exit 1 before it reaches here. Windows reports a closed pipe as EINVAL,
    which neither knows, so there the same event was a traceback and 120. This
    decides the status where the libraries do not; it does not overrule them,
    so on POSIX the status stays whatever they chose.
    """
    status = 0
    try:
        run()
    except SystemExit as exit_:
        status = _status_of(exit_)
    except OSError as error:
        if not reader_left(error):
            raise
        if _flush(sys.stdout):
            # stdout is fine, so it was stderr that went, mid-report.
            context = error.__context__
            status = getattr(context, "exit_code", None) or 1
        else:
            status = 0

    _flush(sys.stdout)
    _flush(sys.stderr)
    return status
