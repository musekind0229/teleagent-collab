#!/usr/bin/env python3
"""v0.3 §5.2: contract boundary + escalation decision semantics."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent
REPO = SRC.parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from framework.delegation import (  # noqa: E402
    KIND_ESCALATE_INSUFFICIENT_AUTH,
    REASON_EMPTY_GRANT,
    REASON_GRANT_EXCEEDS_AUTHORITY,
    REASON_ILLEGAL_VERDICT,
    REASON_SELF_GRANT_FORBIDDEN,
    REASON_STALE_CONTRACT,
)
from framework.durable_api import (  # noqa: E402
    DurableLayer,
    reset_durable_cache,
)
from framework.goal_ownership import (  # noqa: E402
    REASON_CONTRACT_EXPANSION,
    REASON_GATE_REMOVED,
    REASON_PATH_ESCAPE,
    REASON_STALE_OWNERSHIP,
    GoalOwnershipStore,
    reset_goal_ownership_cache,
    validate_plan_revision,
)
from framework.models import CONTRACT_VERSION  # noqa: E402
from framework.path_scope import (  # noqa: E402
    PLATFORM_POSIX,
    PLATFORM_WINDOWS,
    allow_item_covered,
    execution_path_allowed,
    pattern_covered,
)


def _plan(goal_id: str, tasks: list[dict], **extra) -> dict:
    body = {"goal_id": goal_id, "tasks": tasks}
    body.update(extra)
    return body


class _S52Case(unittest.TestCase):
    def setUp(self):
        reset_durable_cache()
        reset_goal_ownership_cache()

    def tearDown(self):
        reset_durable_cache()
        reset_goal_ownership_cache()


class TestPathScopeNotStringMatch(unittest.TestCase):
    def test_dotdot_escape_not_covered_by_tree(self):
        ok, why = allow_item_covered(
            "/safe/../outside/**",
            ["/safe/**"],
            platform=PLATFORM_POSIX,
        )
        self.assertFalse(ok)
        self.assertEqual(why, "path_escape")

    def test_prefix_sibling_not_covered(self):
        ok, why = allow_item_covered("/safe-evil/x", ["/safe"], platform=PLATFORM_POSIX)
        self.assertFalse(ok)
        self.assertEqual(why, "not_covered")

    def test_tree_covers_descendant_and_subglob(self):
        self.assertTrue(pattern_covered("/safe/foo", "/safe/**", platform=PLATFORM_POSIX)[0])
        self.assertTrue(pattern_covered("/safe/foo/**", "/safe/**", platform=PLATFORM_POSIX)[0])
        self.assertTrue(pattern_covered("/tmp", "/tmp", platform=PLATFORM_POSIX)[0])
        self.assertFalse(pattern_covered("/**", "/safe/**", platform=PLATFORM_POSIX)[0])

    def test_windows_lexical_escape_and_case(self):
        ok, why = allow_item_covered(
            r"C:\safe\..\outside\**",
            [r"C:\safe\**"],
            platform=PLATFORM_WINDOWS,
        )
        self.assertFalse(ok)
        self.assertEqual(why, "path_escape")
        self.assertTrue(
            pattern_covered(r"C:\SAFE\foo", r"C:\safe\**", platform=PLATFORM_WINDOWS)[0]
        )
        self.assertFalse(
            pattern_covered("/SAFE/foo", "/safe/**", platform=PLATFORM_POSIX)[0]
        )

    def test_execution_revalidate_posix_realpath(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "safe"
            root.mkdir()
            target = root / "inside.txt"
            target.write_text("ok", encoding="utf-8")
            self.assertTrue(execution_path_allowed(str(target), [str(root)], platform=PLATFORM_POSIX))
            outside = Path(td) / "outside.txt"
            outside.write_text("no", encoding="utf-8")
            self.assertFalse(execution_path_allowed(str(outside), [str(root)], platform=PLATFORM_POSIX))
            if os.name != "nt":
                link = Path(td) / "link_out"
                try:
                    os.symlink(str(outside), str(link))
                except OSError:
                    self.skipTest("symlink not permitted")
                self.assertFalse(
                    execution_path_allowed(str(link), [str(root)], platform=PLATFORM_POSIX)
                )


class TestPlanContractBoundary(_S52Case):
    def _contract(self) -> dict:
        return {
            "budget": {"wall_sec": 360, "max_reworks": 1},
            "boundaries": {
                "must": ["stay in workspace"],
                "must_not": ["deploy"],
                "allow_paths": ["/safe/**"],
                "user_gate_permissions": ["sudo", "human_approval"],
                "path_platform": "posix",
            },
        }

    def test_path_escape_plan_rejected(self):
        contract = self._contract()
        current = _plan(
            "g1",
            [{"task_id": "t1", "goal_id": "g1", "status": "queued"}],
            **contract,
        )
        escaped = _plan(
            "g1",
            [
                {
                    "task_id": "t1",
                    "goal_id": "g1",
                    "status": "queued",
                    "inputs": {"allow_paths": ["/safe/../outside/**"]},
                }
            ],
        )
        ok, why = validate_plan_revision(
            escaped, goal_id="g1", current_plan=current, goal_contract=contract
        )
        self.assertFalse(ok)
        self.assertIn("path_escape", why)

        with tempfile.TemporaryDirectory() as td:
            store = GoalOwnershipStore.open("g1", td)
            claimed = store.claim("coord_a", initial_plan=current, goal_contract=contract)
            self.assertTrue(claimed["ok"], claimed)
            out = store.submit_plan_revision(
                coordinator_id="coord_a",
                ownership_version=1,
                plan=escaped,
            )
            self.assertFalse(out["ok"], out)
            self.assertEqual(out["reason"], REASON_PATH_ESCAPE)
            self.assertEqual(store.plan_revision, 1)

    def test_dropping_required_human_gate_rejected(self):
        contract = self._contract()
        current = _plan(
            "g1",
            [{"task_id": "t1", "goal_id": "g1", "status": "queued"}],
            **contract,
        )
        dropped = _plan(
            "g1",
            [{"task_id": "t1", "goal_id": "g1", "status": "queued"}],
            budget=contract["budget"],
            boundaries={
                "must": ["stay in workspace"],
                "must_not": ["deploy"],
                "allow_paths": ["/safe/**"],
                "user_gate_permissions": [],
            },
        )
        ok, why = validate_plan_revision(
            dropped, goal_id="g1", current_plan=current, goal_contract=contract
        )
        self.assertFalse(ok)
        self.assertIn("required_gate_removed", why)

        partial = _plan(
            "g1",
            [{"task_id": "t1", "goal_id": "g1", "status": "queued"}],
            boundaries={
                "must": ["stay in workspace"],
                "must_not": ["deploy"],
                "allow_paths": ["/safe/**"],
                "user_gate_permissions": ["sudo"],
            },
        )
        ok, why = validate_plan_revision(
            partial, goal_id="g1", current_plan=current, goal_contract=contract
        )
        self.assertFalse(ok)
        self.assertIn("required_gate_removed", why)

        with tempfile.TemporaryDirectory() as td:
            store = GoalOwnershipStore.open("g1", td)
            self.assertTrue(store.claim("coord_a", initial_plan=current, goal_contract=contract)["ok"])
            out = store.submit_plan_revision(
                coordinator_id="coord_a",
                ownership_version=1,
                plan=dropped,
            )
            self.assertFalse(out["ok"], out)
            self.assertEqual(out["reason"], REASON_GATE_REMOVED)

    def test_omitted_auth_fields_inherit(self):
        contract = self._contract()
        current = _plan(
            "g1",
            [{"task_id": "t1", "goal_id": "g1", "status": "queued"}],
            **contract,
        )
        inherited = _plan(
            "g1",
            [
                {"task_id": "t1", "goal_id": "g1", "status": "queued"},
                {"task_id": "t2", "goal_id": "g1", "status": "queued", "title": "split"},
            ],
        )
        ok, why = validate_plan_revision(
            inherited, goal_id="g1", current_plan=current, goal_contract=contract
        )
        self.assertTrue(ok, why)

        with tempfile.TemporaryDirectory() as td:
            store = GoalOwnershipStore.open("g1", td)
            self.assertTrue(store.claim("coord_a", initial_plan=current, goal_contract=contract)["ok"])
            out = store.submit_plan_revision(
                coordinator_id="coord_a",
                ownership_version=1,
                plan=inherited,
            )
            self.assertTrue(out["ok"], out)
            self.assertEqual(store.goal_contract["boundaries"]["user_gate_permissions"], ["sudo", "human_approval"])
            self.assertEqual(store.goal_contract["boundaries"]["allow_paths"], ["/safe/**"])

    def test_true_expansion_still_rejected(self):
        contract = self._contract()
        current = _plan(
            "g1",
            [{"task_id": "t1", "goal_id": "g1", "status": "queued"}],
            **contract,
        )
        expanded = _plan(
            "g1",
            [{"task_id": "t1", "goal_id": "g1", "status": "queued"}],
            boundaries={"allow_paths": ["/safe/**", "/**"]},
        )
        ok, why = validate_plan_revision(
            expanded, goal_id="g1", current_plan=current, goal_contract=contract
        )
        self.assertFalse(ok)
        self.assertIn("allow", why)
        self.assertNotIn("path_escape", why)
        with tempfile.TemporaryDirectory() as td:
            store = GoalOwnershipStore.open("g1", td)
            self.assertTrue(store.claim("coord_a", initial_plan=current, goal_contract=contract)["ok"])
            out = store.submit_plan_revision(
                coordinator_id="coord_a",
                ownership_version=1,
                plan=expanded,
            )
            self.assertFalse(out["ok"], out)
            self.assertEqual(out["reason"], REASON_CONTRACT_EXPANSION)


class TestEscalationDecisions(_S52Case):
    def _open_auth_goal(self, td: str, *, start_task: bool = True) -> tuple[DurableLayer, str, str, str]:
        layer = DurableLayer.open(td)
        opened = layer.submit_goal(
            submit_key="sk-s52",
            title="bounded work",
            desired_outcome="stay in /safe",
            coordinator_id="coord-a",
            submitter_id="upper-1",
            autonomy="bounded_autonomy",
            goal={
                "budget": {"wall_sec": 120},
                "boundaries": {
                    "must": ["workspace"],
                    "must_not": ["deploy"],
                    "allow_paths": ["/safe/**"],
                    "user_gate_permissions": ["sudo"],
                },
            },
            submitter_authority={
                "budget": {"wall_sec": 120},
                "boundaries": {
                    "allow_paths": ["/safe/**", "/outside/data"],
                    "user_gate_permissions": ["sudo"],
                },
            },
            tasks=[{"task_id": "t-work", "title": "work", "status": "queued"}],
        )
        self.assertTrue(opened["ok"], opened)
        gid = opened["goal_id"]
        if start_task:
            started = layer.start_task(gid, "t-work")
            self.assertTrue(started["ok"], started)
            self.assertEqual(started["task"]["status"], "running")
        esc = layer.escalate_to_upper(
            gid,
            kind="insufficient_auth",
            reason="need /outside/data",
            task_id="t-work",
            details={"requested": {"allow_paths": ["/outside/data"]}},
        )
        self.assertTrue(esc["ok"], esc)
        self.assertEqual(esc["decision"]["kind"], KIND_ESCALATE_INSUFFICIENT_AUTH)
        did = esc["decision"]["decision_id"]
        return layer, gid, did, "t-work"

    def _task_status(self, layer: DurableLayer, gid: str, tid: str) -> str:
        for t in layer.get_goal(gid)["goal"]["tasks"]:
            if t.get("task_id") == tid:
                return str(t.get("status") or "")
        return ""

    def test_local_self_approve_rejected_pending_remains(self):
        with tempfile.TemporaryDirectory() as td:
            layer, gid, did, tid = self._open_auth_goal(td)
            self.assertEqual(self._task_status(layer, gid, tid), "awaiting_decision")
            self.assertEqual(len(layer.get_goal(gid)["goal"]["pending_decisions"]), 1)

            out = layer.resolve_decision(
                gid,
                decision_id=did,
                verdict="approve",
                reason="I got this",
                actor_id="coord-a",
                ownership_version=1,
                grant={"allow_paths": ["/outside/data"]},
                contract_version=CONTRACT_VERSION,
            )
            self.assertFalse(out["ok"], out)
            self.assertEqual(out["reason"], REASON_SELF_GRANT_FORBIDDEN)
            pending = layer.get_goal(gid)["goal"]["pending_decisions"]
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["decision_id"], did)
            self.assertEqual(pending[0]["kind"], KIND_ESCALATE_INSUFFICIENT_AUTH)
            self.assertEqual(self._task_status(layer, gid, tid), "awaiting_decision")

            junk = layer.resolve_decision(
                gid,
                decision_id=did,
                verdict="sure_why_not",
                reason="non-empty is not a class",
                actor_id="coord-a",
            )
            self.assertFalse(junk["ok"], junk)
            self.assertEqual(junk["reason"], REASON_ILLEGAL_VERDICT)
            self.assertEqual(len(layer.get_goal(gid)["goal"]["pending_decisions"]), 1)
            self.assertEqual(self._task_status(layer, gid, tid), "awaiting_decision")

    def test_upper_grant_takes_effect(self):
        with tempfile.TemporaryDirectory() as td:
            layer, gid, did, tid = self._open_auth_goal(td)
            fp = layer.get_goal(gid)["goal"]["pending_decisions"][0]["binding"]["contract_fingerprint"]
            out = layer.resolve_decision(
                gid,
                decision_id=did,
                verdict="approve",
                reason="upper grants /outside/data",
                actor_id="upper-1",
                grant={"allow_paths": ["/outside/data"]},
                contract_version=CONTRACT_VERSION,
                contract_fingerprint=fp,
            )
            self.assertTrue(out["ok"], out)
            self.assertTrue(out["grant_applied"])
            self.assertTrue(out["task_resumed"])
            self.assertEqual(len(layer.get_goal(gid)["goal"]["pending_decisions"]), 0)
            self.assertEqual(self._task_status(layer, gid, tid), "running")
            paths = layer.get_goal(gid)["goal"]["goal"]["boundaries"]["allow_paths"]
            self.assertIn("/outside/data", paths)
            store = GoalOwnershipStore.open(gid, td)
            self.assertIn("/outside/data", store.goal_contract["boundaries"]["allow_paths"])

    def test_upper_cannot_exceed_own_authority(self):
        with tempfile.TemporaryDirectory() as td:
            layer, gid, did, tid = self._open_auth_goal(td)
            out = layer.resolve_decision(
                gid,
                decision_id=did,
                verdict="approve",
                reason="try /etc",
                actor_id="upper-1",
                grant={"allow_paths": ["/etc/shadow"]},
                contract_version=CONTRACT_VERSION,
            )
            self.assertFalse(out["ok"], out)
            self.assertEqual(out["reason"], REASON_GRANT_EXCEEDS_AUTHORITY)
            self.assertEqual(len(layer.get_goal(gid)["goal"]["pending_decisions"]), 1)
            self.assertEqual(self._task_status(layer, gid, tid), "awaiting_decision")

            unnamed = layer.escalate_to_upper(
                gid, kind="insufficient_auth", reason="no requested scope"
            )
            self.assertTrue(unnamed["ok"], unnamed)
            empty = layer.resolve_decision(
                gid,
                decision_id=unnamed["decision"]["decision_id"],
                verdict="approve",
                reason="unnamed expansion",
                actor_id="upper-1",
                contract_version=CONTRACT_VERSION,
            )
            self.assertFalse(empty["ok"], empty)
            self.assertEqual(empty["reason"], REASON_EMPTY_GRANT)
            self.assertEqual(len(layer.get_goal(gid)["goal"]["pending_decisions"]), 2)

    def test_stale_contract_old_owner_duplicate_do_not_resume(self):
        with tempfile.TemporaryDirectory() as td:
            layer, gid, did, tid = self._open_auth_goal(td)
            self.assertEqual(self._task_status(layer, gid, tid), "awaiting_decision")

            stale_cv = layer.resolve_decision(
                gid,
                decision_id=did,
                verdict="approve",
                reason="old schema",
                actor_id="upper-1",
                grant={"allow_paths": ["/outside/data"]},
                contract_version="contract.v0-old",
            )
            self.assertFalse(stale_cv["ok"], stale_cv)
            self.assertEqual(stale_cv["reason"], REASON_STALE_CONTRACT)
            self.assertEqual(len(layer.get_goal(gid)["goal"]["pending_decisions"]), 1)
            self.assertEqual(self._task_status(layer, gid, tid), "awaiting_decision")

            stale_fp = layer.resolve_decision(
                gid,
                decision_id=did,
                verdict="approve",
                reason="wrong fingerprint",
                actor_id="upper-1",
                grant={"allow_paths": ["/outside/data"]},
                contract_version=CONTRACT_VERSION,
                contract_fingerprint="deadbeef" * 3,
            )
            self.assertFalse(stale_fp["ok"], stale_fp)
            self.assertEqual(stale_fp["reason"], REASON_STALE_CONTRACT)
            self.assertEqual(self._task_status(layer, gid, tid), "awaiting_decision")

            hand = layer.handoff_coordinator(
                gid,
                from_coordinator_id="coord-a",
                to_coordinator_id="coord-b",
                expected_version=1,
            )
            self.assertTrue(hand["ok"], hand)
            old_owner = layer.resolve_decision(
                gid,
                decision_id=did,
                verdict="approve",
                reason="old coordinator",
                actor_id="coord-a",
                ownership_version=1,
                grant={"allow_paths": ["/outside/data"]},
                contract_version=CONTRACT_VERSION,
            )
            self.assertFalse(old_owner["ok"], old_owner)
            self.assertIn(
                old_owner["reason"],
                {REASON_SELF_GRANT_FORBIDDEN, REASON_STALE_OWNERSHIP, "actor_not_authorized"},
            )
            self.assertEqual(len(layer.get_goal(gid)["goal"]["pending_decisions"]), 1)
            self.assertEqual(self._task_status(layer, gid, tid), "awaiting_decision")

            ok = layer.resolve_decision(
                gid,
                decision_id=did,
                verdict="grant",
                reason="upper after handoff still owns the grant",
                actor_id="upper-1",
                grant={"allow_paths": ["/outside/data"]},
                contract_version=CONTRACT_VERSION,
            )
            self.assertTrue(ok["ok"], ok)
            self.assertTrue(ok["task_resumed"])
            self.assertEqual(self._task_status(layer, gid, tid), "running")

            dup = layer.resolve_decision(
                gid,
                decision_id=did,
                verdict="approve",
                reason="again",
                actor_id="upper-1",
                grant={"allow_paths": ["/outside/data"]},
                contract_version=CONTRACT_VERSION,
            )
            self.assertTrue(dup["ok"], dup)
            self.assertEqual(dup["reason"], "already_resolved")
            self.assertFalse(dup.get("task_resumed"))
            self.assertFalse(dup.get("grant_applied"))
            self.assertEqual(self._task_status(layer, gid, tid), "running")
            self.assertEqual(len(layer.get_goal(gid)["goal"]["pending_decisions"]), 0)


if __name__ == "__main__":
    unittest.main()
