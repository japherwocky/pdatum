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

import json
import os
import sys
from typing import List, Optional

import typer
from rich.table import Table

from pdatum import __version__, config, timespec
from pdatum.client import Client, PdatumError
from pdatum.output import (
    configure_streams,
    console,
    emit,
    emit_error,
    esc,
    line,
    progress,
    run_quietly,
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
bdc_app = typer.Typer(
    help="BDC books: what a lender reports lending, read from its SEC filings, by ticker.",
    no_args_is_help=True,
)
app.add_typer(jobs_app, name="jobs")
app.add_typer(employers_app, name="employers")
app.add_typer(bdc_app, name="bdc")
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


def fact_value(value):
    """A fact's value for a person: text as it is, anything structured as JSON.

    str() of a list or dict is a Python repr (single quotes, True, None), which
    is neither what the API returned nor something to paste into jq.
    """
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def job_table(jobs):
    table = Table(show_edge=False, pad_edge=False)
    for column in ("id", "posted", "company", "position", "location"):
        table.add_column(column, overflow="fold")
    for j in jobs:
        table.add_row(
            str(j["id"]),
            timespec.show(j["posted_at"] or j["first_seen_at"])[:10],
            esc(j["company"] or ""), esc(j["position"] or ""),
            esc((j["location"] or "") + (" (remote)" if j["remote"] else "")),
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
        console.print(f"[bold]{esc(job['position'])}[/bold] -- {esc(job['company'] or '?')} ({state})")
        console.print(
            f"{esc(job['location'] or 'no location')}{' - remote' if job['remote'] else ''}  |  "
            f"posted {timespec.show(job['posted_at'])}  |  "
            f"first seen {timespec.show(job['first_seen_at'])}"
        )
        if job["salary_min"] or job["salary_max"]:
            console.print(f"salary {job['salary_min'] or '?'}-{job['salary_max'] or '?'} "
                          f"{esc(job['salary_currency'])}")
        console.print(f"apply: {esc(job['application_url'])}")
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
                esc(e["slug"]), esc(e["name"]), esc(e["domain"] or ""),
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
        console.print(f"[bold]{esc(e['name'])}[/bold] ({esc(e['slug'])})  {esc(e['domain'] or '')}")
        postings = ", ".join(f"{b} {n}" for b, n in e["open_postings"].items())
        console.print(f"open jobs: {esc(postings)}  |  hiring: {hiring_word(e['hiring'])}")
        if e["facts"]:
            table = Table(show_edge=False, pad_edge=False, title="facts")
            for column in ("key", "value", "source", "observed"):
                table.add_column(column, overflow="fold")
            for f in e["facts"]:
                table.add_row(esc(f["key"]), esc(fact_value(f["value"])), esc(f["source"]),
                              timespec.show(f["observed_at"]))
            console.print(table)
        else:
            console.print("[dim]no facts recorded[/dim]")

    emit(e, render)


# -- federal awards ------------------------------------------------------------------

AWARD_TYPES = (("contracts", "contracts"), ("idvs", "IDVs"), ("grants", "grants"))


def day(epoch):
    """An epoch as a UTC date; '-' for none."""
    return timespec.show(epoch)[:10] if epoch is not None else "-"


def money(amount):
    # An IDV has no amount of its own: null, shown as a dash, never $0.
    return "-" if amount is None else f"${amount:,.0f}"


@app.command("awards")
def awards(
    names: Optional[List[str]] = typer.Argument(
        None, help="The company's name. More than one for other names it goes by, up to 5."),
    employer: Optional[str] = EMPLOYER,
):
    """Federal contracts, IDVs and grants to a company, from USAspending.gov."""
    if not names and not employer:
        raise typer.BadParameter("give a company name, or --employer SLUG", param_hint="NAME")
    data = client().get("/awards", name=list(names) if names else None, employer=employer)["data"]

    def render():
        if not data["found"]:
            console.print(f"no federal awards to a recipient named {esc(' / '.join(data['names']))}")
        for r in data["recipients"]:
            flag = "  [yellow]one-word name: confirm it is this company[/yellow]" if r["one_word_name"] else ""
            console.print(f"[bold]{esc(r['name'])}[/bold]{flag}")
            table = Table(show_edge=False, pad_edge=False)
            for column in ("type", "awards", "obligated", "latest end", "agencies"):
                table.add_column(column, overflow="fold")
            for key, label in AWARD_TYPES:
                g = r.get(key)
                if g:
                    table.add_row(label, f"{g['count']:,}" + ("+" if g["capped"] else ""),
                                  money(g["amount"]), day(g["latest_period_end"]),
                                  esc(", ".join(g["agencies"])))
            console.print(table)
            if any(r.get(k, {}).get("capped") for k, _ in AWARD_TYPES):
                console.print("[dim]+ more awards than were read: counts and amounts are the largest only[/dim]")
        if data["loans_only"]:
            console.print(f"[dim]loans only, not counted: {esc(', '.join(data['loans_only']))}[/dim]")
        console.print(f"[dim]{esc(data['source'])}, fetched {timespec.show(data['observed_at'])} UTC[/dim]")

    emit(data, render)


# -- BDC books ----------------------------------------------------------------------

TICKER = typer.Argument(..., help="The lender's ticker, e.g. WHF, or its CIK.")


def book_path(ticker, table=None):
    from urllib.parse import quote

    path = "/bdc/books/" + quote(ticker.strip().upper(), safe="")
    return path + "/" + table if table else path


@bdc_app.command("request")
def bdc_request(ticker: str = TICKER):
    """Ask for a lender's book. A lender not read before takes a while; follow it with: pdatum bdc book."""
    data = client().post("/bdc/books", {"ticker": ticker})["data"]
    emit(data, lambda: console.print(
        f"{esc(data['ticker'])}: {esc(data['status'])}. "
        f"Follow it with: pdatum bdc book {esc(data['ticker'])}"
    ))


def show_book(data):
    if "version" not in data:
        console.print(f"{esc(data['ticker'])}: {esc(data['status'])}, not read yet")
        return
    as_of = timespec.show(data["as_of"])[:10] if data["as_of"] else "-"
    console.print(f"[bold]{esc(data['name'])}[/bold] ({esc(data['ticker'])})  "
                  f"{esc(data['status'])}  version {esc(data['version'])}")
    console.print(f"{data['quarters']} quarters to {as_of}; "
                  f"recorded {timespec.show(data['recorded_at'])}")
    table = Table(show_edge=False, pad_edge=False)
    for column in ("table", "rows"):
        table.add_column(column)
    for name, meta in data["tables"].items():
        table.add_row(esc(name), f"{meta['rows']:,}")
    console.print(table)
    for caveat in data.get("caveats", []):
        console.print(f"- {esc(caveat)}", soft_wrap=True)
    if data.get("request"):
        console.print(f"[dim]a newer read is {esc(data['request']['status'])}[/dim]")


@bdc_app.command("book")
def bdc_book(
    ticker: str = TICKER,
    recorded_at: Optional[str] = typer.Option(
        None, "--recorded-at", help="The book as we held it then: 2026-09-01, 7d, or epoch seconds."),
):
    """A lender's book: its status, version, tables, and the caveats to read first."""
    data = client().get(book_path(ticker), recorded_at=when(recorded_at, "--recorded-at"))["data"]
    emit(data, lambda: show_book(data))


@bdc_app.command("pull")
def bdc_pull(
    ticker: str = TICKER,
    out: Optional[str] = typer.Option(
        None, "--out", "-o", help="The folder to write to; TICKER-VERSION if omitted."),
    version: Optional[str] = typer.Option(
        None, "--book-version", help="Refuse unless the book is at this version (from: pdatum bdc book)."),
):
    """
    Write a lender's whole book to a folder: manifest.json and one JSON-lines
    file per table, each checked against the manifest. Progress goes to stderr.
    """
    c = client()
    data = c.get(book_path(ticker))["data"]
    if "version" not in data:
        raise PdatumError(f"{data['ticker']} has no book yet: it is {data['status']}. "
                          f"Follow it with: pdatum bdc book {data['ticker']}")
    if version and version != data["version"]:
        raise PdatumError(f"{data['ticker']}'s book is at version {data['version']}, not {version}. "
                          "Pull it without --book-version for the current one.")
    folder = out or f"{data['ticker'].lower()}-{data['version']}"
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
    written = {}
    for name, meta in data["tables"].items():
        digest, rows, _ = c.download(book_path(data["ticker"], name),
                                     os.path.join(folder, name + ".jsonl"),
                                     version=data["version"])
        if digest != meta["sha256"]:
            raise PdatumError(f"{name} arrived different from what the manifest promises "
                              f"(sha256 {digest[:12]}, expected {meta['sha256'][:12]}). Pull again.")
        written[name] = rows
        progress(f"{name}: {rows:,} rows")
    result = {"folder": folder, "ticker": data["ticker"], "version": data["version"],
              "tables": written}
    emit(result, lambda: console.print(folder, markup=False, soft_wrap=True))


# -- the key, the server, and you ------------------------------------------------


@app.command("me")
def me():
    """The key you are using, its scopes, and what it has used."""
    data = client().get("/me")["data"]

    def render():
        _, source = config.api_key()
        console.print(f"[bold]{esc(data['name'])}[/bold] -- {esc(data['holder'])}")
        console.print(f"key {esc(data['prefix'])}... from {esc(source)}; scopes: {esc(' '.join(data['scopes']))}")
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
            f"Saved {esc(data['prefix'])}... ({esc(data['name'])}) to {esc(config.config_path())}"
        ),
    )


@key_app.command("clear")
def key_clear():
    """Forget the saved key. It still works until it is revoked."""
    had = config.clear_key()
    emit({"cleared": had}, lambda: console.print(
        f"Cleared the key from {esc(config.config_path())}" if had else "No key was saved."
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
    emit(data, lambda: [console.print(f"{k}: {esc(v)}") for k, v in data.items()])


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

Federal awards -- contracts, IDVs and grants, from USAspending, for any company by name:
  pdatum awards "Acme Corp" --json       more names for one company: pdatum awards "Acme" "Acme Corp"
  pdatum awards --employer acme          an employer's name and aliases
  found false means USAspending has none; a one_word_name match needs confirming.
  Loans are left out (loans_only lists recipients that hold only loans).

BDC books -- what a lender reports lending, from its SEC filings, by ticker:
  pdatum bdc book WHF            status, version, tables, and caveats: read those first
  pdatum bdc pull WHF --out whf  manifest.json + one JSON-lines file per table
  pdatum bdc request SCM         a lender not read yet; reading one takes a while
Quote the figures table (key, value, unit, definition) rather than doing
arithmetic on positions. The same version is the same data; cache on it.

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

# Options that take no value, so a flag right after one is still ours. These
# are the global ones; every command's own flags are read off the command tree
# by _valueless(). A hand-kept list is how pkanban 0.7.0 shipped with
# `login --no-wait --json` failing "No such option: --json": the new flag
# wasn't on it, so --json was taken for its value (pkanban PR #98).
VALUELESS = frozenset({"--json", "--version", "-V", "--help"})

_valueless_cache = None


def _valueless():
    global _valueless_cache
    if _valueless_cache is None:
        from typer.main import get_command

        # Duck-typed, not isinstance(click.Option): newer typer builds on its
        # own vendored copy of click, which the click package doesn't know.
        flags = set(VALUELESS)
        pending = [get_command(app)]
        while pending:
            command = pending.pop()
            for param in command.params:
                if getattr(param, "is_flag", False) or getattr(param, "count", False):
                    flags.update(param.opts)
                    flags.update(param.secondary_opts)
            pending.extend(getattr(command, "commands", {}).values())
        _valueless_cache = frozenset(flags)
    return _valueless_cache


def _is_ours(argv, i):
    previous = argv[i - 1]
    return not (previous.startswith("-") and previous not in _valueless())


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

    status = run_quietly(lambda: _run(argv))
    if status:
        raise SystemExit(status)


def _run(argv):
    """Run the command line, turning the CLI's own failures into an exit status."""
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


if __name__ == "__main__":
    main()
