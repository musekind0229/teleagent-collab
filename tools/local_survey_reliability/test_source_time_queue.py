import json
import os
from pathlib import Path
import unittest
from memory_claim_rules import annotate_source_times
from survey_reliability import publish_queue_pointer


class SourceTimeAndQueueTests(unittest.TestCase):
    def test_saved_document_mtime_visible_not_claim_time(self):
        rows=[{'quote_LOCAL_ONLY':'SYNTHETIC','sources_LOCAL_ONLY':[{'source_LOCAL_ONLY':'synthetic.md','event_time_raw':None}]}]
        result=annotate_source_times(rows,[{'source_path_LOCAL_ONLY':'synthetic.md','fs_mtime_snapshot_not_creation':'2001-01-02T03:04:05+00:00'}])
        ref=result[0]['sources_LOCAL_ONLY'][0]
        self.assertEqual(ref['visible_time_evidence_kind'],'source_file_mtime_snapshot_not_claim_creation')
        self.assertIsNone(ref['event_time_raw'])
        self.assertNotIn('source_snapshot_mtime_not_claim_creation',rows[0]['sources_LOCAL_ONLY'][0])

    def test_raw_event_time_not_repaired(self):
        rows=[{'sources_LOCAL_ONLY':[{'source_LOCAL_ONLY':'synthetic.md','event_time_raw':'malformed-literal-token'}]}]
        result=annotate_source_times(rows,[])
        self.assertEqual(result[0]['sources_LOCAL_ONLY'][0]['event_time_raw'],'malformed-literal-token')
        self.assertEqual(result[0]['sources_LOCAL_ONLY'][0]['visible_time_evidence_kind'],'unknown')

    def test_valid_event_and_historical_marker(self):
        rows=[{'time_qualified':True,'sources_LOCAL_ONLY':[{'source_LOCAL_ONLY':'synthetic.jsonl','event_time_raw':'2001-01-02T03:04:05+00:00'}]}]
        result=annotate_source_times(rows,[])
        self.assertTrue(result[0]['time_review_required'])
        self.assertEqual(result[0]['sources_LOCAL_ONLY'][0]['visible_time_evidence_kind'],'event_timestamp_declared')

    def test_pointer_follows_authoritative_stage_not_old_B_label(self):
        root=Path(os.environ['TEST_WORKSPACE']);q=root/'synthetic-queue.json';p=root/'synthetic-pointer.json'
        q.write_text(json.dumps({'owner':'synthetic-owner','active_stage':'D','running':False,'executor':None,'last_update':'synthetic-time','next_trigger':'synthetic-review'}))
        p.write_text(json.dumps({'state':'B_active'}))
        value=publish_queue_pointer(q,p)
        self.assertEqual(value['active_stage'],'D')
        self.assertFalse(value['running_native_host_claim'])
        self.assertTrue(value['liveness_requires_pid_birth_check'])
        self.assertEqual(json.loads(p.read_bytes())['active_stage'],'D')

    def test_queued_not_running_executor(self):
        root=Path(os.environ['TEST_WORKSPACE']);q=root/'synthetic-queue2.json';p=root/'synthetic-pointer2.json';q.write_text(json.dumps({'active_stage':'E','executor':None}))
        self.assertFalse(publish_queue_pointer(q,p)['running_native_host_claim'])


if __name__=='__main__':unittest.main()
