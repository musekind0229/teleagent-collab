"""Linux supervised backend, /proc discovery, and --ready. No live TeleAgent.

Credential fixtures are synthetic canaries. Assertions check they do not
appear in errors or public reports. This module does not scan real /proc
and does not dial a remote host.
"""
from __future__ import annotations

import copy
import errno
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
BIN = ROOT / "bin"
for entry in (str(ROOT), str(SRC)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from execution_backend import get_execution_backend  # noqa: E402
from execution_backend.linux_supervised_v1 import LinuxSupervisedExecutionBackend  # noqa: E402
from execution_backend.windows_supervised_v1 import WindowsSupervisedExecutionBackend  # noqa: E402
from framework.app_service import CollabApplication, CollabHttpServer  # noqa: E402
from framework.linux_ready import _port_open, assess_linux_gui_readiness  # noqa: E402
from tests.desktop_lock_isolation import install_desktop_lock_isolation  # noqa: E402
from win_collab.client import KEYS, Client, windows_process_image  # noqa: E402
from win_collab.core import Store, contained, external_directory_scope, hard_reject  # noqa: E402
from win_collab.desktop_lock import lock_root  # noqa: E402
from win_collab.linux_discovery import (  # noqa: E402
    discover_linux,
    linux_image_specs,
    scan_linux_credentials,
)

CANARY = "synthetic-password-canary"
CANARY_OTHER = "synthetic-password-other"
_TIP = "a" * 40


def _load_collab_service():
    path = BIN / "collab-service.py"
    spec = importlib.util.spec_from_file_location("collab_service_linux_mod", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _block(pairs: list[tuple[str, str]]) -> bytes:
    return b"\0".join(f"{key}={value}".encode("utf-8") for key, value in pairs) + b"\0"


def _creds(password: str = CANARY) -> list[tuple[str, str]]:
    return [
        (KEYS[0], "synthetic-user"),
        (KEYS[1], password),
        (KEYS[2], "synthetic-session-key"),
    ]


class _Proc:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.images: dict[str, Path] = {}
        self.deny: set[str] = set()

    def add(self, pid: int, image: Path, *, environ: bytes | None = None, comm: str | None = None, deny: bool = False) -> None:
        directory = self.root / str(pid)
        directory.mkdir(parents=True)
        if environ is not None:
            (directory / "environ").write_bytes(environ)
        if comm is not None:
            (directory / "comm").write_text(comm + "\n", encoding="utf-8")
        (directory / "cmdline").write_bytes(b"synthetic-password-canary-in-cmdline\0")
        self.images[str(pid)] = Path(image)
        if deny:
            self.deny.add(str(pid))

    def resolve_exe(self, pid_dir: Path) -> Path:
        image = self.images.get(pid_dir.name)
        if image is None:
            raise FileNotFoundError(pid_dir.name)
        return image

    def read_bytes(self, path: Path) -> bytes:
        if path.name != "environ":
            raise AssertionError(f"discovery read {path.name}")
        if path.parent.name in self.deny:
            raise PermissionError(CANARY)
        return path.read_bytes()


class _FakeClient:
    base = "http://127.0.0.1:4399"
    instance_id = "fake-linux-teleagent"

    def __init__(self) -> None:
        self.pending: list[dict] = []
        self.questions: list[dict] = []
        self.status: dict = {}
        self.messages: dict = {}
        self.count = 0

    def call(self, method, path, body=None, workspace=None):
        del workspace
        if method == "POST" and path == "/session":
            self.count += 1
            sid = f"ses-{self.count}"
            self.status[sid] = {"type": "busy"}
            self.messages[sid] = []
            return {"id": sid, "permission": None if body is None else body.get("permission")}
        if path.endswith("/prompt_async"):
            sid = path.split("/")[2]
            self.messages[sid].append({"info": {"role": "user"}})
            self.status[sid] = {"type": "busy"}
            return None
        if method == "GET" and path == "/permission":
            return copy.deepcopy(self.pending)
        if method == "GET" and path == "/question":
            return copy.deepcopy(self.questions)
        if method == "GET" and path == "/session/status":
            return copy.deepcopy(self.status)
        if method == "GET" and path.endswith("/message"):
            return copy.deepcopy(self.messages[path.split("/")[2]])
        if path.startswith("/permission/") and path.endswith("/reply"):
            request_id = path.split("/")[2]
            self.pending = [item for item in self.pending if item.get("id") != request_id]
            return True
        if path.endswith("/abort"):
            self.status[path.split("/")[2]] = {"type": "idle"}
            return True
        raise AssertionError((method, path))


def _force_scan(state_dir: Path, run_id: str) -> None:
    store = Store(state_dir)
    try:
        with store.transaction():
            job = store.get(run_id)
            job["next_scan"] = 0
            store.save(job)
    finally:
        store.db.close()


def _job(state_dir: Path, run_id: str) -> dict:
    store = Store(state_dir)
    try:
        return store.get(run_id)
    finally:
        store.db.close()


class LinuxDiscoveryTests(unittest.TestCase):
    def test_client_import_rejects_userinfo_without_echoing_it(self):
        self.assertIn("ctypes", sys.modules)
        with self.assertRaises(ValueError) as caught:
            Client(
                f"http://user:{CANARY}@127.0.0.1:4399",
                {key: "x" for key in KEYS},
            )
        self.assertNotIn(CANARY, str(caught.exception))
        with self.assertRaises(RuntimeError) as windows_only:
            windows_process_image(1, expected_images=())
        self.assertIn("Windows", str(windows_only.exception))

    def test_explicit_env_skips_proc_and_hides_values(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            def explode(_path):
                raise AssertionError("proc walk")

            env = {key: value for key, value in _creds()}
            env["TELEAGENT_URL"] = "http://127.0.0.1:4401/"
            scan = scan_linux_credentials(
                environ=env,
                proc_root=root / "missing",
                resolve_exe=explode,
                read_bytes=explode,
            )
            self.assertTrue(scan.ok)
            self.assertEqual(scan.reason, "process_env")
            self.assertEqual(scan.base_url, "http://127.0.0.1:4401")
            public = json.dumps(scan.public_dict())
            self.assertNotIn(CANARY, public)
            self.assertNotIn(CANARY, repr(scan))
            self.assertNotIn("creds", scan.public_dict())
            base, creds = discover_linux(
                environ=env,
                proc_root=root / "missing",
                resolve_exe=explode,
                read_bytes=explode,
            )
            self.assertEqual(base, "http://127.0.0.1:4401")
            self.assertEqual(creds[KEYS[1]], CANARY)

    def test_alias_keys_and_matching_image(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            image = root / "teleagent"
            image.write_text("bin", encoding="utf-8")
            proc = _Proc(root / "proc")
            proc.add(
                42,
                image,
                environ=_block(
                    [
                        ("SUPER_AGENT_OPENCODE_USERNAME", "synthetic-user"),
                        ("SUPER_AGENT_OPENCODE_PASSWORD", CANARY),
                        (KEYS[2], "synthetic-session-key"),
                    ]
                ),
            )
            specs = [("exact", image.resolve())]
            scan = scan_linux_credentials(
                environ={},
                proc_root=proc.root,
                image_specs=specs,
                resolve_exe=proc.resolve_exe,
                read_bytes=proc.read_bytes,
            )
            self.assertTrue(scan.ok, scan.detail)
            self.assertEqual(scan.reason, "proc_environ")
            self.assertEqual(scan.verified, 1)
            self.assertEqual(scan.creds[KEYS[1]], CANARY)
            self.assertNotIn(CANARY, scan.detail)
            self.assertNotIn(CANARY, json.dumps(scan.public_dict()))

    def test_non_teleagent_process_is_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            image = root / "teleagent"
            image.write_text("bin", encoding="utf-8")
            bash = root / "bash"
            bash.write_text("bin", encoding="utf-8")
            proc = _Proc(root / "proc")
            proc.add(10, bash, environ=_block(_creds()))
            specs = [("exact", image.resolve())]
            scan = scan_linux_credentials(
                environ={},
                proc_root=proc.root,
                image_specs=specs,
                resolve_exe=proc.resolve_exe,
                read_bytes=proc.read_bytes,
            )
            self.assertFalse(scan.ok)
            self.assertEqual(scan.reason, "no_process")
            self.assertIn("no TeleAgent process", scan.detail)
            self.assertEqual(scan.verified, 0)
            self.assertNotIn(CANARY, scan.detail)

    def test_conflicting_creds_do_not_leak_values(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            image = root / "teleagent"
            image.write_text("bin", encoding="utf-8")
            proc = _Proc(root / "proc")
            proc.add(1, image, environ=_block(_creds(CANARY)))
            proc.add(2, image, environ=_block(_creds(CANARY_OTHER)))
            with self.assertRaises(RuntimeError) as caught:
                discover_linux(
                    environ={},
                    proc_root=proc.root,
                    image_specs=[("exact", image.resolve())],
                    resolve_exe=proc.resolve_exe,
                    read_bytes=proc.read_bytes,
                )
        text = str(caught.exception)
        self.assertIn("Refuse to guess", text)
        self.assertIn("Values are not shown", text)
        self.assertNotIn(CANARY, text)
        self.assertNotIn(CANARY_OTHER, text)

    def test_permission_denied_is_counted_without_values(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            image = root / "teleagent"
            image.write_text("bin", encoding="utf-8")
            proc = _Proc(root / "proc")
            proc.add(7, image, environ=_block(_creds()), deny=True, comm="teleagent")
            scan = scan_linux_credentials(
                environ={},
                proc_root=proc.root,
                image_specs=[("exact", image.resolve())],
                resolve_exe=proc.resolve_exe,
                read_bytes=proc.read_bytes,
            )
            self.assertFalse(scan.ok)
            self.assertEqual(scan.reason, "not_readable")
            self.assertEqual(scan.permission_denied, 1)
            self.assertEqual(scan.verified, 1)
            self.assertIn(
                "TeleAgent processes found but environ not readable: "
                "run collab-service as the TeleAgent user or root",
                scan.detail,
            )
            self.assertNotIn(CANARY, scan.detail)
            self.assertNotIn(CANARY, repr(scan))

    def test_errno_and_missing_keys_and_oversized_block(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            image = root / "teleagent"
            image.write_text("bin", encoding="utf-8")
            specs = [("exact", image.resolve())]
            proc = _Proc(root / "proc")
            proc.add(3, image, environ=b"PATH=/usr/bin\0")
            scan = scan_linux_credentials(
                environ={},
                proc_root=proc.root,
                image_specs=specs,
                resolve_exe=proc.resolve_exe,
                read_bytes=proc.read_bytes,
            )
            self.assertEqual(scan.reason, "not_logged_in")
            self.assertIn("keys not in environ (GUI not logged in?)", scan.detail)
            self.assertEqual(scan.keys_missing, 1)

            def denied(path: Path) -> bytes:
                raise OSError(errno.EACCES, CANARY, str(path))

            denied_proc = _Proc(root / "proc-denied")
            denied_proc.add(4, image, environ=_block(_creds()))
            denied_scan = scan_linux_credentials(
                environ={},
                proc_root=denied_proc.root,
                image_specs=specs,
                resolve_exe=denied_proc.resolve_exe,
                read_bytes=denied,
            )
            self.assertEqual(denied_scan.permission_denied, 1)
            self.assertNotIn(CANARY, denied_scan.detail)

            huge = _block(_creds()) + (b"A" * (1024 * 1024))
            huge_proc = _Proc(root / "proc-huge")
            huge_proc.add(5, image, environ=huge)
            huge_scan = scan_linux_credentials(
                environ={},
                proc_root=huge_proc.root,
                image_specs=specs,
                resolve_exe=huge_proc.resolve_exe,
                read_bytes=huge_proc.read_bytes,
            )
            self.assertEqual(huge_scan.reason, "not_logged_in")
            self.assertNotIn(CANARY, huge_scan.detail)

    def test_comm_alone_does_not_count_as_teleagent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            image = root / "teleagent"
            image.write_text("bin", encoding="utf-8")
            proc = _Proc(root / "proc")
            proc.add(8, root / "other", comm="teleagent")

            def unreadable(pid_dir: Path) -> Path:
                raise PermissionError(CANARY)

            scan = scan_linux_credentials(
                environ={},
                proc_root=proc.root,
                image_specs=[("exact", image.resolve())],
                resolve_exe=unreadable,
                read_bytes=proc.read_bytes,
            )
            self.assertEqual(scan.reason, "not_readable")
            self.assertEqual(scan.verified, 0)
            self.assertGreaterEqual(scan.suspected_unreadable, 1)
            self.assertNotIn(CANARY, scan.detail)
            self.assertIsNone(scan.creds)

    def test_prefix_does_not_match_sibling_and_override_replaces_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtimes = root / "runtimes"
            image = runtimes / "node" / "teleagent"
            image.parent.mkdir(parents=True)
            image.write_text("bin", encoding="utf-8")
            evil = root / "runtimes-evil" / "teleagent"
            evil.parent.mkdir()
            evil.write_text("bin", encoding="utf-8")
            specs = linux_image_specs(
                {"TELEAGENT_LINUX_IMAGES": str(runtimes) + os.sep},
                home=root / "unused-home",
                include_system_homes=False,
            )
            self.assertEqual(specs, [("prefix", runtimes.resolve())])
            self.assertNotIn(Path("/opt/TeleAgent/teleagent").resolve(), [item[1] for item in specs])
            proc = _Proc(root / "proc")
            proc.add(1, evil, environ=_block(_creds()))
            proc.add(2, image, environ=_block(_creds()))
            scan = scan_linux_credentials(
                environ={"TELEAGENT_LINUX_IMAGES": str(runtimes) + os.sep},
                proc_root=proc.root,
                resolve_exe=proc.resolve_exe,
                read_bytes=proc.read_bytes,
                home=root / "unused-home",
                include_system_homes=False,
            )
            self.assertTrue(scan.ok, scan.detail)
            self.assertEqual(scan.verified, 1)
            defaults = linux_image_specs({}, home=root, include_system_homes=False)
            self.assertIn(("exact", Path("/opt/TeleAgent/teleagent").resolve()), defaults)
            self.assertIn(("prefix", (root / ".local/share/TeleAgent/runtimes").resolve()), defaults)

    def test_bad_url_is_not_echoed(self):
        scan = scan_linux_credentials(
            environ={"TELEAGENT_URL": f"http://user:{CANARY}@127.0.0.1:4399/extra"},
            proc_root=Path("/does/not/matter"),
        )
        self.assertEqual(scan.reason, "bad_url")
        self.assertNotIn(CANARY, scan.detail)
        self.assertNotIn(CANARY, json.dumps(scan.public_dict()))
        self.assertNotIn("/extra", scan.detail)


class LinuxPathTests(unittest.TestCase):
    def test_posix_external_directory_bounds_and_contained(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "ws"
            workspace.mkdir()
            (workspace / "sub").mkdir()
            (workspace / "sub" / "out.txt").write_text("verified output\n", encoding="utf-8")
            self.assertEqual(
                contained(workspace, "sub/out.txt"),
                (workspace / "sub" / "out.txt").resolve(),
            )
            with self.assertRaises(ValueError):
                contained(workspace, "../out.txt")
            with self.assertRaises(ValueError):
                contained(workspace, "/etc/passwd")

            job = "abc123"
            copy_dir = root / "external-inputs" / job / "0"
            copy_dir.mkdir(parents=True)
            pinned = copy_dir / "ext-input.txt"
            pinned.write_text("pin\n", encoding="utf-8")
            digest = "ab" * 32
            pins = [{"path": str(pinned.resolve()), "sha256": digest}]
            pattern = str(copy_dir.resolve()) + "/*"
            self.assertIsNone(
                hard_reject(
                    {"permission": "external_directory", "patterns": [pattern]},
                    workspace=workspace,
                    external_inputs=pins,
                )
            )
            self.assertIsNotNone(
                hard_reject(
                    {"permission": "external_directory", "patterns": ["/etc/*"]},
                    workspace=workspace,
                    external_inputs=pins,
                )
            )
            parent = str(copy_dir.parent.resolve()) + "/*"
            self.assertIsNotNone(
                hard_reject(
                    {"permission": "external_directory", "patterns": [parent]},
                    workspace=workspace,
                    external_inputs=pins,
                )
            )
            note = root / "notes" / "ext-input.txt"
            note.parent.mkdir()
            note.write_text("pin\n", encoding="utf-8")
            self.assertIsNotNone(
                hard_reject(
                    {"permission": "external_directory", "patterns": [str(note.parent.resolve()) + "/*"]},
                    workspace=workspace,
                    external_inputs=[{"path": str(note.resolve()), "sha256": digest}],
                )
            )
            scope = external_directory_scope(
                {"permission": "external_directory", "patterns": [pattern]},
                pins,
            )
            self.assertEqual(scope[0]["files"], ["ext-input.txt"])
            self.assertTrue(scope[0]["only_pinned"])
            self.assertFalse(scope[0]["truncated"])
            extra = copy_dir / "other.txt"
            extra.write_text("x\n", encoding="utf-8")
            wider = external_directory_scope(
                {"permission": "external_directory", "patterns": [pattern]},
                pins,
            )
            self.assertFalse(wider[0]["only_pinned"])
            extra.unlink()

            real = root / "real-tree" / job / "0"
            real.mkdir(parents=True)
            (real / "ext-input.txt").write_text("pin\n", encoding="utf-8")
            link_root = root / "linked"
            link_root.mkdir()
            (link_root / "external-inputs").symlink_to(root / "real-tree")
            linked_pin = link_root / "external-inputs" / job / "0" / "ext-input.txt"
            self.assertIsNotNone(
                hard_reject(
                    {"permission": "external_directory", "patterns": [str(real.resolve()) + "/*"]},
                    workspace=workspace,
                    external_inputs=[{"path": str(linked_pin), "sha256": digest}],
                )
            )


class LinuxBackendTests(unittest.TestCase):
    def setUp(self):
        install_desktop_lock_isolation(self)

    def test_ids_factory_and_no_stdin_wrap(self):
        self.assertEqual(
            WindowsSupervisedExecutionBackend.backend_id,
            "teleagent.windows.supervised_v1",
        )
        self.assertEqual(
            LinuxSupervisedExecutionBackend.backend_id,
            "teleagent.linux.supervised_v1",
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            windows = WindowsSupervisedExecutionBackend(state_dir=root / "win", client=object())
            self.assertEqual(windows.backend_id, "teleagent.windows.supervised_v1")
            calls: list[str] = []

            def factory():
                calls.append("dial")
                raise AssertionError("dial")

            linux = LinuxSupervisedExecutionBackend(state_dir=root / "linux", client_factory=factory)
            self.assertEqual(calls, [])
            self.assertEqual(linux.backend_id, "teleagent.linux.supervised_v1")
            with self.assertRaises(ValueError):
                LinuxSupervisedExecutionBackend(state_dir=root / "nope", stdin_wrap=True)
            made = get_execution_backend("teleagent-linux", state_dir=root / "factory")
            self.assertIsInstance(made, LinuxSupervisedExecutionBackend)
            alias = get_execution_backend("teleagent.linux.supervised_v1", state_dir=root / "alias")
            self.assertEqual(alias.backend_id, "teleagent.linux.supervised_v1")
            with self.assertRaises(TypeError):
                get_execution_backend("teleagent-linux")

    def test_round_trip_permission_review_and_collect(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            client = _FakeClient()
            state = root / "linux-controller"
            stage = root / "stage"
            stage.mkdir()
            backend = LinuxSupervisedExecutionBackend(state_dir=state, client=client)
            started = backend.start_run(
                title="deliver",
                directory=str(stage),
                instruction="Write delivery.txt",
                artifacts=["delivery.txt"],
                charter={"goal": "Write delivery.txt", "artifacts": ["delivery.txt"]},
            )
            self.assertTrue(started.get("ok"), started.get("error"))
            self.assertEqual(started["backend"], "teleagent.linux.supervised_v1")
            run_id = started["run_id"]
            job = _job(state, run_id)
            sid = job["session_id"]
            self.assertEqual(started["native_handle"], sid)
            client.pending.append(
                {
                    "id": "perm-1",
                    "sessionID": sid,
                    "permission": "edit",
                    "patterns": [str(Path(job["workspace"]) / "delivery.txt")],
                    "metadata": {"diff": "create delivery"},
                }
            )
            _force_scan(state, run_id)
            observed = backend.observe_run(run_id)
            self.assertEqual(observed["controller_state"], "awaiting_permission")
            code, actions = backend.list_pending_actions()
            self.assertEqual(code, 200)
            permission = next(item for item in actions if item["kind"] == "permission")
            self.assertNotIn("scope", permission)
            replied, body = backend.reply_permission(permission["request_id"], "once")
            self.assertEqual(replied, 200, body)
            artifact = Path(job["workspace"]) / "delivery.txt"
            artifact.write_text("verified output\n", encoding="utf-8")
            client.status[sid] = {"type": "idle"}
            client.messages[sid].append({"info": {"role": "assistant", "finish": "stop"}, "parts": []})
            _force_scan(state, run_id)
            reviewed = backend.observe_run(run_id)
            self.assertEqual(reviewed["controller_state"], "awaiting_review", reviewed)
            code, actions = backend.list_pending_actions()
            review = next(item for item in actions if item["kind"] == "review")
            backend.resolve_decision(
                review["request_id"],
                verdict="pass",
                reason="Artifact content and tool evidence accepted",
            )
            collected = backend.collect_result(run_id)
            self.assertTrue(collected["ok"], collected)
            self.assertEqual(collected["state"], "ok")
            self.assertEqual(client.count, 1)

    def test_posix_scope_summary_from_pending_permission(self):
        runs = ROOT / "jobs" / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=runs) as pin_td, tempfile.TemporaryDirectory() as td:
            pin = Path(pin_td) / "ext-input.txt"
            pin.write_text("pin\n", encoding="utf-8")
            digest = hashlib.sha256(pin.read_bytes()).hexdigest()
            root = Path(td)
            client = _FakeClient()
            state = root / "linux-controller"
            stage = root / "stage"
            stage.mkdir()
            backend = LinuxSupervisedExecutionBackend(state_dir=state, client=client)
            started = backend.start_run(
                title="pinned",
                directory=str(stage),
                artifacts=["delivery.txt"],
                charter={
                    "goal": "Read the pinned input and write delivery.txt",
                    "artifacts": ["delivery.txt"],
                    "external_inputs": [{"path": str(pin.resolve()), "sha256": digest}],
                },
            )
            self.assertTrue(started.get("ok"), started.get("error"))
            job = _job(state, started["run_id"])
            copied = job["charter"]["external_inputs"][0]["path"]
            copy_dir = Path(copied).parent
            pattern = str(copy_dir.resolve()) + "/*"
            self.assertIsNone(
                hard_reject(
                    {"permission": "external_directory", "patterns": [pattern]},
                    workspace=job["workspace"],
                    external_inputs=job["charter"]["external_inputs"],
                )
            )
            client.pending.append(
                {
                    "id": "perm-ext",
                    "sessionID": job["session_id"],
                    "permission": "external_directory",
                    "patterns": [pattern],
                }
            )
            _force_scan(state, started["run_id"])
            observed = backend.observe_run(started["run_id"])
            self.assertEqual(observed["controller_state"], "awaiting_permission", observed)
            code, actions = backend.list_pending_actions()
            self.assertEqual(code, 200)
            permission = next(item for item in actions if item["kind"] == "permission")
            scope = permission["scope"]
            self.assertEqual(scope[0]["pattern"], pattern)
            self.assertEqual(scope[0]["files"], ["ext-input.txt"])
            self.assertTrue(scope[0]["only_pinned"])
            self.assertFalse(scope[0]["credential_like"] if "credential_like" in scope[0] else False)


class LinuxReadyTests(unittest.TestCase):
    def _assess(self, **overrides):
        probes = {
            "environ": {},
            "proc_root": Path("/proc-not-used"),
            "include_system_homes": False,
            "process_probe": lambda: {"present": True, "detail": "1 TeleAgent process image(s)"},
            "port_open_fn": lambda host, port: host == "127.0.0.1" and port == 4399,
            "creds_probe": lambda: {
                "ok": True,
                "reason": "proc_environ",
                "detail": "creds discoverable from TeleAgent process environ (values omitted)",
            },
            "session_status_fn": lambda _base: {},
            "jsonschema_probe": lambda: True,
            "display_probe": lambda: {"present": True, "detail": "DISPLAY=:1"},
            "lock_holder_fn": lambda _base: None,
            "running_tip": _TIP,
            "head_tip": _TIP,
            "python_version": (3, 13, 0),
        }
        probes.update(overrides)
        return assess_linux_gui_readiness(**probes)

    def test_port_open_refuses_non_loopback(self):
        with mock.patch(
            "framework.linux_ready.socket.create_connection",
            side_effect=AssertionError("connect"),
        ):
            self.assertFalse(_port_open("10.1.2.3", 80))
            self.assertFalse(_port_open("localhost", 4399))

    def test_no_process(self):
        result = self._assess(
            process_probe=lambda: {"present": False, "detail": "no TeleAgent process"},
            creds_probe=lambda: {
                "ok": False,
                "reason": "no_process",
                "detail": "no TeleAgent process (verified=0, permission_denied=0, keys_missing=0).",
            },
        )
        self.assertFalse(result["ready"])
        self.assertFalse(result["dispatch_allowed"])
        process = next(item for item in result["checks"] if item["name"] == "teleagent_process")
        self.assertEqual(process["code"], "no_process")
        self.assertIn("no TeleAgent process", process["detail"])
        self.assertIn("TeleAgent is not running", process["next_step"])
        self.assertIn(":4399 starts after login", process["next_step"])
        creds = next(item for item in result["checks"] if item["name"] == "creds")
        self.assertIn("no TeleAgent process", creds["next_step"])
        self.assertNotIn(CANARY, json.dumps(result))

    def test_process_but_port_closed(self):
        result = self._assess(port_open_fn=lambda host, port: False)
        self.assertFalse(result["dispatch_allowed"])
        port = next(item for item in result["checks"] if item["name"] == "port")
        self.assertEqual(port["code"], "port_closed")
        self.assertIn("not listening", port["detail"])
        self.assertIn("TeleAgent GUI not logged in", port["next_step"])
        self.assertEqual(result["occupancy"]["state"], "unknown")

    def test_creds_unreadable(self):
        result = self._assess(
            creds_probe=lambda: {
                "ok": False,
                "reason": "not_readable",
                "detail": (
                    "TeleAgent processes found but environ not readable: "
                    "run collab-service as the TeleAgent user or root"
                ),
            },
        )
        self.assertFalse(result["ready"])
        creds = next(item for item in result["checks"] if item["name"] == "creds")
        self.assertEqual(creds["code"], "not_readable")
        self.assertIn(
            "TeleAgent processes found but environ not readable: "
            "run collab-service as the TeleAgent user or root",
            creds["next_step"],
        )
        self.assertEqual(result["occupancy"]["state"], "unknown")

    def test_ready_idle_and_busy(self):
        with mock.patch("win_collab.client.Client", side_effect=AssertionError("dial")):
            ready = self._assess()
        self.assertTrue(ready["ready"])
        self.assertTrue(ready["dispatch_allowed"])
        self.assertEqual(ready["occupancy"]["state"], "idle")
        self.assertEqual(ready["session_count"], 0)
        self.assertTrue(ready["tip_ok"])
        self.assertNotIn(CANARY, json.dumps(ready))
        busy = self._assess(session_status_fn=lambda _base: {"s1": {"type": "busy"}})
        self.assertFalse(busy["ready"])
        self.assertFalse(busy["dispatch_allowed"])
        self.assertEqual(busy["occupancy"]["state"], "busy")
        text = "\n".join(busy["hints"])
        self.assertIn("DO NOT DISPATCH", text)
        self.assertIn("不要派工", text)

    def test_missing_jsonschema_and_display_do_not_block(self):
        result = self._assess(
            jsonschema_probe=lambda: False,
            display_probe=lambda: {"present": False, "detail": "DISPLAY is unset and no X11 socket was found"},
        )
        self.assertTrue(result["dispatch_allowed"])
        schema = next(item for item in result["checks"] if item["name"] == "jsonschema")
        self.assertFalse(schema["ok"])
        self.assertFalse(schema["blocks_dispatch"])
        self.assertEqual(schema["code"], "missing_dependency")
        self.assertEqual(schema["next_step"], "pip install -r requirements.txt")
        display = next(item for item in result["checks"] if item["name"] == "x_display")
        self.assertFalse(display["blocks_dispatch"])
        old = self._assess(python_version=(3, 9, 18))
        self.assertFalse(old["dispatch_allowed"])
        python = next(item for item in old["checks"] if item["name"] == "python")
        self.assertFalse(python["ok"])

    def test_bad_url_and_explicit_creds_stay_out_of_the_report(self):
        env = {key: value for key, value in _creds()}
        env["TELEAGENT_URL"] = f"http://user:{CANARY}@127.0.0.1:4399"
        bad = self._assess(
            environ=env,
            process_probe=lambda: {"present": True, "detail": "1 TeleAgent process image(s)"},
            creds_probe=None,
            session_status_fn=lambda _base: {},
        )
        self.assertNotIn(CANARY, json.dumps(bad))
        port = next(item for item in bad["checks"] if item["name"] == "port")
        self.assertEqual(port["code"], "bad_url")
        env["TELEAGENT_URL"] = "http://127.0.0.1:4399"
        with mock.patch("win_collab.client.Client", side_effect=AssertionError("dial")):
            good = self._assess(
                environ=env,
                creds_probe=None,
                proc_root=Path("/no-such-proc-linux-test"),
                session_status_fn=lambda _base: {},
            )
        self.assertTrue(good["ready"], good["hints"])
        self.assertNotIn(CANARY, json.dumps(good))


class LinuxServiceTests(unittest.TestCase):
    def test_backend_wiring_and_ready_switch(self):
        service = _load_collab_service()
        with tempfile.TemporaryDirectory() as td:
            persist = Path(td)
            linux = service._backend("teleagent-linux", persist)
            alias = service._backend("teleagent_linux", persist)
            self.assertEqual(linux.state_dir, persist.resolve() / "linux-controller")
            self.assertEqual(alias.state_dir, persist.resolve() / "linux-controller")
            self.assertEqual(linux.backend_id, "teleagent.linux.supervised_v1")
            self.assertFalse(linux.stdin_wrap)
        linux_report = {"ready": True, "dispatch_allowed": True, "hints": []}
        windows_report = {"ready": False, "dispatch_allowed": False, "reasons": ["win-only"]}
        with mock.patch.object(service.sys, "platform", "linux"), mock.patch(
            "framework.app_service.assess_linux_gui_readiness",
            return_value=linux_report,
        ) as linux_assess, mock.patch(
            "framework.app_service.assess_win_gui_readiness",
            side_effect=AssertionError("windows"),
        ), redirect_stdout(io.StringIO()):
            self.assertEqual(service.main(["--ready"]), 0)
        linux_assess.assert_called_once()
        self.assertEqual(linux_assess.call_args.kwargs["tip_path"].name, "running_tip.json")
        with mock.patch.object(service.sys, "platform", "win32"), mock.patch(
            "framework.app_service.assess_linux_gui_readiness",
            return_value=linux_report,
        ) as linux_assess, mock.patch(
            "framework.app_service.assess_win_gui_readiness",
            return_value=windows_report,
        ) as windows_assess, redirect_stdout(io.StringIO()):
            self.assertEqual(service.main(["--backend", "teleagent-linux", "--ready"]), 0)
            self.assertEqual(service.main(["--ready"]), 1)
        linux_assess.assert_called_once()
        windows_assess.assert_called_once()
        with mock.patch.object(service.sys, "platform", "linux"), mock.patch(
            "framework.app_service.assess_linux_gui_readiness",
            return_value={"ready": False, "dispatch_allowed": False, "hints": ["no TeleAgent process"]},
        ) as linux_assess, mock.patch(
            "framework.app_service.probe_win_gui_connection",
            side_effect=AssertionError("probe"),
        ), redirect_stdout(io.StringIO()) as buf:
            self.assertEqual(service.main(["--check-gui"]), 1)
        linux_assess.assert_called_once()
        self.assertIn("no TeleAgent process", buf.getvalue())

    def test_once_does_not_dial_and_health_does_not_either(self):
        service = _load_collab_service()
        with tempfile.TemporaryDirectory() as td:
            with mock.patch(
                "execution_backend.linux_supervised_v1.default_linux_client",
                side_effect=AssertionError("dial"),
            ), redirect_stdout(io.StringIO()):
                code = service.main(
                    ["--persist", td, "--backend", "teleagent-linux", "--planner", "deterministic", "--once"]
                )
            self.assertEqual(code, 0)
            calls: list[str] = []

            def factory():
                calls.append("dial")
                raise AssertionError("dial")

            backend = LinuxSupervisedExecutionBackend(state_dir=Path(td) / "linux-controller", client_factory=factory)
            app = CollabApplication(Path(td) / "app", backend=backend)
            server = CollabHttpServer(("127.0.0.1", 0), app)
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as response:
                    body = json.loads(response.read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)
            self.assertTrue(body["ok"])
            self.assertIn("api_version", body)
            self.assertEqual(calls, [])
            with mock.patch(
                "framework.linux_ready.assess_linux_gui_readiness",
                return_value={"ready": False, "dispatch_allowed": False, "hints": ["no TeleAgent process"]},
            ) as assess:
                probe = app._probe_connection()
            self.assertFalse(probe["ok"])
            self.assertIn("no TeleAgent process", probe["reason"])
            assess.assert_called_once()


class LinuxDesktopLockTests(unittest.TestCase):
    def test_posix_flock_and_lock_root_fallback(self):
        from platform_services import get_file_lock, get_platform_services

        self.assertEqual(get_platform_services().kind, "posix")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "locks" / "desktop.lock"
            held = get_file_lock().acquire(target, blocking=False)
            script = (
                "import sys\n"
                "from platform_services import get_file_lock\n"
                "try:\n"
                "    get_file_lock().acquire(sys.argv[1], blocking=False)\n"
                "except BlockingIOError:\n"
                "    print('BUSY')\n"
                "else:\n"
                "    print('GOT')\n"
            )
            env = {
                "PATH": os.environ.get("PATH", ""),
                "PYTHONPATH": os.pathsep.join([str(ROOT), str(SRC)]),
                "HOME": str(root / "home"),
                "LANG": "C.UTF-8",
            }
            try:
                completed = subprocess.run(
                    [sys.executable, "-c", script, str(target)],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
            finally:
                held.unlock_and_close()
            self.assertEqual(completed.stdout.strip(), "BUSY", completed.stderr)
            home = root / "home"
            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                os.environ.pop("LOCALAPPDATA", None)
                os.environ.pop("TELEAGENT_DESKTOP_LOCK_DIR", None)
                with mock.patch("win_collab.desktop_lock.Path.home", return_value=home):
                    found = lock_root()
            self.assertEqual(
                found,
                home / ".local" / "share" / "teleagent-collab" / "desktop-locks",
            )


if __name__ == "__main__":
    unittest.main()
