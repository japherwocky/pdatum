# pdatum

Job postings, the employers behind them, federal awards to any company, BDC
lenders' books and the private companies they hold, and state WARN layoff
notices, from the command line. Built for scripts and AI agents: everything
prints JSON on request, errors go to stderr, and large pulls stream JSON lines.

pdatum is the data jobwolverine.com and rxraven.com are built on.

## Install

macOS, Linux, WSL:

```bash
curl -fsSL https://pdatum.pearachute.com/install.sh | sh
```

Windows PowerShell:

```powershell
irm https://pdatum.pearachute.com/install.ps1 | iex
```

Windows CMD:

```bat
curl -fsSL https://pdatum.pearachute.com/install.cmd -o install.cmd && install.cmd && del install.cmd
```

The installer uses uv or pipx if you have one, and installs uv (which brings
its own Python) if you don't, then puts `pdatum` on your PATH for new
terminals. `PDATUM_NO_MODIFY_PATH=1` leaves your PATH alone. Already have
Python? `pip install pdatum` works too.

## Use

```bash
pdatum key save pdatum_...    # keys are issued by hand for now

pdatum jobs count --q "data engineer" --posted-since 30d
pdatum jobs search --q nurse --remote
pdatum jobs pull --brand rxraven > jobs.jsonl
pdatum employers get pfizer --json
pdatum awards "Acme Corp"
```

## Commands

| Command | |
|---|---|
| `pdatum jobs count [filters]` | Returns how many open jobs match, and nothing else. It's cheap, so run it first. |
| `pdatum jobs search [filters] [-n N]` | Shows the newest matches: one page. |
| `pdatum jobs pull [filters] [--full] [--max N]` | Streams every match to stdout as JSON lines. Progress goes to stderr. |
| `pdatum jobs get ID` | Returns one job, open or closed, with its full text. |
| `pdatum jobs changes --since T` | Streams every job added, closed or reopened since `T`. |
| `pdatum employers list / pull / get SLUG` | Employer records. Each fact names its source. |
| `pdatum awards NAME... [--employer SLUG]` | Federal contracts, IDVs and grants to a company, from USAspending.gov: counts, amounts, agencies, and when the work ends. Any company, by its whole name. Loans are left out. |
| `pdatum bdc book TICKER` | A business development company's book: its status, version, tables, and the caveats to read first. |
| `pdatum bdc pull TICKER [--out DIR]` | Writes the whole book to a folder: `manifest.json` and one JSON-lines file per table, each checked against the manifest. |
| `pdatum bdc request TICKER` | Asks for a lender that has not been read yet. Reading one takes a while. |
| `pdatum companies resolve NAME` | Looks up which company a name belongs to: `matched`, `ambiguous` or `unknown`. Starts nothing. |
| `pdatum companies request NAME` | Gives the same answer for any name. For a name we don't hold, it searches EDGAR for the BDCs that name it and reads them. That takes a while. |
| `pdatum companies list / pull / get SLUG` | The registry: each company's names, and the lenders that hold it. |
| `pdatum warn states` | Lists every state, whether we read it or not: when it was last read, how many notices we hold, and why we don't read the rest. |
| `pdatum warn list / count / pull [filters]` | State WARN notices of layoffs and closures, newest notice first. |
| `pdatum warn changes --since T` | Streams every notice read for the first time, or newly linked to one of our records, since `T`. |
| `pdatum me` | Shows your key, its scopes, and what it has used. |
| `pdatum skill` | Prints short instructions for an AI agent to save. |
| `pdatum guide` | Prints the full API reference, from the server. |

The job filters are `--brand`, `--employer`, `-q`, `--location`,
`--remote/--not-remote`, `--posted-since` and `--posted-before`. A time can be
`2026-09-01`, an age like `7d`, `12h` or `30m`, or epoch seconds.

## BDC books

A book is what one business development company reports lending, read from
its SEC filings: its positions quarter by quarter, the companies behind them
under every name they are filed as, how every other lender we read marks the
same loans, what changed between filings, the lender's own reported totals,
and every figure a report would quote, already computed.

```bash
pdatum bdc book WHF                 # read the caveats before any number
pdatum bdc pull WHF --out whf/      # manifest.json + positions.jsonl, figures.jsonl, ...
```

The same version is the same data, so a harness can cache on it. Quote the
`figures` table rather than doing arithmetic on positions.

## Companies

A private borrower has no ticker. It's a line in its lenders' schedules,
spelled a little differently by each one. The registry joins those lines into
one company with one slug, and that slug is the `company` field in every book
table.

```bash
pdatum companies resolve "Acme Buyer, Inc."    # which company a name belongs to
pdatum companies request "Zeta Holdings, LLC"  # not held yet: search EDGAR and read its lenders
pdatum companies get acme-buyer-inc            # every name, and each lender that holds it
```

Matching ignores case and spacing, and nothing else. An unknown name comes
back with `similar` names, and none of them is a match until you decide it is.
Slugs stay valid: when two companies merge, the old slug answers with
`merged_into`, pointing at the company it became.

## WARN notices

An employer with 100 or more staff has to give 60 days' notice of a mass
layoff or a closure, and each state publishes the notices in its own format.
pdatum serves every notice from every state we read, in one shape.

```bash
pdatum warn states                            # read this first
pdatum warn list --state tx --notice-since 90d
pdatum warn pull --employer pfizer > warn.jsonl
```

Not every state is read, and a state we don't read isn't a state with no
layoffs. `pdatum warn states` says which states are read, when each was last
read, and why we don't read the rest. A notice links to a company and to an
employer record when the employer's name is theirs. That's a match on the
name, not proof that it's the same business.

The filters are `--state` (repeat it for more than one), `--company`,
`--employer`, `-q`, `--notice-since` and `--notice-before`.

## Keeping a copy in sync

```bash
pdatum jobs pull > jobs.jsonl                       # once
pdatum jobs changes --since 2026-09-01 > delta.jsonl
```

Upsert each line of `delta.jsonl` by `id`. A line with `"open": false` means
that job has closed. The last line on stderr gives the `--since` to use next
time. A change can arrive twice, but never not at all.

## Keys and configuration

The key is taken from `--api-key`, then `PDATUM_API_KEY`, then the config
file, which `pdatum key save <key>` writes after checking the key with the
server. The config file is `~/.pdatum.json`; `PDATUM_CONFIG_PATH` moves it.
`PDATUM_URL` or `pdatum config --url` points the CLI at another server.

Every command takes `--json`, or set `PDATUM_OUTPUT=json`. Errors go to
stderr, and the exit code is non-zero: 1 for a failure, 2 for a usage error.

## Development

```bash
pip install -e .
python -m unittest discover -s tests
```

The tests never reach a server. The API this talks to is documented at
https://pdatum.pearachute.com/api/v1/docs, and `pdatum guide` prints the same
reference from whichever server you point it at. MIT licensed.
