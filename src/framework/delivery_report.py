"""Goal-level delivery report for the caller, written in the goal's language.

Rendered on demand from the public status document (``CollabApplication.status``)
and served next to the durable report as ``delivery_report_md``. It merges the
caller-facing questions (how far the goal got, scope, risks, decisions needed,
how to check it yourself, cost) with the September run-report items (what
changed, test/review result, uncertainties, scope, memory promotion).

Cost lines are recorded, not judged. Memory promotion is a suggestion only: the
kernel never promotes identity or memory.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

_STATE_ZH = {
    "completed": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
    "running": "进行中",
    "queued": "排队中",
    "blocked": "受阻",
    "cancel_requested": "取消中",
}
_TASK_ZH = {
    "succeeded": "完成",
    "failed": "失败",
    "running": "进行中",
    "queued": "排队",
    "awaiting_decision": "等待决定",
    "review": "审查中",
    "cancelled": "已取消",
}
_STATUS_ZH = {"added": "新增", "modified": "修改", "deleted": "删除"}
_TECH_ZH = {
    "passed_by_lead": "组长逐个审查，全部通过",
    "failed_by_lead": "组长审查未通过",
    "partially_reviewed": "只有部分任务经过组长审查",
    "via_lead_gate": "经组长门禁",
    "unsupported": "本次没有组长审查（规划器不带组长）",
    "not_available": "本次没有组长审查",
    "not_concluded": "审查没有结论",
}


# (English prefix, Chinese text, keep the detail after ":")
_WARNING_ZH = (
    (
        "backend runs with skip-permissions",
        "工人以跳过权限确认的方式运行，没有逐条权限把关；任务外的输入只靠提示词约束",
        False,
    ),
    ("acceptance text was not independently verified", "验收文字没有经过独立核对", False),
    ("worker changed files outside this task's artifacts", "工人改动了任务产物以外的文件", True),
    ("no workspace baseline for this run", "这次运行没有工作区快照，没法检查是否越界", False),
)


def _warning_zh(text: str) -> str:
    for prefix, zh, keep_detail in _WARNING_ZH:
        if text.startswith(prefix):
            detail = text[len(prefix):].lstrip(" :")
            return f"{zh}：{detail}" if keep_detail and detail else zh
    return text


def _m(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _tokens(usage: Any) -> int:
    u = _m(usage)
    fields = _m(u.get("fields")) or u
    try:
        return int(fields.get("total_tokens") or 0) or int(fields.get("input_tokens") or 0) + int(
            fields.get("output_tokens") or 0
        )
    except (TypeError, ValueError):
        return 0


def _parse(ts: Any) -> datetime | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _elapsed(status: Mapping[str, Any]) -> str:
    starts, ends = [], []
    for row in status.get("phase_timeline") or []:
        r = _m(row)
        s, e = _parse(r.get("started_at")), _parse(r.get("ended_at"))
        if s:
            starts.append(s)
        if e:
            ends.append(e)
    if not starts or not ends:
        return "未知"
    sec = max(0, int((max(ends) - min(starts)).total_seconds()))
    return f"{sec // 60} 分 {sec % 60} 秒" if sec >= 60 else f"{sec} 秒"


def _reviews(task: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    result = _m(task.get("result"))
    rows = result.get("review_history") or task.get("review_history") or []
    rows = [r for r in rows if isinstance(r, Mapping)]
    if not rows and isinstance(result.get("review"), Mapping):
        rows = [result["review"]]
    return rows


def render_delivery_report(status: Mapping[str, Any]) -> str:
    goal = _m(status.get("goal"))
    tasks = [t for t in (status.get("tasks") or []) if isinstance(t, Mapping)]
    acc = _m(status.get("acceptance_status"))
    state = str(status.get("state") or "")
    title = str(goal.get("title") or status.get("request_id") or "")
    lines: list[str] = [f"# 交付报告：{title}", ""]
    lines.append(f"- 请求：`{status.get('request_id') or status.get('goal_id') or ''}`")
    lines.append(f"- 结果：**{_STATE_ZH.get(state, state or '未知')}**")
    tech = str(acc.get("technical_review") or "")
    lines.append(f"- 审查：{_TECH_ZH.get(tech, tech or '未知')}")
    lines.append("- 业务验收：还没有做，需要上层自己看产物后决定是否接受")
    lines.append("")

    # 1. 目标完成度
    lines += ["## 目标完成度", "", str(goal.get("desired_outcome") or "").strip(), ""]
    delivered = list(acc.get("delivered_artifacts") or [])
    missing = list(acc.get("missing_artifacts") or [])
    lines.append(f"- 已交付：{', '.join(delivered) if delivered else '无'}")
    lines.append(f"- 还差：{', '.join(missing) if missing else '无'}")
    for t in tasks:
        st = str(t.get("status") or "")
        rw = int(t.get("rework_count") or 0)
        extra = f"，返工 {rw} 次" if rw else ""
        lines.append(f"- 任务「{t.get('title')}」：{_TASK_ZH.get(st, st)}{extra}")
    if status.get("failure_reason"):
        lines.append(f"- 失败原因：{status.get('failure_reason')}")
    lines.append("")

    # 2. 改了什么 + 审查/测试结果
    lines += ["## 改了什么、审查结果", ""]
    for t in tasks:
        result = _m(t.get("result"))
        changed = _m(result.get("scope_report")).get("changed_files") or []
        lines.append(f"### {t.get('title')}")
        if changed:
            parts = [f"{_STATUS_ZH.get(c.get('status'), c.get('status'))} {c.get('path')}" for c in changed if isinstance(c, Mapping)]
            lines.append(f"- 改动：{'；'.join(parts)}")
        elif result:
            lines.append("- 改动：没有记录（这次运行没有工作区快照）")
        for r in _reviews(t):
            verdict = {"passed": "通过", "failed": "不通过", "error": "审查出错", "unsupported": "未审查"}.get(
                str(r.get("status") or ""), str(r.get("status") or "")
            )
            src = "组长" if r.get("source") == "lead" else "机械检查" if r.get("source") == "agy_exact_content" else "无"
            evidence = str(r.get("evidence") or "").strip()
            lines.append(f"- {src}审查（第 {int(r.get('round') or 0) + 1} 轮）：{verdict}。{evidence}")
        if result.get("error") and t.get("status") == "failed":
            lines.append(f"- 错误：{result.get('error')}")
        lines.append("")

    # 3. 是否越界
    lines += ["## 是否越界", ""]
    scope_rows: list[str] = []
    for t in tasks:
        result = _m(t.get("result"))
        for r in _reviews(t):
            oos = r.get("out_of_scope") or []
            if oos:
                fixed = "，collab 已恢复原样并让工人返工" if r.get("restored") else ""
                scope_rows.append(f"- 「{t.get('title')}」第 {int(r.get('round') or 0) + 1} 轮越界：{', '.join(oos)}{fixed}")
        final = _m(result.get("scope_report")).get("out_of_scope_changes") or []
        if final and not any(r.get("source") == "lead" for r in _reviews(t)):
            names = ", ".join(f"{c.get('path')}（{_STATUS_ZH.get(c.get('status'), c.get('status'))}）" for c in final)
            scope_rows.append(f"- 「{t.get('title')}」最终交付里仍有任务外改动（没有组长审查）：{names}")
    lines += scope_rows or ["- 无"]
    lines.append("")

    # 4. 不确定的地方和剩余风险
    lines += ["## 不确定的地方和剩余风险", ""]
    risks = [str(w) for w in (status.get("warnings") or []) if isinstance(w, str) and w.strip()]
    lines += [f"- {_warning_zh(w)}" for w in risks] or ["- 无额外警告"]
    lines.append("- 组长审查看的是改动和工人自述，不等于实际运行过产物；关键产物请按下一节亲手验证")
    lines.append("")

    # 5. 需要上层做的取舍
    lines += ["## 需要上层做的取舍", ""]
    pending = [d for d in (status.get("pending_decisions") or []) if isinstance(d, Mapping)]
    asks = [f"- 待决定：{d.get('title') or d.get('kind')}（{d.get('decision_id')}）" for d in pending]
    if status.get("need_human"):
        asks.append(f"- 需要人处理：{status.get('failure_reason') or '见失败原因'}")
    if state == "failed" and not asks:
        asks.append("- 是否重开或调整目标后再提交")
    lines += asks or ["- 无"]
    lines.append("")

    # 6. 亲手验证
    lines += ["## 怎么亲手验证", ""]
    shown = False
    for t in tasks:
        ws = str(t.get("workspace") or "")
        arts = [str(a) for a in (t.get("expected_artifacts") or [])]
        if ws and arts and t.get("status") == "succeeded":
            for a in arts:
                lines.append(f"- 「{t.get('title')}」产物：`{Path(ws) / a}`")
            shown = True
    if shown:
        last = next((t for t in reversed(tasks) if t.get("workspace") and t.get("status") == "succeeded"), None)
        if last:
            lines.append(f"- 最后一个任务的工作区包含前面任务交来的文件，可以在这里直接复跑：`{last.get('workspace')}`")
    else:
        lines.append("- 没有可验证的交付产物")
    rid = status.get("request_id") or status.get("goal_id") or ""
    lines.append(f"- 查看完整状态：`python bin/hermes-collab-request.py status \"{rid}\" --full`")
    lines.append("")

    # 7. 成本数据（只记录）
    lines += ["## 成本数据", ""]
    total = 0
    runs = 0
    for t in tasks:
        result = _m(t.get("result"))
        if result:
            total += _tokens(result.get("usage_report") or result.get("usage"))
            runs += 1
        for r in t.get("review_history") or []:
            if isinstance(r, Mapping) and r.get("usage"):
                total += _tokens(r.get("usage"))
                runs += 1
    reworks = sum(int(t.get("rework_count") or 0) for t in tasks)
    lines.append(f"- 工人用量（工人自报，不是账单）：约 {total:,} token，共 {runs} 次工人运行")
    lines.append(f"- 耗时：{_elapsed(status)}")
    lines.append(f"- 返工次数：{reworks}")
    lines.append(f"- 最终是否通过：{'是' if state == 'completed' else '否'}")
    lines.append("")

    # 8. 记忆晋升建议
    lines += [
        "## 是否建议晋升记忆",
        "",
        "- 无（collab 不写任何长期记忆；如果这次有值得记住的经验，由上层自己决定）",
        "",
    ]
    return "\n".join(lines)


__all__ = ["render_delivery_report"]
