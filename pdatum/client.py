"""
The HTTP client. Knows the API's envelope, its errors and its paging, and
nothing about how results are printed.
"""

import hashlib
import time

import requests

from pdatum import __version__

# Seconds to connect, then seconds to wait for an answer. The server's nginx
# gives a request 180s (proxy_read_timeout, in jobwolverine's sys/nginx), and
# a company's first awards lookup goes to USAspending and has taken over a
# minute. At 60s the client reported an answer still on its way as a failure,
# and the retry it suggested came back at once, from the cache that answer
# filled.
CONNECT_TIMEOUT = 15
TIMEOUT = 190

# How many times one request waits out a 429 before giving up. A long pull
# hits the per-key limit on purpose; waiting is the right answer, forever is
# not.
MAX_RETRIES = 8


class PdatumError(Exception):
    """A failure the user can act on, reported without a traceback."""

    def __init__(self, message, code=None, status=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


class NotReady(PdatumError):
    """
    A 202 where data was asked for: it is being read, and is not here yet.

    Not an error on the server's side, but it must not reach a command as data:
    a 202's body says what is being read, in a different shape, and a WARN
    count or listing would otherwise print nothing, or a zero, as the answer.
    """


def _later(retry_after):
    """Retry-After seconds, said the way a person would: 'in about 5 minutes'."""
    try:
        seconds = int(retry_after)
    except (TypeError, ValueError):
        return "later"
    minutes = max(1, round(seconds / 60))
    if minutes < 90:
        return f"in about {minutes} minute{'s' if minutes != 1 else ''}"
    return f"in about {round(minutes / 60)} hours"


def _param(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


class Client:

    def __init__(self, url, key=None, session=None, sleep=time.sleep):
        self.url = url.rstrip("/")
        self.key = key
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = f"pdatum-cli/{__version__}"
        if key:
            self.session.headers["Authorization"] = f"Bearer {key}"
        self._sleep = sleep

    def _send(self, path, params=None, body=None, stream=False, pending_ok=False):
        url = f"{self.url}/api/v1{path}"
        params = {k: _param(v) for k, v in (params or {}).items() if v is not None}

        for attempt in range(MAX_RETRIES + 1):
            timeout = (CONNECT_TIMEOUT, TIMEOUT)
            try:
                if body is not None:
                    response = self.session.post(url, params=params, json=body, timeout=timeout)
                elif stream:
                    response = self.session.get(url, params=params, timeout=timeout, stream=True)
                else:
                    response = self.session.get(url, params=params, timeout=timeout)
            except (requests.exceptions.MissingSchema, requests.exceptions.InvalidURL,
                    requests.exceptions.InvalidSchema):
                raise PdatumError(
                    f"{self.url!r} is not a URL. Set one with: pdatum config --url https://..."
                )
            # Before Timeout: a ConnectTimeout is both, and a server that never
            # answered the connection was not reached, not slow.
            except requests.exceptions.ConnectionError:
                raise PdatumError(f"Could not reach {self.url}. Check the URL (pdatum config).")
            except requests.exceptions.Timeout:
                raise PdatumError(f"{self.url} took more than {TIMEOUT}s to answer. Try again.")

            if response.status_code == 429 and attempt < MAX_RETRIES:
                try:
                    wait = float(response.headers.get("Retry-After", "1"))
                except ValueError:
                    wait = 1.0
                self._sleep(max(wait, 0.5))
                continue
            break

        if response.status_code >= 400:
            raise self._error(response)
        if response.status_code == 202 and not pending_ok:
            raise self._not_ready(response)
        return response

    @staticmethod
    def _not_ready(response):
        try:
            data = response.json().get("data") or {}
        except (ValueError, AttributeError):
            data = {}
        states = data.get("states") if isinstance(data, dict) else None
        if states:
            what = "Not read yet: %s, waiting for a first read." % ", ".join(
                str(s).upper() for s in states)
        else:
            what = "Not here yet: it is being read."
        later = _later(response.headers.get("Retry-After"))
        return NotReady(f"{what} Try again {later}.", code="not_ready", status=202)

    @staticmethod
    def _error(response):
        code, message = None, None
        try:
            error = response.json().get("error") or {}
            code, message = error.get("code"), error.get("message")
        except (ValueError, AttributeError):
            pass
        if not message:
            message = f"The server answered {response.status_code}."
        if response.status_code == 401 and code == "missing_key":
            message += " Set PDATUM_API_KEY, or run: pdatum key save <key>"
        return PdatumError(message, code=code, status=response.status_code)

    def get(self, path, pending_ok=False, **params):
        """
        One request; the parsed JSON body. A 202 raises NotReady unless
        `pending_ok`, for a caller that shows what is being read.
        """
        return self._send(path, params, pending_ok=pending_ok).json()

    def post(self, path, body):
        """One POST with a JSON body; the parsed JSON answer. A 202 is its usual answer."""
        return self._send(path, body=body, pending_ok=True).json()

    def text(self, path):
        return self._send(path).text

    def download(self, path, dest, **params):
        """
        Stream a response body into the file `dest` without holding it in memory.

        Returns (sha256 of what was written, lines written, response headers),
        so a caller can check what arrived against what it was promised.
        """
        response = self._send(path, params, stream=True)
        digest, lines = hashlib.sha256(), 0
        with open(dest, "wb") as f:
            for chunk in response.iter_content(chunk_size=1 << 16):
                if chunk:
                    f.write(chunk)
                    digest.update(chunk)
                    lines += chunk.count(b"\n")
        return digest.hexdigest(), lines, response.headers

    def pages(self, path, on_page=None, on_first=None, **params):
        """
        Every record a list endpoint returns, following next_cursor to the
        end. on_first(body) is called with the first page, before any of its
        records; on_page(received so far, total) after each page.
        """
        cursor, received = None, 0
        while True:
            body = self.get(path, cursor=cursor, **params)
            if on_first is not None and cursor is None:
                on_first(body)
            for record in body["data"]:
                yield record
            received += len(body["data"])
            if on_page is not None:
                on_page(received, body.get("total"))
            cursor = body.get("next_cursor")
            if not cursor:
                return
