# 评测集 Schema(评测契约)

每条用例是 yaml 列表里的一个对象:

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | ✓ | 全局唯一。命名 `<域>-<类型>-<序号>`,如 `climate-fuzzy-003`。加载时查重 |
| `input` | ✓ | 用户话术(一句话) |
| `expect.intents` | ✓* | 期望意图列表,每项 `{intent, slots?}`;slots 省略=只断言意图方向 |
| `expect.reply_contains` | | 播报必须包含的关键词列表(硬断言,幻觉探针用) |
| `tags` | ✓ | 类型标签,见下表 |
| `note` | | 设计理由(一句话)。面试讲解用,体现"这条用例为什么存在" |

## tags 分类

`normal` 常规显式指令 / `fuzzy` 模糊表达 / `boundary` 参数边界 / `compound` 多意图复合 /
`multi_turn` 多轮上下文(P1.2 扩展) / `famous_name` 专名边界 / `refuse` 敏感拒答 /
`out_of_scope` 能力圈外 / `hallucination_probe` 幻觉探针 / `qa` 开放问答

## 断言语义(L1 规则层,test_rule.py)

1. `intent` 精确匹配(大小写敏感)
2. `slots` **子集匹配**: 期望的每个槽位必须在实际输出中出现且值相等(实际多出的槽位不报错)
   ——子集语义让断言只约束我们关心的维度,不被模型的额外输出干扰
3. 数值容错: `"22"` 与 `22` 视为相等(模型输出类型不稳定,归一化后比对)
4. `reply_contains`: 播报文本子串匹配(区分大小写)
5. 输出不可解析为 JSON 本身即为失败(对应真实链路的 NLU 输出不可断言)

> 断言粒度设计原则: **确定性的交给 L1**(意图/槽位/参数截断/关键词),
> **生成式的交给 L2 judge**(模糊表达的调节幅度、回复自然度、引导话术是否得体)。

## 槽位取值规范(与 SUT 系统提示词一致,改动需两侧同步)

| 意图 | 槽位 | 取值 |
|---|---|---|
| climate.set_temp | temp | 整数 16-32(越界截断到边界) |
| climate.set_fan | fan | 整数 1-7 |
| climate.power | power | "on" / "off" |
| climate.set_mode | mode | "cool"/"heat"/"auto"/"vent" |
| window.open/close | position, opening | position: "front_left"/"front_right"/"rear_left"/"rear_right"/"all";opening: 0-100 |
| seat.heat | level | 整数 0-3 |
| navigation.start | dest | 目的地字符串(模糊请求可省略) |
| media.play/pause | song, artist | 字符串(可省略) |
| phone.call | contact | 字符串 |

## 已知边界(TODO)

- 多轮上下文用例(如"导航去公司"→"换走高速的")需要 SUT 支持 history,P1.2 扩展 `ask(text, history)` 后再补 `multi_turn` 类用例
