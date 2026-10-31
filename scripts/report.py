"""汇总报告: 一次执行全量评测(L1+L2),产出 reports/summary.md。

除明细外,报告提供三层质量度量(测试报告的灵魂是准出结论,不是数字罗列):
  A. 准出标准判定(exit criteria): 每项指标有目标值,报告给出"通过/未通过"结论
  B. 契约稳定性: JSON 一次解析成功率/重试率/API错误率(弱模型输出契约的真实健康度)
  C. 语义质量指数: 加权综合分(权重与口径见报告脚注)

用法: python scripts/report.py
"""
from __future__ import annotations

import statistics
from datetime import datetime
from pathlib import Path

from deepeval.test_case import LLMTestCase

from evalsets import load_eval_cases, match_intent
from judge.calibrate import agreement_rows
from judge.glm_judge import TAG_METRICS, VEHICLE_SPEC, metrics_for
from sut.assistant import get_assistant

ROOT = Path(__file__).parent.parent
OUT = ROOT / "reports" / "summary.md"

# ===== 准出标准(目标值即"这版能不能放行"的门槛) =====
TARGET_L1_VALID_PASS = 1.00   # L1 有效用例(不含已知缺陷)通过率
TARGET_L2_DIM_PASS = 0.80     # L2 每个维度的有效通过率下限
TARGET_FIRST_TRY = 0.90       # JSON 一次解析成功率
TARGET_API_ERROR = 0.05       # API 错误率上限
TARGET_P95_MS = 8000          # SUT 时延 P95 上限(纯 LLM API 口径)
TARGET_AGREE = 0.80           # 裁判判定一致率下限(最近一次人工校准)
TARGET_INDEX = 85             # 语义质量指数下限
# 语义质量指数权重: 执行正确性 0.5 / 播报质量 0.3 / 契约稳定性 0.2
W_L1, W_L2, W_STAB = 0.5, 0.3, 0.2


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
    first_try_ok, retried, api_errors = 0, 0, 0

    for case in cases:
        resp = sut.ask(case["input"])
        latencies.append(resp.latency_ms)
        if resp.raw.startswith("[APIError]"):
            api_errors += 1
        elif resp.parsed is not None and resp.retries == 0:
            first_try_ok += 1
        elif resp.parsed is not None and resp.retries > 0:
            retried += 1
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

    n = len(cases)
    # ===== 核心度量计算 =====
    l1_valid = [r for r in l1_rows if not r["known"]]
    l1_valid_rate = sum(r["ok"] for r in l1_valid) / len(l1_valid)
    l2_valid = [r for r in l2_rows if not r["known"]]
    l2_valid_rate = (sum(r["passed"] for r in l2_valid) / len(l2_valid)) if l2_valid else 1.0
    dim_stats: dict[str, list[dict]] = {}
    for r in l2_valid:
        dim_stats.setdefault(r["dimension"], []).append(r)
    dim_pass = {d: sum(x["passed"] for x in rs) / len(rs) for d, rs in dim_stats.items()}
    first_try_rate = first_try_ok / n
    api_error_rate = api_errors / n
    p95_ms = _pct(latencies, 0.95)
    index = round(100 * (W_L1 * l1_valid_rate + W_L2 * l2_valid_rate + W_STAB * first_try_rate))
    cal = agreement_rows()
    agree_rate = (sum(r["agree"] for r in cal) / len(cal)) if cal else None

    bad_dims = [d for d, p in dim_pass.items() if p < TARGET_L2_DIM_PASS]
    criteria = [
        ("L1 有效通过率(不含已知缺陷)", f"{l1_valid_rate:.0%}", f"100%",
         l1_valid_rate >= TARGET_L1_VALID_PASS, ""),
        ("L2 各维度有效通过率", f"{min(dim_pass.values()):.0%}" if dim_pass else "N/A",
         f">={TARGET_L2_DIM_PASS:.0%}", not bad_dims,
         f"未达标维度: {', '.join(bad_dims)}(受裁判抖动影响见 F-012,建议多次采样)" if bad_dims else ""),
        ("JSON 一次解析成功率", f"{first_try_rate:.0%}", f">={TARGET_FIRST_TRY:.0%}",
         first_try_rate >= TARGET_FIRST_TRY, f"重试 {retried} 条" if retried else ""),
        ("API 错误率", f"{api_error_rate:.0%}", f"<={TARGET_API_ERROR:.0%}",
         api_error_rate <= TARGET_API_ERROR, "平台风控拦截计入" if api_errors else ""),
        ("SUT 时延 P95", f"{p95_ms/1000:.1f}s", f"<={TARGET_P95_MS/1000:.0f}s",
         p95_ms <= TARGET_P95_MS, "口径: 纯 LLM API 往返,非车机整链路"),
        ("裁判判定一致率(最近校准)", f"{agree_rate:.0%}" if agree_rate is not None else "未校准",
         f">={TARGET_AGREE:.0%}", agree_rate is not None and agree_rate >= TARGET_AGREE,
         "无校准数据同样阻断准出" if agree_rate is None else ""),
    ]
    all_pass = all(ok for *_, ok, _ in criteria)
    verdict = "通过 ✓" if all_pass else "未通过 ✗"

    # ===== 报告输出: 结论先行 =====
    lines = [
        "# 座舱语音助手评测汇总报告", "",
        f"> ## 准出结论: {verdict}", "",
        f"- 生成时间: {datetime.now().isoformat(timespec='seconds')} | "
        f"SUT: {sut.model} / prompt v{resp.prompt_version} | judge: 见 .env JUDGE_MODEL",
        f"- 评测集: {n} 条 | SUT 时延: P50 {_pct(latencies, 0.5)/1000:.1f}s / P95 {p95_ms/1000:.1f}s", "",
        "## 准出标准判定", "",
        "| 指标 | 实际 | 目标 | 判定 | 备注 |", "|---|---|---|---|---|",
    ]
    for name, actual, target, ok, note in criteria:
        lines.append(f"| {name} | {actual} | {target} | {'✓' if ok else '✗'} | {note} |")
    lines += [
        "", "## 质量度量", "",
        f"- **语义质量指数: {index} / 100**(目标 ≥{TARGET_INDEX};"
        f"权重 = 执行正确性{W_L1} + 播报质量{W_L2} + 契约稳定性{W_STAB})",
        f"- 契约稳定性: 一次解析成功 {first_try_ok}/{n}({first_try_rate:.0%}),"
        f"重试 {retried} 条,API 错误 {api_errors} 条({api_error_rate:.0%})",
        f"- 裁判可信度: 判定一致率 {agree_rate:.0%}" if agree_rate is not None
        else "- 裁判可信度: 未校准",
        "",
    ]

    lines += ["## L1 规则断言", "",
              f"- 通过 {sum(r['ok'] for r in l1_rows)}/{n}"
              f"(其中已知缺陷 xfail {sum(1 for r in l1_rows if not r['ok'] and r['known'])} 条;"
              f"有效通过率 {l1_valid_rate:.0%})", "",
              "| tag | 通过率 |", "|---|---|"]
    tag_stats: dict[str, list[bool]] = {}
    for r in l1_rows:
        for t in r["tags"]:
            tag_stats.setdefault(t, []).append(r["ok"])
    for t, oks in tag_stats.items():
        lines.append(f"| {t} | {sum(oks)}/{len(oks)} |")

    fails = [r for r in l1_rows if not r["ok"]]
    if fails:
        lines += ["", "### L1 失败明细", "", "| 用例 | 定性 | 失败原因 |", "|---|---|---|"]
        for r in fails:
            kind = "已知缺陷" if r["known"] else "**新失败**"
            lines.append(f"| {r['id']} | {kind} | {r['msg'][:70]} |")

    lines += ["", "## L2 LLM-as-Judge(有效口径: 剔除 judge_xfail 已知问题)", "",
              "| 维度 | 有效n | 有效通过率 | 平均分 | 阈值 |", "|---|---|---|---|---|"]
    all_dim_stats: dict[str, list[dict]] = {}
    for r in l2_rows:
        all_dim_stats.setdefault(r["dimension"], []).append(r)
    for dim, rs in all_dim_stats.items():
        valid = [x for x in rs if not x["known"]]
        vp = (sum(x["passed"] for x in valid) / len(valid)) if valid else None
        avg = statistics.mean(x["score"] for x in rs)
        vp_str = f"{vp:.0%}" if vp is not None else "全豁免"
        lines.append(f"| {dim} | {len(valid)}/{len(rs)} | {vp_str} | {avg:.2f} | {rs[0]['threshold']} |")

    l2_fails = [r for r in l2_rows if not r["passed"]]
    if l2_fails:
        lines += ["", "### L2 失败明细", "", "| 用例 | 维度 | 得分 | 裁判理由(截断) | 定性 |", "|---|---|---|---|---|"]
        for r in l2_fails:
            kind = "已知问题" if r["known"] else "**新失败**"
            lines.append(f"| {r['id']} | {r['dimension']} | {r['score']} | {r['reason'][:60]} | {kind} |")
    lines += [
        "", "---",
        "口径说明: 时延为纯 LLM API 往返(非车机整链路);有效通过率剔除带 xfail 标记的已知问题用例;"
        "裁判判定一致率来自最近一次人工盲标校准(judge/calibration_data.yaml)。",
    ]

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"汇总报告: {OUT}")
    print(f"准出结论: {verdict} | 质量指数 {index}/100 | "
          f"L1有效通过率 {l1_valid_rate:.0%} | L2有效通过率 {l2_valid_rate:.0%} | "
          f"一次解析成功率 {first_try_rate:.0%} | P95 {p95_ms/1000:.1f}s")


if __name__ == "__main__":
    main()
