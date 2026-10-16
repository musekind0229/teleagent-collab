import unittest
from memory_claim_rules import classify_claim, user_texts, has_secret_material, memory_blocks


class MemoryAttributionTests(unittest.TestCase):
    def claim(self, text, kind):
        result = classify_claim(text, kind)
        assert isinstance(result, dict), 'expected a classified synthetic claim'
        return result

    def test_saved_claim_not_verified_identity(self):
        r = self.claim('用户喜欢合成示例中的绿色。', 'memory_document')
        self.assertEqual(r['kind'], 'persisted_memory_claim')
        self.assertIn('not_independently_verified', r['attribution'])

    def test_user_role_identity_stays_unverified(self):
        self.assertEqual(self.claim('I prefer synthetic green.', 'ordinary_session')['attribution'], 'user_role_human_identity_unverified')

    def test_chinese_third_person(self):
        self.assertEqual(self.claim('我的朋友喜欢绿色。', 'ordinary_session')['attribution'], 'do_not_assign_to_user')

    def test_english_third_person(self):
        self.assertEqual(self.claim('My friend likes green.', 'ordinary_session')['attribution'], 'do_not_assign_to_user')

    def test_quoted_not_user_fact(self):
        self.assertEqual(self.claim('“我喜欢绿色。”', 'ordinary_session')['attribution'], 'do_not_assign_to_user')

    def test_question_not_fact(self):
        self.assertEqual(self.claim('I prefer green?', 'ordinary_session')['kind'], 'question_not_verified_fact')

    def test_request_not_stable_fact(self):
        self.assertEqual(self.claim('请帮我，我想制作一个合成页面。', 'ordinary_session')['kind'], 'request_or_goal_not_stable_fact')

    def test_historical_marker_preserved(self):
        self.assertTrue(self.claim('我以前喜欢绿色，现在不再喜欢。', 'ordinary_session')['time_qualified'])

    def test_saved_environment_not_live_system_fact(self):
        self.assertEqual(self.claim('本机有一个合成测试目录。', 'memory_document')['kind'], 'saved_environment_claim_not_current_system_state')

    def test_peer_message_not_human(self):
        self.assertEqual(self.claim('Message from 🤖 helper: I prefer green.', 'ordinary_session')['confidence'], 'not_user_fact')

    def test_compaction_not_original_user(self):
        self.assertEqual(self.claim('[CONTEXT COMPACTION] I prefer green.', 'ordinary_session')['confidence'], 'not_user_fact')

    def test_tool_result_not_user_text(self):
        record = {'message': {'role': 'user', 'content': [{'type': 'tool_result', 'content': 'I like green.'}]}}
        self.assertEqual(user_texts(record), [])

    def test_assistant_not_user_text(self):
        self.assertEqual(user_texts({'message': {'role': 'assistant', 'content': 'I like green.'}}), [])

    def test_claude_text_schema(self):
        self.assertEqual(user_texts({'message': {'role': 'user', 'content': [{'type': 'text', 'text': 'SYNTHETIC'}]}}), ['SYNTHETIC'])

    def test_codex_event_schema(self):
        self.assertEqual(user_texts({'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'SYNTHETIC'}}), ['SYNTHETIC'])

    def test_codex_response_schema(self):
        self.assertEqual(user_texts({'type': 'response_item', 'payload': {'role': 'user', 'content': [{'type': 'input_text', 'text': 'SYNTHETIC'}]}}), ['SYNTHETIC'])

    def test_meta_record_not_human(self):
        self.assertEqual(user_texts({'isMeta': True, 'message': {'role': 'user', 'content': 'I like green.'}}), [])

    def test_secret_assignment(self):
        self.assertTrue(has_secret_material('api_key = ' + ('x' * 32)))

    def test_short_password_still_sensitive(self):
        self.assertTrue(has_secret_material('password=' + 'tiny'))

    def test_redaction_exact_only(self):
        self.assertFalse(has_secret_material('api_key = "[REDACTED]"'))
        self.assertTrue(has_secret_material('api_key = "not-really-redacted-data"'))

    def test_provider_marker(self):
        self.assertTrue(has_secret_material('sk' + '-' + ('x' * 32)))

    def test_regular_memory_statement(self):
        self.assertFalse(has_secret_material('用户喜欢合成测试，不提供任何凭据。'))

    def test_saved_third_person(self):
        self.assertEqual(self.claim('用户的朋友喜欢合成绿色。', 'memory_document')['attribution'], 'do_not_assign_to_user')

    def test_agent_persona_not_user(self):
        self.assertEqual(self.claim('I am Claude, an assistant.', 'memory_document')['confidence'], 'not_user_fact')

    def test_bullet_first_person(self):
        self.assertEqual(self.claim('- 我喜欢合成绿色。', 'ordinary_session')['kind'], 'explicit_statement_in_user_role')

    def test_contiguous_memory_spans_do_not_merge_around_code(self):
        blocks = memory_blocks('用户喜欢绿色。\n```python\nraise RuntimeError()\n```\n用户喜欢蓝色。')
        self.assertEqual(blocks, [('用户喜欢绿色。', 1), ('用户喜欢蓝色。', 5)])

    def test_memory_markers_and_list_items(self):
        self.assertEqual(memory_blocks('# 标题\n- 用户偏好绿色\n- 用户偏好蓝色\n§\n用户相关合成陈述'), [('- 用户偏好绿色', 2), ('- 用户偏好蓝色', 3), ('用户相关合成陈述', 5)])

    def test_password_english_phrase(self):
        self.assertTrue(has_secret_material('password is ' + 'tiny'))

    def test_session_reference_not_credential(self):
        self.assertFalse(has_secret_material('{"sessionId":"synthetic-session-reference"}'))

    def test_unknown_record_not_interpreted(self):
        self.assertEqual(user_texts({'type': 'unknown', 'content': 'I like green.'}), [])


if __name__ == '__main__':
    unittest.main()
