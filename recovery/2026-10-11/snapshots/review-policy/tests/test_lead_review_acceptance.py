"""Tests for GH-002 / GH-015 lead review verdict acceptance and completion lifecycle.

Verifies:
1. Legacy tasks without review_required retain execution-only completion.
2. Tasks with review_required=True cannot promote Goal to completed or business_accepted=True without explicit ACCEPTED lead review.
3. Unreviewed tasks fail closed (state remains non-completed, business_accepted=False).
4. REJECTED review verdicts fail closed (business_accepted=False, technical_review='rejected').
5. Worker self-authorization (reviewer_role='worker') is rejected.
6. Execution-failed tasks cannot be approved by lead review.
7. Artifact content hash mismatches / corrupted artifacts fail closed before review approval.
8. Duplicate / stale review submission cannot overwrite an already ACCEPTED review verdict.
9. Durable persistence across clean store reopening (no cache, fresh instances).
10. All offline guards and process restrictions honored.
"""
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from framework.app_service import AppError, CollabApplication
from framework.durable_api import DurableLayer
from framework.lead_review import (
    compute_sha256,
    hash_artifacts_in_workspace,
    validate_review_verdict_payload,
    verify_artifact_hashes_match,
)


class TestLeadReviewAcceptance(unittest.TestCase):
    def setUp(self):
        # Child-isolated temp directory
        child_temp = os.environ.get("TEMP") or os.environ.get("TMP")
        self.tmp_dir = tempfile.TemporaryDirectory(dir=child_temp)
        self.persist_root = Path(self.tmp_dir.name) / "durable_store"
        self.persist_root.mkdir(parents=True, exist_ok=True)
        self.workspace_root = Path(self.tmp_dir.name) / "workspaces"
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.app = CollabApplication(persist_root=self.persist_root)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_legacy_execution_only_compatibility(self):
        """Tasks without review_required retain legacy execution-only completion."""
        app = self.app
        submit_res = app.submit({
            "goal": "Legacy task goal without review requirements",
            "desired_outcome": "Finished module",
            "acceptance": {"artifacts": [], "text": "Legacy check"},
        })
        self.assertTrue(submit_res.get("ok"))
        goal_id = submit_res["request_id"]

        layer = DurableLayer.open(self.persist_root, use_cache=False)
        add_res = layer.add_child_task(
            goal_id,
            title="Legacy worker step",
            task={"task_id": "task-legacy-1"},
        )
        self.assertTrue(add_res.get("ok"))
        layer.start_task(goal_id, "task-legacy-1")

        finish_res = layer.finish_task(
            goal_id,
            "task-legacy-1",
            succeeded=True,
            result={"ok": True, "artifacts": []},
            close_goal=True,
        )
        self.assertTrue(finish_res.get("ok"))

        # Goal is completed under legacy behavior
        status = app.status(goal_id)
        self.assertEqual(status["state"], "completed")
        # But legacy execution completion does NOT masquerade as business-accepted
        self.assertFalse(status.get("business_accepted", False))
        self.assertEqual(status["acceptance_status"]["business_acceptance"], "not_performed")

    def test_review_required_blocks_completed_until_accepted(self):
        """When review_required=True, worker execution finish leaves goal in running until lead ACCEPTED."""
        app = self.app
        submit_res = app.submit({
            "goal": "Payment module with mandatory lead review gate",
            "desired_outcome": "Working payment connector",
            "acceptance": {"artifacts": ["payment.py"], "text": "Must produce payment.py"},
        })
        self.assertTrue(submit_res.get("ok"))
        goal_id = submit_res["request_id"]

        # Prepare workspace and artifact
        task_ws = self.workspace_root / goal_id / "task-rev-1"
        task_ws.mkdir(parents=True, exist_ok=True)
        art_path = task_ws / "payment.py"
        art_path.write_text("def process_payment(): return True\n", encoding="utf-8")
        art_hash = compute_sha256(art_path)

        layer = DurableLayer.open(self.persist_root, use_cache=False)
        add_res = layer.add_child_task(
            goal_id,
            title="Implement payment connector",
            task={
                "task_id": "task-rev-1",
                "review_required": True,
                "workspace": str(task_ws),
                "expected_artifacts": ["payment.py"],
            },
        )
        self.assertTrue(add_res.get("ok"))
        layer.start_task(goal_id, "task-rev-1")

        # Worker finishes cleanly
        finish_res = layer.finish_task(
            goal_id,
            "task-rev-1",
            succeeded=True,
            result={
                "ok": True,
                "workspace": str(task_ws),
                "artifacts": [str(art_path)],
            },
            close_goal=True,
        )
        self.assertTrue(finish_res.get("ok"))

        # 1. Unreviewed status check: Goal CANNOT complete; business_accepted is False
        status_before = app.status(goal_id)
        self.assertNotEqual(status_before["state"], "completed")
        self.assertFalse(status_before.get("business_accepted", False))
        self.assertEqual(status_before["acceptance_status"]["business_acceptance"], "needs_review")

        # 2. Submit valid lead review verdict ACCEPTED
        review_payload = {
            "verdict": "ACCEPTED",
            "reviewer_role": "lead_architect",
            "reason": "Payment connector verified with proper boundary error handling",
            "artifact_hashes": {"payment.py": art_hash},
            "workspace": str(task_ws),
            "linked_work_order": "wo-test-01",
        }
        submit_rev = app.submit_review_verdict(goal_id, "task-rev-1", review_payload, caller_authority=app.issue_dispatch_authority(goal_id, "task-rev-1"))
        self.assertTrue(submit_rev.get("ok"))
        self.assertEqual(submit_rev["verdict"], "ACCEPTED")
        self.assertTrue(submit_rev["business_accepted"])
        self.assertEqual(submit_rev["goal_state"], "completed")

        # 3. Final status check: Goal is now completed and business_accepted=True
        status_after = app.status(goal_id)
        self.assertEqual(status_after["state"], "completed")
        self.assertTrue(status_after.get("business_accepted"))
        self.assertEqual(status_after["acceptance_status"]["business_acceptance"], "accepted")
        self.assertEqual(status_after["acceptance_status"]["technical_review"], "accepted")

    def test_rejected_review_verdict_fails_closed(self):
        """Lead review REJECTED prevents completion and marks business_acceptance as rejected."""
        app = self.app
        submit_res = app.submit({
            "goal": "Module with rejected review",
            "desired_outcome": "Defective module to be rejected",
            "acceptance": {"artifacts": [], "text": "Rejection test"},
        })
        goal_id = submit_res["request_id"]

        layer = DurableLayer.open(self.persist_root, use_cache=False)
        layer.add_child_task(
            goal_id,
            title="Worker task",
            task={"task_id": "task-rej-1", "review_required": True},
        )
        layer.start_task(goal_id, "task-rej-1")
        layer.finish_task(
            goal_id,
            "task-rej-1",
            succeeded=True,
            result={"ok": True, "artifacts": []},
            close_goal=True,
        )

        review_payload = {
            "verdict": "REJECTED",
            "reviewer_role": "technical_lead",
            "reason": "Security vulnerabilities identified; rejected for rework",
        }
        submit_rev = app.submit_review_verdict(goal_id, "task-rej-1", review_payload, caller_authority=app.issue_dispatch_authority(goal_id, "task-rej-1"))
        self.assertTrue(submit_rev.get("ok"))
        self.assertEqual(submit_rev["verdict"], "REJECTED")
        self.assertFalse(submit_rev["business_accepted"])

        status = app.status(goal_id)
        self.assertNotEqual(status["state"], "completed")
        self.assertFalse(status.get("business_accepted"))
        self.assertEqual(status["acceptance_status"]["business_acceptance"], "rejected")
        self.assertEqual(status["acceptance_status"]["technical_review"], "rejected")

    def test_worker_cannot_self_authorize_review(self):
        """Worker role cannot submit review verdicts (self-authorization forbidden)."""
        app = self.app
        submit_res = app.submit({
            "goal": "Self-auth defense test",
            "acceptance": {"artifacts": [], "text": "Defense test"},
        })
        goal_id = submit_res["request_id"]

        layer = DurableLayer.open(self.persist_root, use_cache=False)
        layer.add_child_task(
            goal_id,
            title="Worker task",
            task={"task_id": "task-worker-1", "review_required": True},
        )
        layer.start_task(goal_id, "task-worker-1")
        layer.finish_task(goal_id, "task-worker-1", succeeded=True, result={"ok": True})

        invalid_payload = {
            "verdict": "ACCEPTED",
            "reviewer_role": "worker",  # Forbidden
            "reason": "Self-approved by worker",
        }
        with self.assertRaises(AppError) as ctx:
            app.submit_review_verdict(goal_id, "task-worker-1", invalid_payload)
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("cannot be worker", str(ctx.exception))

    def test_failed_execution_cannot_be_approved_by_review(self):
        """A task whose execution failed cannot be business-approved by review."""
        app = self.app
        submit_res = app.submit({
            "goal": "Failed execution test",
            "acceptance": {"artifacts": [], "text": "Fail test"},
        })
        goal_id = submit_res["request_id"]

        layer = DurableLayer.open(self.persist_root, use_cache=False)
        layer.add_child_task(
            goal_id,
            title="Failing worker task",
            task={"task_id": "task-fail-1", "review_required": True},
        )
        layer.start_task(goal_id, "task-fail-1")
        layer.finish_task(
            goal_id,
            "task-fail-1",
            succeeded=False,
            result={"ok": False, "error": "subprocess crashed"},
            close_goal=True,
        )

        review_payload = {
            "verdict": "ACCEPTED",
            "reviewer_role": "lead_architect",
            "reason": "Premature attempt to approve failed task",
        }
        with self.assertRaises(AppError) as ctx:
            app.submit_review_verdict(goal_id, "task-fail-1", review_payload)
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("execution did not succeed", str(ctx.exception))

    def test_tampered_or_wrong_artifact_hashes_fail_closed(self):
        """Content hash mismatch fails closed before review approval."""
        app = self.app
        submit_res = app.submit({
            "goal": "Tampered artifact test",
            "acceptance": {"artifacts": ["binary.bin"], "text": "Tamper test"},
        })
        goal_id = submit_res["request_id"]

        task_ws = self.workspace_root / goal_id / "task-tamper-1"
        task_ws.mkdir(parents=True, exist_ok=True)
        art_path = task_ws / "binary.bin"
        art_path.write_bytes(b"genuine_content_123")

        layer = DurableLayer.open(self.persist_root, use_cache=False)
        layer.add_child_task(
            goal_id,
            title="Worker task",
            task={
                "task_id": "task-tamper-1",
                "review_required": True,
                "workspace": str(task_ws),
                "expected_artifacts": ["binary.bin"],
            },
        )
        layer.start_task(goal_id, "task-tamper-1")
        layer.finish_task(
            goal_id,
            "task-tamper-1",
            succeeded=True,
            result={"ok": True, "workspace": str(task_ws), "artifacts": [str(art_path)]},
        )

        # Submit review specifying wrong/tampered hash
        wrong_hash = hashlib.sha256(b"tampered_or_different_bytes").hexdigest()
        review_payload = {
            "verdict": "ACCEPTED",
            "reviewer_role": "lead_verifier",
            "reason": "Review with mismatching expected hash",
            "artifact_hashes": {"binary.bin": wrong_hash},
            "workspace": str(task_ws),
        }
        with self.assertRaises(AppError) as ctx:
            app.submit_review_verdict(goal_id, "task-tamper-1", review_payload, caller_authority=app.issue_dispatch_authority(goal_id, "task-tamper-1"))
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("hash mismatch", str(ctx.exception))

    def test_duplicate_or_stale_review_cannot_overwrite_accepted_verdict(self):
        """Once ACCEPTED, subsequent duplicate or stale review submissions cannot overwrite it."""
        app = self.app
        submit_res = app.submit({
            "goal": "Idempotency test",
            "acceptance": {"artifacts": [], "text": "Idempotency test"},
        })
        goal_id = submit_res["request_id"]

        layer = DurableLayer.open(self.persist_root, use_cache=False)
        layer.add_child_task(
            goal_id,
            title="Task",
            task={"task_id": "task-idemp-1", "review_required": True},
        )
        layer.start_task(goal_id, "task-idemp-1")
        layer.finish_task(goal_id, "task-idemp-1", succeeded=True, result={"ok": True})

        # 1. Accept
        app.submit_review_verdict(
            goal_id,
            "task-idemp-1",
            {
                "verdict": "ACCEPTED",
                "reviewer_role": "lead_architect",
                "reason": "First review passes",
            },
        caller_authority=app.issue_dispatch_authority(goal_id, "task-idemp-1"),
        )
        status1 = app.status(goal_id)
        self.assertTrue(status1.get("business_accepted"))
        self.assertEqual(status1["state"], "completed")

        # 2. Later stale/duplicate attempt to REJECT or overwrite cannot overwrite
        stale_res = app.submit_review_verdict(
            goal_id,
            "task-idemp-1",
            {
                "verdict": "REJECTED",
                "reviewer_role": "lead_architect",
                "reason": "Late stale rejected verdict",
            },
        caller_authority=app.issue_dispatch_authority(goal_id, "task-idemp-1"),
                    )
        # Idempotency preserves ACCEPTED
        self.assertEqual(stale_res["verdict"], "ACCEPTED")
        status2 = app.status(goal_id)
        self.assertTrue(status2.get("business_accepted"))
        self.assertEqual(status2["state"], "completed")

    def test_store_reopen_preserves_lead_review_and_business_acceptance(self):
        """Full store reload proof: review verdict and business_accepted persist truthfully."""
        app = self.app
        submit_res = app.submit({
            "goal": "Durable reload proof",
            "acceptance": {"artifacts": ["result.txt"], "text": "Durable reload text"},
        })
        goal_id = submit_res["request_id"]

        task_ws = self.workspace_root / goal_id / "task-persist-1"
        task_ws.mkdir(parents=True, exist_ok=True)
        art = task_ws / "result.txt"
        art.write_text("verified_durable_bytes", encoding="utf-8")
        art_hash = compute_sha256(art)

        layer = DurableLayer.open(self.persist_root, use_cache=False)
        layer.add_child_task(
            goal_id,
            title="Durable task",
            task={
                "task_id": "task-persist-1",
                "review_required": True,
                "workspace": str(task_ws),
                "expected_artifacts": ["result.txt"],
            },
        )
        layer.start_task(goal_id, "task-persist-1")
        layer.finish_task(
            goal_id,
            "task-persist-1",
            succeeded=True,
            result={"ok": True, "workspace": str(task_ws), "artifacts": [str(art)]},
        )

        app.submit_review_verdict(
            goal_id,
            "task-persist-1",
            {
                "verdict": "ACCEPTED",
                "reviewer_role": "trusted_configured_lead",
                "reason": "Verified across restart boundaries",
                "artifact_hashes": {"result.txt": art_hash},
                "workspace": str(task_ws),
            },
        caller_authority=app.issue_dispatch_authority(goal_id, "task-persist-1"),
        )

        # Discard in-memory instances
        del app
        del layer

        # Fresh instances
        fresh_layer = DurableLayer.open(self.persist_root, use_cache=False)
        persisted_goal = fresh_layer.goals[goal_id]
        self.assertEqual(persisted_goal.get("state"), "completed")

        fresh_app = CollabApplication(persist_root=self.persist_root)
        reopened_status = fresh_app.status(goal_id)
        self.assertEqual(reopened_status.get("state"), "completed")
        self.assertTrue(reopened_status.get("business_accepted"))
        self.assertEqual(reopened_status["acceptance_status"]["business_acceptance"], "accepted")
        self.assertEqual(reopened_status["acceptance_status"]["technical_review"], "accepted")

    def test_untrusted_caller_fails_closed_with_403(self):
        """Untrusted caller without authority configuration fails closed with 403."""
        untrusted_app = CollabApplication(persist_root=self.persist_root)  # trusted_reviewer_fixture=False
        submit_res = untrusted_app.submit({
            "goal": "Untrusted caller rejection test",
            "acceptance": {"artifacts": [], "text": "Auth test"},
        })
        goal_id = submit_res["request_id"]
        layer = DurableLayer.open(self.persist_root, use_cache=False)
        layer.add_child_task(goal_id, title="Task", task={"task_id": "t1", "review_required": True})
        layer.start_task(goal_id, "t1")
        layer.finish_task(goal_id, "t1", succeeded=True, result={"ok": True})

        with self.assertRaises(AppError) as ctx:
            untrusted_app.submit_review_verdict(goal_id, "t1", {
                "verdict": "ACCEPTED",
                "reviewer_role": "lead_architect",
                "reason": "Should be rejected because app has no trusted reviewer fixture",
            })
        self.assertEqual(ctx.exception.status, 403)
        self.assertIn("untrusted review caller", str(ctx.exception))

    def test_missing_required_artifacts_fails_closed_with_409(self):
        """Missing required artifact files fail closed with 409 even with authorized reviewer."""
        app = self.app
        submit_res = app.submit({
            "goal": "Missing artifact test",
            "acceptance": {"artifacts": ["needed.txt"], "text": "Artifact gate test"},
        })
        goal_id = submit_res["request_id"]
        task_ws = self.workspace_root / goal_id / "t2"
        task_ws.mkdir(parents=True, exist_ok=True)
        # We do NOT create needed.txt

        layer = DurableLayer.open(self.persist_root, use_cache=False)
        layer.add_child_task(goal_id, title="Task", task={
            "task_id": "t2",
            "review_required": True,
            "workspace": str(task_ws),
            "expected_artifacts": ["needed.txt"],
        })
        layer.start_task(goal_id, "t2")
        layer.finish_task(goal_id, "t2", succeeded=True, result={"ok": True, "workspace": str(task_ws)})

        with self.assertRaises(AppError) as ctx:
            app.submit_review_verdict(goal_id, "t2", {
                "verdict": "ACCEPTED",
                "reviewer_role": "lead_architect",
                "reason": "Attempting to approve missing artifact",
                "workspace": str(task_ws),
                "artifact_hashes": {"needed.txt": "somehash"},
            }, caller_authority=app.issue_dispatch_authority(goal_id, "t2"),
            )
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("incomplete, missing", str(ctx.exception))


class TestCoordinatorReviewBootstrap(unittest.TestCase):
    def test_current_caller_dispatches_bound_semantic_review(self):
        """Narrow registry task -> REAL public coordinator, not public planner mapping."""
        import framework.app_service as service
        from execution_backend.inprocess_v1 import InProcessExecutionBackend
        calls = []

        class Reviewer:
            def decide(inner, request, *, schema, cwd, timeout_sec):
                calls.append(request)
                current = request['current_application']
                self.assertEqual(current['task_id'], 'bootstrap-task')
                self.assertTrue(current['run_id'])
                self.assertEqual(current['inputs']['instruction'], 'Write hello artifact')
                self.assertEqual(schema['properties']['application_id']['const'], request['application_id'])
                self.assertEqual(schema['properties']['context_summary']['const'], request['context_summary'])
                self.assertNotIn('caller_authority', json.dumps(request))
                task = app.layer.get_goal(gid)['goal']['tasks'][0]
                self.assertEqual(task['status'], 'succeeded')
                self.assertTrue(task['result']['ok'])
                # Real content check, NOT approval by mere artifact presence.
                self.assertEqual((Path(cwd) / 'hello.txt').read_text(), 'hello from inprocess backend\n')
                return '', {'application_id': request['application_id'],
                            'context_summary': request['context_summary'],
                            'verdict': 'pass', 'reason': 'Exact controlled hello bytes independently checked'}

        with tempfile.TemporaryDirectory(dir=os.environ.get('TEMP')) as tmp:
            # Baseline-compatible RED probe: missing typed config runs the real
            # caller and fails on absent dispatch, rather than an import error.
            config_type = getattr(service, 'CoordinatorReviewConfig', None)
            kwargs = {} if config_type is None else {'reviewer_config': config_type(adapter=Reviewer(), reviewer_role='lead_verifier', timeout_sec=0.5)}
            app = service.CollabApplication(tmp, backend=InProcessExecutionBackend(), **kwargs)
            gid = app.submit({'goal': 'Controlled semantic review bootstrap', 'acceptance': {'artifacts': ['hello.txt']}})['request_id']
            self.assertTrue(app.layer.add_child_task(gid, title='Write hello artifact', task={
                'task_id': 'bootstrap-task', 'review_required': True,
                'inputs': {'instruction': 'Write hello artifact'}, 'expected_artifacts': ['hello.txt']})['ok'])
            outcome = app.coordinator.process_goal(gid)
            self.assertTrue(outcome['ok'])
            self.assertEqual(len(calls), 1, 'current caller never dispatched a configured review')
            self.assertTrue(app.status(gid)['business_accepted'])
            self.assertFalse(app._dispatch_authorities)
            self.assertTrue(config_type is not None)


class TestPublicGoalReviewMapping(unittest.TestCase):
    def test_public_submission_with_review_policy_dispatches_genuine_review(self):
        """Submits goal solely through app.submit with review_policy, coordinates via process_goal,
        and verifies planner decomposition emits review_required and dispatches to LeadAdapter
        without any manual add_child_task injection."""
        import framework.app_service as service
        from execution_backend.inprocess_v1 import InProcessExecutionBackend

        calls = []

        class LeadReviewer:
            def decide(inner, request, *, schema, cwd, timeout_sec):
                calls.append(request)
                current = request['current_application']
                self.assertTrue(current['task_id'])
                self.assertTrue(current['run_id'])
                self.assertEqual((Path(cwd) / 'widget.json').read_text(), '{"widget": true}\n')
                return '', {
                    'application_id': request['application_id'],
                    'context_summary': request['context_summary'],
                    'verdict': 'pass',
                    'reason': 'Widget artifact independently checked and approved'
                }

        class MockBackend(InProcessExecutionBackend):
            def start_run(self, **kwargs):
                ret = super().start_run(**kwargs)
                directory = kwargs.get('directory')
                if directory:
                    (Path(directory) / 'widget.json').write_text('{"widget": true}\n')
                return ret

        with tempfile.TemporaryDirectory(dir=os.environ.get('TEMP')) as tmp:
            reviewer_config = service.CoordinatorReviewConfig(
                adapter=LeadReviewer(),
                reviewer_role='lead_verifier',
                timeout_sec=1.0,
            )
            app = service.CollabApplication(
                tmp,
                backend=MockBackend(),
                reviewer_config=reviewer_config,
            )
            submit_res = app.submit({
                'goal': 'Deploy widget',
                'review_policy': 'always',
                'acceptance': {'artifacts': ['widget.json']},
            })
            self.assertTrue(submit_res.get('ok'))
            gid = submit_res['request_id']

            # NO manual add_child_task fixture!
            outcome = app.coordinator.process_goal(gid)
            self.assertTrue(outcome.get('ok'))
            self.assertEqual(len(calls), 1, "0 != 1 : public submit did not map review_policy to review_required task dispatch")
            self.assertTrue(app.status(gid)['business_accepted'])

    def test_duplicate_completion_not_redispatch_already_accepted_result(self):
        """Duplicate completion/coordination does not re-dispatch or re-review an already accepted task."""
        import framework.app_service as service
        from execution_backend.inprocess_v1 import InProcessExecutionBackend

        calls = []

        class LeadReviewer:
            def decide(inner, request, *, schema, cwd, timeout_sec):
                calls.append(request)
                return '', {
                    'application_id': request['application_id'],
                    'context_summary': request['context_summary'],
                    'verdict': 'pass',
                    'reason': 'Widget artifact checked and approved'
                }

        class MockBackend(InProcessExecutionBackend):
            def start_run(self, **kwargs):
                ret = super().start_run(**kwargs)
                directory = kwargs.get('directory')
                if directory:
                    (Path(directory) / 'widget.json').write_text('{"widget": true}\n')
                return ret

        with tempfile.TemporaryDirectory(dir=os.environ.get('TEMP')) as tmp:
            app = service.CollabApplication(
                tmp,
                backend=MockBackend(),
                reviewer_config=service.CoordinatorReviewConfig(
                    adapter=LeadReviewer(),
                    reviewer_role='lead_verifier',
                    timeout_sec=1.0,
                ),
            )
            submit_res = app.submit({
                'goal': 'Deploy widget idempotent',
                'review_policy': 'always',
                'acceptance': {'artifacts': ['widget.json']},
            })
            gid = submit_res['request_id']
            outcome1 = app.coordinator.process_goal(gid)
            self.assertTrue(outcome1.get('ok'))
            self.assertEqual(len(calls), 1)
            self.assertTrue(app.status(gid)['business_accepted'])

            # Second coordination tick
            outcome2 = app.coordinator.process_goal(gid)
            self.assertTrue(outcome2.get('ok'))
            self.assertEqual(len(calls), 1, "Already accepted result should not re-dispatch review")

    def test_untrusted_caller_direct_verdict_rejection(self):
        """External direct submit_review_verdict from untrusted caller without controller token is rejected."""
        import framework.app_service as service
        from execution_backend.inprocess_v1 import InProcessExecutionBackend

        class DummyAdapter:
            def decide(self, *a, **k):
                return "", {}

        class MockBackend(InProcessExecutionBackend):
            def start_run(self, **kwargs):
                ret = super().start_run(**kwargs)
                directory = kwargs.get('directory')
                if directory:
                    (Path(directory) / 'widget.json').write_text('{"widget": true}\n')
                return ret

        with tempfile.TemporaryDirectory(dir=os.environ.get('TEMP')) as tmp:
            app = service.CollabApplication(
                tmp,
                backend=MockBackend(),
                reviewer_config=service.CoordinatorReviewConfig(
                    adapter=DummyAdapter(),
                    reviewer_role='lead_verifier',
                    timeout_sec=1.0,
                ),
            )
            submit_res = app.submit({
                'goal': 'Untrusted caller test',
                'review_policy': 'always',
                'acceptance': {'artifacts': ['widget.json']},
            })
            gid = submit_res['request_id']
            # Run goal to execute task
            snap = app.layer.get_goal(gid)
            app.coordinator.process_goal(gid)
            goal_snap = app.layer.get_goal(gid)['goal']
            task_id = goal_snap['tasks'][0]['task_id']
            # Direct untrusted review call without token
            with self.assertRaises(service.AppError) as ctx:
                app.submit_review_verdict(gid, task_id, {
                    "verdict": "ACCEPTED",
                    "reviewer_role": "lead_verifier",
                    "reason": "Untrusted external injection",
                })
            self.assertEqual(ctx.exception.status, 403)
            self.assertIn("untrusted review caller", str(ctx.exception))

    def test_missing_artifact_review_fails_closed(self):
        """When expected artifact is missing from workspace, review dispatch fails closed and leaves explicit continuation."""
        import framework.app_service as service
        from execution_backend.inprocess_v1 import InProcessExecutionBackend

        calls = []

        class LeadReviewer:
            def decide(inner, request, *, schema, cwd, timeout_sec):
                calls.append(request)
                return '', {
                    'application_id': request['application_id'],
                    'context_summary': request['context_summary'],
                    'verdict': 'pass',
                    'reason': 'Approved'
                }

        class EmptyMockBackend(InProcessExecutionBackend):
            def start_run(self, **kwargs):
                ret = super().start_run(**kwargs)
                directory = kwargs.get('directory')
                if directory:
                    for p in Path(directory).glob('*'):
                        if p.is_file():
                            p.unlink()
                return ret

        with tempfile.TemporaryDirectory(dir=os.environ.get('TEMP')) as tmp:
            app = service.CollabApplication(
                tmp,
                backend=EmptyMockBackend(),
                reviewer_config=service.CoordinatorReviewConfig(
                    adapter=LeadReviewer(),
                    reviewer_role='lead_verifier',
                    timeout_sec=1.0,
                ),
            )
            submit_res = app.submit({
                'goal': 'Missing artifact test',
                'review_policy': 'always',
                'acceptance': {'artifacts': ['widget.json']},
            })
            gid = submit_res['request_id']
            outcome = app.coordinator.process_goal(gid)
            # Artifact was missing, so execution gate / review fails closed
            self.assertEqual(len(calls), 0, "Reviewer must not be called when artifact is missing")
            status = app.status(gid)
            self.assertFalse(status['business_accepted'])
            # Explicit continuation observable
            self.assertIn(status['state'], {'failed', 'running', 'queued'})

    def test_stale_replay_submit_content_conflict(self):
        """Replay of submit_key with different review_policy/content is rejected as submit_content_conflict."""
        import framework.app_service as service
        from execution_backend.inprocess_v1 import InProcessExecutionBackend

        with tempfile.TemporaryDirectory(dir=os.environ.get('TEMP')) as tmp:
            app = service.CollabApplication(tmp, backend=InProcessExecutionBackend())
            res1 = app.submit({
                'idempotency_key': 'key-replay-1',
                'goal': 'Replay test',
                'review_policy': 'always',
                'acceptance': {'artifacts': ['widget.json']},
            })
            self.assertTrue(res1['ok'])
            self.assertFalse(res1['duplicate'])

            # Exact replay is idempotent duplicate
            res2 = app.submit({
                'idempotency_key': 'key-replay-1',
                'goal': 'Replay test',
                'review_policy': 'always',
                'acceptance': {'artifacts': ['widget.json']},
            })
            self.assertTrue(res2['ok'])
            self.assertTrue(res2['duplicate'])

            # Conflicting review_policy payload is rejected with 409
            with self.assertRaises(service.AppError) as ctx:
                app.submit({
                    'idempotency_key': 'key-replay-1',
                    'goal': 'Replay test',
                    'review_policy': 'never',
                    'acceptance': {'artifacts': ['widget.json']},
                })
            self.assertEqual(ctx.exception.status, 409)
            self.assertEqual(ctx.exception.code, 'submit_content_conflict')

    def test_durable_reopen_retains_acceptance_and_history(self):
        """Verifies DurableLayer and CollabApplication retain prior acceptance and recorded review verdict."""
        import framework.app_service as service
        from execution_backend.inprocess_v1 import InProcessExecutionBackend

        calls = []

        class LeadReviewer:
            def decide(inner, request, *, schema, cwd, timeout_sec):
                calls.append(request)
                return '', {
                    'application_id': request['application_id'],
                    'context_summary': request['context_summary'],
                    'verdict': 'pass',
                    'reason': 'Widget artifact verified'
                }

        class MockBackend(InProcessExecutionBackend):
            def start_run(self, **kwargs):
                ret = super().start_run(**kwargs)
                directory = kwargs.get('directory')
                if directory:
                    (Path(directory) / 'widget.json').write_text('{"widget": true}\n')
                return ret

        with tempfile.TemporaryDirectory(dir=os.environ.get('TEMP')) as tmp:
            app = service.CollabApplication(
                tmp,
                backend=MockBackend(),
                reviewer_config=service.CoordinatorReviewConfig(
                    adapter=LeadReviewer(),
                    reviewer_role='lead_verifier',
                    timeout_sec=1.0,
                ),
            )
            submit_res = app.submit({
                'goal': 'Durable persistence test',
                'review_policy': 'always',
                'acceptance': {'artifacts': ['widget.json']},
            })
            gid = submit_res['request_id']
            outcome = app.coordinator.process_goal(gid)
            self.assertTrue(outcome['ok'])
            self.assertEqual(len(calls), 1)
            self.assertTrue(app.status(gid)['business_accepted'])

            # Re-open durable layer from disk
            app2 = service.CollabApplication(
                tmp,
                backend=MockBackend(),
                reviewer_config=service.CoordinatorReviewConfig(
                    adapter=LeadReviewer(),
                    reviewer_role='lead_verifier',
                    timeout_sec=1.0,
                ),
            )
            status2 = app2.status(gid)
            self.assertTrue(status2['business_accepted'])
            self.assertEqual(status2['state'], 'completed')
            # Verify lead_review is preserved in task snapshot
            snap = app2.layer.get_goal(gid)['goal']
            task = snap['tasks'][0]
            self.assertEqual(task['lead_review']['verdict'], 'ACCEPTED')
            self.assertEqual(task['lead_review']['reviewer_role'], 'lead_verifier')


if __name__ == "__main__":
    unittest.main()
