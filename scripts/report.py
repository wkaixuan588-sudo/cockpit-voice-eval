"""汇总报告: 一次执行全量评测(L1+L2),产出 reports/summary.md(维度通过率 + badcase 清单 + 时延分位)。

用法: python scripts/report.py
与 pytest 各层独立断言不同,这里聚合"一次执行、多层判定"的整体视图。
"""
from __future__ import annotations

import statistics
from datetime import datetime
from pathlib import Path

from deepeval.test_case import LLMTestCase

from evalsets import load_eval_cases, match_intent
from judge.glm_judge import TAG_METRICS, VEHICLE_SPEC, metrics_for
from sut.assistant import get_assistant

ROOT = Path(__file__).parent.parent
OUT = ROOT / "reports" / "summary.md"


def _actual_output(parsed: dict) -> str:
    action = "; ".join(
        f"{i.get('intent')}({i.get('slots') or {}})" for i in parsed.get("intents", [])
    )
    return f"执行动作: {action}\n播报: {parsed.get('spoken_reply', '')}"


def check_l1(case: dict, resp) -> tuple[bool, str]:
    if resp.parsed is None:
        return False, f"输出不可解析: {resp.raw[:80]}"
    got = resp.parsed.get("intents")
    if not (isinstance(got, list) and got):
        return False, f"缺少 intents 数组: {str(resp.parsed)[:80]}"
    expect = case.get("expect") or {}
    for exp in expect.get("intents", []):
        if not any(match_intent(g, exp) for g in got):
            return False, f"未命中 {exp['intent']}{exp.get('slots') or ''}, 实际 {got}"
    for kw in expect.get("reply_contains", []):
        if kw not in resp.parsed.get("spoken_reply", ""):
            return False, f"播报缺少关键词 '{kw}'"
    return True, ""


def measure_l2(case: dict, parsed: dict) -> list[dict]:
    kwargs = {"input": case["input"], "actual_output": _actual_output(parsed)}
    if "hallucination_probe" in (case.get("tags") or []):
        kwargs["expected_output"] = (case.get("expect") or {}).get("facts") or VEHICLE_SPEC
    tc = LLMTestCase(**kwargs)
    results = []
    for m in metrics_for(case):
        m.measure(tc)
        results.append({
            "dimension": m.name, "score": round(m.score, 3),
            "threshold": m.threshold, "passed": m.score >= m.threshold,
            "reason": (m.reason or "")[:200],
        })
    return results


def _pct(data: list[float], p: float) -> float:
    s = sorted(data)
    return s[min(int(len(s) * p), len(s) - 1)]


def main() -> None:
    sut = get_assistant()
    cases = load_eval_cases()
    l1_rows, l2_rows, latencies = [], [], []

    for case in cases:
        resp = sut.ask(case["input"])
        latencies.append(resp.latency_ms)
        ok, msg = check_l1(case, resp)
        l1_rows.append({
            "id": case["id"], "tags": case["tags"], "ok": ok, "msg": msg,
            "known": case.get("xfail", ""),
        })
        if resp.parsed and set(case.get("tags") or []) & set(TAG_METRICS):
            for r in measure_l2(case, resp.parsed):
                r["id"] = case["id"]
                r["known"] = case.get("judge_xfail") or case.get("xfail") or ""
                l2_rows.append(r)

    lines = [
        "# 座舱语音助手评测汇总报告", "",
        f"- 生成时间: {datetime.now().isoformat(timespec='seconds')}",
        f"- SUT: {sut.model} / prompt v{resp.prompt_version} | judge: glm 系(见 .env JUDGE_MODEL)",
        f"- 评测集: {len(cases)} 条 | SUT 时延: P50 {_pct(latencies, 0.5) / 1000:.1f}s / "
        f"P95 {_pct(latencies, 0.95) / 1000:.1f}s", "",
        "## L1 规则断言", "",
        f"- 通过 {sum(r['ok'] for r in l1_rows)}/{len(l1_rows)}"
        f"(其中已知缺陷 xfail {sum(1 for r in l1_rows if not r['ok'] and r['known'])} 条)", "",
    ]
    tag_stats: dict[str, list[bool]] = {}
    for r in l1_rows:
        for t in r["tags"]:
            tag_stats.setdefault(t, []).append(r["ok"])
    lines += ["| tag | 通过率 |", "|---|---|"]
    for t, oks in tag_stats.items():
        lines.append(f"| {t} | {sum(oks)}/{len(oks)} |")

    fails = [r for r in l1_rows if not r["ok"]]
    if fails:
        lines += ["", "### L1 失败明细", "", "| 用例 | 定性 | 失败原因 |", "|---|---|---|"]
        for r in fails:
            kind = "已知缺陷" if r["known"] else "**新失败**"
            lines.append(f"| {r['id']} | {kind} | {r['msg'][:70]} |")

    lines += ["", "## L2 LLM-as-Judge", "", "| 维度 | n | 平均分 | 阈值 | 通过率 |", "|---|---|---|---|---|"]
    dim_stats: dict[str, list[dict]] = {}
    for r in l2_rows:
        dim_stats.setdefault(r["dimension"], []).append(r)
    for dim, rs in dim_stats.items():
        avg = statistics.mean(r["score"] for r in rs)
        passed = sum(r["passed"] for r in rs)
        lines.append(f"| {dim} | {len(rs)} | {avg:.2f} | {rs[0]['threshold']} | {passed}/{len(rs)} |")

    l2_fails = [r for r in l2_rows if not r["passed"]]
    if l2_fails:
        lines += ["", "### L2 失败明细", "", "| 用例 | 维度 | 得分 | 裁判理由(截断) | 定性 |", "|---|---|---|---|---|"]
        for r in l2_fails:
            kind = "已知问题" if r["known"] else "**新失败**"
            lines.append(f"| {r['id']} | {r['dimension']} | {r['score']} | {r['reason'][:60]} | {kind} |")

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"汇总报告: {OUT}")
    print(f"L1 通过 {sum(r['ok'] for r in l1_rows)}/{len(l1_rows)} | "
          f"L2 通过 {sum(r['passed'] for r in l2_rows)}/{len(l2_rows)} | "
          f"P50 {_pct(latencies, 0.5) / 1000:.1f}s / P95 {_pct(latencies, 0.95) / 1000:.1f}s")


if __name__ == "__main__":
    main()
