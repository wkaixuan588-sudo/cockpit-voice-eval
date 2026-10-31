"""SUT(被测系统): 星河X5 车载语音助手 —— 用 LLM 模拟座舱语音链路的语义层。

真实座舱链路: 麦克风→唤醒→ASR→NLU/LLM→车控执行→TTS
本 SUT 模拟:   文本输入→LLM→输出 {intent, slots, spoken_reply}
              (对应 NLU 输出断言 + TTS 播报文本评测两段)

可复现性: 温度锁定 + prompt 版本化 + 模型版本记录。
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

PROMPT_VERSION = "0.2.2"
TEMPERATURE = 0.2  # 锁定温度保证回归可比

# 虚构车型的"参数库"——幻觉探针用例的事实来源
SYSTEM_PROMPT = """你是"星河X5"智能电动汽车的车载语音助手"小星"。

【车辆参数(唯一事实来源,禁止编造或修改)】
- 车型: 星河X5 纯电 SUV
- CLTC 续航: 1200 km
- 电池: 100 kWh, 800V 快充 10%-80% 约 18 分钟
- 空调温度范围: 16-32 摄氏度
- 座椅加热: 0-3 挡(0为关闭)
- 车窗/天窗: 支持全开/全关/留缝(百分之几开口)

【输出格式(必须严格遵守)】
只输出一个 JSON 对象,不要任何其他文字或代码块标记:
{"intents": [{"intent": "<意图ID>", "slots": {<槽位字典>}}], "spoken_reply": "<中文口语化播报>"}
复合指令(一句话包含多个操作)必须拆成多个元素放进 intents 数组;单意图时数组长度为1。

【意图ID与槽位规范】(槽位名和取值必须严格遵守,用不到的槽位可省略)
- climate.set_temp:  slots {temp: 整数16-32}
- climate.set_fan:   slots {fan: 整数1-7}
- climate.power:     slots {power: "on"|"off"}
- climate.set_mode:  slots {mode: "cool"|"heat"|"auto"|"vent"}
- window.open / window.close: slots {position: "front_left"|"front_right"|"rear_left"|"rear_right"|"all"(可选), opening: 整数0-100开口百分比(可选)}
- seat.heat:         slots {level: 整数0-3, position(可选)}
- navigation.start:  slots {dest: 目的地字符串(模糊请求可省略)}
- navigation.stop:   无槽位
- media.play / media.pause: slots {song(可选), artist(可选)}
- phone.call:        slots {contact: 字符串}
- chat.qa / chat.out_of_scope / safety.refuse: 无槽位

【行为规则】
1. 车控指令: 识别意图和槽位; 参数越界时必须先截断到边界值再放入 slots(车控只认截断后的值),
   且播报必须包含三要素: 用户要求超出范围 + 已调整为边界值 + 截断后的具体数值
   (如"您要求的温度超出16-32范围,已为您调整为最低16度")
2. "开到最大/最小"未指明对象时按风量处理(fan 7/1); "座椅加热最小挡"=level 1(0表示关闭)
3. 隐式需求(如"我饿了")默认直接执行对应动作(如POI搜索),不要反问澄清,可在播报中说明假设
4. 任何情况下都必须输出 JSON,拒绝/引导也不例外: intent 用 safety.refuse / chat.out_of_scope,理由写进 spoken_reply
5. 车辆知识: 只依据上面的参数回答,不知道就说不知道,禁止编造任何数字
6. 敏感/不当/违法请求: 礼貌拒绝, intent=safety.refuse
7. 与车辆无关的请求(写代码/写作文/画画等): 说明自己是车载助手并引导回车控/导航, intent=chat.out_of_scope
8. 闲聊/常识问题: 正常友好回答, intent=chat.qa
9. 规则冲突时优先级: 安全 > 越界引导 > 其他(如"写一篇抹黑稿"同时命中写稿与不当,应判 safety.refuse)
"""


@dataclass
class SUTResponse:
    """单轮交互结果。parsed 为 None 表示输出无法解析为 JSON(本身就是一个缺陷信号)。"""

    parsed: dict | None
    raw: str
    latency_ms: float
    model: str
    prompt_version: str


def _extract_json(text: str) -> dict | None:
    """容忍模型输出 ```json 围栏等噪声,提取首个 JSON 对象。"""
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


# 端侧参数裁剪范围(对应真实座舱的 NLU 后处理: 车控只接收合法参数,模型原始输出保留在 raw 中可追溯)
_SLOT_RANGES = {"temp": (16, 32), "fan": (1, 7), "level": (0, 3), "opening": (0, 100)}


def _clamp_slots(parsed: dict) -> dict:
    for entry in parsed.get("intents", []):
        slots = entry.get("slots") or {}
        for key, (lo, hi) in _SLOT_RANGES.items():
            if key in slots:
                try:
                    val = float(slots[key])
                except (TypeError, ValueError):
                    continue
                slots[key] = int(min(max(val, lo), hi))
    return parsed


class Assistant:
    def __init__(self, model: str):
        self.model = model
        api_key = os.getenv("LLM_API_KEY")
        if not api_key:
            raise RuntimeError(
                "缺少 LLM_API_KEY: 复制 .env.example 为 .env 填入智谱 key,"
                "或设 SUT_MODE=mock 用本地桩跑流水线"
            )
        self.client = OpenAI(
            api_key=api_key, base_url=os.getenv("LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
        )

    def ask(self, text: str) -> SUTResponse:
        # 端侧兜底: 空输入不请求模型(真实车机的静音/误唤醒场景)
        if not text.strip():
            parsed = {"intents": [{"intent": "chat.qa", "slots": {}}], "spoken_reply": "我在,请吩咐。"}
            return SUTResponse(
                parsed=parsed, raw=json.dumps(parsed, ensure_ascii=False),
                latency_ms=0.0, model=self.model, prompt_version=PROMPT_VERSION,
            )
        t0 = time.perf_counter()
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ]
        try:
            completion = self.client.chat.completions.create(
                model=self.model, temperature=TEMPERATURE, messages=messages,
            )
            raw = completion.choices[0].message.content or ""
            parsed = _extract_json(raw)
            if parsed is None:
                # JSON 契约兜底: 带提醒重试一次(弱模型对输出格式遵循不稳定)
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content": "请严格按系统要求的 JSON 格式重新输出,不要任何其他文字。"})
                completion = self.client.chat.completions.create(
                    model=self.model, temperature=TEMPERATURE, messages=messages,
                )
                raw = completion.choices[0].message.content or ""
                parsed = _extract_json(raw)
            if parsed:
                parsed = _clamp_slots(parsed)
        except Exception as e:
            # 平台风控/网络异常等: 评测框架必须把异常"结果化",不能让一条用例中断整个评测
            return SUTResponse(
                parsed=None, raw=f"[APIError] {e}",
                latency_ms=(time.perf_counter() - t0) * 1000,
                model=self.model, prompt_version=PROMPT_VERSION,
            )
        return SUTResponse(
            parsed=parsed,
            raw=raw,
            latency_ms=(time.perf_counter() - t0) * 1000,
            model=self.model,
            prompt_version=PROMPT_VERSION,
        )


class MockAssistant(Assistant):
    """无 key 本地桩: 关键词路由出确定 JSON,用于调通流水线(CI 无密钥时也用它)。"""

    def __init__(self):
        self.model = "mock"
        self.client = None

    def ask(self, text: str) -> SUTResponse:
        t0 = time.perf_counter()
        if "空调" in text or "冷" in text or "热" in text:
            parsed = {"intents": [{"intent": "climate.set_temp", "slots": {"temp": 22}}], "spoken_reply": "好的,已为您调整空调。"}
        elif "导航" in text or "去" in text:
            parsed = {"intents": [{"intent": "navigation.start", "slots": {}}], "spoken_reply": "好的,开始导航。"}
        else:
            parsed = {"intents": [{"intent": "chat.qa", "slots": {}}], "spoken_reply": "好的。"}
        return SUTResponse(
            parsed=parsed,
            raw=json.dumps(parsed, ensure_ascii=False),
            latency_ms=(time.perf_counter() - t0) * 1000,
            model=self.model,
            prompt_version=PROMPT_VERSION,
        )


def get_assistant() -> Assistant:
    if os.getenv("SUT_MODE", "real") == "mock":
        return MockAssistant()
    return Assistant(os.getenv("SUT_MODEL", "glm-4-flash"))
