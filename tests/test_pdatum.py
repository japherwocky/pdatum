"""
The pdatum CLI, with the HTTP session replaced. Nothing here reaches a server.

Run from the repository root:  python -m unittest discover -s tests
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pdatum import cli, config, timespec  # noqa: E402
from pdatum.client import MAX_RETRIES, Client, PdatumError  # noqa: E402
from pdatum.output import reader_left, run_quietly  # noqa: E402

KEY = "pdatum_" + "k" * 32


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None, text=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(body)

    def json(self):
        if self._body is None:
            raise ValueError("no JSON")
        return self._body

    def iter_content(self, chunk_size=1):
        data = self.text.encode("utf-8")
        for start in range(0, len(data), 7):  # small chunks, so a line can span two
            yield data[start:start + 7]


class FakeSession:
    """Answers from a function of (path, params); records every call."""

    def __init__(self, answer):
        self.answer = answer
        self.headers = {}
        self.calls = []
        self.posted = []

    def get(self, url, params=None, timeout=None, stream=False):
        path = url.split("/api/v1", 1)[1]
        self.calls.append((path, dict(params or {})))
        return self.answer(path, dict(params or {}))

    def post(self, url, params=None, json=None, timeout=None):
        path = url.split("/api/v1", 1)[1]
        self.calls.append((path, dict(params or {})))
        self.posted.append(json)
        return self.answer(path, dict(params or {}))


def pages_of(records, size):
    """An answer() serving `records` as a paged list, cursor = next offset."""

    def answer(path, params):
        start = int(params.get("cursor") or 0)
        chunk = records[start:start + size]
        more = start + size < len(records)
        return FakeResponse(body={
            "data": chunk, "next_cursor": str(start + size) if more else None,
            "total": len(records),
        })

    return answer


class Isolated(unittest.TestCase):
    """A scratch config file, no key in the environment, no runtime key."""

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "PDATUM_CONFIG_PATH": os.path.join(self.scratch.name, "config.json"),
        })
        self.env.start()
        for name in ("PDATUM_API_KEY", "PDATUM_URL", "PDATUM_OUTPUT"):
            os.environ.pop(name, None)
        config.set_runtime_key(None)
        cli.set_json_output(False)

    def tearDown(self):
        config.set_runtime_key(None)
        cli.set_json_output(False)
        self.env.stop()
        self.scratch.cleanup()

    def run_cli(self, *args, answer=None):
        """Run `pdatum <args>` through main(). Returns (exit code, stdout, stderr, session)."""
        session = FakeSession(answer or (lambda path, params: FakeResponse(404, {})))
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with patch("pdatum.client.requests.Session", return_value=session), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main(["pdatum", *args])
            except SystemExit as e:
                code = e.code or 0
        return code, out.getvalue(), err.getvalue(), session


class TimespecTestCase(unittest.TestCase):

    def test_forms(self):
        self.assertEqual(timespec.parse("1727000000"), 1727000000)
        self.assertEqual(timespec.parse("2026-09-01"), 1788220800)
        self.assertEqual(timespec.parse("2026-09-01T00:00:00Z"), 1788220800)
        self.assertEqual(timespec.parse("2026-09-01T02:00:00+02:00"), 1788220800)
        self.assertEqual(timespec.parse("7d", now=1_000_000), 1_000_000 - 7 * 86400)
        self.assertEqual(timespec.parse("12h", now=1_000_000), 1_000_000 - 12 * 3600)
        self.assertEqual(timespec.parse("30m", now=1_000_000), 1_000_000 - 1800)

    def test_nonsense_says_what_it_takes(self):
        with self.assertRaises(ValueError) as caught:
            timespec.parse("last tuesday")
        self.assertIn("7d", str(caught.exception))


class ConfigTestCase(Isolated):

    def test_key_precedence(self):
        self.assertEqual(config.api_key(), (None, None))
        config.save_key("from-file")
        self.assertEqual(config.api_key()[0], "from-file")
        os.environ["PDATUM_API_KEY"] = "from-env"
        self.assertEqual(config.api_key(), ("from-env", "PDATUM_API_KEY"))
        config.set_runtime_key("from-flag")
        self.assertEqual(config.api_key(), ("from-flag", "--api-key"))

    def test_config_path_expands_tilde(self):
        with patch.dict(os.environ, {"PDATUM_CONFIG_PATH": "~/x.json",
                                     "HOME": self.scratch.name,
                                     "USERPROFILE": self.scratch.name}):
            self.assertEqual(config.config_path(), Path(self.scratch.name) / "x.json")

    def test_url(self):
        self.assertEqual(config.base_url(), config.DEFAULT_URL)
        config.set_base_url("http://localhost:8001/")
        self.assertEqual(config.base_url(), "http://localhost:8001")
        os.environ["PDATUM_URL"] = "http://elsewhere.test"
        self.assertEqual(config.base_url(), "http://elsewhere.test")


class ClientTestCase(unittest.TestCase):

    def client(self, answer, sleeps=None):
        session = FakeSession(answer)
        return Client("https://api.test", KEY, session=session,
                      sleep=(sleeps.append if sleeps is not None else lambda s: None)), session

    def test_sends_the_key_and_encodes_parameters(self):
        c, session = self.client(lambda p, q: FakeResponse(body={"data": {}}))
        c.get("/jobs/count", remote=True, brand=None, q="x")
        self.assertEqual(session.headers["Authorization"], f"Bearer {KEY}")
        self.assertEqual(session.calls, [("/jobs/count", {"remote": "true", "q": "x"})])

    def test_waits_out_a_rate_limit(self):
        answers = [FakeResponse(429, {"error": {}}, {"Retry-After": "2"}),
                   FakeResponse(body={"data": "ok"})]
        sleeps = []
        c, _ = self.client(lambda p, q: answers.pop(0), sleeps)
        self.assertEqual(c.get("/me")["data"], "ok")
        self.assertEqual(sleeps, [2.0])

    def test_gives_up_on_a_rate_limit_eventually(self):
        c, session = self.client(lambda p, q: FakeResponse(
            429, {"error": {"code": "rate_limited", "message": "Slow down"}}))
        with self.assertRaises(PdatumError) as caught:
            c.get("/me")
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(len(session.calls), MAX_RETRIES + 1)

    def test_errors_carry_the_servers_code_and_message(self):
        c, _ = self.client(lambda p, q: FakeResponse(
            400, {"error": {"code": "invalid_parameter", "message": "brand: one of ..."}}))
        with self.assertRaises(PdatumError) as caught:
            c.get("/jobs", brand="nope")
        self.assertEqual(caught.exception.code, "invalid_parameter")
        self.assertIn("brand", caught.exception.message)

    def test_an_error_without_json_still_says_something(self):
        c, _ = self.client(lambda p, q: FakeResponse(502, None, text="<html>bad gateway"))
        with self.assertRaises(PdatumError) as caught:
            c.get("/me")
        self.assertIn("502", caught.exception.message)

    def test_pages_follows_the_cursor_to_the_end(self):
        records = [{"id": n} for n in range(7)]
        seen = []
        c, session = self.client(pages_of(records, 3))
        got = list(c.pages("/jobs", on_page=lambda n, total: seen.append((n, total)), limit=3))
        self.assertEqual(got, records)
        self.assertEqual(seen, [(3, 7), (6, 7), (7, 7)])
        self.assertEqual([q.get("cursor") for _, q in session.calls], [None, "3", "6"])


class CommandTestCase(Isolated):

    def setUp(self):
        super().setUp()
        os.environ["PDATUM_API_KEY"] = KEY

    def test_pull_writes_json_lines_and_reports_on_stderr(self):
        records = [{"id": n} for n in range(1200)]
        code, out, err, session = self.run_cli("jobs", "pull", "--q", "nurse",
                                               answer=pages_of(records, 500))
        self.assertEqual(code, 0, err)
        lines = [json.loads(x) for x in out.splitlines()]
        self.assertEqual(lines, records)
        self.assertIn("pulled 1,200 of 1,200 jobs", err)
        self.assertEqual(session.calls[0][1]["limit"], 500)
        self.assertEqual(session.calls[0][1]["q"], "nurse")

    def test_pull_stops_at_max(self):
        records = [{"id": n} for n in range(50)]
        _, out, _, _ = self.run_cli("jobs", "pull", "--max", "7", answer=pages_of(records, 500))
        self.assertEqual(len(out.splitlines()), 7)

    def test_changes_says_where_to_resume(self):
        records = [{"id": 1, "changed_at": 100}, {"id": 2, "changed_at": 250}]
        code, out, err, session = self.run_cli("jobs", "changes", "--since", "50",
                                               answer=pages_of(records, 500))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(out.splitlines()), 2)
        self.assertIn("--since 249", err)
        self.assertEqual(session.calls[0][1]["since"], 50)

    def test_relative_times_are_sent_as_epochs(self):
        _, _, err, session = self.run_cli(
            "jobs", "count", "--posted-since", "7d",
            answer=lambda p, q: FakeResponse(body={"data": {"count": 3}}))
        sent = session.calls[0][1]["posted_since"]
        self.assertIsInstance(sent, int)

    def test_a_bad_time_is_a_usage_error(self):
        code, _, err, session = self.run_cli("jobs", "count", "--posted-since", "whenever")
        self.assertEqual(code, 2)
        self.assertEqual(session.calls, [])

    def test_json_works_after_the_subcommand(self):
        code, out, _, _ = self.run_cli(
            "jobs", "count", "--json",
            answer=lambda p, q: FakeResponse(body={"data": {"count": 42}}))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"count": 42})

    def test_json_as_another_options_value_stays_a_value(self):
        _, _, _, session = self.run_cli(
            "jobs", "count", "--q", "--json",
            answer=lambda p, q: FakeResponse(body={"data": {"count": 0}}))
        self.assertEqual(session.calls[0][1]["q"], "--json")

    def test_json_after_every_flag_is_still_ours(self):
        # pkanban 0.7.0 kept this list by hand, missed a new flag, and shipped
        # `login --no-wait --json` failing "No such option: --json".
        from typer.main import get_command

        flags, pending = set(), [get_command(cli.app)]
        while pending:
            command = pending.pop()
            for param in command.params:
                if getattr(param, "is_flag", False):
                    flags.update(param.opts)
                    flags.update(param.secondary_opts)
            pending.extend(getattr(command, "commands", {}).values())
        flags -= {"--json", "--help"}
        self.assertIn("--not-remote", flags)
        for flag in sorted(flags):
            with self.subTest(flag=flag):
                argv = ["pdatum", "jobs", "list", flag, "--json"]
                self.assertTrue(cli.extract_json_flag(argv))
                self.assertEqual(argv, ["pdatum", "jobs", "list", flag])

    def test_json_after_a_flag_reaches_the_command(self):
        code, out, _, session = self.run_cli(
            "jobs", "count", "--remote", "--json",
            answer=lambda p, q: FakeResponse(body={"data": {"count": 3}}))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"count": 3})

    def test_nothing_after_double_dash_is_an_option(self):
        argv = ["pdatum", "employers", "get", "--", "-k"]
        self.assertIsNone(cli.extract_api_key(argv))
        self.assertFalse(cli.extract_json_flag(argv))
        self.assertEqual(argv, ["pdatum", "employers", "get", "--", "-k"])

    def test_api_key_flag_is_used_and_never_saved(self):
        os.environ.pop("PDATUM_API_KEY")
        other = "pdatum_" + "o" * 32
        _, _, _, session = self.run_cli(
            "me", "-k", other,
            answer=lambda p, q: FakeResponse(body={"data": {
                "name": "n", "holder": "h", "prefix": "p", "scopes": [], "expires_at": None,
                "usage": {}}}))
        self.assertEqual(session.headers["Authorization"], f"Bearer {other}")
        self.assertFalse(config.config_path().exists())

    def test_no_key_says_how_to_get_one(self):
        os.environ.pop("PDATUM_API_KEY")
        code, out, err, session = self.run_cli("jobs", "count")
        self.assertEqual(code, 1)
        self.assertIn("PDATUM_API_KEY", err)
        self.assertEqual(session.calls, [])

    def test_errors_are_json_on_stderr_in_json_mode(self):
        code, out, err, _ = self.run_cli(
            "--json", "jobs", "get", "5",
            answer=lambda p, q: FakeResponse(404, {"error": {"code": "not_found",
                                                             "message": "No job 5."}}))
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(json.loads(err), {"error": "No job 5.", "code": "not_found",
                                           "status": 404})

    def test_key_save_checks_the_key_first(self):
        os.environ.pop("PDATUM_API_KEY")
        code, _, err, _ = self.run_cli(
            "key", "save", KEY,
            answer=lambda p, q: FakeResponse(401, {"error": {"code": "invalid_key",
                                                             "message": "Not ours."}}))
        self.assertEqual(code, 1)
        self.assertFalse(config.config_path().exists())

        me = {"data": {"name": "n", "holder": "h", "prefix": KEY[:15]}}
        code, _, err, _ = self.run_cli("key", "save", KEY,
                                       answer=lambda p, q: FakeResponse(body=me))
        self.assertEqual(code, 0, err)
        self.assertEqual(config.api_key()[0], KEY)

    def test_click_does_not_expand_arguments(self):
        """On Windows Click turns "~4,100" into a home directory and "*.py"
        into a list of files, unless told not to."""
        with patch.object(cli, "app") as app:
            cli.main(["pdatum", "jobs", "count", "--q", "~4,100"])
        self.assertIs(app.call_args.kwargs["windows_expand_args"], False)

    def test_skill_needs_no_key_and_no_network(self):
        os.environ.pop("PDATUM_API_KEY")
        code, out, _, session = self.run_cli("skill")
        self.assertEqual(code, 0)
        self.assertIn("pdatum jobs count", out)
        self.assertEqual(session.calls, [])

    def test_guide_needs_no_key(self):
        os.environ.pop("PDATUM_API_KEY")
        code, out, _, session = self.run_cli(
            "guide", answer=lambda p, q: FakeResponse(body=None, text="# pdatum API"))
        self.assertEqual(code, 0)
        self.assertIn("# pdatum API", out)
        self.assertEqual(session.calls[0][0], "/docs")
        self.assertNotIn("Authorization", session.headers)


# Text a crawled job or a user can really contain: a lowercase tag rich would
# swallow, a closing tag that matches nothing, markup that must stay literal.
HOSTILE = ["Nurse [per diem]", "[remote] Analyst", "Q3 [/x] plan", "[red]not red[/red]"]


class HostileTextTestCase(Isolated):
    """rich reads [brackets] as markup. Nothing printed may be altered by that."""

    def setUp(self):
        super().setUp()
        os.environ["PDATUM_API_KEY"] = KEY

    def job(self, name):
        return {
            "id": 7, "posted_at": 1788220800, "first_seen_at": 1788220800,
            "company": name, "position": name, "location": name, "remote": False,
            "open": True, "salary_min": None, "salary_max": None, "salary_currency": name,
            "application_url": "https://example.com/?a[b]=" + name, "description": name,
        }

    def test_job_listing_and_job_detail_print_as_written(self):
        for name in HOSTILE:
            with self.subTest(name=name):
                code, out, err, _ = self.run_cli(
                    "jobs", "search", answer=pages_of([self.job(name)], 20))
                self.assertEqual(code, 0, err)
                self.assertGreaterEqual(out.count(name), 1, out)

                code, out, err, _ = self.run_cli(
                    "jobs", "get", "7",
                    answer=lambda p, q: FakeResponse(body={"data": self.job(name)}))
                self.assertEqual(code, 0, err)
                self.assertGreaterEqual(out.count(name), 3, out)  # position, company, location

    def employer(self, name):
        return {
            "slug": "acme", "name": name, "domain": name, "hiring": True,
            "open_postings": {"jobwolverine": 1},
            "facts": [{"key": name, "value": name, "source": name, "observed_at": 1788220800}],
        }

    def test_employer_listing_and_detail_print_as_written(self):
        for name in HOSTILE:
            with self.subTest(name=name):
                code, out, err, _ = self.run_cli(
                    "employers", "list", answer=pages_of([self.employer(name)], 20))
                self.assertEqual(code, 0, err)
                self.assertIn(name, out)

                code, out, err, _ = self.run_cli(
                    "employers", "get", "acme",
                    answer=lambda p, q: FakeResponse(body={"data": self.employer(name)}))
                self.assertEqual(code, 0, err)
                self.assertGreaterEqual(out.count(name), 4, out)  # name, key, value, source

    def test_a_structured_fact_value_is_json_not_a_python_repr(self):
        value = [{"board": "10xgenomics", "how": "crawled", "jobs": 34, "ok": True, "x": None}]
        e = self.employer("Acme")
        e["facts"] = [{"key": "job_boards", "value": value, "source": "feeds",
                       "observed_at": 1788220800}]
        code, out, err, _ = self.run_cli(
            "employers", "get", "acme", answer=lambda p, q: FakeResponse(body={"data": e}))
        self.assertEqual(code, 0, err)
        # The table folds a long cell across lines, so look for short tokens
        # that cannot be split: JSON's spelling, not Python's.
        for token in ('"crawled"', '"ok":', "true", "null"):
            self.assertIn(token, out)
        for token in ("'board'", "True", "None"):
            self.assertNotIn(token, out)

    def test_fact_value_keeps_text_and_writes_the_rest_as_json(self):
        self.assertEqual(cli.fact_value("kula"), "kula")
        self.assertEqual(cli.fact_value(34), "34")
        self.assertEqual(cli.fact_value(True), "true")
        self.assertEqual(cli.fact_value(None), "null")
        value = [{"board": "10xgenomics", "ok": True}]
        self.assertEqual(cli.fact_value(value), json.dumps(value))

    def test_an_error_message_is_not_markup_and_goes_to_stderr(self):
        for name in HOSTILE:
            with self.subTest(name=name):
                code, out, err, _ = self.run_cli(
                    "jobs", "count", answer=lambda p, q: FakeResponse(
                        400, {"error": {"code": "invalid_parameter",
                                        "message": f"limit: expected a number, got {name!r}"}}))
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertIn(name, err)

    def test_an_error_is_not_reflowed_to_the_terminal_width(self):
        os.environ.pop("PDATUM_API_KEY")
        with patch.dict(os.environ, {"COLUMNS": "40"}):
            code, out, err, _ = self.run_cli("jobs", "count")
        self.assertEqual(code, 1)
        self.assertIn("pdatum key save <key>", err)  # on one line, not split at 40 columns


REPO = Path(__file__).resolve().parents[1]


def dead_pipe():
    """A pipe's write end with nobody holding the read end: every write fails."""
    read_end, write_end = os.pipe()
    os.close(read_end)
    return write_end


def pdatum_process(*args, stdout, stderr):
    env = dict(os.environ, PYTHONPATH=str(REPO), PDATUM_CONFIG_PATH=os.devnull)
    for name in ("PDATUM_API_KEY", "PDATUM_URL", "PDATUM_OUTPUT"):
        env.pop(name, None)
    return subprocess.run(
        [sys.executable, "-m", "pdatum", *args],
        stdout=stdout, stderr=stderr, env=env, timeout=60,
    )


def assert_quiet_exit(case, done, windows_status, posix_statuses):
    """No 120 on any platform; the status exact where this code decides it.

    On Windows a closed pipe is EINVAL, which rich and Click do not know, so the
    status is ours: before this it was 120 for stderr. On POSIX they already turn
    EPIPE into a quiet exit 1 themselves, and what they choose is not ours to pin.
    """
    case.assertNotEqual(done.returncode, 120)
    if os.name == "nt":
        case.assertEqual(done.returncode, windows_status)
    else:
        case.assertIn(done.returncode, posix_statuses)


class ReaderLeftTestCase(unittest.TestCase):
    """`pdatum ... | head`: the reader goes, and that is not a crash.

    Left alone the next write raises, usually in the interpreter's final flush
    where no handler is waiting: a traceback, and exit status 120. These run the
    real CLI in a subprocess against a pipe that nobody is reading.
    """

    def test_a_reader_that_left_stdout_is_not_an_error(self):
        stdout = dead_pipe()
        try:
            done = pdatum_process("--help", stdout=stdout, stderr=subprocess.PIPE)
        finally:
            os.close(stdout)
        self.assertEqual(done.stderr, b"", done.stderr.decode(errors="replace"))
        assert_quiet_exit(self, done, windows_status=0, posix_statuses=(0, 1))

    def test_a_short_output_that_fails_in_the_final_flush_is_quiet_too(self):
        stdout = dead_pipe()
        try:
            done = pdatum_process("--version", stdout=stdout, stderr=subprocess.PIPE)
        finally:
            os.close(stdout)
        self.assertEqual(done.stderr, b"", done.stderr.decode(errors="replace"))
        assert_quiet_exit(self, done, windows_status=0, posix_statuses=(0, 1))

    def test_a_reader_that_left_stderr_keeps_the_commands_own_status(self):
        """A usage error is status 2; a closed stderr must not turn it into 120."""
        stderr = dead_pipe()
        try:
            done = pdatum_process("--no-such-option", stdout=subprocess.PIPE, stderr=stderr)
        finally:
            os.close(stderr)
        self.assertEqual(done.stdout, b"")
        assert_quiet_exit(self, done, windows_status=2, posix_statuses=(1, 2))

    def test_run_quietly_returns_the_status_the_command_exits_with(self):
        def fails():
            raise SystemExit(3)

        self.assertEqual(run_quietly(fails), 3)
        self.assertEqual(run_quietly(lambda: None), 0)

    def test_run_quietly_still_raises_an_error_that_is_not_a_reader_leaving(self):
        def broken():
            raise PermissionError("not a pipe")

        with self.assertRaises(PermissionError):
            run_quietly(broken)

    def test_reader_left_means_a_closed_pipe_and_nothing_else(self):
        self.assertTrue(reader_left(BrokenPipeError()))
        self.assertFalse(reader_left(PermissionError()))
        self.assertFalse(reader_left(FileNotFoundError()))


if __name__ == "__main__":
    unittest.main()


class BookTestCase(Isolated):
    """pdatum bdc: a lender's book, by ticker (wolverine card #906)."""

    VERSION = "87adfe96d67108e288f5"
    TABLES = {
        "positions": '{"as_of":1,"company":"acme","mark":90.0}\n{"as_of":2,"company":"acme","mark":88.5}\n',
        "figures": '{"key":"totals.nav","unit":"usd","value":252812000}\n',
    }

    def setUp(self):
        super().setUp()
        os.environ["PDATUM_API_KEY"] = KEY

    def manifest(self, **changes):
        import hashlib

        data = {
            "ticker": "WHF", "lender": "whf", "name": "WhiteHorse Finance, Inc.",
            "status": "ready", "version": self.VERSION, "recorded_at": 1791161463,
            "as_of": 1782777600, "quarters": 16, "request": None,
            "caveats": ["Other lenders mark 17% [per diem] of this book."],
            "tables": {name: {"rows": text.count("\n"),
                              "sha256": hashlib.sha256(text.encode()).hexdigest()}
                       for name, text in self.TABLES.items()},
        }
        data.update(changes)
        return data

    def answer(self, manifest=None, tables=None):
        tables = tables or self.TABLES

        def answer(path, params):
            if path == "/bdc/books/WHF":
                return FakeResponse(body={"data": manifest or self.manifest()})
            name = path.rsplit("/", 1)[1]
            return FakeResponse(text=tables[name], headers={"X-Book-Version": self.VERSION})

        return answer

    def test_request_posts_one_ticker(self):
        code, out, err, session = self.run_cli(
            "bdc", "request", "scm",
            answer=lambda p, q: FakeResponse(202, {"data": {"ticker": "SCM", "status": "queued"}}))
        self.assertEqual(code, 0, err)
        self.assertEqual(session.posted, [{"ticker": "scm"}])
        self.assertEqual(session.calls[0][0], "/bdc/books")
        self.assertIn("SCM: queued", out)

    def test_book_prints_the_caveats_as_written(self):
        code, out, err, _ = self.run_cli("bdc", "book", "whf", answer=self.answer())
        self.assertEqual(code, 0, err)
        self.assertIn("version " + self.VERSION, out)
        self.assertIn("[per diem]", out, "a caveat is server text, printed literally")

    def test_a_book_being_read_says_so(self):
        answer = lambda p, q: FakeResponse(202, {"data": {"ticker": "SCM", "status": "reading"}})
        code, out, err, _ = self.run_cli("bdc", "book", "SCM", answer=answer)
        self.assertEqual(code, 0, err)
        self.assertIn("SCM: reading, not read yet", out)

    def test_pull_writes_every_table_and_the_manifest(self):
        folder = os.path.join(self.scratch.name, "whf")
        code, out, err, session = self.run_cli("bdc", "pull", "WHF", "--out", folder,
                                               answer=self.answer())
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), folder)
        self.assertIn("positions: 2 rows", err)
        for name, text in self.TABLES.items():
            with open(os.path.join(folder, name + ".jsonl"), encoding="utf-8") as f:
                self.assertEqual(f.read(), text)
        with open(os.path.join(folder, "manifest.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["version"], self.VERSION)
        self.assertEqual(session.calls[1][1], {"version": self.VERSION},
                         "tables are fetched at the manifest's version")

    def test_pull_refuses_a_table_that_is_not_what_the_manifest_promised(self):
        tampered = dict(self.TABLES, figures='{"key":"totals.nav","value":1}\n')
        folder = os.path.join(self.scratch.name, "whf")
        code, _, err, _ = self.run_cli("bdc", "pull", "WHF", "--out", folder,
                                       answer=self.answer(tables=tampered))
        self.assertEqual(code, 1)
        self.assertIn("figures arrived different from what the manifest promises", err)

    def test_pull_refuses_a_book_not_read_yet(self):
        answer = lambda p, q: FakeResponse(202, {"data": {"ticker": "WHF", "status": "queued"}})
        code, _, err, _ = self.run_cli("bdc", "pull", "WHF", answer=answer)
        self.assertEqual(code, 1)
        self.assertIn("WHF has no book yet: it is queued", err)

    def test_pull_json_reports_what_it_wrote(self):
        folder = os.path.join(self.scratch.name, "whf")
        code, out, err, _ = self.run_cli("bdc", "pull", "WHF", "--out", folder, "--json",
                                         answer=self.answer())
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result["tables"], {"positions": 2, "figures": 1})
        self.assertEqual(result["version"], self.VERSION)
