#!/usr/bin/env python3
"""Simulated Codex CLI lead adapter tests. No real codex binary, no credentials."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from lead_adapter import (
    CodexCliLeadAdapter,
    InProcessLeadAdapter,
    LeadDecisionError,
    build_lead_request,
    get_lead_adapter,
    lead_permission_response_schema,
    lead_review_response_schema,
    validate_lead_decision,
)
from lead_adapter.codex_cli import (
    EXAMPLE_WINDOWS_CODEX_BIN,
    FORBIDDEN_FLAGS,
    TEMP_PREFIX,
    CodexArgvRejected,
    CodexBinNotFound,
    build_command,
    codex_output_schema,
    find_forbidden_flag,
    redact_secrets,
    resolve_codex_bin,
    spawn_spec,
)
from lead_adapter.schema import pin_lead_response_schema

SIMULATED = True

_FAKE_PY = r"""
import json
import os
import sys
import time
from pathlib import Path

argv = sys.argv[1:]
record = os.environ.get("COLLAB_CODEX_FAKE_RECORD", "")
if not record:
    sys.stderr.write("COLLAB_CODEX_FAKE_RECORD missing\n")
    raise SystemExit(2)
rec = Path(record)
rec.mkdir(parents=True, exist_ok=True)


def flag(name):
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return ""


stdin_text = sys.stdin.read()
(rec / "stdin.txt").write_text(stdin_text, encoding="utf-8")
(rec / "argv.json").write_text(json.dumps(argv), encoding="utf-8")
schema_path = flag("--output-schema")
schema_text = ""
if schema_path:
    try:
        schema_text = Path(schema_path).read_text(encoding="utf-8")
    except OSError as exc:
        schema_text = "READ_ERROR " + str(exc)
(rec / "schema.json").write_text(schema_text, encoding="utf-8")
count_path = rec / "count"
n = int(count_path.read_text(encoding="utf-8")) if count_path.exists() else 0
count_path.write_text(str(n + 1), encoding="utf-8")
(rec / "pid").write_text(str(os.getpid()), encoding="utf-8")

delay = (os.environ.get("COLLAB_CODEX_FAKE_SLEEP") or "").strip()
if delay:
    time.sleep(float(delay))

out_path = flag("-o")
skip = os.environ.get("COLLAB_CODEX_FAKE_SKIP_LAST") == "1"
reply = os.environ.get("COLLAB_CODEX_FAKE_REPLY", "")
if out_path and not skip:
    Path(out_path).write_text(reply, encoding="utf-8")
stdout_text = os.environ.get("COLLAB_CODEX_FAKE_STDOUT", "")
if stdout_text:
    sys.stdout.write(stdout_text)
sys.stderr.write(os.environ.get("COLLAB_CODEX_FAKE_STDERR", ""))
raise SystemExit(int(os.environ.get("COLLAB_CODEX_FAKE_EXIT", "0") or "0"))
"""


def _req(kind: str = "permission") -> dict:
    return build_lead_request(
        kind=kind,
        goal="stay inside the workspace",
        authorized_scope=["ws"],
        prohibitions=["secrets"],
        acceptance_criteria={"artifacts": ["a.txt"]},
        current_application={"tool": "write", "path": "a.txt"},
    )


def _permission_json(req: dict, *, decision: str = "once", application_id: str | None = None) -> str:
    body = {
        "application_id": req["application_id"] if application_id is None else application_id,
        "context_summary": req["context_summary"],
        "decision": decision,
        "reason": "in scope",
        "safe_path_hint": "",
    }
    return json.dumps(body)


def _review_json(req: dict, *, verdict: str = "pass", application_id: str | None = None) -> str:
    body = {
        "application_id": req["application_id"] if application_id is None else application_id,
        "context_summary": req["context_summary"],
        "verdict": verdict,
        "reason": "artifacts present",
    }
    return json.dumps(body)


def _schema_has_key(obj, key: str) -> bool:
    if isinstance(obj, dict):
        if key in obj:
            return True
        return any(_schema_has_key(v, key) for v in obj.values())
    if isinstance(obj, list):
        return any(_schema_has_key(v, key) for v in obj)
    return False


def _assert_clean_argv(test: unittest.TestCase, argv: list[str]) -> None:
    test.assertEqual(argv[1], "exec")
    test.assertEqual(argv[2:6], ["--sandbox", "read-only", "-c", "approval_policy=never"])
    test.assertEqual(
        argv[6:10],
        ["--ephemeral", "--skip-git-repo-check", "--color", "never"],
    )
    test.assertEqual(argv[10], "-C")
    test.assertEqual(argv[12], "--output-schema")
    test.assertTrue(str(argv[13]).endswith("schema.json"))
    test.assertEqual(argv[14], "-o")
    test.assertTrue(str(argv[15]).endswith("last.json"))
    test.assertEqual(argv[-1], "-")
    test.assertEqual(len(argv), 17)
    test.assertIsNone(find_forbidden_flag(argv))
    joined = " ".join(argv)
    for flag in FORBIDDEN_FLAGS:
        if " " in flag:
            test.assertNotIn(flag, joined)
        else:
            test.assertNotIn(flag, argv)


class TestPureHelpers(unittest.TestCase):
    def test_build_command_shape(self):
        argv = build_command(
            "codex",
            cwd="/work",
            schema_path="/tmp/schema.json",
            last_message_path="/tmp/last.json",
        )
        self.assertEqual(
            argv,
            [
                "codex",
                "exec",
                "--sandbox",
                "read-only",
                "-c",
                "approval_policy=never",
                "--ephemeral",
                "--skip-git-repo-check",
                "--color",
                "never",
                "-C",
                "/work",
                "--output-schema",
                "/tmp/schema.json",
                "-o",
                "/tmp/last.json",
                "-",
            ],
        )
        self.assertIsNone(find_forbidden_flag(argv))
        _assert_clean_argv(self, argv)

    def test_forbidden_flag_detector(self):
        self.assertEqual(find_forbidden_flag(["codex", "--full-auto"]), "--full-auto")
        self.assertEqual(
            find_forbidden_flag(["codex", "-s", "workspace-write"]),
            "-s workspace-write",
        )
        self.assertEqual(
            find_forbidden_flag(["codex", "--sandbox", "danger-full-access"]),
            "--sandbox danger-full-access",
        )
        self.assertIsNone(find_forbidden_flag(["codex", "--sandbox", "read-only"]))

    def test_codex_output_schema_strict(self):
        req = _req("permission")
        pinned = pin_lead_response_schema(lead_permission_response_schema(), req)
        out = codex_output_schema(pinned)
        self.assertEqual(pinned["properties"]["application_id"]["const"], req["application_id"])
        self.assertNotIn("enum", pinned["properties"]["application_id"])
        props = out["properties"]
        self.assertEqual(props["application_id"]["enum"], [req["application_id"]])
        self.assertEqual(props["application_id"]["type"], "string")
        self.assertNotIn("const", props["application_id"])
        self.assertEqual(props["context_summary"]["enum"], [req["context_summary"]])
        self.assertEqual(out["required"], list(props.keys()))
        self.assertIn("safe_path_hint", out["required"])
        self.assertIs(out["additionalProperties"], False)
        self.assertFalse(_schema_has_key(out, "const"))
        review = codex_output_schema(
            pin_lead_response_schema(lead_review_response_schema(), _req("review"))
        )
        self.assertNotIn("safe_path_hint", review["properties"])
        self.assertEqual(review["required"], list(review["properties"].keys()))
        self.assertIs(review["additionalProperties"], False)

    def test_redact_secrets_masks_tokens_only(self):
        plain = "codex_cli spawn failed: [Errno 2] No such file /tmp/codex"
        self.assertEqual(redact_secrets(plain), plain)
        secret = "sk-" + ("A" * 20)
        bearer = "Bearer " + ("B" * 24)
        hexrun = "ab" * 16  # 32 hex chars
        b64 = "Ab9+" * 10  # 40 chars, not pure hex
        text = f"fail {secret} then {bearer} then {hexrun} and {b64} end"
        out = redact_secrets(text)
        self.assertNotIn(secret, out)
        self.assertNotIn("B" * 24, out)
        self.assertNotIn(hexrun, out)
        self.assertNotIn(b64, out)
        self.assertIn("sk-***", out)
        self.assertIn("Bearer ***", out)
        self.assertIn("fail", out)
        self.assertIn("end", out)

    def test_spawn_spec_windows_cmd_string(self):
        comspec = "cmd.exe"
        argv = build_command(
            r"C:\Users\Admin\.local\share\TeleAgent\runtimes\node\codex.cmd",
            cwd=r"C:\work space",
            schema_path=r"C:\temp dir\schema.json",
            last_message_path=r"C:\temp dir\last.json",
        )
        args, use_string = spawn_spec(argv, platform="win32", comspec=comspec)
        self.assertTrue(use_string)
        self.assertIsInstance(args, str)
        self.assertTrue(args.startswith(f'{comspec} /d /s /c "'), args)
        self.assertIn("codex.cmd", args)
        self.assertIn("work space", args)
        self.assertTrue(args.endswith('"'))

    def test_spawn_spec_cmd_case_and_non_batch(self):
        args, use_string = spawn_spec(
            [r"C:\Tools\CODEX.CMD", "exec"],
            platform="win32",
            comspec=r"C:\Windows\System32\cmd.exe",
        )
        self.assertTrue(use_string)
        self.assertTrue(
            args.startswith('C:\\Windows\\System32\\cmd.exe /d /s /c "'),
            args,
        )
        listed, use_string = spawn_spec(
            [r"C:\Tools\codex.exe", "exec", "a&b"],
            platform="win32",
        )
        self.assertFalse(use_string)
        self.assertEqual(listed, [r"C:\Tools\codex.exe", "exec", "a&b"])
        listed, use_string = spawn_spec(
            [r"C:\Tools\codex.cmd", "a&b"],
            platform="linux",
        )
        self.assertFalse(use_string)
        self.assertIsInstance(listed, list)

    def test_spawn_spec_refuses_cmd_metacharacters(self):
        for bad in ("a&b", "a%b", "a!b", "a^b", "a|b", "a<b", "a>b", 'a"b', "a\nb", "a\rb"):
            with self.subTest(bad=bad):
                with self.assertRaises(CodexArgvRejected) as cm:
                    spawn_spec(
                        [r"C:\codex.cmd", "exec", bad],
                        platform="win32",
                        comspec="cmd.exe",
                    )
                self.assertIn(repr(bad), str(cm.exception))
        # Fixed flags carry no metacharacters, so the whole argv passes.
        args, use_string = spawn_spec(
            [r"C:\codex.cmd", "-c", "approval_policy=never"],
            platform="win32",
            comspec="cmd.exe",
        )
        self.assertTrue(use_string)
        self.assertIn('approval_policy', args)


class TestResolveCodexBin(unittest.TestCase):
    def test_order_codex_env_beats_lead_env_and_path(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            specific = root / "from-codex-env"
            lead = root / "from-lead-env"
            path_bin = root / "from-path"
            for p in (specific, lead, path_bin):
                p.write_text("x", encoding="utf-8")
            got = resolve_codex_bin(
                env={
                    "COLLAB_CODEX_LEAD_BIN": str(specific),
                    "COLLAB_LEAD_BIN": str(lead),
                },
                which=lambda name: str(path_bin) if name == "codex" else None,
                platform="linux",
            )
            self.assertEqual(got, str(specific))

    def test_missing_specific_falls_through_to_lead_then_path(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            lead = root / "lead-codex"
            path_bin = root / "path-codex"
            lead.write_text("x", encoding="utf-8")
            path_bin.write_text("x", encoding="utf-8")
            missing = str(root / "nope")
            got = resolve_codex_bin(
                env={
                    "COLLAB_CODEX_LEAD_BIN": missing,
                    "COLLAB_LEAD_BIN": str(lead),
                },
                which=lambda name: str(path_bin) if name == "codex" else None,
                platform="linux",
            )
            self.assertEqual(got, str(lead))
            got_path = resolve_codex_bin(
                env={
                    "COLLAB_CODEX_LEAD_BIN": "  ",
                    "COLLAB_LEAD_BIN": missing,
                },
                which=lambda name: str(path_bin) if name == "codex" else None,
                platform="linux",
            )
            self.assertEqual(got_path, str(path_bin))

    def test_windows_path_order_and_isfile(self):
        calls: list[str] = []

        def which(name: str) -> str | None:
            calls.append(name)
            return {
                "codex": r"C:\missing\codex",
                "codex.cmd": r"C:\bin\codex.cmd",
                "codex.exe": r"C:\bin\codex.exe",
            }.get(name)

        def is_file(path: str) -> bool:
            return path.endswith("codex.cmd") or path.endswith("codex.exe")

        got = resolve_codex_bin(
            env={},
            which=which,
            platform="win32",
            is_file=is_file,
        )
        self.assertEqual(got, r"C:\bin\codex.cmd")
        self.assertEqual(calls, ["codex", "codex.cmd"])

    def test_linux_does_not_probe_cmd_or_exe(self):
        calls: list[str] = []

        def which(name: str) -> str | None:
            calls.append(name)
            return "/usr/bin/codex" if name == "codex" else None

        got = resolve_codex_bin(
            env={},
            which=which,
            platform="linux",
            is_file=lambda p: p == "/usr/bin/codex",
        )
        self.assertEqual(got, "/usr/bin/codex")
        self.assertEqual(calls, ["codex"])

    def test_not_found_lists_tried_and_example(self):
        with self.assertRaises(CodexBinNotFound) as cm:
            resolve_codex_bin(
                env={
                    "COLLAB_CODEX_LEAD_BIN": r"C:\missing\codex.cmd",
                    "COLLAB_LEAD_BIN": r"C:\missing\grok.exe",
                },
                which=lambda name: None,
                platform="win32",
                is_file=lambda p: False,
            )
        msg = str(cm.exception)
        self.assertIn("COLLAB_CODEX_LEAD_BIN", msg)
        self.assertIn("COLLAB_LEAD_BIN", msg)
        self.assertIn("PATH codex", msg)
        self.assertIn("PATH codex.cmd", msg)
        self.assertIn("PATH codex.exe", msg)
        self.assertIn(EXAMPLE_WINDOWS_CODEX_BIN, msg)
        self.assertIn("0.155.0", msg)

    def test_which_hit_ignored_when_not_a_file(self):
        with self.assertRaises(CodexBinNotFound):
            resolve_codex_bin(
                env={},
                which=lambda name: "/tmp/not-really-codex" if name == "codex" else None,
                platform="linux",
                is_file=lambda p: False,
            )


class _FakeCodex:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.record = root / "record"
        self.record.mkdir()
        self.work = root / "work"
        self.work.mkdir()
        self.script = root / "fake_codex.py"
        self.script.write_text(_FAKE_PY, encoding="utf-8")
        self.shim = root / "codex"
        self.shim.write_text(
            "#!/bin/sh\n"
            f"exec {shlex.quote(sys.executable)} {shlex.quote(str(self.script))} "
            '"$0" "$@"\n',
            encoding="utf-8",
        )
        self.shim.chmod(0o755)

    def clear_record(self) -> None:
        for child in self.record.iterdir():
            if child.is_file():
                child.unlink()

    def env(self, **opts: str) -> dict[str, str]:
        return {
            "COLLAB_CODEX_FAKE_RECORD": str(self.record),
            "COLLAB_CODEX_FAKE_REPLY": opts.get("reply", ""),
            "COLLAB_CODEX_FAKE_STDOUT": opts.get("stdout", ""),
            "COLLAB_CODEX_FAKE_STDERR": opts.get("stderr", ""),
            "COLLAB_CODEX_FAKE_EXIT": opts.get("exit", "0"),
            "COLLAB_CODEX_FAKE_SLEEP": opts.get("sleep", ""),
            "COLLAB_CODEX_FAKE_SKIP_LAST": opts.get("skip_last", ""),
        }

    def recorded_argv(self) -> list[str]:
        return json.loads((self.record / "argv.json").read_text(encoding="utf-8"))

    def recorded_schema(self) -> dict:
        return json.loads((self.record / "schema.json").read_text(encoding="utf-8"))

    def recorded_stdin(self) -> str:
        return (self.record / "stdin.txt").read_text(encoding="utf-8")

    def count(self) -> int:
        path = self.record / "count"
        if not path.exists():
            return 0
        return int(path.read_text(encoding="utf-8"))


class TestCodexCliDecide(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="codex-lead-test-")
        self.fake = _FakeCodex(Path(self._tmp.name))
        self.ad = CodexCliLeadAdapter(bin_path=str(self.fake.shim))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _decide(self, req, schema, *, cwd=None, timeout_sec=30, **opts):
        self.fake.clear_record()
        calls: list[tuple] = []
        real = subprocess.Popen

        def wrapped(args, **kwargs):
            calls.append((args, kwargs))
            return real(args, **kwargs)

        created: list[str] = []
        real_mkdtemp = tempfile.mkdtemp

        def mkdtemp_wrap(*a, **k):
            path = real_mkdtemp(*a, **k)
            created.append(path)
            return path

        with mock.patch.dict(os.environ, self.fake.env(**opts), clear=False):
            with mock.patch("lead_adapter.codex_cli.subprocess.Popen", wrapped):
                with mock.patch("lead_adapter.codex_cli.tempfile.mkdtemp", mkdtemp_wrap):
                    t0 = time.monotonic()
                    raw, parsed = self.ad.decide(
                        req,
                        schema=schema,
                        cwd=str(self.fake.work if cwd is None else cwd),
                        timeout_sec=timeout_sec,
                    )
                    elapsed = time.monotonic() - t0
        return raw, parsed, calls, created, elapsed

    def _assert_failed(self, raw, parsed, req, *, kind, code):
        self.assertNotIn("decision", parsed)
        self.assertNotIn("verdict", parsed)
        with self.assertRaises(LeadDecisionError) as cm:
            validate_lead_decision(raw, parsed, request=req, kind=kind)
        self.assertEqual(cm.exception.code, code)

    def _assert_temp_gone(self, created: list[str]) -> None:
        self.assertTrue(created, "expected a temp dir")
        for path in created:
            self.assertTrue(Path(path).name.startswith(TEMP_PREFIX))
            self.assertFalse(os.path.exists(path), path)

    def test_legal_permission_and_review(self):
        hint = self.ad.doctor_hint()
        self.assertEqual(hint["status"], "wired_unverified")
        self.assertEqual(hint["name"], "codex_cli")
        self.assertFalse(hint["fake_pass"])
        self.assertFalse(hint["live_verified"])
        self.assertEqual(hint["sandbox"], "read-only")
        self.assertEqual(hint["approval_policy"], "never")
        self.assertEqual(hint["bin_path"], str(self.fake.shim))
        self.assertIsNone(hint["bin_error"])

        cases = (
            ("permission", lead_permission_response_schema(), _permission_json, "once", "decision"),
            ("review", lead_review_response_schema(), _review_json, "pass", "verdict"),
        )
        for kind, schema, builder, expect, field in cases:
            with self.subTest(kind=kind):
                req = _req(kind)
                raw, parsed, calls, created, _elapsed = self._decide(
                    req, schema, reply=builder(req)
                )
                self.assertEqual(len(calls), 1)
                self.assertEqual(self.fake.count(), 1)
                argv, kwargs = calls[0]
                self.assertIsInstance(argv, list)
                self.assertFalse(kwargs.get("shell", False))
                self.assertTrue(kwargs.get("start_new_session"))
                self.assertEqual(kwargs.get("encoding"), "utf-8")
                self.assertEqual(kwargs.get("errors"), "replace")
                self.assertIs(kwargs.get("stdin"), subprocess.PIPE)
                _assert_clean_argv(self, argv)
                self.assertEqual(argv[0], str(self.fake.shim))
                self.assertEqual(argv[11], str(self.fake.work))
                self.assertEqual(
                    argv,
                    build_command(
                        str(self.fake.shim),
                        cwd=str(self.fake.work),
                        schema_path=argv[13],
                        last_message_path=argv[15],
                    ),
                )
                stdin = self.fake.recorded_stdin()
                self.assertIn(req["application_id"], stdin)
                self.assertIn("ONLY one JSON object", stdin)
                self.assertIn("do not run commands or edit files", stdin.lower())
                schema_obj = self.fake.recorded_schema()
                self.assertEqual(
                    schema_obj["properties"]["application_id"]["enum"],
                    [req["application_id"]],
                )
                self.assertEqual(
                    schema_obj["properties"]["context_summary"]["enum"],
                    [req["context_summary"]],
                )
                self.assertEqual(schema_obj["required"], list(schema_obj["properties"].keys()))
                self.assertIs(schema_obj["additionalProperties"], False)
                self.assertFalse(_schema_has_key(schema_obj, "const"))
                if kind == "permission":
                    self.assertIn("safe_path_hint", schema_obj["required"])
                    self.assertNotIn("safe_path_hint", parsed)
                self.assertEqual(parsed[field], expect)
                # raw is the last.json text; dropping empty safe_path_hint does not rewrite it.
                self.assertEqual(raw, builder(req))
                out = validate_lead_decision(raw, parsed, request=req, kind=kind)
                self.assertEqual(out[field], expect)
                self.assertEqual(out["application_id"], req["application_id"])
                self.assertEqual(out["context_summary"], req["context_summary"])
                self._assert_temp_gone(created)

    def test_legacy_prompt_prefix_and_allow_hint(self):
        req = _req("permission")
        req["extra"] = {"legacy_prompt": "LEGACY-PREFIX-XYZ", "allow_hint": "HINT-ALLOW-99"}
        raw, parsed, calls, created, _elapsed = self._decide(
            req,
            lead_permission_response_schema(),
            reply=_permission_json(req),
        )
        self.assertEqual(len(calls), 1)
        stdin = self.fake.recorded_stdin()
        self.assertTrue(stdin.startswith("LEGACY-PREFIX-XYZ\n\n"))
        self.assertIn("HINT-ALLOW-99", stdin)
        self.assertIn(req["application_id"], stdin)
        self.assertTrue(stdin.strip().endswith("Do not run commands or edit files."))
        out = validate_lead_decision(raw, parsed, request=req, kind="permission")
        self.assertEqual(out["decision"], "once")
        self._assert_temp_gone(created)

    def test_missing_cwd_uses_temp_dir(self):
        req = _req("permission")
        raw, parsed, calls, created, _elapsed = self._decide(
            req,
            lead_permission_response_schema(),
            cwd="/no/such/codex-lead-cwd",
            reply=_permission_json(req),
        )
        argv = calls[0][0]
        self.assertEqual(len(calls), 1)
        self.assertEqual(argv[11], created[0])
        self.assertEqual(Path(argv[13]).parent, Path(created[0]))
        validate_lead_decision(raw, parsed, request=req, kind="permission")
        self._assert_temp_gone(created)

    def test_application_id_mismatch(self):
        req = _req("permission")
        bad_id = req["application_id"] + "-other"
        raw, parsed, calls, created, _elapsed = self._decide(
            req,
            lead_permission_response_schema(),
            reply=_permission_json(req, application_id=bad_id),
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.fake.count(), 1)
        self.assertEqual(raw, "ILLEGAL_OUTPUT")
        self.assertEqual(parsed["_lead_status"], "error")
        self.assertEqual(parsed["lead_error_code"], "application_id_mismatch")
        self.assertIn(req["application_id"], parsed["error"])
        self.assertIn(bad_id, parsed["error"])
        self.assertNotIn("once", json.dumps(parsed))
        self._assert_failed(raw, parsed, req, kind="permission", code="call_failed")
        self._assert_temp_gone(created)

    def test_illegal_output_shapes(self):
        req = _req("permission")
        legal = _permission_json(req)
        prose = "word " * 2000
        cases = {
            "prose": prose,
            "json_in_prose": "Here you go, decision follows: " + legal,
            "code_fence": "```json\n" + legal + "\n```",
            "array": json.dumps([json.loads(legal)]),
            "empty": "",
        }
        for name, reply in cases.items():
            with self.subTest(name=name):
                opts = {"reply": reply}
                if name == "empty":
                    opts["skip_last"] = "1"
                raw, parsed, calls, created, _elapsed = self._decide(
                    req, lead_permission_response_schema(), **opts
                )
                self.assertEqual(len(calls), 1, name)
                self.assertEqual(self.fake.count(), 1, name)
                self.assertEqual(raw, "ILLEGAL_OUTPUT")
                self.assertEqual(parsed.get("_lead_status"), "error")
                self.assertEqual(parsed.get("lead_error_code"), "illegal_output")
                self.assertTrue(parsed["error"].startswith("codex_cli illegal output:"))
                self.assertLess(len(parsed["error"]), 500)
                if name == "prose":
                    self.assertIn("word " * 40, parsed["error"])
                    self.assertNotIn("word " * 61, parsed["error"])
                self._assert_failed(raw, parsed, req, kind="permission", code="call_failed")
                self._assert_temp_gone(created)

    def test_stdout_fallback_and_stderr_ignored(self):
        req = _req("permission")
        legal = _permission_json(req)
        raw, parsed, calls, created, _elapsed = self._decide(
            req,
            lead_permission_response_schema(),
            reply="",
            skip_last="1",
            stdout=legal,
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(parsed["decision"], "once")
        validate_lead_decision(raw, parsed, request=req, kind="permission")
        self._assert_temp_gone(created)

        raw, parsed, calls, created, _elapsed = self._decide(
            req,
            lead_permission_response_schema(),
            reply="",
            stdout=legal,
        )
        self.assertEqual(parsed["decision"], "once")
        self.assertEqual(len(calls), 1)
        self._assert_temp_gone(created)

        # Non-empty garbage in last.json is not rescued by a valid stdout.
        raw, parsed, calls, created, _elapsed = self._decide(
            req,
            lead_permission_response_schema(),
            reply="not json",
            stdout=legal,
            stderr=legal,
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(raw, "ILLEGAL_OUTPUT")
        self._assert_failed(raw, parsed, req, kind="permission", code="call_failed")
        self.assertNotIn("once", json.dumps({k: parsed[k] for k in parsed if k != "error"}))
        self._assert_temp_gone(created)

    def test_timeout_kills_and_returns_promptly(self):
        req = _req("review")
        raw, parsed, calls, created, elapsed = self._decide(
            req,
            lead_review_response_schema(),
            reply=_review_json(req),
            sleep="30",
            timeout_sec=1.0,
        )
        self.assertLess(elapsed, 6.0, f"timeout path took {elapsed:.2f}s")
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.fake.count(), 1)
        self.assertEqual(raw, "TIMEOUT")
        self.assertEqual(parsed["_lead_status"], "timeout")
        self.assertIn("codex_cli timeout after", parsed["error"])
        self.assertIn("1s", parsed["error"])
        self._assert_failed(raw, parsed, req, kind="review", code="timeout")
        self._assert_temp_gone(created)

    def test_nonzero_exit_not_trusted(self):
        req = _req("permission")
        secret = "sk-" + ("C" * 16)
        stderr = f"sandbox exploded {secret} near the end"
        raw, parsed, calls, created, _elapsed = self._decide(
            req,
            lead_permission_response_schema(),
            reply=_permission_json(req),
            stderr=stderr,
            exit="3",
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.fake.count(), 1)
        self.assertEqual(raw, "CALL_FAILED")
        self.assertEqual(parsed["_lead_status"], "call_failed")
        self.assertEqual(parsed["returncode"], 3)
        self.assertNotIn(secret, parsed["error"])
        self.assertIn("sk-***", parsed["error"])
        self.assertNotIn("once", json.dumps(parsed))
        self._assert_failed(raw, parsed, req, kind="permission", code="call_failed")
        self._assert_temp_gone(created)

        long_err = "boom " * 400  # 2000 chars, no 32-char token
        raw, parsed, calls, created, _elapsed = self._decide(
            req,
            lead_permission_response_schema(),
            reply=_permission_json(req),
            stderr=long_err,
            stdout=_permission_json(req),
            exit="1",
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(parsed["error"], long_err.strip()[-1500:])
        self.assertEqual(len(parsed["error"]), 1500)
        self.assertEqual(parsed["returncode"], 1)
        self._assert_failed(raw, parsed, req, kind="permission", code="call_failed")
        self._assert_temp_gone(created)

        raw, parsed, calls, _created, _elapsed = self._decide(
            req,
            lead_permission_response_schema(),
            reply=_permission_json(req),
            stderr="",
            exit="2",
        )
        self.assertEqual(parsed["error"], "exit=2")
        self.assertEqual(parsed["returncode"], 2)
        self.assertEqual(len(calls), 1)

    def test_forbidden_flag_does_not_spawn(self):
        req = _req("permission")
        real = build_command

        def wrapped(*a, **k):
            argv = real(*a, **k)
            return argv[:2] + ["--full-auto"] + argv[2:]

        created: list[str] = []
        real_mkdtemp = tempfile.mkdtemp

        def mkdtemp_wrap(*a, **k):
            path = real_mkdtemp(*a, **k)
            created.append(path)
            return path

        with mock.patch("lead_adapter.codex_cli.build_command", wrapped):
            with mock.patch("lead_adapter.codex_cli.subprocess.Popen") as popen:
                with mock.patch("lead_adapter.codex_cli.tempfile.mkdtemp", mkdtemp_wrap):
                    raw, parsed = self.ad.decide(
                        req,
                        schema=lead_permission_response_schema(),
                        cwd=str(self.fake.work),
                    )
        popen.assert_not_called()
        self.assertEqual(self.fake.count(), 0)
        self.assertEqual(parsed["_lead_status"], "call_failed")
        self.assertIn("--full-auto", parsed["error"])
        self._assert_failed(raw, parsed, req, kind="permission", code="call_failed")
        self._assert_temp_gone(created)

    def test_cmd_metachar_refused_before_spawn(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cmd_bin = root / "codex.cmd"
            cmd_bin.write_text("@echo off\r\n", encoding="utf-8")
            bad_amp = root / "bad&dir"
            bad_pct = root / "bad%dir"
            bad_amp.mkdir()
            bad_pct.mkdir()
            ad = CodexCliLeadAdapter(bin_path=str(cmd_bin), platform="win32")
            req = _req("permission")
            for folder, needle in ((bad_amp, "&"), (bad_pct, "%")):
                with self.subTest(needle=needle):
                    created: list[str] = []
                    real_mkdtemp = tempfile.mkdtemp

                    def mkdtemp_wrap(*a, **k):
                        path = real_mkdtemp(*a, **k)
                        created.append(path)
                        return path

                    with mock.patch("lead_adapter.codex_cli.subprocess.Popen") as popen:
                        with mock.patch(
                            "lead_adapter.codex_cli.tempfile.mkdtemp", mkdtemp_wrap
                        ):
                            raw, parsed = ad.decide(
                                req,
                                schema=lead_permission_response_schema(),
                                cwd=str(folder),
                            )
                    popen.assert_not_called()
                    self.assertEqual(parsed["_lead_status"], "call_failed")
                    self.assertIn(str(folder), parsed["error"])
                    self.assertIn(needle, parsed["error"])
                    self._assert_failed(raw, parsed, req, kind="permission", code="call_failed")
                    self._assert_temp_gone(created)


class TestLazyMissingAndAlias(unittest.TestCase):
    def test_factory_and_constructor_do_not_resolve(self):
        with mock.patch(
            "lead_adapter.codex_cli.resolve_codex_bin",
            side_effect=AssertionError("eager resolve"),
        ):
            ad = CodexCliLeadAdapter()
            ad2 = get_lead_adapter("codex_cli")
            ad3 = get_lead_adapter("codex-cli")
        self.assertIsInstance(ad, CodexCliLeadAdapter)
        self.assertIsInstance(ad2, CodexCliLeadAdapter)
        self.assertIsInstance(ad3, CodexCliLeadAdapter)

    def test_missing_binary_lazy_call_failed(self):
        missing = "/no/such/codex-lead-bin"
        with mock.patch.dict(
            os.environ,
            {"COLLAB_CODEX_LEAD_BIN": "", "COLLAB_LEAD_BIN": missing},
            clear=False,
        ):
            with mock.patch("lead_adapter.codex_cli.shutil.which", return_value=None):
                ad = get_lead_adapter("codex_cli")
                hint = ad.doctor_hint()
                self.assertEqual(hint["status"], "wired_unverified")
                self.assertIsNone(hint["bin_path"])
                self.assertTrue(hint["bin_error"])
                self.assertIn("not found", hint["bin_error"].lower())
                self.assertIn(EXAMPLE_WINDOWS_CODEX_BIN, hint["bin_error"])
                self.assertFalse(hint["fake_pass"])
                self.assertFalse(hint["live_verified"])
                req = _req("review")
                with mock.patch("lead_adapter.codex_cli.subprocess.Popen") as popen:
                    with mock.patch("lead_adapter.codex_cli.tempfile.mkdtemp") as mkd:
                        raw, parsed = ad.decide(
                            req, schema=lead_review_response_schema(), cwd="/tmp"
                        )
                popen.assert_not_called()
                mkd.assert_not_called()
        self.assertEqual(parsed["_lead_status"], "call_failed")
        self.assertNotIn("decision", parsed)
        self.assertNotIn("verdict", parsed)
        with self.assertRaises(LeadDecisionError) as cm:
            validate_lead_decision(raw, parsed, request=req, kind="review")
        self.assertEqual(cm.exception.code, "call_failed")

        forced = get_lead_adapter("codex_cli", bin_path=missing)
        hint2 = forced.doctor_hint()
        self.assertIsNone(hint2["bin_path"])
        self.assertIn(missing, hint2["bin_error"])
        with mock.patch("lead_adapter.codex_cli.subprocess.Popen") as popen:
            raw2, parsed2 = forced.decide(
                req, schema=lead_review_response_schema(), cwd="/tmp"
            )
        popen.assert_not_called()
        self.assertEqual(parsed2["_lead_status"], "call_failed")
        self.assertNotIn("verdict", parsed2)

    def test_env_kind_codex_cli(self):
        with tempfile.TemporaryDirectory() as d:
            shim = Path(d) / "codex"
            shim.write_text("#!/bin/sh\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"COLLAB_LEAD_ADAPTER": "codex_cli"}, clear=False):
                ad = get_lead_adapter(bin_path=str(shim))
            self.assertIsInstance(ad, CodexCliLeadAdapter)
            self.assertEqual(ad.doctor_hint()["bin_path"], str(shim))

    def test_codex_alias_warns_but_stays_inprocess(self):
        with self.assertLogs("lead_adapter", level="WARNING") as cm:
            ad = get_lead_adapter("codex")
        self.assertIsInstance(ad, InProcessLeadAdapter)
        self.assertEqual(ad.name, "inprocess")
        self.assertTrue(any("codex_cli" in line for line in cm.output))
        self.assertTrue(any("要用 Codex CLI 请设 codex_cli" in line for line in cm.output))

        with mock.patch.dict(os.environ, {"COLLAB_LEAD_ADAPTER": "codex"}, clear=False):
            with self.assertLogs("lead_adapter", level="WARNING") as cm_env:
                ad_env = get_lead_adapter()
        self.assertEqual(ad_env.name, "inprocess")
        self.assertTrue(any("codex_cli" in line for line in cm_env.output))

        with self.assertNoLogs("lead_adapter", level="WARNING"):
            quiet = get_lead_adapter("inprocess")
        self.assertEqual(quiet.name, "inprocess")

        with self.assertNoLogs("lead_adapter", level="WARNING"):
            get_lead_adapter("codex_cli", bin_path="/no/such/codex-for-alias-check")


@unittest.skipUnless(sys.platform == "win32", "cmd.exe .cmd spawn is Windows-only")
class TestWindowsCmdShim(unittest.TestCase):
    def test_cmd_shim_legal_json_space_in_temp(self):
        space_root = Path(tempfile.mkdtemp(prefix="codex space "))
        old_tempdir = tempfile.tempdir
        try:
            bin_dir = space_root / "bin dir"
            bin_dir.mkdir()
            script = bin_dir / "fake_codex.py"
            script.write_text(_FAKE_PY, encoding="utf-8")
            cmd_path = bin_dir / "codex.cmd"
            cmd_path.write_text(
                "@echo off\r\n"
                f'"{sys.executable}" "{script}" %*\r\n',
                encoding="utf-8",
            )
            record = space_root / "record"
            record.mkdir()
            work = space_root / "work dir"
            work.mkdir()
            req = _req("permission")
            reply = _permission_json(req)
            env = os.environ.copy()
            env.update(
                {
                    "COLLAB_CODEX_FAKE_RECORD": str(record),
                    "COLLAB_CODEX_FAKE_REPLY": reply,
                    "COLLAB_CODEX_FAKE_STDOUT": "",
                    "COLLAB_CODEX_FAKE_STDERR": "",
                    "COLLAB_CODEX_FAKE_EXIT": "0",
                    "COLLAB_CODEX_FAKE_SLEEP": "",
                    "COLLAB_CODEX_FAKE_SKIP_LAST": "",
                    "PYTHONUTF8": "1",
                    "PYTHONIOENCODING": "utf-8",
                }
            )
            tempfile.tempdir = str(space_root)
            ad = CodexCliLeadAdapter(bin_path=str(cmd_path))
            with mock.patch.dict(os.environ, env, clear=False):
                raw, parsed = ad.decide(
                    req,
                    schema=lead_permission_response_schema(),
                    cwd=str(work),
                    timeout_sec=30,
                )
            self.assertEqual(parsed["decision"], "once")
            out = validate_lead_decision(raw, parsed, request=req, kind="permission")
            self.assertEqual(out["decision"], "once")
            argv = json.loads((record / "argv.json").read_text(encoding="utf-8"))
            joined = " ".join(argv)
            self.assertIn("--sandbox", argv)
            self.assertIn("read-only", argv)
            self.assertIn("approval_policy=never", argv)
            self.assertNotIn("--full-auto", argv)
            self.assertIsNone(find_forbidden_flag(argv))
            self.assertIn(req["application_id"], (record / "stdin.txt").read_text(encoding="utf-8"))
            schema = json.loads((record / "schema.json").read_text(encoding="utf-8"))
            self.assertEqual(schema["properties"]["application_id"]["enum"], [req["application_id"]])
            self.assertIn("safe_path_hint", schema["required"])
            # -o path lives under the spaced temp root and was removed after decide.
            o_index = argv.index("-o")
            last_path = Path(argv[o_index + 1])
            self.assertIn(" ", str(last_path))
            self.assertTrue(str(last_path).startswith(str(space_root)))
            self.assertTrue(TEMP_PREFIX in str(last_path))
            self.assertFalse(last_path.exists())
            self.assertFalse(last_path.parent.exists())
            self.assertEqual(int((record / "count").read_text(encoding="utf-8")), 1)
        finally:
            tempfile.tempdir = old_tempdir
            shutil.rmtree(space_root, ignore_errors=True)

    def test_cmd_shim_timeout_kills_node_like_child(self):
        """cmd /c leaves the real CLI as a grandchild; timeout must kill the tree."""
        root = Path(tempfile.mkdtemp(prefix="codex-timeout-"))
        try:
            script = root / "fake_codex.py"
            script.write_text(_FAKE_PY, encoding="utf-8")
            cmd_path = root / "codex.cmd"
            cmd_path.write_text(
                "@echo off\r\n" f'"{sys.executable}" "{script}" %*\r\n',
                encoding="utf-8",
            )
            record = root / "record"
            record.mkdir()
            req = _req("review")
            env = {
                "COLLAB_CODEX_FAKE_RECORD": str(record),
                "COLLAB_CODEX_FAKE_REPLY": _review_json(req),
                "COLLAB_CODEX_FAKE_SLEEP": "60",
                "COLLAB_CODEX_FAKE_EXIT": "0",
                "COLLAB_CODEX_FAKE_SKIP_LAST": "",
                "COLLAB_CODEX_FAKE_STDOUT": "",
                "COLLAB_CODEX_FAKE_STDERR": "",
            }
            ad = CodexCliLeadAdapter(bin_path=str(cmd_path))
            t0 = time.monotonic()
            with mock.patch.dict(os.environ, env, clear=False):
                raw, parsed = ad.decide(
                    req, schema=lead_review_response_schema(), cwd=str(root), timeout_sec=3
                )
            self.assertLess(time.monotonic() - t0, 30)
            self.assertEqual(parsed.get("_lead_status"), "timeout")
            self.assertNotIn("verdict", parsed)
            pid = (record / "pid").read_text(encoding="utf-8").strip()
            listing = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, errors="replace",
            ).stdout
            self.assertNotIn(pid, listing)
            with self.assertRaises(LeadDecisionError) as cm:
                validate_lead_decision(raw, parsed, request=req, kind="review")
            self.assertEqual(cm.exception.code, "timeout")
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
