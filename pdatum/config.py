"""
Where the CLI finds its server and its key.

The key is looked for in this order, and the first one found is used:

  1. --api-key on the command line, for one invocation;
  2. PDATUM_API_KEY in the environment -- the way to give an agent a key,
     since it needs no file and nothing is written anywhere;
  3. the config file, written by `pdatum key save`.

The config file is ~/.pdatum.json unless PDATUM_CONFIG_PATH says otherwise.
Both are resolved on every call rather than at import, so a test (or a
script) that sets the variable after importing still gets the file it asked
for -- pkanban once wrote to developers' real credentials for want of that.
"""

import json
import os
from pathlib import Path

DEFAULT_URL = "https://pdatum.pearachute.com"

_runtime_key = None


def config_path():
    raw = os.environ.get("PDATUM_CONFIG_PATH")
    if raw:
        # Expanded here because nothing else will: a quoted "~/x.json" would
        # otherwise create a directory literally named "~".
        return Path(raw).expanduser()
    return Path.home() / ".pdatum.json"


def load():
    path = config_path()
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        try:
            data = json.load(f)
        except ValueError:
            return {}
    return data if isinstance(data, dict) else {}


def save(data):
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    try:
        # The file holds a key. Readable by its owner only, where the
        # platform has such a thing.
        os.chmod(path, 0o600)
    except OSError:
        pass


def set_runtime_key(key):
    """--api-key: this invocation only, never written anywhere."""
    global _runtime_key
    _runtime_key = key


def api_key():
    """(key, where it came from), or (None, None)."""
    if _runtime_key:
        return _runtime_key, "--api-key"
    env = os.environ.get("PDATUM_API_KEY", "").strip()
    if env:
        return env, "PDATUM_API_KEY"
    saved = load().get("api_key")
    if saved:
        return saved, str(config_path())
    return None, None


def save_key(key):
    data = load()
    data["api_key"] = key
    save(data)


def clear_key():
    data = load()
    had = data.pop("api_key", None) is not None
    save(data)
    return had


def base_url():
    env = os.environ.get("PDATUM_URL", "").strip()
    if env:
        return env.rstrip("/")
    return (load().get("url") or DEFAULT_URL).rstrip("/")


def set_base_url(url):
    data = load()
    data["url"] = url.rstrip("/")
    save(data)


def display_key(key):
    """Enough of a key to recognise it, never enough to use it."""
    return key[:15] + "..." if key else None
