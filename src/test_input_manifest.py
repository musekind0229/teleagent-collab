"""Bounded input manifests and consistent SQLite / JSONL snapshots.

Python stdlib only. Snapshot directories stay under COLLAB_SNAPSHOT_DIR inside
the test temp dir. No chmod assertions on Windows.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import sqlite3
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from jsonschema import Draft202012Validator

from framework.app_service import AppError, CollabApplication
from framework.artifact_handoff import scheduler_task_workspace
from framework.contract_render import normalize_worker_contract, render_contract_section
from framework.input_manifest import (
    MAX_MANIFEST_BYTES,
    MAX_MANIFEST_FILES,
    InputManifestError,
    assert_sqlite_pin_allowed,
    build_client_manifest,
    clean_snapshots,
    file_snapshot,
    hash_file,
    make_snapshot_dir,
    project_input_manifest,
    sqlite_snapshot,
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "hermes-collab-request.py"
GOAL_SCHEMA = json.loads((ROOT / "contracts" / "goal.schema.json").read_text(encoding="utf-8"))
_SHA = "ab" * 32
_TOKEN = "TOKEN_DO_NOT_LEAK_991"
_TARGET = "LINK_TARGET_TOKEN_772"


def _load_client():
    spec = importlib.util.spec_from_file_location("hermes_collab_request_manifest", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HCR = _load_client()


def _link(target: Path, link: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:
        raise unittest.SkipTest(f"symlinks unavailable: {exc}") from exc


def _request(**overrides) -> dict:
    body = {
        "idempotency_key": "manifest-1",
        "client_id": "test-suite",
        "title": "stage inputs",
        "goal": "Use the staged manifest copies inside the assigned workspace.",
        "boundaries": {
            "must": ["Stay inside the assigned workspace"],
            "must_not": ["Do not use network or system tools"],
        },
        "acceptance": {"artifacts": ["delivery.txt"], "text": "delivery.txt exists"},
        "budget": {"wall_sec": 30, "max_reworks": 0},
    }
    body.update(overrides)
    return body


def _entry(relative: str, *, size: int = 1, digest: str = _SHA) -> dict:
    return {"relative": relative, "sha256": digest, "size": size}


def _wire(root: Path, entries: list[dict], *, max_files: int = 256, max_bytes: int = 1024) -> dict:
    return {
        "root": str(root),
        "entries": entries,
        "max_files": max_files,
        "max_total_bytes": max_bytes,
    }


class _RecordingBackend:
    backend_id = "fake.manifest_v1"

    def __init__(self) -> None:
        self.starts: list[dict] = []

    def start_run(self, *, title, directory, instruction="", artifacts=None, charter=None):
        root = Path(directory)
        files: dict[str, bytes] = {}
        if root.exists():
            for path in root.rglob("*"):
                if path.is_symlink() or not path.is_file():
                    continue
                files[path.relative_to(root).as_posix()] = path.read_bytes()
        written = []
        for rel in artifacts or []:
            dest = root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text("ok\n", encoding="utf-8")
            written.append(str(dest))
        run_id = f"rec-{len(self.starts) + 1}"
        self.starts.append(
            {
                "title": title,
                "directory": str(root),
                "instruction": instruction,
                "charter": dict(charter or {}),
                "files": files,
                "run_id": run_id,
            }
        )
        return {"ok": True, "backend": self.backend_id, "run_id": run_id, "native_handle": run_id}

    def observe_run(self, run_id, **kwargs):
        return {"busy": False, "finish_successful": True}

    def collect_result(self, run_id):
        rec = next(item for item in self.starts if item["run_id"] == run_id)
        return {
            "ok": True,
            "run_id": run_id,
            "artifacts": [str(Path(rec["directory"]) / "delivery.txt")],
            "workspace": rec["directory"],
        }

    def list_pending_actions(self, *, session_id=None):
        return 200, []

    def cancel(self, run_id):
        return 200, {"ok": True, "run_id": run_id, "state": "cancelled"}


class _ClientCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.snap_root = self.tmp / "snapshots"
        missing = str(self.tmp / "missing.env")
        self._env = mock.patch.dict(
            os.environ,
            {
                "COLLAB_API_BASE": "http://127.0.0.1:8765",
                "COLLAB_API_TOKEN": "test-token",
                "COLLAB_ENV_FILE": missing,
                "COLLAB_JSON_UNICODE": "",
                "COLLAB_OUTPUT_FULL": "",
                "COLLAB_SNAPSHOT_DIR": str(self.snap_root),
            },
            clear=False,
        )
        self._env.start()
        os.environ.pop("HERMES_HOME", None)

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def _run(self, argv: list[str], urlopen) -> tuple[int, str]:
        buf = io.StringIO()
        with mock.patch("urllib.request.urlopen", side_effect=urlopen):
            with mock.patch("sys.stdout", buf):
                code = HCR.main(argv)
        return code, buf.getvalue()

    def _ok_urlopen(self, captured: dict):
        def fake(req, timeout=30):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResp({"ok": True, "request_id": "g1", "state": "queued"})

        return fake

    def _forbid_http(self, req, timeout=30):
        raise AssertionError("HTTP must not be sent")


class _FakeResp:
    def __init__(self, body: dict, status: int = 200):
        self._raw = json.dumps(body).encode("utf-8")
        self.status = status

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _forbid_hash(*_args, **_kwargs):
    raise AssertionError("hash called")


class ClientManifestTests(_ClientCase):
    def _write_manifest(self, doc: dict) -> Path:
        path = self.tmp / "manifest.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path

    def test_over_max_files_fails_before_hash_and_http(self) -> None:
        root = self.tmp / "root"
        root.mkdir()
        for index in range(3):
            (root / f"f{index}.txt").write_text("x", encoding="utf-8")
        manifest = self._write_manifest(
            {
                "root": str(root),
                "include": ["*.txt"],
                "max_files": 2,
                "max_total_bytes": 1000,
            }
        )
        with mock.patch("framework.input_manifest.hash_file", side_effect=_forbid_hash) as hashed:
            with mock.patch("framework.input_manifest.hashlib.sha256", side_effect=_forbid_hash) as sha:
                with mock.patch.object(HCR, "_sha256_file", side_effect=_forbid_hash) as streamed:
                    code, text = self._run(
                        ["open", "--goal", "too many files", "--input-manifest", str(manifest)],
                        self._forbid_http,
                    )
        self.assertEqual(code, 1)
        self.assertEqual(hashed.call_count, 0)
        self.assertEqual(sha.call_count, 0)
        self.assertEqual(streamed.call_count, 0)
        payload = json.loads(text)
        self.assertEqual(payload["code"], "manifest_too_many_files")
        self.assertNotIn(_TOKEN, text)

    def test_declared_cap_above_hard_limit_is_not_clamped(self) -> None:
        root = self.tmp / "root"
        root.mkdir()
        (root / "a.txt").write_text("x", encoding="utf-8")
        manifest = self._write_manifest(
            {"root": str(root), "include": ["a.txt"], "max_files": MAX_MANIFEST_FILES + 1}
        )
        with mock.patch("framework.input_manifest.hash_file", side_effect=_forbid_hash) as hashed:
            code, text = self._run(
                ["open", "--goal", "cap", "--input-manifest", str(manifest)],
                self._forbid_http,
            )
        self.assertEqual(code, 1)
        self.assertEqual(hashed.call_count, 0)
        self.assertEqual(json.loads(text)["code"], "bad_input_manifest")

    def test_byte_budget_fails_before_hash(self) -> None:
        root = self.tmp / "root"
        root.mkdir()
        (root / "a.txt").write_bytes(b"abcd")
        manifest = self._write_manifest(
            {"root": str(root), "include": ["a.txt"], "max_total_bytes": 3}
        )
        with mock.patch("framework.input_manifest.hash_file", side_effect=_forbid_hash) as hashed:
            code, text = self._run(
                ["open", "--goal", "too big", "--input-manifest", str(manifest)],
                self._forbid_http,
            )
        self.assertEqual(code, 1)
        self.assertEqual(hashed.call_count, 0)
        self.assertEqual(json.loads(text)["code"], "manifest_too_large")

    def test_recursive_refused_by_default(self) -> None:
        root = self.tmp / "root"
        nested = root / "sub"
        nested.mkdir(parents=True)
        (nested / "a.txt").write_text(_TOKEN, encoding="utf-8")
        manifest = self._write_manifest({"root": str(root), "include": ["**/*.txt"]})
        with mock.patch("framework.input_manifest.hash_file", side_effect=_forbid_hash) as hashed:
            code, text = self._run(
                ["open", "--goal", "recurse", "--input-manifest", str(manifest)],
                self._forbid_http,
            )
        self.assertEqual(code, 1)
        self.assertEqual(hashed.call_count, 0)
        payload = json.loads(text)
        self.assertEqual(payload["code"], "manifest_recursive_refused")
        self.assertNotIn(_TOKEN, payload["error"])

    def test_star_does_not_descend_and_recursive_stays_bounded(self) -> None:
        root = self.tmp / "root"
        (root / "a.txt").parent.mkdir()
        (root / "a.txt").write_text("top", encoding="utf-8")
        (root / "sub").mkdir()
        (root / "sub" / "b.txt").write_text("nested", encoding="utf-8")
        (root / "logs").mkdir()
        (root / "logs" / "a.jsonl").write_text("{}\n", encoding="utf-8")
        (root / "logs" / "sub").mkdir()
        (root / "logs" / "sub" / "b.jsonl").write_text("{}\n", encoding="utf-8")
        flat = build_client_manifest({"root": str(root), "include": ["*"]})
        self.assertEqual([item["relative"] for item in flat["entries"]], ["a.txt"])
        logs = build_client_manifest({"root": str(root), "include": ["logs/*.jsonl"]})
        self.assertEqual([item["relative"] for item in logs["entries"]], ["logs/a.jsonl"])
        with mock.patch("framework.input_manifest.hash_file", side_effect=_forbid_hash) as hashed:
            with self.assertRaises(InputManifestError) as caught:
                build_client_manifest(
                    {
                        "root": str(root),
                        "include": ["**/*.txt"],
                        "recursive": True,
                        "max_files": 1,
                    }
                )
        self.assertEqual(caught.exception.code, "manifest_too_many_files")
        self.assertEqual(hashed.call_count, 0)
        both = build_client_manifest(
            {"root": str(root), "include": ["**/*.txt"], "recursive": True, "max_files": 2}
        )
        self.assertEqual(
            [item["relative"] for item in both["entries"]],
            ["a.txt", "sub/b.txt"],
        )

    def test_symlink_outside_root_and_credential_refused_before_hash(self) -> None:
        root = self.tmp / "root"
        root.mkdir()
        secret = self.tmp / "secret.txt"
        secret.write_text(_TARGET, encoding="utf-8")
        _link(secret, root / "alias.txt")
        (root / ".env").write_text(_TOKEN, encoding="utf-8")
        outside = self.tmp / "outside.txt"
        outside.write_text(_TOKEN, encoding="utf-8")
        link_root = self.tmp / "linkroot"
        _link(root, link_root, directory=True)

        cases = [
            ({"root": str(root), "include": ["alias.txt"]}, "manifest_symlink_refused", secret),
            ({"root": str(root), "include": ["../outside.txt"]}, "manifest_outside_root", outside),
            ({"root": str(root), "include": [".env"]}, "manifest_credential_refused", root / ".env"),
            ({"root": str(link_root), "include": ["alias.txt"]}, "manifest_symlink_refused", link_root),
        ]
        for doc, code, hidden in cases:
            with self.subTest(code=code):
                with mock.patch("framework.input_manifest.hash_file", side_effect=_forbid_hash) as hashed:
                    with self.assertRaises(InputManifestError) as caught:
                        build_client_manifest(doc)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(hashed.call_count, 0)
                message = str(caught.exception)
                self.assertNotIn(_TOKEN, message)
                self.assertNotIn(_TARGET, message)
                self.assertNotIn(str(hidden.resolve()), message)

        (root / "sub").mkdir()
        with mock.patch("framework.input_manifest.hash_file", side_effect=_forbid_hash) as hashed:
            with self.assertRaises(InputManifestError) as caught:
                build_client_manifest({"root": str(root), "include": ["sub"]})
        self.assertEqual(caught.exception.code, "manifest_directory_refused")
        self.assertEqual(hashed.call_count, 0)

    def test_one_hundred_files_are_listed_without_widening(self) -> None:
        root = self.tmp / "srcroot-MARKER"
        batch = root / "batch"
        batch.mkdir(parents=True)
        expected = {}
        for index in range(100):
            name = f"n{index:03d}.txt"
            payload = f"n{index}\n".encode("utf-8")
            (batch / name).write_bytes(payload)
            expected[f"batch/{name}"] = hashlib.sha256(payload).hexdigest()
        (batch / "nested").mkdir()
        (batch / "nested" / "nope.txt").write_text(_TOKEN, encoding="utf-8")
        (root / "note.txt").write_text(_TOKEN, encoding="utf-8")
        manifest = self._write_manifest(
            {
                "root": str(root),
                "include": ["batch/*.txt"],
                "max_files": 100,
                "max_total_bytes": 100000,
            }
        )
        captured: dict = {}
        code, text = self._run(
            ["open", "--goal", "many files", "--input-manifest", str(manifest)],
            self._ok_urlopen(captured),
        )
        self.assertEqual(code, 0, text)
        body = captured["body"]
        entries = body["input_manifest"]["entries"]
        self.assertEqual(len(entries), 100)
        self.assertEqual(body["input_manifest"]["max_files"], 100)
        self.assertEqual(
            {item["relative"]: item["sha256"] for item in entries},
            expected,
        )
        self.assertNotIn("external_inputs", body)
        self.assertEqual(Path(body["input_manifest"]["root"]).resolve(), root.resolve())
        raw = json.dumps(body)
        self.assertNotIn(_TOKEN, raw)
        self.assertNotIn("nested/nope.txt", raw)

    def test_combined_pin_limit_is_before_snapshot_io(self) -> None:
        argv = ["open", "--goal", "too many pins"]
        for index in range(7):
            argv.extend(["--external-input", str(self.tmp / f"missing-{index}.txt")])
        argv.extend(
            [
                "--sqlite-snapshot",
                str(self.tmp / "live.db"),
                "--file-snapshot",
                str(self.tmp / "live.jsonl"),
            ]
        )
        with mock.patch.object(HCR, "make_snapshot_dir", side_effect=_forbid_hash) as made:
            with mock.patch.object(HCR, "sqlite_snapshot", side_effect=_forbid_hash) as sqlite:
                with mock.patch.object(HCR, "file_snapshot", side_effect=_forbid_hash) as files:
                    with mock.patch.object(HCR, "_sha256_file", side_effect=_forbid_hash) as hashed:
                        with mock.patch.object(Path, "open", side_effect=_forbid_hash):
                            code, text = self._run(argv, self._forbid_http)
        self.assertEqual(code, 1)
        self.assertEqual(made.call_count, 0)
        self.assertEqual(sqlite.call_count, 0)
        self.assertEqual(files.call_count, 0)
        self.assertEqual(hashed.call_count, 0)
        self.assertEqual(json.loads(text)["code"], "too_many_external_inputs")


class ServerManifestTests(unittest.TestCase):
    def test_shape_is_rejected_without_hashing_or_persisting(self) -> None:
        root = Path(tempfile.gettempdir()).resolve() / "manifest-does-not-need-to-exist"
        digest = _SHA
        good_entry = _entry("a.txt")
        cases = [
            {"input_manifest": {"root": "relative", "entries": [], "max_files": 1, "max_total_bytes": 1}},
            {"input_manifest": _wire(root, [_entry("../a.txt")])},
            {"input_manifest": _wire(root, [_entry("a.txt", digest="abcd")])},
            {"input_manifest": _wire(root, [_entry("a.txt"), _entry("a.txt")])},
            {"input_manifest": _wire(root, [_entry("a/b.txt"), _entry("a\\b.txt")])},
            {"input_manifest": _wire(root, [_entry(".env")])},
            {
                "input_manifest": _wire(
                    root,
                    [_entry(f"f{i}.txt") for i in range(MAX_MANIFEST_FILES + 1)],
                    max_files=MAX_MANIFEST_FILES,
                    max_bytes=MAX_MANIFEST_BYTES,
                )
            },
            {"input_manifest": {**_wire(root, []), "exclude": ["*.txt"]}},
            {"input_manifest": {**_wire(root, [good_entry]), "recursive": True}},
            {
                "input_manifest": _wire(
                    root,
                    [],
                    max_files=MAX_MANIFEST_FILES + 1,
                )
            },
            {
                "input_manifest": _wire(
                    root,
                    [],
                    max_bytes=MAX_MANIFEST_BYTES + 1,
                )
            },
            {
                "input_manifest": _wire(
                    root,
                    [_entry("a.txt", size=3), _entry("b.txt", size=3)],
                    max_bytes=5,
                )
            },
        ]
        with tempfile.TemporaryDirectory() as td:
            app = CollabApplication(td)
            for index, extra in enumerate(cases):
                with self.subTest(index=index):
                    with mock.patch("framework.input_manifest.hash_file", side_effect=_forbid_hash) as hashed:
                        with mock.patch("framework.input_manifest._open_read", side_effect=_forbid_hash) as opened:
                            body = _request(idempotency_key=f"bad-{index}")
                            body.update(extra)
                            with self.assertRaises(AppError) as caught:
                                app.submit(body)
                    self.assertEqual(caught.exception.code, "invalid_input_manifest", caught.exception)
                    self.assertEqual(hashed.call_count, 0)
                    self.assertEqual(opened.call_count, 0)
            self.assertEqual(app.layer.list_goals()["goal_count"], 0)

    def test_capabilities_document_manifest_and_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            caps = CollabApplication(td).capabilities()
        self.assertEqual(
            caps["input_manifest"],
            {
                "max_files": 256,
                "max_total_bytes": 256 * 1024 * 1024,
                "staging": "copy_into_workspace",
            },
        )
        self.assertEqual(
            caps["snapshots"],
            {"sqlite": "client_backup_api", "file": "client_prefix_copy"},
        )

    def test_snapshot_metadata_is_stored_but_unknown_keys_stay_rejected(self) -> None:
        digest = _SHA
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = str((root / "snap.db").resolve())
            app = CollabApplication(root / "app")
            pin = {
                "path": path,
                "sha256": digest.upper(),
                "metadata": {
                    "kind": "sqlite_snapshot",
                    "source": "snap.db",
                    "taken_at": "2026-10-02T00:00:00Z",
                },
            }
            opened = app.submit(_request(external_inputs=[pin]))
            stored = app.status(opened["goal_id"])["goal"]["external_inputs"]
            self.assertEqual(stored[0]["sha256"], digest.upper())
            self.assertEqual(stored[0]["metadata"]["kind"], "sqlite_snapshot")
            self.assertEqual(stored[0]["metadata"]["source"], "snap.db")
            Draft202012Validator(GOAL_SCHEMA).validate(app.status(opened["goal_id"])["goal"])
            bad = [
                {**pin, "note": "nope"},
                {**pin, "metadata": {"kind": "sqlite_snapshot", "source": "a/b", "taken_at": "2026-10-02T00:00:00Z"}},
                {**pin, "metadata": {"kind": "wal", "source": "snap.db", "taken_at": "2026-10-02T00:00:00Z"}},
            ]
            for index, item in enumerate(bad):
                body = _request(idempotency_key=f"meta-{index}", external_inputs=[item])
                with self.assertRaises(AppError) as caught:
                    app.submit(body)
                self.assertEqual(caught.exception.code, "invalid_external_inputs")


class DispatchTests(unittest.TestCase):
    def _app(self, root: Path) -> tuple[CollabApplication, _RecordingBackend]:
        backend = _RecordingBackend()
        return CollabApplication(root / "app", backend=backend), backend

    def _workspace(self, app: CollabApplication, goal_id: str) -> Path:
        task_id = app.status(goal_id)["tasks"][0]["task_id"]
        return scheduler_task_workspace(app.coordinator.workspaces_root, goal_id, task_id)

    def test_one_hundred_files_are_staged_without_widening_scope(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / "srcroot-MARKER"
            batch = root / "batch"
            batch.mkdir(parents=True)
            payloads = {}
            for index in range(100):
                relative = f"batch/n{index:03d}.txt"
                payload = f"n{index}\n".encode("utf-8")
                (root / relative).parent.mkdir(parents=True, exist_ok=True)
                (root / relative).write_bytes(payload)
                payloads[relative] = payload
            (batch / "nested").mkdir()
            (batch / "nested" / "nope.txt").write_text(_TOKEN, encoding="utf-8")
            manifest = build_client_manifest(
                {"root": str(root), "include": ["batch/*.txt"], "max_files": 100, "max_total_bytes": 100000}
            )
            app, backend = self._app(base)
            opened = app.submit(_request(input_manifest=manifest))
            goal = app.status(opened["goal_id"])["goal"]
            Draft202012Validator(GOAL_SCHEMA).validate(goal)
            self.assertNotIn("srcroot-MARKER", json.dumps(goal["desired_outcome"]))
            tick = app.coordinator.process_goal(opened["goal_id"])
            self.assertEqual(tick.get("action"), "task_finished", tick)
            self.assertEqual(len(backend.starts), 1)
            charter = backend.starts[0]["charter"]
            self.assertNotIn("external_inputs", charter)
            names = charter["input_files"]
            self.assertEqual(len(names), 100)
            self.assertEqual(names, [f"inputs/manifest/{relative}" for relative in sorted(payloads)])
            for name in names:
                self.assertFalse(Path(name).is_absolute())
                self.assertNotIn("..", Path(name).parts)
            rendered = render_contract_section(normalize_worker_contract(charter))
            self.assertIn("inputs/manifest/batch/n000.txt", rendered)
            self.assertNotIn("srcroot-MARKER", rendered)
            self.assertNotIn(_TOKEN, rendered)
            workspace = Path(backend.starts[0]["directory"])
            for relative, payload in payloads.items():
                staged = workspace / "inputs" / "manifest" / relative
                self.assertEqual(staged.read_bytes(), payload)
            tree = _tree_bytes(workspace)
            self.assertNotIn(_TOKEN.encode("utf-8"), tree)
            self.assertNotIn(b"srcroot-MARKER", tree)
            self.assertFalse((workspace / "inputs" / "manifest" / "nested" / "nope.txt").exists())

    def test_hash_changed_fails_before_worker_and_leaves_no_partial(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / "root"
            root.mkdir()
            (root / "a.txt").write_text("alpha-stable", encoding="utf-8")
            (root / "b.txt").write_text("beta-old", encoding="utf-8")
            manifest = build_client_manifest({"root": str(root), "include": ["*.txt"]})
            (root / "b.txt").write_text("beta-" + _TOKEN, encoding="utf-8")
            app, backend = self._app(base)
            opened = app.submit(_request(idempotency_key="changed", input_manifest=manifest))
            app.coordinator.process_goal(opened["goal_id"])
            status = app.status(opened["goal_id"])
            self.assertEqual(status["state"], "failed")
            self.assertEqual(status["tasks"][0]["result"]["error"], "hash_changed: b.txt")
            self.assertEqual(backend.starts, [])
            workspace = self._workspace(app, opened["goal_id"])
            self.assertFalse((workspace / "inputs" / "manifest").exists())
            self.assertEqual(list(workspace.rglob(".manifest-partial-*")), [])
            blob = _tree_bytes(workspace)
            self.assertNotIn(b"alpha-stable", blob)
            self.assertNotIn(b"beta-old", blob)
            self.assertNotIn(_TOKEN.encode("utf-8"), blob)

    def test_symlink_at_copy_time_does_not_start_worker_or_leak_target(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / "root"
            nested = root / "sub"
            nested.mkdir(parents=True)
            (nested / "a.txt").write_text("stable", encoding="utf-8")
            manifest = build_client_manifest({"root": str(root), "include": ["sub/a.txt"]})
            outside = base / "outside-secret"
            outside.mkdir()
            (outside / "a.txt").write_text(_TARGET, encoding="utf-8")
            nested.rename(base / "sub-real")
            _link(outside, nested, directory=True)
            app, backend = self._app(base)
            opened = app.submit(_request(idempotency_key="link", input_manifest=manifest))
            app.coordinator.process_goal(opened["goal_id"])
            status = app.status(opened["goal_id"])
            error = status["tasks"][0]["result"]["error"]
            self.assertIn("manifest_symlink_refused", error)
            self.assertNotIn(_TARGET, error)
            self.assertNotIn(str(outside), error)
            self.assertEqual(backend.starts, [])
            workspace = self._workspace(app, opened["goal_id"])
            self.assertNotIn(_TARGET.encode("utf-8"), _tree_bytes(workspace))
            self.assertFalse((workspace / "inputs" / "manifest").exists())


def _tree_bytes(root: Path) -> bytes:
    chunks: list[bytes] = []
    if not root.exists():
        return b""
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        chunks.append(path.read_bytes())
    return b"\n".join(chunks)


def _open_writer(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db)
    conn.isolation_level = "DEFERRED"
    mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    if str(mode).lower() != "wal":
        raise unittest.SkipTest(f"WAL unavailable ({mode})")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    return conn


class SnapshotTests(_ClientCase):
    def test_sqlite_snapshot_keeps_committed_rows_only(self) -> None:
        db = self.tmp / "live-source-MARKER" / "chat.db"
        db.parent.mkdir()
        conn = _open_writer(db)
        try:
            conn.execute("CREATE TABLE rows(v TEXT)")
            conn.execute("INSERT INTO rows VALUES ('committed')")
            conn.commit()
            conn.execute("INSERT INTO rows VALUES ('open')")
            seen = [row[0] for row in conn.execute("SELECT v FROM rows ORDER BY v")]
            self.assertEqual(seen, ["committed", "open"])
            other = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                visible = [row[0] for row in other.execute("SELECT v FROM rows")]
            finally:
                other.close()
            self.assertEqual(visible, ["committed"])
            self.assertGreater(Path(str(db) + "-wal").stat().st_size, 0)
            dest = self.tmp / "snap"
            dest.mkdir()
            if os.name != "nt":
                os.chmod(dest, 0o700)
            snap = sqlite_snapshot(str(db), dest)
            self.assertFalse(str(snap.path).endswith("-wal"))
            self.assertFalse(str(snap.path).endswith("-shm"))
            self.assertFalse(Path(str(snap.path) + "-wal").exists())
            self.assertFalse(Path(str(snap.path) + "-shm").exists())
            self.assertEqual(snap.metadata["kind"], "sqlite_snapshot")
            self.assertEqual(snap.metadata["source"], "chat.db")
            self.assertNotIn("MARKER", snap.metadata["source"])
            copied = sqlite3.connect(snap.path)
            try:
                self.assertEqual(copied.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                mode = str(copied.execute("PRAGMA journal_mode").fetchone()[0]).lower()
                self.assertNotEqual(mode, "wal")
                self.assertEqual([row[0] for row in copied.execute("SELECT v FROM rows")], ["committed"])
            finally:
                copied.close()
            still = [row[0] for row in conn.execute("SELECT v FROM rows ORDER BY v")]
            self.assertEqual(still, ["committed", "open"])
            self.assertTrue(Path(str(db) + "-wal").exists())
        finally:
            conn.close()

    def test_concurrent_writer_snapshot_stays_consistent(self) -> None:
        db = self.tmp / "race.db"
        conn = _open_writer(db)
        conn.execute("CREATE TABLE rows(v INTEGER)")
        conn.execute("INSERT INTO rows VALUES (1)")
        conn.commit()
        conn.close()
        stop = threading.Event()
        errors: list[BaseException] = []

        def writer() -> None:
            local = sqlite3.connect(db, timeout=5, isolation_level="DEFERRED")
            index = 0
            try:
                while not stop.is_set():
                    try:
                        local.execute("INSERT INTO rows VALUES (?)", (index,))
                        local.commit()
                        index += 1
                    except sqlite3.OperationalError:
                        time.sleep(0.01)
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                local.close()

        thread = threading.Thread(target=writer)
        thread.start()
        dest = self.tmp / "snap"
        dest.mkdir()
        try:
            time.sleep(0.05)
            snap = sqlite_snapshot(str(db), dest)
        finally:
            stop.set()
            thread.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertFalse(thread.is_alive())
        copied = sqlite3.connect(snap.path)
        try:
            self.assertEqual(copied.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            mode = str(copied.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            self.assertNotEqual(mode, "wal")
            count = copied.execute("SELECT COUNT(*) FROM rows").fetchone()[0]
            self.assertGreaterEqual(count, 1)
        finally:
            copied.close()
        self.assertFalse(Path(str(snap.path) + "-wal").exists())
        self.assertFalse(Path(str(snap.path) + "-shm").exists())

    def test_external_input_refuses_live_wal_and_sidecars(self) -> None:
        db = self.tmp / "live-source-MARKER" / "chat.db"
        db.parent.mkdir()
        conn = _open_writer(db)
        try:
            conn.execute("CREATE TABLE rows(v TEXT)")
            conn.execute("INSERT INTO rows VALUES ('committed')")
            conn.commit()
            wal = Path(str(db) + "-wal")
            self.assertGreater(wal.stat().st_size, 0)
            with self.assertRaises(InputManifestError) as live:
                assert_sqlite_pin_allowed(db.resolve())
            self.assertEqual(live.exception.code, "sqlite_live_wal")
            with self.assertRaises(InputManifestError) as side:
                assert_sqlite_pin_allowed(wal.resolve())
            self.assertEqual(side.exception.code, "sqlite_sidecar_refused")

            with mock.patch.object(HCR, "_sha256_file", side_effect=_forbid_hash) as hashed:
                code, text = self._run(
                    ["open", "--goal", "pin live", "--external-input", str(db)],
                    self._forbid_http,
                )
            self.assertEqual(code, 1)
            self.assertEqual(hashed.call_count, 0)
            payload = json.loads(text)
            self.assertEqual(payload["code"], "sqlite_live_wal")
            self.assertIn("--sqlite-snapshot", payload["error"])
            self.assertNotIn("snapshots", payload)

            with mock.patch.object(HCR, "_sha256_file", side_effect=_forbid_hash) as hashed:
                code, text = self._run(
                    ["open", "--goal", "pin wal", "--external-input", str(wal)],
                    self._forbid_http,
                )
            self.assertEqual(code, 1)
            self.assertEqual(hashed.call_count, 0)
            self.assertEqual(json.loads(text)["code"], "sqlite_sidecar_refused")
        finally:
            conn.close()

    def test_sqlite_snapshot_cli_pins_snapshot_not_the_live_db(self) -> None:
        db = self.tmp / "live-source-MARKER" / "chat.db"
        db.parent.mkdir()
        conn = _open_writer(db)
        try:
            conn.execute("CREATE TABLE rows(v TEXT)")
            conn.execute("INSERT INTO rows VALUES ('committed')")
            conn.commit()
            conn.execute("INSERT INTO rows VALUES ('open')")
            captured: dict = {}
            code, text = self._run(
                ["open", "--goal", "snapshot db", "--sqlite-snapshot", str(db)],
                self._ok_urlopen(captured),
            )
            self.assertEqual(code, 0, text)
            body = captured["body"]
            raw = json.dumps(body)
            self.assertNotIn("live-source-MARKER", raw)
            pins = body["external_inputs"]
            self.assertEqual(len(pins), 1)
            pin = pins[0]
            self.assertFalse(pin["path"].endswith("-wal"))
            self.assertFalse(pin["path"].endswith("-shm"))
            self.assertNotEqual(Path(pin["path"]).resolve(), db.resolve())
            self.assertEqual(pin["metadata"]["kind"], "sqlite_snapshot")
            self.assertEqual(pin["metadata"]["source"], "chat.db")
            self.assertEqual(pin["sha256"], hash_file(Path(pin["path"])))
            stdout = json.loads(text)
            self.assertEqual(stdout["snapshots"], [pin["path"]])
            copied = sqlite3.connect(pin["path"])
            try:
                self.assertEqual([row[0] for row in copied.execute("SELECT v FROM rows")], ["committed"])
                mode = str(copied.execute("PRAGMA journal_mode").fetchone()[0]).lower()
                self.assertNotEqual(mode, "wal")
            finally:
                copied.close()
            if os.name != "nt":
                snap_dir = Path(pin["path"]).parent
                self.assertEqual(stat.S_IMODE(snap_dir.stat().st_mode), 0o700)
        finally:
            conn.close()

    def test_jsonl_snapshot_drops_trailing_partial_line_and_keeps_source(self) -> None:
        source = self.tmp / "live-source-MARKER" / "events.JSONL"
        source.parent.mkdir()
        original = b'{"a":1}\n{"b":'
        source.write_bytes(original)
        plain = self.tmp / "notes.txt"
        plain.write_bytes(b"abc\npartial")
        dest = self.tmp / "copies"
        dest.mkdir()
        jsonl = file_snapshot(str(source), dest)
        self.assertEqual(jsonl.path.read_bytes(), b'{"a":1}\n')
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(jsonl.metadata["kind"], "file_snapshot")
        self.assertEqual(jsonl.metadata["source"], "events.JSONL")
        kept = file_snapshot(str(plain), dest)
        self.assertEqual(kept.path.read_bytes(), b"abc\npartial")
        empty_partial = self.tmp / "only.jsonl"
        empty_partial.write_bytes(b'{"no-newline":true}')
        blank = file_snapshot(str(empty_partial), dest)
        self.assertEqual(blank.path.read_bytes(), b"")
        self.assertEqual(empty_partial.read_bytes(), b'{"no-newline":true}')

        captured: dict = {}
        code, text = self._run(
            ["open", "--goal", "snapshot log", "--file-snapshot", str(source)],
            self._ok_urlopen(captured),
        )
        self.assertEqual(code, 0, text)
        pin = captured["body"]["external_inputs"][0]
        self.assertEqual(Path(pin["path"]).read_bytes(), b'{"a":1}\n')
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(pin["metadata"]["kind"], "file_snapshot")
        self.assertEqual(pin["metadata"]["source"], "events.JSONL")
        self.assertNotIn("live-source-MARKER", json.dumps(captured["body"]))
        self.assertEqual(json.loads(text)["snapshots"], [pin["path"]])

    def test_dispatch_strips_snapshot_metadata_from_the_worker_charter(self) -> None:
        db = self.tmp / "chat.db"
        conn = _open_writer(db)
        try:
            conn.execute("CREATE TABLE rows(v TEXT)")
            conn.execute("INSERT INTO rows VALUES ('committed')")
            conn.commit()
            snap_dir = self.tmp / "snap"
            snap_dir.mkdir()
            snap = sqlite_snapshot(str(db), snap_dir)
        finally:
            conn.close()
        digest = hash_file(snap.path)
        pin = {"path": str(snap.path.resolve()), "sha256": digest, "metadata": dict(snap.metadata)}
        with tempfile.TemporaryDirectory() as td:
            backend = _RecordingBackend()
            app = CollabApplication(td, backend=backend)
            opened = app.submit(_request(external_inputs=[pin]))
            tick = app.coordinator.process_goal(opened["goal_id"])
            self.assertEqual(tick.get("action"), "task_finished", tick)
            charter_pins = backend.starts[0]["charter"]["external_inputs"]
            self.assertEqual(charter_pins, [{"path": pin["path"], "sha256": digest}])
            normalize_worker_contract(backend.starts[0]["charter"])

    def test_snapshots_clean_stays_inside_the_root(self) -> None:
        root = self.snap_root
        root.mkdir()
        old = root / "old-snap"
        old.mkdir()
        fresh = root / "fresh-snap"
        fresh.mkdir()
        (fresh / "keep.txt").write_text("fresh", encoding="utf-8")
        outside = self.tmp / "outside"
        outside.mkdir()
        marker = outside / "keep.txt"
        marker.write_text("outside-keep", encoding="utf-8")
        _link(marker, old / "leak")
        linked = root / "linked-out"
        _link(outside, linked, directory=True)
        loose = root / "not-a-dir.txt"
        loose.write_text("file", encoding="utf-8")
        old_time = time.time() - (48 * 3600)
        os.utime(old, (old_time, old_time))
        result = clean_snapshots(hours=24)
        self.assertIn(str(old), result["deleted"])
        self.assertFalse(old.exists())
        self.assertTrue(fresh.is_dir())
        self.assertEqual((fresh / "keep.txt").read_text(encoding="utf-8"), "fresh")
        self.assertEqual(marker.read_text(encoding="utf-8"), "outside-keep")
        self.assertTrue(outside.is_dir())
        self.assertTrue(loose.is_file())
        self.assertTrue(linked.is_symlink())
        code, text = self._run(["snapshots-clean", "--hours", "24"], self._forbid_http)
        self.assertEqual(code, 0, text)
        payload = json.loads(text)
        self.assertTrue(payload["ok"])
        self.assertEqual(marker.read_text(encoding="utf-8"), "outside-keep")
        self.assertTrue(fresh.is_dir())
        link_root = self.tmp / "link-root"
        _link(root, link_root, directory=True)
        with mock.patch.dict(os.environ, {"COLLAB_SNAPSHOT_DIR": str(link_root)}):
            with self.assertRaises(InputManifestError) as caught:
                clean_snapshots(hours=0)
        self.assertEqual(caught.exception.code, "bad_snapshot_root")
        self.assertTrue(fresh.is_dir())
        self.assertEqual(marker.read_text(encoding="utf-8"), "outside-keep")

    def test_make_snapshot_dir_is_private_where_the_os_supports_it(self) -> None:
        if os.name == "nt":
            dest = make_snapshot_dir()
            self.assertTrue(dest.is_dir())
            self.assertTrue(dest.parent.samefile(self.snap_root))
            return
        parent = self.tmp / "new-root"
        with mock.patch.dict(os.environ, {"COLLAB_SNAPSHOT_DIR": str(parent)}):
            dest = make_snapshot_dir()
        self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o700)


class ProjectHelperTests(unittest.TestCase):
    def test_absent_manifest_is_none_and_hashes_are_not_opened(self) -> None:
        self.assertIsNone(project_input_manifest({}))
        self.assertIsNone(project_input_manifest({"input_manifest": None}))
        absolute = Path(tempfile.gettempdir()).resolve() / "abs-root"
        with mock.patch("framework.input_manifest._open_read", side_effect=_forbid_hash) as opened:
            projected = project_input_manifest(
                {
                    "input_manifest": _wire(
                        absolute,
                        [_entry("A.TXT", digest=_SHA.upper())],
                    )
                }
            )
        self.assertEqual(opened.call_count, 0)
        assert projected is not None
        self.assertEqual(projected["entries"][0]["sha256"], _SHA)
        self.assertEqual(projected["entries"][0]["relative"], "A.TXT")
