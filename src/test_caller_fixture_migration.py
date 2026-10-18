"""Independent positive/negative checks of the TEST-ONLY migration helper."""
import tempfile,sys,unittest
from pathlib import Path
from unittest.mock import patch
from execution_backend.antigravity_cli_v1 import AntigravityCliExecutionBackend
from test_synthetic_caller_fixture import fixture_backend,fixture_start
class SyntheticCallerMigrationTests(unittest.TestCase):
 def test_default_backend_still_denies_without_spawn(self):
  with tempfile.TemporaryDirectory()as td:
   be=AntigravityCliExecutionBackend(bin_path=sys.executable,environ={})
   with patch.object(be,'_start_run_locked')as spawn:
    result=be.start_run(title='synthetic',directory=td);spawn.assert_not_called()
   self.assertFalse(result['ok']);self.assertTrue(result['preflight_blocked'])
 def test_configuring_fixture_does_not_grant_arbitrary_task(self):
  with tempfile.TemporaryDirectory()as td:
   be=fixture_backend(AntigravityCliExecutionBackend(bin_path=sys.executable,environ={}))
   with patch.object(be,'_start_run_locked')as spawn:
    result=be.start_run(title='synthetic',directory=td,caller_task_id='ungranted');spawn.assert_not_called()
   self.assertFalse(result['ok'])
 def test_exact_synthetic_binding_dispatch_and_replay_denial(self):
  with tempfile.TemporaryDirectory()as td:
   be=fixture_backend(AntigravityCliExecutionBackend(bin_path=sys.executable,environ={}))
   with patch.object(be,'_start_run_locked',return_value=({}, {'ok':True,'offline_recording':True}))as spawn:
    result=fixture_start(be,title='synthetic',directory=td,caller_task_id='synthetic-exact');self.assertTrue(result['offline_recording']);spawn.assert_called_once()
    replay=be.start_run(title='synthetic',directory=td,caller_task_id='synthetic-exact');self.assertFalse(replay['ok']);self.assertEqual(spawn.call_count,1)
 def test_real_model_binary_never_configured_as_fixture(self):
  with self.assertRaises(AssertionError):fixture_backend(AntigravityCliExecutionBackend(bin_path='agy.exe',environ={}))
if __name__=='__main__':unittest.main()
