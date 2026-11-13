"""
server.json: pdatum's listing in the MCP registry.

The publish workflow (.github/workflows/publish-mcp-registry.yml) checks it
against the registry's schema only when someone runs it, long after a careless
edit. These are the parts that would fail there, or list the wrong thing.
"""

import json
import os
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

with open(os.path.join(ROOT, "server.json"), encoding="utf-8") as f:
    SERVER = json.load(f)


class ServerJsonTestCase(unittest.TestCase):

    def test_name_is_the_namespace_github_oidc_proves(self):
        # The workflow signs in as this repository's owner, which grants only
        # io.github.japherwocky/*.
        self.assertEqual(SERVER["name"], "io.github.japherwocky/pdatum")

    def test_lists_the_hosted_endpoint_and_the_key_it_takes(self):
        (remote,) = SERVER["remotes"]
        self.assertEqual(remote["type"], "streamable-http")
        self.assertEqual(remote["url"], "https://pdatum.pearachute.com/mcp")
        (header,) = remote["headers"]
        self.assertEqual(header["name"], "Authorization")
        # Optional: an app that signs in with OAuth gets the key from pdatum's
        # own sign-in page, and needs no header configured.
        self.assertFalse(header["isRequired"])
        self.assertTrue(header["isSecret"], "a key must never be shown as a plain value")

    def test_fits_the_registrys_limits(self):
        # From the registry's server.schema.json (2025-12-11).
        self.assertTrue(1 <= len(SERVER["description"]) <= 100, len(SERVER["description"]))
        self.assertTrue(1 <= len(SERVER["title"]) <= 100)

    def test_the_repository_is_this_one(self):
        self.assertEqual(SERVER["repository"]["url"], "https://github.com/japherwocky/pdatum")


if __name__ == "__main__":
    unittest.main()
