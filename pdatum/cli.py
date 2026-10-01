"""
The `pdatum` command.

Built on pkanban's model (typer, rich, `--json` everywhere, errors on stderr),
with the fixes pkanban needed: Click's Windows expansion of `~` and wildcards
is off, `--json` and `--api-key` are only taken from where they can be ours
(never from another option's value, never after `--`), and long text never
has to pass through a shell.

Streams -- `jobs pull`, `jobs changes`, `employers pull` -- write JSON lines to
stdout and progress to stderr, in either mode, so they can be piped straight
into a file or another tool.
"""

import errno
import os
import sys
from typing import Optional

import typer
from rich.table import Table

from pdatum import __version__, config, timespec
from pdatum.client import Client, PdatumError
from pdatum.output import (
    configure_streams,
    console,
    emit,
    emit_error,
    line,
    progress,
    set_json_output,
)

app = typer.Typer(
    help="Job postings and the employers behind them, for scripts and agents. "
    "Start with: pdatum skill",
    no_args_is_help=True,
    add_completion=False,
)
jobs_app = typer.Typer(help="Job postings.", no_args_is_help=True)
employers_app = typer.Typer(help="Employer records, with facts and their sources.", no_args_is_help=True)
key_app = typer.Typer(help="Store or forget your API key.", no_args_is_help=True)
app.add_typer(jobs_app, name="jobs")
app.add_typer(employers_app, name="employers")
app.add_typer(key_app, name="key")


def _version(value: bool):
    if value:
        emit({"version": __version__}, lambda: console.print(f"pdatum {__version__}"))
        raise typer.Exit()


@app.callback()
def root(
    json_out: bool = typer.Option(
        False, "--json", help="Print results as JSON. Or set PDATUM_OUTPUT=json."
    ),
    api_key: Optional[str] = typer.Option(
        None, "--api-key", "-k", help="Use this key for this command only."
    ),
    version: bool = typer.Option(
        False, "--version", "-V", callback=_version, is_eager=True, help="Show the version."
    ),
):
    """pdatum"""
    # main() usually takes these off argv before typer sees them, so they work
    # after the subcommand too; declaring them here puts them in --help.
    if json_out:
        set_json_output(True)
    if api_key:
        config.set_runtime_key(api_key)


def client(need_key=True):
    key, _ = config.api_key()
    if need_key and not key:
        raise PdatumError(
            "No API key. Set PDATUM_API_KEY, pass --api-key, or run: pdatum key save <key>",
            code="missing_key",
        )
    return Client(config.base_url(), key)


def when(value, name):
    """A --posted-since style option, parsed, or a usage error that says why."""
    if value is None:
        return None
    try:
        return timespec.parse(value)
    except ValueError as e:
        raise typer.BadParameter(str(e), param_hint=name)


# -- shared options -------------------------------------------------------------

BRAND = typer.Option(None, "--brand", help="jobwolverine or rxraven. Both if omitted.")
EMPLOYER = typer.Option(None, "--employer", help="An employer slug (see: pdatum employers list).")
QUERY = typer.Option(None, "--q", "-q", help="Text in the position or company.")
LOCATION = typer.Option(None, "--location", help="Text in the location.")
REMOTE = typer.Option(None, "--remote/--not-remote", help="Only remote jobs, or only not.")
SINCE = typer.Option(None, "--posted-since", help="2026-09-01, 7d, or epoch seconds.")
BEFORE = typer.Option(None, "--posted-before", help="2026-09-01, 7d, or epoch seconds.")


def job_filters(brand, employer, q, location, remote, posted_since, posted_before):
    return dict(
        brand=brand,
        employer=employer,
        q=q,
        location=location,
        remote=remote,
        posted_since=when(posted_since, "--posted-since"),
        posted_before=when(posted_before, "--posted-before"),
    )


def fraction(shown, total):
    """Coverage first, the way #430 asks: '20 of 1,234'."""
    return f"{shown:,} of {total:,}" if total is not None else f"{shown:,}"


# -- jobs -----------------------------------------------------------------------


@jobs_app.command("count")
def jobs_count(
    brand: Optional[str] = BRAND, employer: Optional[str] = EMPLOYER,
    q: Optional[str] = QUERY, location: Optional[str] = LOCATION,
    remote: Optional[bool] = REMOTE, posted_since: Optional[str] = SINCE,
    posted_before: Optional[str] = BEFORE,
):
    """How many open jobs match. Cheap: size a query before pulling it."""
    body = client().get(
        "/jobs/count", **job_filters(brand, employer, q, location, remote, posted_since, posted_before)
    )
    emit(body["data"], lambda: console.print(f"{body['data']['count']:,}"))


def job_table(jobs):
    table = Table(show_edge=False, pad_edge=False)
    for column in ("id", "posted", "company", "position", "location"):
        table.add_column(column, overflow="fold")
    for j in jobs:
        table.add_row(
            str(j["id"]),
            timespec.show(j["posted_at"] or j["first_seen_at"])[:10],
            j["company"] or "", j["position"] or "",
            (j["location"] or "") + (" (remote)" if j["remote"] else ""),
        )
    return table


@jobs_app.command("search")
def jobs_search(
    brand: Optional[str] = BRAND, employer: Optional[str] = EMPLOYER,
    q: Optional[str] = QUERY, location: Optional[str] = LOCATION,
    remote: Optional[bool] = REMOTE, posted_since: Optional[str] = SINCE,
    posted_before: Optional[str] = BEFORE,
    limit: int = typer.Option(20, "--limit", "-n", min=1, max=500, help="How many to show."),
):
    """The newest open jobs that match: one page. For all of them, use pull."""
    body = client().get(
        "/jobs", limit=limit,
        **job_filters(brand, employer, q, location, remote, posted_since, posted_before),
    )

    def render():
        console.print(job_table(body["data"]))
        more = " -- pdatum jobs pull for all of them" if body["next_cursor"] else ""
        console.print(f"[dim]{fraction(len(body['data']), body['total'])}{more}[/dim]")

    emit(body, render)


@jobs_app.command("get")
def jobs_get(job_id: int = typer.Argument(..., help="The job's id.")):
    """One job, open or closed, with its full description."""
    job = client().get(f"/jobs/{job_id}")["data"]

    def render():
        state = "open" if job["open"] else "[red]closed[/red]"
        console.print(f"[bold]{job['position']}[/bold] -- {job['company'] or '?'} ({state})")
        console.print(
            f"{job['location'] or 'no location'}{' - remote' if job['remote'] else ''}  |  "
            f"posted {timespec.show(job['posted_at'])}  |  "
            f"first seen {timespec.show(job['first_seen_at'])}"
        )
        if job["salary_min"] or job["salary_max"]:
            console.print(f"salary {job['salary_min'] or '?'}-{job['salary_max'] or '?'} "
                          f"{job['salary_currency']}")
        console.print(f"apply: {job['application_url']}")
        console.print()
        console.print(job["description"], markup=False)

    emit(job, render)


def pull_progress(noun):
    def report(received, total):
        progress(f"pulled {fraction(received, total)} {noun}")
    return report


@jobs_app.command("pull")
def jobs_pull(
    brand: Optional[str] = BRAND, employer: Optional[str] = EMPLOYER,
    q: Optional[str] = QUERY, location: Optional[str] = LOCATION,
    remote: Optional[bool] = REMOTE, posted_since: Optional[str] = SINCE,
    posted_before: Optional[str] = BEFORE,
    full: bool = typer.Option(False, "--full", help="Whole descriptions, not excerpts."),
    max_records: Optional[int] = typer.Option(None, "--max", min=1, help="Stop after this many."),
):
    """Every open job that matches, as JSON lines on stdout."""
    filters = job_filters(brand, employer, q, location, remote, posted_since, posted_before)
    written = 0
    for job in client().pages(
        "/jobs", on_page=pull_progress("jobs"),
        detail="full" if full else "summary", limit=100 if full else 500, **filters,
    ):
        line(job)
        written += 1
        if max_records and written >= max_records:
            break


@jobs_app.command("changes")
def jobs_changes(
    since: str = typer.Option(..., "--since", help="2026-09-01, 7d, or epoch seconds."),
    brand: Optional[str] = BRAND,
    full: bool = typer.Option(False, "--full", help="Whole descriptions, not excerpts."),
):
    """
    Every job added, closed or reopened since then, as JSON lines. Upsert by
    id; closed jobs arrive with "open": false. The last line on stderr says
    what --since to use next time.
    """
    start = when(since, "--since")
    latest = None
    for job in client().pages(
        "/jobs/changes", on_page=pull_progress("changes"),
        since=start, brand=brand, detail="full" if full else "summary",
        limit=100 if full else 500,
    ):
        line(job)
        latest = job["changed_at"]
    # Minus one: times are whole seconds, so re-reading the last second is
    # what guarantees nothing that landed in it is missed. See the API docs.
    resume = (latest - 1) if latest is not None else start
    progress(f"next time: pdatum jobs changes --since {resume}")


# -- employers --------------------------------------------------------------------


EMPLOYER_Q = typer.Option(None, "--q", "-q", help="Text in the name.")
DOMAIN = typer.Option(None, "--domain", help="An exact domain, e.g. pfizer.com.")
EMPLOYER_BRAND = typer.Option(None, "--brand", help="Only employers with open jobs there.")


def hiring_word(value):
    return {True: "yes", False: "no", None: "unknown"}[value]


@employers_app.command("list")
def employers_list(
    q: Optional[str] = EMPLOYER_Q, domain: Optional[str] = DOMAIN,
    brand: Optional[str] = EMPLOYER_BRAND,
    limit: int = typer.Option(50, "--limit", "-n", min=1, max=500),
):
    """Employer records: one page. For all of them, use pull."""
    body = client().get("/employers", q=q, domain=domain, brand=brand, limit=limit)

    def render():
        table = Table(show_edge=False, pad_edge=False)
        for column in ("slug", "name", "domain", "open jobs", "hiring"):
            table.add_column(column, overflow="fold")
        for e in body["data"]:
            table.add_row(
                e["slug"], e["name"], e["domain"] or "",
                str(sum(e["open_postings"].values())), hiring_word(e["hiring"]),
            )
        console.print(table)
        console.print(f"[dim]{fraction(len(body['data']), body['total'])}[/dim]")

    emit(body, render)


@employers_app.command("pull")
def employers_pull(
    q: Optional[str] = EMPLOYER_Q, domain: Optional[str] = DOMAIN,
    brand: Optional[str] = EMPLOYER_BRAND,
):
    """Every employer record that matches, as JSON lines on stdout."""
    for employer in client().pages(
        "/employers", on_page=pull_progress("employers"),
        q=q, domain=domain, brand=brand, limit=500,
    ):
        line(employer)


@employers_app.command("get")
def employers_get(slug: str = typer.Argument(..., help="The employer's slug.")):
    """One employer: open jobs per brand, every fact with its source, and history."""
    e = client().get(f"/employers/{slug}")["data"]

    def render():
        console.print(f"[bold]{e['name']}[/bold] ({e['slug']})  {e['domain'] or ''}")
        postings = ", ".join(f"{b} {n}" for b, n in e["open_postings"].items())
        console.print(f"open jobs: {postings}  |  hiring: {hiring_word(e['hiring'])}")
        if e["facts"]:
            table = Table(show_edge=False, pad_edge=False, title="facts")
            for column in ("key", "value", "source", "observed"):
                table.add_column(column, overflow="fold")
            for f in e["facts"]:
                table.add_row(f["key"], str(f["value"]), f["source"],
                              timespec.show(f["observed_at"]))
            console.print(table)
        else:
            console.print("[dim]no facts recorded[/dim]")

    emit(e, render)


# -- the key, the server, and you ------------------------------------------------


@app.command("me")
def me():
    """The key you are using, its scopes, and what it has used."""
    data = client().get("/me")["data"]

    def render():
        _, source = config.api_key()
        console.print(f"[bold]{data['name']}[/bold] -- {data['holder']}")
        console.print(f"key {data['prefix']}... from {source}; scopes: {' '.join(data['scopes'])}")
        console.print(f"expires {timespec.show(data['expires_at'])}")
        for period, used in data["usage"].items():
            console.print(f"{period.replace('_', ' ')}: {used['requests']:,} requests, "
                          f"{used['records']:,} records")

    emit(data, render)


@key_app.command("save")
def key_save(key: str = typer.Argument(..., help="The key you were issued.")):
    """Check a key against the server, then save it to the config file."""
    data = Client(config.base_url(), key).get("/me")["data"]
    config.save_key(key)
    emit(
        {"saved": str(config.config_path()), "key": data},
        lambda: console.print(
            f"Saved {data['prefix']}... ({data['name']}) to {config.config_path()}"
        ),
    )


@key_app.command("clear")
def key_clear():
    """Forget the saved key. It still works until it is revoked."""
    had = config.clear_key()
    emit({"cleared": had}, lambda: console.print(
        f"Cleared the key from {config.config_path()}" if had else "No key was saved."
    ))


@app.command("config")
def cmd_config(
    url: Optional[str] = typer.Option(None, "--url", help="Point at another server."),
):
    """Show, or set, which server this talks to and where the key comes from."""
    if url:
        config.set_base_url(url)
    key, source = config.api_key()
    data = {
        "url": config.base_url(),
        "config": str(config.config_path()),
        "key": config.display_key(key),
        "key_from": source,
    }
    emit(data, lambda: [console.print(f"{k}: {v}") for k, v in data.items()])


SKILL = """\
pdatum -- job postings and the employers behind them, for agents.

Auth: the key comes from PDATUM_API_KEY. Do not write it into files.
Check it: pdatum me

Size a query before pulling it (cheap, returns only a number):
  pdatum jobs count --q "data engineer" --brand jobwolverine --posted-since 30d

Look at a page:
  pdatum jobs search --q nurse --remote --json

Pull everything that matches, as JSON lines (progress goes to stderr):
  pdatum jobs pull --q nurse --brand rxraven > jobs.jsonl

One job in full:          pdatum jobs get 48213 --json
Employers and their facts: pdatum employers list --q pfizer --json
                           pdatum employers get pfizer --json
Every fact names its source. hiring is true, false, or null (unknown).

Stay in sync: pdatum jobs changes --since 2026-09-01 > delta.jsonl
  Upsert each line by id; "open": false means the job closed.
  The last stderr line gives the --since to use next time.

Times take 2026-09-01, 7d / 12h / 30m, or epoch seconds.
Every command takes --json; errors go to stderr, exit code non-zero.
The full API reference: pdatum guide
"""


@app.command("skill")
def skill():
    """Short instructions for an AI agent. Save them where your agent reads them."""
    emit({"skill": SKILL}, lambda: print(SKILL, end=""))


@app.command("guide")
def guide():
    """The full API reference, fetched from the server so it is never stale."""
    text = client(need_key=False).text("/docs")
    emit({"guide": text}, lambda: print(text))


# -- entry point ----------------------------------------------------------------

# Options that take no value, so a flag right after one is still ours.
VALUELESS = frozenset({
    "--json", "--version", "-V", "--help", "--full", "--remote", "--not-remote",
})


def _is_ours(argv, i):
    previous = argv[i - 1]
    return not (previous.startswith("-") and previous not in VALUELESS)


def _options_end(argv):
    return argv.index("--") if "--" in argv else len(argv)


def extract_json_flag(argv):
    """
    Take --json off argv wherever it can be ours, so it works after the
    subcommand too. Not when it is another option's value (`--q --json`),
    and nothing after `--`.
    """
    found = False
    for i in range(_options_end(argv) - 1, 0, -1):
        if argv[i] == "--json" and _is_ours(argv, i):
            argv.pop(i)
            found = True
    return found


def extract_api_key(argv):
    """Take --api-key KEY / -k KEY / --api-key=KEY off argv, by the same rules."""
    for i in range(1, _options_end(argv)):
        token = argv[i]
        if token.startswith("--api-key=") and _is_ours(argv, i):
            argv.pop(i)
            return token.split("=", 1)[1]
        if token in ("--api-key", "-k") and _is_ours(argv, i):
            if i + 1 >= len(argv):
                emit_error(f"{token} needs a value.")
                raise SystemExit(2)
            argv.pop(i)
            return argv.pop(i)
    return None


def main(argv=None):
    configure_streams()
    argv = sys.argv if argv is None else argv

    if extract_json_flag(argv):
        set_json_output(True)
    key = extract_api_key(argv)
    if key is not None:
        config.set_runtime_key(key)

    try:
        # Click expands ~ and wildcards in every argument on Windows, standing
        # in for a shell that does not: "~4,100" becomes a home directory and
        # "*.py" a list of files. Nothing here is a path pattern.
        app(args=argv[1:], prog_name="pdatum", windows_expand_args=False)
    except PdatumError as e:
        extra = {k: v for k, v in (("code", e.code), ("status", e.status)) if v is not None}
        emit_error(e.message, **extra)
        raise SystemExit(1)
    except KeyboardInterrupt:
        raise SystemExit(130)
    except OSError as e:
        if not _reader_left(e):
            raise
        # `pdatum jobs pull | head`: the reader left, which is not an error.
        # Point stdout at nothing, or Python's own flush at exit raises the
        # same error again and prints it.
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except (OSError, ValueError):
            pass
        raise SystemExit(0)


def _reader_left(error):
    """
    Whether an OSError means whoever was reading stdout has gone. POSIX says
    so with EPIPE; Windows says EINVAL when the pipe's far end is closed.
    """
    if isinstance(error, BrokenPipeError):
        return True
    return os.name == "nt" and error.errno == errno.EINVAL


if __name__ == "__main__":
    main()
