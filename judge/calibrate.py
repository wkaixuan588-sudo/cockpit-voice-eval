"""L3 人工校准: 检验裁判模型打分与人工判断的一致率。

用法:
  python -m judge.calibrate sample   # 冻结样本: 跑 SUT+judge,生成标注文件与数据文件
  python -m judge.calibrate report   # 汇总: 人工标注 vs judge 打分 -> reports/calibration_report.md

方法学:
  - 标注文件(annotation.yaml)不含 judge 分数——盲标,避免锚定偏差;
  - judge 结果连同当时的 SUT 输出一起冻结在 calibration_data.yaml,保证人标与裁判评的是同一份输出;
  - 一致性指标: 判定一致率(human_pass 与 judge 阈值判定一致)为主,分数接近率(|Δ|≤0.2)为辅;
  - 决策规则: 维度判定一致率 ≥ 0.80 视为该维度可信,否则该维度保留人工抽检。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import yaml
from deepeval.test_case import LLMTestCase

from evalsets import load_eval_cases
from judge.glm_judge import RUBRICS, TAG_METRICS, VEHICLE_SPEC, metrics_for
from sut.assistant import get_assistant

ROOT = Path(__file__).parent.parent
DATA_FILE = ROOT / "judge" / "calibration_data.yaml"
ANN_FILE = ROOT / "judge" / "annotation.yaml"
REPORT_FILE = ROOT / "reports" / "calibration_report.md"
SCORE_TOL = 0.2   # 分数接近率容差(归一化后)
TRUST_BAR = 0.80  # 维度判定一致率可信门槛


def _actual_output(parsed: dict) -> str:
    action = "; ".join(
        f"{i.get('intent')}({i.get('slots') or {}})" for i in parsed.get("intents", [])
    )
    return f"执行动作: {action}\n播报: {parsed.get('spoken_reply', '')}"


def _build_tc(case: dict, parsed: dict) -> LLMTestCase:
    kwargs = {
        "input": case["input"],
        "actual_output": _actual_output(parsed),
    }
    if "hallucination_probe" in (case.get("tags") or []):
        kwargs["expected_output"] = (case.get("expect") or {}).get("facts") or VEHICLE_SPEC
    return LLMTestCase(**kwargs)


def sample() -> None:
    sut = get_assistant()
    judged = [c for c in load_eval_cases() if set(c.get("tags") or []) & set(TAG_METRICS)]
    data_items, ann_items = [], []
    for case in judged:
        resp = sut.ask(case["input"])
        meta = {"prompt_version": resp.prompt_version, "latency_ms": round(resp.latency_ms)}
        if resp.parsed is None:
            data_items.append({"id": case["id"], "tags": case["tags"], "error": resp.raw[:200], "meta": meta})
            continue
        tc = _build_tc(case, resp.parsed)
        metrics_data = []
        for m in metrics_for(case):
            m.measure(tc)
            metrics_data.append({
                "dimension": m.name,
                "judge_score": round(m.score, 3),
                "threshold": m.threshold,
                "judge_reason": (m.reason or "")[:300],
            })
        item = {
            "id": case["id"], "tags": case["tags"], "input": case["input"],
            "actual_output": _actual_output(resp.parsed),
            "metrics": metrics_data, "meta": meta,
        }
        data_items.append(item)
        for md in metrics_data:
            ann_items.append({
                "case_id": case["id"],
                "dimension": md["dimension"],
                "criteria": RUBRICS[md["dimension"]]["criteria"],
                "input": case["input"],
                "actual_output": item["actual_output"],
                # ↓↓↓ 人工填写区 ↓↓↓
                "human_score_1to5": None,   # 1=完全不合格 3=勉强及格 5=优秀
                "human_pass": None,         # true/false: 该维度是否达到你认可的可用标准
                "human_comment": "",
            })

    frozen_at = datetime.now().isoformat(timespec="seconds")
    DATA_FILE.write_text(
        yaml.safe_dump({"frozen_at": frozen_at, "sut_model": sut.model, "items": data_items},
                       allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    ANN_FILE.write_text(
        "# L3 人工标注文件(盲标)\n"
        "# 1. 逐条阅读 input 与 actual_output,对照 criteria 独立打分(1-5)并给出 human_pass 判断\n"
        "# 2. 请先标完再打开 calibration_data.yaml(盲标,避免被裁判分数锚定)\n"
        "# 3. 标完执行: python -m judge.calibrate report\n\n"
        + yaml.safe_dump({"items": ann_items}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(f"冻结 {len(data_items)} 条用例 / {len(ann_items)} 个维度评分项")
    print(f"标注文件: {ANN_FILE}\n数据文件: {DATA_FILE}")


def agreement_rows() -> list[dict] | None:
    """返回人机逐项对比行(供校准报告与汇总报告共用);未标注或文件缺失时返回 None。"""
    if not DATA_FILE.exists() or not ANN_FILE.exists():
        return None
    data = yaml.safe_load(DATA_FILE.read_text(encoding="utf-8"))
    ann = yaml.safe_load(ANN_FILE.read_text(encoding="utf-8"))["items"]
    judge_map = {
        (it["id"], md["dimension"]): md
        for it in data["items"] for md in it.get("metrics", [])
    }
    rows = []
    for a in ann:
        md = judge_map.get((a["case_id"], a["dimension"]))
        if md is None or a.get("human_score_1to5") is None or a.get("human_pass") is None:
            continue
        human_norm = (float(a["human_score_1to5"]) - 1) / 4  # 1-5 → 0-1
        judge_pass = md["judge_score"] >= md["threshold"]
        rows.append({
            "case_id": a["case_id"], "dim": a["dimension"],
            "human": round(human_norm, 3), "judge": md["judge_score"],
            "human_pass": bool(a["human_pass"]), "judge_pass": judge_pass,
            "agree": bool(a["human_pass"]) == judge_pass,
            "close": abs(md["judge_score"] - human_norm) <= SCORE_TOL,
            "judge_reason": md["judge_reason"],
            "comment": a.get("human_comment") or "",
        })
    return rows or None


def report() -> None:
    rows = agreement_rows()
    if rows is None:
        sys.exit("没有任何已标注项——先填写 judge/annotation.yaml 再跑 report")
    data = yaml.safe_load(DATA_FILE.read_text(encoding="utf-8"))

    # 分维度统计
    dims = {}
    for r in rows:
        dims.setdefault(r["dim"], []).append(r)
    lines = [
        "# LLM-as-Judge 人工校准报告", "",
        f"- 冻结时间: {data.get('frozen_at')} | SUT: {data.get('sut_model')} | "
        f"标注: 人工盲标 {len(rows)} 项 / {len(dims)} 个维度",
        f"- 一致性口径: 判定一致率(human_pass vs judge 阈值判定)为主,分数接近率(|Δ|≤{SCORE_TOL})为辅;"
        f"维度判定一致率 ≥ {TRUST_BAR:.2f} 视为可信", "",
        "| 维度 | n | 判定一致率 | 分数接近率 | 平均Δ(judge-人) | 结论 |",
        "|---|---|---|---|---|---|",
    ]
    overall_agree = [r["agree"] for r in rows]
    for dim, rs in dims.items():
        n = len(rs)
        agree_rate = sum(r["agree"] for r in rs) / n
        close_rate = sum(r["close"] for r in rs) / n
        delta = sum(r["judge"] - r["human"] for r in rs) / n
        verdict = "可信" if agree_rate >= TRUST_BAR else "保留人工抽检"
        lines.append(f"| {dim} | {n} | {agree_rate:.0%} | {close_rate:.0%} | {delta:+.2f} | {verdict} |")
    lines += [
        f"| **总体** | {len(rows)} | {sum(overall_agree)/len(rows):.0%} | "
        f"{sum(r['close'] for r in rows)/len(rows):.0%} | "
        f"{sum(r['judge']-r['human'] for r in rows)/len(rows):+.2f} | — |", "",
    ]

    disputes = [r for r in rows if not r["agree"]]
    if disputes:
        lines += ["## 分歧明细(判定不一致项,逐条复核)", "",
                  "| 用例 | 维度 | 人工 | 裁判 | 裁判理由(截断) | 人工备注 |", "|---|---|---|---|---|---|"]
        for r in disputes:
            lines.append(
                f"| {r['case_id']} | {r['dim']} | {'过' if r['human_pass'] else '不过'}(分{r['human']:.2f}) "
                f"| {'过' if r['judge_pass'] else '不过'}(分{r['judge']:.2f}) "
                f"| {r['judge_reason'][:60]} | {r['comment'][:40]} |")
        lines.append("")
    lines += [
        "## 样本量说明", "",
        f"每维度 n={min(len(v) for v in dims.values())}~{max(len(v) for v in dims.values())},"
        "为小样本初校准——结论用于决定'哪些维度必须保留人工抽检',不代表统计显著;"
        "正式评测体系中应按维度扩充至 30+ 并定期重校准(裁判模型或 rubric 变更后必须重跑)。", "",
    ]

    REPORT_FILE.parent.mkdir(exist_ok=True)
    REPORT_FILE.write_text("\n".join(lines), encoding="utf-8")
    print(f"校准报告已生成: {REPORT_FILE}")
    print(f"总体判定一致率: {sum(overall_agree)/len(rows):.0%} | 分歧 {len(disputes)} 项")


if __name__ == "__main__":
    {"sample": sample, "report": report}[sys.argv[1]]()
