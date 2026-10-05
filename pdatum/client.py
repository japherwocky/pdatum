"""
The HTTP client. Knows the API's envelope, its errors and its paging, and
nothing about how results are printed.
"""

import hashlib
import time

import requests

from pdatum import __version__

TIMEOUT = 60

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

    def _send(self, path, params=None, body=None, stream=False):
        url = f"{self.url}/api/v1{path}"
        params = {k: _param(v) for k, v in (params or {}).items() if v is not None}

        for attempt in range(MAX_RETRIES + 1):
            try:
                if body is not None:
                    response = self.session.post(url, params=params, json=body, timeout=TIMEOUT)
                elif stream:
                    response = self.session.get(url, params=params, timeout=TIMEOUT, stream=True)
                else:
                    response = self.session.get(url, params=params, timeout=TIMEOUT)
            except requests.exceptions.Timeout:
                raise PdatumError(f"{self.url} took more than {TIMEOUT}s to answer. Try again.")
            except (requests.exceptions.MissingSchema, requests.exceptions.InvalidURL,
                    requests.exceptions.InvalidSchema):
                raise PdatumError(
                    f"{self.url!r} is not a URL. Set one with: pdatum config --url https://..."
                )
            except requests.exceptions.ConnectionError:
                raise PdatumError(f"Could not reach {self.url}. Check the URL (pdatum config).")

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
        return response

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

    def get(self, path, **params):
        """One request; the parsed JSON body."""
        return self._send(path, params).json()

    def post(self, path, body):
        """One POST with a JSON body; the parsed JSON answer."""
        return self._send(path, body=body).json()

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

    def pages(self, path, on_page=None, **params):
        """
        Every record a list endpoint returns, following next_cursor to the
        end. on_page(received so far, total) is called after each page.
        """
        cursor, received = None, 0
        while True:
            body = self.get(path, cursor=cursor, **params)
            for record in body["data"]:
                yield record
            received += len(body["data"])
            if on_page is not None:
                on_page(received, body.get("total"))
            cursor = body.get("next_cursor")
            if not cursor:
                return
