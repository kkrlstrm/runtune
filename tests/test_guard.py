"""The write guard: what RunTune writes is checked first, and a hit refuses (never redacts).

Borrowed from @parcha/pi-agent-learn's guard. These hold:
- a configured secret is found as written, split over lines, base64/base64url at all three
  alignments, hex and percent-encoded, and the refusal never repeats it;
- token shapes, invisible Unicode and the host's case data are refused;
- joiners that are text (emoji sequences, Persian/Indic letters) are not;
- a case value matches as a word, so a short client name does not refuse every longer word;
- a configured source that is missing is an error, never a guard that checks nothing;
- stage and apply both run it; a --git publish runs it over the staged diff and PR body.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import unittest
import urllib.parse

from runtune import cli
from runtune import guard as g
from runtune import gitops
from runtune.derive import capabilities
from runtune.lifecycle import promoter
from runtune.lifecycle.store import Workspace

from . import fixtures as fx

SECRET = "pgpass-9f8e7d6c5b4a"


def reasons(refusals):
    return sorted({r.reason for r in refusals})


class SecretValues(unittest.TestCase):
    def check(self, text):
        return g.check_text("f", text, secrets=[SECRET])

    def test_found_in_every_encoding(self):
        raw = SECRET.encode()
        samples = {
            "as written": f"export X={SECRET}",
            "split over lines": f"value: {SECRET[:7]}\n  {SECRET[7:]}",
            "hex": raw.hex(),
            "HEX": raw.hex().upper(),
            "percent": urllib.parse.quote("a b " + SECRET + "/"),
        }
        for k, pre in enumerate((b"", b"x", b"xy")):
            for enc in (base64.b64encode, base64.urlsafe_b64encode):
                samples[f"base64 align {k} {enc.__name__}"] = enc(b"header:" + pre + raw + b":tail").decode()
        for name, text in samples.items():
            with self.subTest(name):
                self.assertIn("secret_value", reasons(self.check(text)))

    def test_refusal_never_repeats_the_secret(self):
        refusals = self.check(SECRET)
        self.assertTrue(refusals)
        for r in refusals:
            self.assertNotIn(SECRET, str(r))
            self.assertNotIn(SECRET[:8], str(r))

    def test_unrelated_text_passes(self):
        self.assertEqual(self.check("Use the toolkit CLI instead of an inline program."), [])


class Shapes(unittest.TestCase):
    def test_token_shapes(self):
        for text in ("AKIA" + "A" * 16, "ghp_" + "a" * 36, "sk-or-" + "b" * 30, "xoxb-12345-abcdefghij",
                     "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.c2lnbmF0dXJlLXZhbHVl",
                     "postgresql://user:hunter22@db.example.com/x", "-----BEGIN RSA PRIVATE KEY-----"):
            with self.subTest(text[:12]):
                self.assertIn("secret_shape", reasons(g.check_text("f", text)))

    def test_public_material_passes(self):
        for text in ("-----BEGIN CERTIFICATE-----", "-----BEGIN PUBLIC KEY-----",
                     "https://user@example.com/repo.git", "sk-short"):
            with self.subTest(text):
                self.assertEqual(g.check_text("f", text), [])


class Invisible(unittest.TestCase):
    def test_hidden_characters_refused(self):
        for ch in ("​", "‮", "﻿", "\U000e0041", "", "\x07"):
            with self.subTest(hex(ord(ch))):
                self.assertIn("invisible_unicode", reasons(g.check_text("f", f"run{ch} this")))

    def test_joiners_that_are_text_pass(self):
        family = "\U0001F468‍\U0001F469‍\U0001F467"
        persian = "می‌خواهم"
        devanagari = "क्‍ष"
        for text in (family, persian, devanagari, "❤️"):
            with self.subTest(repr(text)):
                self.assertEqual(g.check_text("f", text), [])

    def test_zero_width_inside_latin_word_refused(self):
        self.assertIn("invisible_unicode", reasons(g.check_text("f", "ad‌min")))


class CaseData(unittest.TestCase):
    CASES = [("Acme Learning", "clients.json"), ("spur", "clients.json"), ("jane.doe@acme.example", "people.txt")]

    def check(self, text, **kw):
        return g.check_text("f", text, case_values=self.CASES, **kw)

    def test_case_values_refused_whatever_the_spelling(self):
        for text in ("Run it for ACME learning first", "acme\nlearning", "ask Jane.Doe@acme.example",
                     "spur: the dialer list", "ｓｐｕｒ wide letters"):
            with self.subTest(text):
                self.assertIn("case_data", reasons(self.check(text)))

    def test_matches_whole_words_only(self):
        self.assertEqual(self.check("a spurious failure; spurt"), [])

    def test_ignore_and_min_len(self):
        self.assertEqual(self.check("spur again", ignore={"spur"}), [])
        self.assertEqual(g.check_text("f", "the abc job", case_values=[("abc", "x")]), [])

    def test_refusal_names_the_source_not_the_value(self):
        r = self.check("Acme Learning")[0]
        self.assertIn("clients.json", str(r))
        self.assertNotIn("Acme", str(r))


class Sources(unittest.TestCase):
    def test_json_keys_and_text_lines(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "clients.json"), "w") as f:
                json.dump({"clients": {"acme": {"slug": "acme", "name": "Acme Learning", "id": 7},
                                       "beta": {"slug": "beta-ed", "name": "Beta Ed"}}}, f)
            with open(os.path.join(d, "people.txt"), "w") as f:
                f.write("# people\nJane Doe\n\n")
            vals = g.load_case_values({"case_values": [{"path": "clients.json", "keys": ["slug", "name"]},
                                                       {"path": "people.txt"}]}, d)
            self.assertEqual(sorted(v for v, _ in vals), ["Acme Learning", "Beta Ed", "Jane Doe", "acme", "beta-ed"])
            keys = g.load_case_values({"case_values": [{"path": "clients.json", "keys": ["clients", "*key"]}]}, d)
            self.assertIn("acme", [v for v, _ in keys])

    def test_missing_source_is_an_error(self):
        with tempfile.TemporaryDirectory() as d, self.assertRaises(FileNotFoundError):
            g.Guard({"case_values": [{"path": "nope.json", "keys": ["slug"]}]}, d)

    def test_env_secrets_by_name(self):
        old = dict(os.environ)
        try:
            os.environ.update({"MY_API_TOKEN": "tok-1234567890", "ODD": "odd-value-123", "PLAIN": "plain-value-123"})
            got = g.env_secrets(["ODD"])
            self.assertIn("tok-1234567890", got)
            self.assertIn("odd-value-123", got)
            self.assertNotIn("plain-value-123", got)
        finally:
            os.environ.clear()
            os.environ.update(old)


class Lifecycle(unittest.TestCase):
    """stage and apply both refuse an artifact the guard refuses."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.ws = Workspace(os.path.join(self.root, ".runtune"))
        caps, _ = capabilities.derive_inline(fx.inline_ttt())
        self.cand = caps[0].to_dict()
        self.ws.save_run({"run_id": "r1", "candidates": [self.cand], "withheld": []})

    def tearDown(self):
        self.tmp.cleanup()

    def _authority(self, guard_cfg):
        p = os.path.join(self.ws.root, "authority.json")
        auth = json.load(open(p))
        auth["guard"] = guard_cfg
        json.dump(auth, open(p, "w"))

    def _case_file(self, *values):
        with open(os.path.join(self.root, "case-values.txt"), "w") as f:
            f.write("\n".join(values) + "\n")

    def test_clean_artifact_stages_and_applies(self):
        self._case_file("Acme Learning")
        self._authority({"case_values": [{"path": "case-values.txt"}]})
        promoter.stage(self.ws, self.cand["id"], self.root)
        art = promoter.apply(self.ws, self.cand["id"], "alice")
        self.assertEqual(art["state"], "active")

    def test_stage_refuses_case_data(self):
        word = self.cand["proposal"]["skill_md"].split()[-1].strip("`.,:()")
        self._case_file(word)
        self._authority({"case_values": [{"path": "case-values.txt"}], "min_len": 1})
        with self.assertRaises(promoter.Refused) as ctx:
            promoter.stage(self.ws, self.cand["id"], self.root)
        self.assertIn("case_data", str(ctx.exception))
        self.assertIsNone(self.ws.get(self.cand["id"]))

    def test_apply_rechecks_with_the_case_data_of_now(self):
        self._case_file("Acme Learning")
        self._authority({"case_values": [{"path": "case-values.txt"}]})
        promoter.stage(self.ws, self.cand["id"], self.root)
        word = self.cand["proposal"]["skill_md"].split()[-1].strip("`.,:()")
        self._case_file("Acme Learning", word)
        self._authority({"case_values": [{"path": "case-values.txt"}], "min_len": 1})
        with self.assertRaises(promoter.Refused):
            promoter.apply(self.ws, self.cand["id"], "alice")
        self.assertEqual(self.ws.get(self.cand["id"])["state"], "staged")

    def test_misconfigured_guard_refuses(self):
        self._authority({"case_values": [{"path": "missing.txt"}]})
        with self.assertRaises(promoter.Refused) as ctx:
            promoter.stage(self.ws, self.cand["id"], self.root)
        self.assertIn("not configured correctly", str(ctx.exception))


class Publish(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name
        run = lambda *a: subprocess.run(["git", "-C", self.repo, *a], check=True, capture_output=True)
        self.run = run
        run("init", "-q", "-b", "main")
        open(os.path.join(self.repo, "README.md"), "w").write("x\n")
        run("add", "-A")
        run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        bare = os.path.join(self.repo, "..", os.path.basename(self.repo) + "-remote.git")
        self.bare = os.path.abspath(bare)
        subprocess.run(["git", "init", "-q", "--bare", self.bare], check=True)
        run("remote", "add", "origin", self.bare)

    def tearDown(self):
        self.tmp.cleanup()
        subprocess.run(["rm", "-rf", self.bare])

    def test_publish_refuses_and_pushes_nothing(self):
        open(os.path.join(self.repo, "SKILL.md"), "w").write("Use this for Acme Learning.\n")
        open(os.path.join(self.repo, "cases.txt"), "w").write("Acme Learning\n")
        cfg = {"case_values": [{"path": "cases.txt"}]}
        with self.assertRaises(g.GuardRefused):
            gitops.publish(self.repo, "D1", "approved 1", cfg)
        branches = subprocess.run(["git", "-C", self.repo, "branch", "--list"], capture_output=True, text=True).stdout
        self.assertNotIn("runtune/", branches)
        remote = subprocess.run(["git", "-C", self.bare, "branch", "--list"], capture_output=True, text=True).stdout
        self.assertEqual(remote.strip(), "")

    def test_publish_checks_the_pr_body(self):
        open(os.path.join(self.repo, "SKILL.md"), "w").write("Use the CLI.\n")
        with self.assertRaises(g.GuardRefused):
            gitops.publish(self.repo, "D2", "approved; token ghp_" + "a" * 36, {})


class Cli(unittest.TestCase):
    def test_guard_command_exit_codes(self):
        with tempfile.TemporaryDirectory() as d:
            ws = os.path.join(d, ".runtune")
            Workspace(ws)
            clean, dirty = os.path.join(d, "a.md"), os.path.join(d, "b.md")
            open(clean, "w").write("Use the CLI.\n")
            open(dirty, "w").write("key AKIA" + "B" * 16 + "\n")
            self.assertEqual(cli.main(["guard", clean, "--workspace", ws, "--target-root", d]), 0)
            self.assertEqual(cli.main(["guard", dirty, "--workspace", ws, "--target-root", d]), 4)


if __name__ == "__main__":
    unittest.main()
