"""Chinese report chrome only. This function must receive owned JS, not data."""
import json

KIND_LABELS = {
    'persisted_memory_claim': '已保存的用户相关声明（未认证）',
    'explicit_statement_in_user_role': 'user角色中的明确陈述（主体未认证）',
    'request_or_goal_not_stable_fact': '请求/目标，不直接当作稳定事实',
    'question_not_verified_fact': '问题或待确认表述',
    'third_party_or_quoted_claim': '第三人/引用，不能归为用户本人事实',
    'saved_environment_claim_not_current_system_state': '历史环境陈述，不代表当前主机状态',
}
TOPIC_LABELS = {'preferences_and_response_style': '偏好/回复风格',
                'work_and_goals': '工作/目标', 'identity_or_context': '身份/背景',
                'tools_and_environment': '工具/历史环境',
                'relationships_or_other': '关系/其他', 'other_user_related_claim': '其他相关声明'}
SUBJECT_LABELS = {'saved_profile_not_independently_verified': '已保存profile文本，未独立核实主体',
                  'user_role_human_identity_unverified': '来源标记为user角色，人类身份未核实',
                  'do_not_assign_to_user': '第三人或引用，不归给用户本人'}
CONFIDENCE_LABELS = {'source_claim_only_not_verified_fact': '仅来源陈述，尚未独立核实',
                     'unverified': '来源/主体尚未核实', 'not_user_fact': '不能作为用户事实'}


def localize_memory_ui_js(script):
    if 'const kindLabels=' in script:
        return script
    if 'for(let t of new Set' not in script:
        raise ValueError('owned_memory_ui_schema_not_recognized')
    declarations = ('const kindLabels=' + json.dumps(KIND_LABELS, ensure_ascii=False) +
                    ',topicLabels=' + json.dumps(TOPIC_LABELS, ensure_ascii=False) +
                    ',subjectLabels=' + json.dumps(SUBJECT_LABELS, ensure_ascii=False) +
                    ',confidenceLabels=' + json.dumps(CONFIDENCE_LABELS, ensure_ascii=False) + ';')
    script = script.replace('for(let t of new Set', declarations + 'for(let t of new Set', 1)
    script = script.replace('o.textContent=t;', 'o.textContent=kindLabels[t]||t;')
    script = script.replace('${esc(x.topic||x.kind)}', '${esc(topicLabels[x.topic]||kindLabels[x.kind]||x.topic||x.kind)}')
    script = script.replace('${esc(x.kind)}', '${esc(kindLabels[x.kind]||x.kind)}')
    script = script.replace('${esc(x.attribution)}', '${esc(subjectLabels[x.attribution]||x.attribution)}')
    script = script.replace('${esc(x.confidence)}', '${esc(confidenceLabels[x.confidence]||x.confidence)}')
    script = script.replace('${esc(x.current_validity)}', '${esc(x.time_review_required?"尚未确认当前有效性；有时间限定或缺陈述时间，需复核":"尚未确认当前有效性，事件时间不是事实认证")}')
    script = script.replace('[x.quote_LOCAL_ONLY,x.topic,x.kind]', "[x.quote_LOCAL_ONLY,x.topic,x.kind,topicLabels[x.topic]||'',kindLabels[x.kind]||'']")
    return script
