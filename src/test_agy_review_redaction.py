#!/usr/bin/env python3
"""Recursive redaction of agy tool-call args. Red until _cap_args walks nested values."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from execution_backend.agy_review import (
    _calls_from_transcript,
    build_payload,
    read_tool_evidence,
)

_REVIEW = {"round": 1, "max_redos": 1, "history": []}
_SNAP = {"artifacts": {}, "artifact_hash": "abc"}
_REDACTED = "[redacted]"


def _transcript(args: dict, output: str = "ok") -> str:
    planner = {
        "type": "PLANNER_RESPONSE",
        "status": "DONE",
        "tool_calls": [{"name": "run_command", "args": args}],
    }
    generic = {"type": "GENERIC", "status": "DONE", "content": output}
    return json.dumps(planner) + "\n" + json.dumps(generic) + "\n"


def _rows(args: dict, *, input_cap: int = 1000, output_cap: int = 2000, max_calls: int = 30) -> list:
    return _calls_from_transcript(_transcript(args), max_calls, output_cap, input_cap)


def _blob(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _at(obj, path: tuple):
    cur = obj
    for part in path:
        cur = cur[part]
    return cur


def _depth(obj: object) -> int:
    if isinstance(obj, dict):
        return 1 + max((_depth(value) for value in obj.values()), default=0)
    if isinstance(obj, list):
        return 1 + max((_depth(value) for value in obj), default=0)
    return 0


def _count_nodes(obj: object) -> int:
    if isinstance(obj, dict):
        return 1 + sum(_count_nodes(value) for value in obj.values())
    if isinstance(obj, list):
        return 1 + sum(_count_nodes(value) for value in obj)
    return 1


def _sensitive_args() -> tuple[dict, tuple[tuple[tuple, str], ...]]:
    """Order example plus every sensitive key shape, with sibling scalars kept."""
    args = {
        "api_key": "SYNTH_A",
        "x_api_key": "SYNTH_XAPI",
        "user_token": {"raw": "SYNTH_OBJ", "n": 1},
        "cookie": ["SYNTH_COOKIE_LIST", "second"],
        "passwd_n": 424242,
        "CommandLine": "echo hello",
        "Cwd": "/w/task",
        "WaitMsBeforeAsync": 5000,
        "flag": True,
        "count": 0,
        "ratio": 1.25,
        "empty": None,
        "nested": {
            "password": "SYNTH_B",
            "dbPassword": "SYNTH_DBPW",
            "passwd": "SYNTH_PASSWD",
            "path": "/w/task/notes.md",
            "inner": {
                "secret": "SYNTH_SECRET",
                "client_secret": "SYNTH_CLIENT",
                "clientSecret": "SYNTH_CLIENT2",
                "access_token": "SYNTH_ACCESS",
                "refresh_token": "SYNTH_REFRESH",
                "authorization": "SYNTH_AUTHZ",
                "auth": "SYNTH_AUTH",
                "cookie": "SYNTH_COOKIE",
                "credential": "SYNTH_CRED",
                "credentials": "SYNTH_CREDS",
                "private_key": "SYNTH_PK",
                "PrivateKey": "SYNTH_PK2",
                "APIKEY": "SYNTH_APIKEY",
                "api-key": "SYNTH_API_DASH",
                "n": 2,
            },
        },
        "list": [
            {"token": "SYNTH_C", "path": "/w/keep"},
            {"Token": "SYNTH_TOKEN2"},
            {"Password": "SYNTH_PWCASE"},
            "keep-scalar",
            7,
            False,
            [{"auth": "SYNTH_DEEPLIST"}],
        ],
    }
    secrets = (
        (("api_key",), "SYNTH_A"),
        (("x_api_key",), "SYNTH_XAPI"),
        (("user_token",), "SYNTH_OBJ"),
        (("cookie",), "SYNTH_COOKIE_LIST"),
        (("passwd_n",), "424242"),
        (("nested", "password"), "SYNTH_B"),
        (("nested", "dbPassword"), "SYNTH_DBPW"),
        (("nested", "passwd"), "SYNTH_PASSWD"),
        (("nested", "inner", "secret"), "SYNTH_SECRET"),
        (("nested", "inner", "client_secret"), "SYNTH_CLIENT"),
        (("nested", "inner", "clientSecret"), "SYNTH_CLIENT2"),
        (("nested", "inner", "access_token"), "SYNTH_ACCESS"),
        (("nested", "inner", "refresh_token"), "SYNTH_REFRESH"),
        (("nested", "inner", "authorization"), "SYNTH_AUTHZ"),
        (("nested", "inner", "auth"), "SYNTH_AUTH"),
        (("nested", "inner", "cookie"), "SYNTH_COOKIE"),
        (("nested", "inner", "credential"), "SYNTH_CRED"),
        (("nested", "inner", "credentials"), "SYNTH_CREDS"),
        (("nested", "inner", "private_key"), "SYNTH_PK"),
        (("nested", "inner", "PrivateKey"), "SYNTH_PK2"),
        (("nested", "inner", "APIKEY"), "SYNTH_APIKEY"),
        (("nested", "inner", "api-key"), "SYNTH_API_DASH"),
        (("list", 0, "token"), "SYNTH_C"),
        (("list", 1, "Token"), "SYNTH_TOKEN2"),
        (("list", 2, "Password"), "SYNTH_PWCASE"),
        (("list", 6, 0, "auth"), "SYNTH_DEEPLIST"),
    )
    return args, secrets


class AgyReviewRedactionTests(unittest.TestCase):
    def test_sensitive_keys_redacted_at_any_depth(self) -> None:
        """asserts values under sensitive key names become [redacted] at any depth in dicts and lists, and no synthetic secret remains in json.dumps of the rows"""
        args, secrets = _sensitive_args()
        rows = _rows(args)
        self.assertEqual(len(rows), 1)
        inp = rows[0]["input"]
        blob = _blob(rows)
        for path, secret in secrets:
            with self.subTest(path=path):
                self.assertEqual(_at(inp, path), _REDACTED)
                self.assertNotIn(secret, blob)
        self.assertEqual(inp["CommandLine"], "echo hello")
        self.assertEqual(inp["nested"]["path"], "/w/task/notes.md")
        self.assertEqual(inp["nested"]["inner"]["n"], 2)
        self.assertEqual(inp["list"][0]["path"], "/w/keep")
        self.assertEqual(inp["list"][3], "keep-scalar")
        self.assertEqual(inp["count"], 0)
        for _, secret in secrets:
            self.assertNotIn(secret, blob)

    def test_string_values_keep_secret_re_redaction(self) -> None:
        """asserts string values anywhere still get the existing Bearer, key=value, and sk- redaction"""
        args = {
            "CommandLine": "prefix Bearer SYNTH_BEARER_ZZ suffix",
            "nested": {"note": "see api_key=SYNTH_EQ_ZZ next"},
            "items": [
                "sk-SYNTHFAKEKEY999",
                {"msg": "token=SYNTH_TOK_ZZ next"},
                "password: SYNTH_PW_ZZ",
            ],
        }
        rows = _rows(args)
        inp = rows[0]["input"]
        self.assertEqual(inp["nested"]["note"], "see [redacted] next")
        self.assertEqual(inp["items"][0], _REDACTED)
        self.assertEqual(inp["items"][1]["msg"], "[redacted] next")
        self.assertEqual(inp["items"][2], _REDACTED)
        self.assertEqual(inp["CommandLine"], "prefix [redacted] suffix")
        blob = _blob(rows)
        for secret in (
            "SYNTH_BEARER_ZZ",
            "SYNTH_EQ_ZZ",
            "SYNTHFAKEKEY999",
            "sk-SYNTHFAKEKEY999",
            "SYNTH_TOK_ZZ",
            "SYNTH_PW_ZZ",
        ):
            self.assertNotIn(secret, blob)
        self.assertIn("[redacted]", blob)

    def test_nonsensitive_values_preserved_and_strings_capped(self) -> None:
        """asserts non-sensitive keys and scalars are preserved and input_cap still bounds every string"""
        plain = "HEADMARKER-" + ("x" * 90) + "-TAILMARKER"
        bearer = "prefix Bearer SYNTH_BEARER_ZZ " + ("q" * 80)
        cap = 24
        args = {
            "CommandLine": plain,
            "Cwd": "/w/task",
            "WaitMsBeforeAsync": 5000,
            "flag": True,
            "count": 0,
            "ratio": 1.25,
            "empty": None,
            "note": "my api_key is fine",
            "word": "password",
            "nested": {
                "note": plain,
                "msg": bearer,
                "path": "/w/task/notes.md",
                "n": 2,
                "ok": False,
            },
            "items": [plain, "keep-me", 7, None],
        }
        rows = _rows(args, input_cap=cap)
        inp = rows[0]["input"]
        self.assertEqual(inp["nested"]["note"], plain[:cap])
        self.assertEqual(inp["nested"]["msg"], ("prefix [redacted] " + ("q" * 80))[:cap])
        self.assertEqual(inp["items"][0], plain[:cap])
        self.assertEqual(inp["CommandLine"], plain[:cap])
        self.assertNotIn("TAILMARKER", _blob(inp))
        self.assertNotIn("SYNTH_BEARER_ZZ", _blob(inp))
        self.assertEqual(inp["Cwd"], "/w/task")
        self.assertEqual(inp["WaitMsBeforeAsync"], 5000)
        self.assertIs(inp["flag"], True)
        self.assertEqual(inp["count"], 0)
        self.assertEqual(inp["ratio"], 1.25)
        self.assertIsNone(inp["empty"])
        self.assertEqual(inp["note"], "my api_key is fine")
        self.assertEqual(inp["word"], "password")
        self.assertEqual(inp["nested"]["path"], "/w/task/notes.md")
        self.assertEqual(inp["nested"]["n"], 2)
        self.assertIs(inp["nested"]["ok"], False)
        self.assertEqual(inp["items"][1], "keep-me")
        self.assertEqual(inp["items"][2], 7)
        self.assertIsNone(inp["items"][3])

    def test_depth_and_size_stay_bounded(self) -> None:
        """asserts very deep or huge nesting is capped, never raises, and dropped leaves cannot leak"""
        deep: dict = {"note": "SYNTH_DEEP_PLAIN"}
        for _ in range(64):
            deep = {"wrap": deep}
        try:
            deep_rows = _rows({"password": "SYNTH_SHALLOW_PW", "tree": deep, "title": "visible"})
        except Exception as exc:  # noqa: BLE001 — the cap must not raise on deep input
            self.fail(f"deep nesting raised {exc!r}")
        deep_inp = deep_rows[0]["input"]
        self.assertEqual(deep_inp["password"], _REDACTED)
        deep_blob = _blob(deep_rows)
        self.assertNotIn("SYNTH_SHALLOW_PW", deep_blob)
        if "SYNTH_DEEP_PLAIN" in deep_blob:
            self.fail("deep nesting kept SYNTH_DEEP_PLAIN unredacted")
        self.assertIsInstance(deep_inp["tree"], dict)
        self.assertIn("wrap", deep_inp["tree"])
        self.assertLessEqual(_depth(deep_inp), 20)
        self.assertEqual(deep_inp["title"], "visible")

        items = [f"keep-{i}" for i in range(5000)]
        items.append("SYNTH_OVERFLOW_PLAIN")
        try:
            wide_rows = _rows({"items": items, "password": "SYNTH_WIDE_PW"})
        except Exception as exc:  # noqa: BLE001
            self.fail(f"wide nesting raised {exc!r}")
        wide_inp = wide_rows[0]["input"]
        wide_blob = _blob(wide_rows)
        self.assertEqual(wide_inp["password"], _REDACTED)
        if "SYNTH_OVERFLOW_PLAIN" in wide_blob:
            self.fail("oversized list kept SYNTH_OVERFLOW_PLAIN")
        self.assertNotIn("SYNTH_WIDE_PW", wide_blob)
        self.assertLessEqual(len(wide_inp["items"]), 64)
        self.assertEqual(wide_inp["items"][0], "keep-0")

        try:
            huge_rows = _rows({
                "password": {"blob": "Z" * 100000, "note": "SYNTH_UNDER_PASSWORD"},
                "title": "visible",
            })
        except Exception as exc:  # noqa: BLE001
            self.fail(f"sensitive huge value raised {exc!r}")
        huge_inp = huge_rows[0]["input"]
        huge_blob = _blob(huge_rows)
        self.assertEqual(huge_inp["password"], _REDACTED)
        if "SYNTH_UNDER_PASSWORD" in huge_blob:
            self.fail("value under password kept SYNTH_UNDER_PASSWORD")
        self.assertLess(len(huge_blob), 5000)
        self.assertEqual(huge_inp["title"], "visible")

        matrix = [[f"row-{i}-{j}" for j in range(40)] for i in range(40)]
        matrix[-1][-1] = "SYNTH_BUSHY_PLAIN"
        try:
            bushy_rows = _rows({"matrix": matrix})
        except Exception as exc:  # noqa: BLE001
            self.fail(f"bushy nesting raised {exc!r}")
        bushy_inp = bushy_rows[0]["input"]
        if "SYNTH_BUSHY_PLAIN" in _blob(bushy_rows):
            self.fail("bushy nesting kept SYNTH_BUSHY_PLAIN")
        self.assertLessEqual(_count_nodes(bushy_inp), 400)
        self.assertEqual(bushy_inp["matrix"][0][0], "row-0-0")

    def test_read_tool_evidence_redacts_transcript_file(self) -> None:
        """asserts read_tool_evidence redacts sensitive values from a synthetic transcript file"""
        args = {
            "api_key": "SYNTH_A",
            "nested": {"password": "SYNTH_B"},
            "list": [{"token": "SYNTH_C"}],
            "Cwd": "/w/task",
        }
        text = _transcript(args)
        direct = _calls_from_transcript(text, 30, 2000, 1000)
        with tempfile.TemporaryDirectory(prefix="agy-redact-") as tmp:
            home = Path(tmp) / "home"
            dest = (
                home / ".gemini" / "antigravity-cli" / "brain" / "convredact"
                / ".system_generated" / "logs" / "transcript_full.jsonl"
            )
            dest.parent.mkdir(parents=True)
            dest.write_text(text, encoding="utf-8")
            loaded = read_tool_evidence(
                {"HOME": str(home)},
                "convredact",
                max_calls=30,
                output_cap=2000,
                input_cap=1000,
            )
        self.assertEqual(loaded, direct)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        inp = loaded[0]["input"]
        self.assertEqual(inp["api_key"], _REDACTED)
        self.assertEqual(inp["nested"]["password"], _REDACTED)
        self.assertEqual(inp["list"][0]["token"], _REDACTED)
        self.assertEqual(inp["Cwd"], "/w/task")
        blob = _blob(loaded)
        self.assertNotIn("SYNTH_A", blob)
        self.assertNotIn("SYNTH_B", blob)
        self.assertNotIn("SYNTH_C", blob)

    def test_build_payload_carries_redacted_tool_evidence(self) -> None:
        """asserts build_payload tool_evidence carries the redacted tool inputs and no synthetic secrets"""
        args, secrets = _sensitive_args()
        rows = _rows(args)
        payload = build_payload(
            _REVIEW,
            _SNAP,
            acceptance_text="prose acceptance",
            response_excerpt="worker-ok",
            tools=rows,
        )
        self.assertEqual(payload["tool_evidence"], {"source": "agy_transcript", "calls": 1})
        self.assertEqual(payload["tools"], rows)
        inp = payload["tools"][0]["input"]
        self.assertEqual(inp["api_key"], _REDACTED)
        self.assertEqual(inp["nested"]["password"], _REDACTED)
        self.assertEqual(inp["list"][0]["token"], _REDACTED)
        blob = _blob(payload)
        self.assertNotIn("SYNTH_A", blob)
        self.assertNotIn("SYNTH_B", blob)
        self.assertNotIn("SYNTH_C", blob)
        for _, secret in secrets:
            self.assertNotIn(secret, blob)
        self.assertIn("[redacted]", blob)


if __name__ == "__main__":
    unittest.main()
