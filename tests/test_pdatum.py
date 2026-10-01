"""
The pdatum CLI, with the HTTP session replaced. Nothing here reaches a server.

Run from the repository root:  python -m unittest discover -s tests
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pdatum import cli, config, timespec  # noqa: E402
from pdatum.client import MAX_RETRIES, Client, PdatumError  # noqa: E402

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


class FakeSession:
    """Answers from a function of (path, params); records every call."""

    def __init__(self, answer):
        self.answer = answer
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        path = url.split("/api/v1", 1)[1]
        self.calls.append((path, dict(params or {})))
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


if __name__ == "__main__":
    unittest.main()
