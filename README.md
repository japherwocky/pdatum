# pdatum

Job postings, and the employers behind them, from the command line. Built for
scripts and AI agents: everything prints JSON on request, errors go to
stderr, and large pulls stream JSON lines.

pdatum is the data jobwolverine.com and rxraven.com are built on.

```bash
pip install pdatum
export PDATUM_API_KEY=pdatum_...    # keys are issued by hand for now

pdatum jobs count --q "data engineer" --posted-since 30d
pdatum jobs search --q nurse --remote
pdatum jobs pull --brand rxraven > jobs.jsonl
pdatum employers get pfizer --json
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
| `pdatum me` | Shows your key, its scopes, and what it has used. |
| `pdatum skill` | Prints short instructions for an AI agent to save. |
| `pdatum guide` | Prints the full API reference, from the server. |

The job filters are `--brand`, `--employer`, `-q`, `--location`,
`--remote/--not-remote`, `--posted-since` and `--posted-before`. A time can be
`2026-09-01`, an age like `7d`, `12h` or `30m`, or epoch seconds.

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
