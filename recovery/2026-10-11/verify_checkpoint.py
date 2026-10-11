"""Hermes checkpoint verifier; no native/account/service operations."""
from __future__ import annotations
import argparse
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
PATTERNS = {
    "personal-path": r"(?i)(?:[a-z]:[\\/]+(?:Users|codex|home)[\\/]|/(?:home|Users)/[^\s\"']+)",
    "runtime-private-identifier": r"(?i)(?:login\d+-staged|\d{8}_\d{6}_[a-f0-9]{6}|proc_[a-f0-9]{12})",
    "credential-value": r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{30,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)",
    "url-query-secret": r"(?i)https?://[^\s\"']*[?&](?:token|key|password|secret|signature|access_token)=[^\s\"'&]+",
    "literal-secret-assignment": r"(?i)(?:password|api_key|access_token|client_secret)\s*[:=]\s*[\"'][^\"'\n]{8,}[\"']",
}


def validate():
    manifest = json.loads((ROOT / "SOURCE_MANIFEST.json").read_text(encoding="utf-8"))
    compiled = []
    for entry in manifest["files"]:
        path = ROOT / entry["path"]
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        assert digest == entry["original_sha256"] == entry["published_sha256"], entry["path"]
        assert len(raw) == entry["bytes"]
        compile(raw, entry["path"], "exec")  # compile only, no import/exec
        ast.parse(raw)
        compiled.append(entry["path"])
    findings = []
    scanned = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or ".verification-tmp" in path.parts:
            continue
        relative = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8-sig")
        scanned.append(relative)
        # This verifier necessarily contains detection patterns, not private values.
        if path.name == "verify_checkpoint.py":
            tree = ast.parse(text)
            pattern_node = next(n for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PATTERNS" for t in n.targets))
            assert pattern_node.end_lineno is not None
            lines = text.splitlines()
            text = "\n".join("" if pattern_node.lineno <= i <= pattern_node.end_lineno else line for i, line in enumerate(lines, 1))
        for label, pattern in PATTERNS.items():
            if re.search(pattern, text):
                findings.append({"file": relative, "category": label})
        assert path.suffix not in {".env", ".log", ".jsonl", ".pem", ".key"}, relative
    assert not findings, findings
    return {"source_hashes_verified": len(compiled), "compiled_source_files": compiled,
            "privacy_files_scanned": scanned, "privacy_findings": findings,
            "scope": "checkpoint additions only; manual inspection complements bounded patterns"}


def offline_tests():
    import jsonschema  # dependency check before installing the default-deny hook
    denied = []
    def guard(event, args):
        if event.startswith(("subprocess.", "socket.", "ctypes.")) or event in {"os.system", "os.posix_spawn", "os.spawn", "os.exec", "os.fork", "os.startfile"}:
            denied.append(event)
            raise PermissionError("offline verifier denied operation")
        if event == "import" and str(args[0]).split(".")[0] in {"keyring", "win32cred", "execution_backend"}:
            denied.append(event)
            raise PermissionError("offline verifier denied account/credential import")
    sys.addaudithook(guard)
    controls = []
    for event, args in [("subprocess.Popen", ()), ("os.system", ("not-executed",)), ("socket.connect", ())]:
        try:
            sys.audit(event, *args)
        except PermissionError:
            controls.append(event)
        else:
            raise AssertionError("audit control failed")
    def load(name, file):
        spec = importlib.util.spec_from_file_location(name, file)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    suite = unittest.TestSuite()
    temp_root = ROOT / ".verification-tmp"
    temp_root.mkdir(exist_ok=False)
    old_tempdir = tempfile.tempdir
    try:
        tempfile.tempdir = str(temp_root)
        load("native_entry", ROOT / "snapshots/native-entry/native_entry.py")
        native_tests = load("checkpoint_native_tests", ROOT / "snapshots/native-entry/test_native_entry.py")
        load("lease_witness", ROOT / "snapshots/lease-witness/lease_witness.py")
        witness_tests = load("checkpoint_witness_tests", ROOT / "snapshots/lease-witness/lease_witness_test.py")
        for module in (native_tests, witness_tests):
            suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(module))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        assert result.testsRun == 12 and result.wasSuccessful()
        assert len(denied) == len(controls), "unexpected attempted prohibited operation"
        return {"tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
                "audit_positive_controls": controls, "unexpected_denied_operations": 0,
                "scope": "nine fake-runner native-entry + three pure witness tests; no official-service/account operations"}
    finally:
        tempfile.tempdir = old_tempdir
        temp_root.rmdir()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline-tests", action="store_true")
    args = parser.parse_args()
    report = validate()
    if args.offline_tests:
        report["offline_tests"] = offline_tests()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
