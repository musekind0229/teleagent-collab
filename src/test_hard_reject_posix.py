"""POSIX reverse tests for hard_reject's external-inputs copy-dir widening.

Symlink and hardlink setup is skipped on Windows, where creating them needs
privileges. Layouts use Store and isolate_external_inputs when the pin is a
real copy; attack trees are built directly under external-inputs/<job>/<index>/.
"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from win_collab.core import Store, hard_reject, isolate_external_inputs  # noqa: E402


def _pin(path):
    """external_inputs entry shaped like the charter pins hard_reject receives."""
    data = Path(path).read_bytes()
    return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest()}


@unittest.skipIf(
    os.name == "nt",
    "POSIX-only: symlink and hardlink creation need privileges on Windows",
)
class PosixHardRejectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.addCleanup(self.store.db.close)
        self.workspace = Path(self.store.home) / "workspaces" / "jobwork"
        self.workspace.mkdir(parents=True)
        nested = self.workspace / "nested"
        nested.mkdir()
        (nested / "note.txt").write_text("workspace file\n", encoding="utf-8")
        self.ext = Path(self.store.home) / "external-inputs"

    def _request(self, patterns, filepath=None):
        payload = {"permission": "external_directory", "patterns": list(patterns)}
        if filepath is not None:
            payload["metadata"] = {"filepath": str(filepath)}
        return payload

    def _reason(self, patterns, external_inputs, filepath=None, workspace=None):
        return hard_reject(
            self._request(patterns, filepath),
            workspace=str(workspace or self.workspace),
            external_inputs=external_inputs,
        )

    def _assert_rejected(self, patterns, external_inputs, filepath=None, workspace=None):
        reason = self._reason(patterns, external_inputs, filepath, workspace)
        self.assertIsInstance(reason, str, patterns)
        self.assertTrue(reason.strip(), patterns)
        return reason

    def _assert_allowed(self, patterns, external_inputs, filepath=None, workspace=None):
        reason = self._reason(patterns, external_inputs, filepath, workspace)
        self.assertIsNone(reason, (patterns, reason))

    def _isolate(self, job_id, files):
        """Copy files through the production helper. ``files`` is name -> text."""
        source_dir = Path(self.tmp.name) / "caller-files" / job_id
        source_dir.mkdir(parents=True)
        entries = []
        sources = []
        for name, text in files:
            source = source_dir / name
            source.write_text(text, encoding="utf-8")
            sources.append(source)
            entries.append(_pin(source))
        charter = isolate_external_inputs(
            self.store, job_id, {"external_inputs": entries},
        )
        return sources, charter["external_inputs"]

    def _write_pin(self, job, index, name="file.txt", text="pin\n"):
        directory = self.ext / job / str(index)
        directory.mkdir(parents=True)
        path = directory / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_allow_copy_dir_exact_file_and_workspace(self):
        _sources, pins = self._isolate("joballow", [("file.txt", "pinned\n")])
        copy_file = Path(pins[0]["path"])
        copy_dir = copy_file.parent
        self.assertEqual(copy_dir.parent.parent.name, "external-inputs")
        self.assertEqual(copy_dir.name, "0")
        self.assertTrue(_unique_regular(copy_file))

        self._assert_allowed([f"{copy_dir}/*"], pins)
        self._assert_allowed([str(copy_file)], pins)
        self._assert_allowed([f"{copy_dir}/*", str(copy_file)], pins)
        self._assert_allowed([f"{copy_dir / 'file'}*"], pins)
        self._assert_allowed([f"{copy_dir}/*"], pins, filepath=copy_file)
        self._assert_allowed([str(copy_file)], pins, filepath=copy_file)

        self._assert_allowed([f"{self.workspace}/*"], pins)
        self._assert_allowed([str(self.workspace / "nested" / "note.txt")], pins)
        self._assert_allowed([f"{self.workspace / 'nested'}/*"], pins)
        self._assert_allowed([f"{self.workspace / 'nested'}/**"], pins)
        self._assert_allowed(
            [f"{self.workspace}/*", f"{copy_dir}/*"],
            pins,
        )
        self._assert_allowed([f"{self.workspace}/*"], [])
        self._assert_allowed([str(self.workspace / "nested" / "note.txt")], [])

        _both_sources, both = self._isolate("jobtwo", [
            ("a.txt", "first\n"),
            ("b.txt", "second\n"),
        ])
        dir0 = Path(both[0]["path"]).parent
        dir1 = Path(both[1]["path"]).parent
        self.assertEqual(dir0.name, "0")
        self.assertEqual(dir1.name, "1")
        self._assert_allowed([f"{dir0}/*"], both)
        self._assert_allowed([f"{dir1}/*"], both)
        self._assert_allowed([str(Path(both[1]["path"]))], both)
        self._assert_rejected([f"{dir0.parent / '2'}/*"], both)

    def test_reject_symlink_index_job_and_external_inputs_root(self):
        outside = Path(self.tmp.name) / "other-index"
        outside.mkdir()
        (outside / "file.txt").write_text("secret\n", encoding="utf-8")
        job_dir = self.ext / "idxunrel"
        job_dir.mkdir(parents=True)
        (job_dir / "0").symlink_to(outside, target_is_directory=True)
        pin = job_dir / "0" / "file.txt"
        pins = [_pin(pin)]
        with self.subTest("index symlink to an unrelated directory"):
            self._assert_rejected([f"{job_dir / '0'}/*"], pins)
            self._assert_rejected([f"{outside}/*"], pins)

        real = self._write_pin("realjob", "0", text="real-pin\n")
        link_job = self.ext / "linkjob"
        link_job.mkdir()
        (link_job / "0").symlink_to(real.parent, target_is_directory=True)
        linked = link_job / "0" / real.name
        pins = [_pin(linked)]
        with self.subTest("index symlink to another valid copy dir"):
            self._assert_rejected([f"{link_job / '0'}/*"], pins)
            self._assert_rejected([f"{real.parent}/*"], pins)
            self._assert_rejected([str(linked)], pins)

        (self.ext / "joblink").symlink_to(self.ext / "realjob", target_is_directory=True)
        job_pin = self.ext / "joblink" / "0" / real.name
        pins = [_pin(job_pin)]
        with self.subTest("job dir symlink to another job"):
            self._assert_rejected([f"{job_pin.parent}/*"], pins)
            self._assert_rejected([f"{real.parent}/*"], pins)

        unrelated = Path(self.tmp.name) / "unrelated-job" / "0"
        unrelated.mkdir(parents=True)
        (unrelated / "file.txt").write_text("x\n", encoding="utf-8")
        (self.ext / "jobunrel").symlink_to(unrelated.parent, target_is_directory=True)
        unrel_pin = self.ext / "jobunrel" / "0" / "file.txt"
        pins = [_pin(unrel_pin)]
        with self.subTest("job dir symlink to an unrelated directory"):
            self._assert_rejected([f"{unrel_pin.parent}/*"], pins)
            self._assert_rejected([f"{unrelated}/*"], pins)

        real_tree = Path(self.tmp.name) / "real-tree" / "jobE" / "0"
        real_tree.mkdir(parents=True)
        (real_tree / "file.txt").write_text("pin\n", encoding="utf-8")
        decoy = Path(self.tmp.name) / "decoy-root"
        decoy.mkdir()
        (decoy / "external-inputs").symlink_to(
            real_tree.parent.parent, target_is_directory=True,
        )
        root_pin = decoy / "external-inputs" / "jobE" / "0" / "file.txt"
        pins = [_pin(root_pin)]
        with self.subTest("external-inputs root symlink to a differently named tree"):
            self._assert_rejected([f"{root_pin.parent}/*"], pins)
            self._assert_rejected([f"{real_tree}/*"], pins)

        vault = Path(self.tmp.name) / "vault" / "external-inputs" / "jobV" / "0"
        vault.mkdir(parents=True)
        (vault / "file.txt").write_text("pin\n", encoding="utf-8")
        named = Path(self.tmp.name) / "decoy-named"
        named.mkdir()
        (named / "external-inputs").symlink_to(vault.parent.parent, target_is_directory=True)
        named_pin = named / "external-inputs" / "jobV" / "0" / "file.txt"
        pins = [_pin(named_pin)]
        with self.subTest("external-inputs root symlink whose target is also named external-inputs"):
            self._assert_rejected([f"{named_pin.parent}/*"], pins)
            self._assert_rejected([f"{vault}/*"], pins)
            self._assert_rejected([str(named_pin)], pins, filepath=named_pin)

        _sources, good = self._isolate("goodjob", [("file.txt", "ok\n")])
        good_dir = Path(good[0]["path"]).parent
        with self.subTest("a symlink pin does not authorize itself or disturb a real pin"):
            self._assert_allowed([f"{good_dir}/*"], good + pins)
            self._assert_rejected([f"{named_pin.parent}/*"], good + pins)

    def test_reject_symlink_pin_file_pointing_outside(self):
        copy_dir = self.ext / "jobsym" / "0"
        copy_dir.mkdir(parents=True)
        outside = Path(self.tmp.name) / "outside-target.txt"
        outside.write_text("secret-outside\n", encoding="utf-8")
        link = copy_dir / "file.txt"
        link.symlink_to(outside)
        pins = [_pin(link)]
        with self.subTest("symlink file, pattern is the copy dir wildcard"):
            self._assert_rejected([f"{copy_dir}/*"], pins)
            self._assert_rejected([str(link)], pins)
            self._assert_rejected([f"{outside.parent}/*"], pins)
            self._assert_rejected([str(outside)], pins)
            self._assert_rejected([f"{copy_dir}/*"], pins, filepath=link)

        real = self._write_pin("jobsyma", "0", text="real-target\n")
        alias = copy_dir / "alias.txt"
        alias.symlink_to(real)
        pins = [_pin(alias)]
        with self.subTest("symlink file whose target is another valid copy"):
            self._assert_rejected([f"{copy_dir}/*"], pins)
            self._assert_rejected([f"{real.parent}/*"], pins)
            self._assert_rejected([str(real)], pins)
            self._assert_rejected([str(alias)], pins, filepath=alias)

    def test_reject_hardlink_inside_copy_dir(self):
        outside = Path(self.tmp.name) / "hard-outside.txt"
        outside.write_text("hard-secret\n", encoding="utf-8")
        copy_dir = self.ext / "jobhard" / "0"
        copy_dir.mkdir(parents=True)
        inside = copy_dir / "file.txt"
        try:
            os.link(outside, inside)
        except OSError as exc:
            self.skipTest(f"hardlink unsupported: {exc}")
        self.assertGreater(inside.stat().st_nlink, 1)
        self.assertEqual(inside.stat().st_ino, outside.stat().st_ino)
        pins = [_pin(inside)]
        with self.subTest("pattern cannot widen to the outside inode"):
            self._assert_rejected([f"{outside.parent}/*"], pins)
            self._assert_rejected([str(outside)], pins)
            self._assert_rejected([str(outside)], pins, filepath=outside)
        with self.subTest("copy dir refuses the hardlinked pin"):
            self._assert_rejected([f"{copy_dir}/*"], pins)
            self._assert_rejected([str(inside)], pins)
            self._assert_rejected([f"{copy_dir}/*"], pins, filepath=inside)
            self._assert_rejected([str(inside)], [], filepath=inside)

        clean = self._write_pin("jobhardsib", "0", text="clean-pin\n")
        extra_out = Path(self.tmp.name) / "extra-outside.txt"
        extra_out.write_text("extra-secret\n", encoding="utf-8")
        extra_in = clean.parent / "extra.txt"
        try:
            os.link(extra_out, extra_in)
        except OSError as exc:
            self.skipTest(f"hardlink unsupported: {exc}")
        self.assertEqual(clean.stat().st_nlink, 1)
        self.assertGreater(extra_in.stat().st_nlink, 1)
        pins = [_pin(clean)]
        with self.subTest("sibling hardlink poisons the copy dir wildcard"):
            self._assert_rejected([f"{extra_out.parent}/*"], pins)
            self._assert_rejected([str(extra_out)], pins)
            self._assert_rejected([f"{clean.parent}/*"], pins)
            self._assert_rejected([str(extra_in)], pins)
            self._assert_rejected([f"{clean.parent}/*"], pins, filepath=clean)

    def test_reject_directory_symlink_to_root_or_parent(self):
        pinned = self._write_pin("jobdir", "0", text="pin\n")
        copy_dir = pinned.parent
        pins = [_pin(pinned)]
        root_link = copy_dir / "sub"
        root_link.symlink_to("/", target_is_directory=True)
        with self.subTest("subdir symlink to /"):
            self._assert_rejected([f"{root_link}/*"], pins)
            self._assert_rejected([f"{copy_dir}/sub/*"], pins)
            self._assert_rejected([f"{root_link}/etc/*"], pins)

        parent_link = copy_dir / "up"
        parent_link.symlink_to(copy_dir.parent, target_is_directory=True)
        with self.subTest("subdir symlink to the parent job dir"):
            self._assert_rejected([f"{parent_link}/*"], pins)
            self._assert_rejected([f"{copy_dir}/up/*"], pins)

        rel_link = copy_dir / "reldot"
        rel_link.symlink_to("..", target_is_directory=True)
        with self.subTest("relative subdir symlink to .."):
            self._assert_rejected([f"{rel_link}/*"], pins)

        inputs_link = copy_dir / "inputs"
        inputs_link.symlink_to(copy_dir.parent.parent, target_is_directory=True)
        with self.subTest("subdir symlink to the external-inputs root"):
            self._assert_rejected([f"{inputs_link}/*"], pins)

    def test_reject_wildcard_escaping_copy_dir(self):
        _sources, pins = self._isolate("jobesc", [("file.txt", "pinned\n")])
        copy_file = Path(pins[0]["path"])
        copy_dir = copy_file.parent
        job_dir = copy_dir.parent
        (job_dir / "1").mkdir()
        (job_dir / "1" / "secret.txt").write_text("other-index\n", encoding="utf-8")
        other = self.ext / "otherjob" / "0"
        other.mkdir(parents=True)
        (other / "secret.txt").write_text("other-job\n", encoding="utf-8")

        cases = {
            "job dir": f"{job_dir}/*",
            "external-inputs root": f"{self.ext}/*",
            "dotdot into index 1": f"{copy_dir}/../1/*",
            "dotdot above the job": f"{copy_dir}/../../*",
            "unpinned index 1": f"{job_dir / '1'}/*",
            "another job": f"{other}/*",
            "workspace parent": f"{self.workspace.parent}/*",
            "store home": f"{self.store.home}/*",
            "filesystem root": "/*",
            "tmp": "/tmp/*",
            "tilde slash": "~/*",
            "tilde home": "~/secret/*",
            "open index component": f"{job_dir}/0*",
            "open index then slash": f"{copy_dir}*/*",
            "dotdot etc": f"{copy_dir}/../../../*",
        }
        for label, pattern in cases.items():
            with self.subTest(label):
                self._assert_rejected([pattern], pins)
                self._assert_rejected([pattern], pins, filepath=copy_file)

        with self.subTest("one escaping pattern rejects the whole request"):
            self._assert_rejected([f"{copy_dir}/*", "/tmp/*"], pins)
            self._assert_rejected([f"{copy_dir}/*", f"{job_dir}/*"], pins, filepath=copy_file)

        old = os.getcwd()
        try:
            os.chdir(self.workspace)
            with self.subTest("relative pattern from inside the workspace"):
                self._assert_rejected(["nested/note.txt"], pins)
                self._assert_rejected(["./nested/note.txt"], pins)
                self._assert_rejected(["*"], pins)
                self._assert_rejected(["../../*"], pins)
            os.chdir(copy_dir)
            with self.subTest("relative pattern from inside the copy dir"):
                self._assert_rejected(["file.txt"], pins)
                self._assert_rejected(["./*"], pins)
                self._assert_rejected(["*"], pins)
                self._assert_rejected(["../*"], pins)
        finally:
            os.chdir(old)
        with self.subTest("relative pattern independent of cwd"):
            self._assert_rejected(["file.txt"], pins)
            self._assert_rejected(["../file.txt"], pins)

    def test_reject_original_file_parent_wildcard(self):
        source_dir = Path(self.tmp.name) / "caller-files" / "loose"
        source_dir.mkdir(parents=True)
        original = source_dir / "notes.txt"
        original.write_text("original\n", encoding="utf-8")
        pins = [_pin(original)]
        self.assertNotIn("external-inputs", original.parts)
        with self.subTest("parent wildcard is not authorized"):
            self._assert_rejected([f"{original.parent}/*"], pins)
            self._assert_rejected([f"{original.parent}/*"], pins, filepath=original)
            self._assert_rejected([str(original)], pins)
        with self.subTest("workspace patterns still allowed"):
            self._assert_allowed([f"{self.workspace}/*"], pins)
            self._assert_allowed(
                [str(self.workspace / "nested" / "note.txt")],
                pins,
                filepath=self.workspace / "nested" / "note.txt",
            )
        _copied_from, copies = self._isolate("joborig", [("notes.txt", "original\n")])
        copy_dir = Path(copies[0]["path"]).parent
        with self.subTest("the isolated copy is authorized and the source parent is not"):
            self._assert_allowed([f"{copy_dir}/*"], copies)
            self._assert_rejected([f"{_copied_from[0].parent}/*"], copies)
            self._assert_rejected(
                [f"{_copied_from[0].parent}/*"],
                copies,
                filepath=_copied_from[0],
            )

    def test_reject_non_numeric_index_and_unsafe_job_name(self):
        for index_name in ("0a", "-1"):
            path = self._write_pin("jobindex", index_name)
            pins = [_pin(path)]
            with self.subTest(index=index_name):
                self._assert_rejected([f"{path.parent}/*"], pins)
                self._assert_rejected([str(path)], pins)
                self._assert_rejected([f"{path.parent}/*"], pins, filepath=path)

        unsafe = (
            ".hidden",
            "-bad",
            "has space",
            "a" * 82,
        )
        for job_name in unsafe:
            path = self._write_pin(job_name, "0")
            pins = [_pin(path)]
            with self.subTest(job=job_name):
                self._assert_rejected([f"{path.parent}/*"], pins)
                self._assert_rejected([str(path)], pins, filepath=path)

        with self.subTest("purely numeric index of a safe job remains the allow case"):
            path = self._write_pin("jobsafe", "0", text="still-ok\n")
            pins = [_pin(path)]
            self._assert_allowed([f"{path.parent}/*"], pins)
            self._assert_allowed([str(path)], pins)


def _unique_regular(path):
    st = Path(path).stat()
    return path.is_file() and not path.is_symlink() and st.st_nlink == 1


if __name__ == "__main__":
    unittest.main()
