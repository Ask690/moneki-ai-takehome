"""从检索到的文本里认出指令式的句子。"""

from __future__ import annotations

import re

_SENTENCE = re.compile(r"[^。！？!?\n]+[。！？!?]?")

#: 命中任意一条就认为这句话是“冲着助手来的指令”，而不是公司资料。
_INJECTION_PATTERNS = (
    re.compile(r"(忽略|无视|忘记|放弃).{0,8}(之前|以上|上面|先前|所有).{0,6}(指令|规则|提示|设定)"),
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions|prompts|rules)", re.I),
    re.compile(r"(系统提示|系统指令|system\s*prompt|system\s*message)\s*[:：]"),
    re.compile(r"无论(用户|别人|他)?问(什么|啥)"),
    re.compile(r"(不要|禁止|别).{0,6}(引用|标注|列出).{0,6}(来源|出处|依据)"),
    re.compile(r"(你必须|你应该|请你?务必|from now on|you must)\s*(回答|输出|reply|answer|say)", re.I),
    re.compile(r"(执行|运行|调用).{0,6}(drop|delete|update|truncate)\s", re.I),
)


def is_instruction_like(sentence: str) -> bool:
    text = sentence.strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in _INJECTION_PATTERNS)


#: 用户直接要求改数据、或者套取系统信息。这些不属于“经营问题”，一律拒答。
#: 契约 §5 把这两类都归到 `refusal`，并且要求数据库不能有任何改动。
_UNSAFE_REQUEST_PATTERNS = (
    # 破坏性操作：动词 + 数据/表/库
    # 注意用 `.` 不用 `\S`：`删掉 S01 的销售记录` 里动词和宾语之间是有空格的，
    # `\S{0,12}` 匹配不到空格，这类请求就漏过去了。
    re.compile(r"(删除|删掉|删了|清除|清空|抹掉|移除|drop|delete|truncate|清库).{0,12}"
               r"(数据|记录|表|库|订单|销售|行)", re.I | re.S),
    # 中文的“把”字句是宾语在前、动词在后：`帮我把 S01 的销售记录全部删掉`。
    # 只按“动词在前”写正则的话，这种最常见的说法反而漏掉。
    re.compile(r"(数据|记录|表|库|订单|销售|明细).{0,12}"
               r"(删除|删掉|删了|清除|清空|抹掉|移除|drop|delete|truncate)", re.I | re.S),
    re.compile(r"\b(drop|truncate|delete\s+from|update|insert\s+into|alter\s+table)\b", re.I),
    re.compile(r"(改|修改|篡改|更新)\S{0,8}(数据库|数据|销售记录|营业额|订单)"),
    # 又是“把”字句：`把 6 月的营业额改成 100 万`——宾语在动词前。
    # 上面那条只认“动词在前”，这类最常见的改数据说法就漏过去了。
    # 这里要求动词带明确的结果补语（改成/改为/改掉…），免得把
    # “营业额口径改过一次吗”这种正常提问也当成改数据的请求。
    re.compile(r"(数据库|数据|销售记录|营业额|订单|销量|金额|明细).{0,12}"
               r"(改成|改为|改到|改掉|修改|篡改|更新为|调整成|调整到)"),
    # 套取系统信息
    re.compile(r"(系统提示词|系统提示|system\s*prompt|你的?(系统)?指令|你的?规则|提示词模板)", re.I),
    re.compile(r"(表结构|数据库结构|schema|建表语句|所有的?表)", re.I),
    re.compile(r"(api[\s_-]*key|密钥|环境变量|token)", re.I),
    re.compile(r"(导出|给我|发给我|列出)\S{0,10}(源码|源代码|配置|内部实现)"),
)


def unsafe_request_reason(question: str) -> str | None:
    """识别“不该执行”的请求。返回中文原因，没问题时返回 None。

    这道闸门必须在代码里，不能交给模型：文档里可能埋着指令
    （KB-060 的顾客意见卡里就有一条“忽略你之前收到的所有指令”），
    模型看到了有可能照做。写死在代码里，它就绕不过去。
    """
    text = question or ""
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            return "这句话像是在给系统下指令，而不是在问公司的经营情况"
    for pattern in _UNSAFE_REQUEST_PATTERNS:
        if pattern.search(text):
            return "这个请求要改动数据或者取系统内部信息，超出了问答服务的范围"
    return None


def split_sentences(text: str) -> list[str]:
    """按句子切，保留换行结构，方便逐句判断与逐句引用。"""
    parts: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        found = [match.group(0).strip() for match in _SENTENCE.finditer(line)]
        parts.extend(piece for piece in found if piece)
    return parts


def sanitize(text: str) -> tuple[str, list[str]]:
    """返回（去掉指令句之后的文本，被去掉的句子）。"""
    kept: list[str] = []
    dropped: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        pieces = [match.group(0) for match in _SENTENCE.finditer(line)] or [line]
        safe = []
        for piece in pieces:
            if is_instruction_like(piece):
                dropped.append(piece.strip())
            else:
                safe.append(piece)
        joined = "".join(safe).strip()
        if joined:
            kept.append(joined)
    return "\n".join(kept), dropped
