"""分词。

中文不写空格，按空白切词等于把整句话当成一个词，任何查询都匹配不上，
检索会退化成“谁都不得分、随便补几条”。所以中文按**二元组**切：
「外卖订单多久内可以退款」切成 外卖/卖订/订单/单多/多久/久内/内可/可以/以退/退款，
文档里只要出现过「退款」这两个字就能命中，不需要分词词典。

英文与编号（`poke`、`S02`、`P06`）按字母数字串整词保留，不拆成单个字母。
入库与查询走的是同一个函数，两边必须一致。
"""

from __future__ import annotations

import unicodedata

#: 分词规则变了，索引缓存必须失效。
TOKENIZER_VERSION = "tokenizer-3"

#: 中文里几乎不携带信息的字。只用在“查询覆盖率”上，索引照常保留全部词。
STOP_CHARS = frozenset("的了吗呢是在有和与及或就都也还把被给对从向于个些这那哪什么怎样如何多少几请帮我你他它可以能要想会一下少吧啊呀们么样过得着为所")
STOP_WORDS = frozenset("the a an of to in is are and or for on at it this that how what".split())


def normalise(text: str) -> str:
    """全角转半角、统一大小写，比较与分词都走这一层。"""
    return unicodedata.normalize("NFKC", text or "").lower()


def _is_cjk(char: str) -> bool:
    """汉字与常见中文标点之外的 CJK 区段都算中文，按二元组切。"""
    code = ord(char)
    return (
        0x3400 <= code <= 0x4DBF      # 扩展 A
        or 0x4E00 <= code <= 0x9FFF   # 基本区
        or 0xF900 <= code <= 0xFAFF   # 兼容汉字
    )


def tokenize(text: str) -> list[str]:
    """切成检索用的词：中文二元组 + 英文数字整词。"""
    normalized = normalise(text)
    tokens: list[str] = []
    run: list[str] = []
    run_is_cjk = False

    def flush() -> None:
        nonlocal run_is_cjk
        if not run:
            return
        if run_is_cjk:
            if len(run) == 1:
                # 单个汉字（“月”“店”）也是词，权重在检索层压低。
                tokens.append(run[0])
            else:
                tokens.extend(left + right for left, right in zip(run, run[1:]))
        else:
            # run 里攒的是单个字符，必须拼成整词：拆成字母的话
            # `super` 会变成 s/u/p/e/r，'s' 这类字字母到处都是，排名全乱。
            tokens.append("".join(run))
        run.clear()
        run_is_cjk = False

    for char in normalized:
        if _is_cjk(char):
            if run and not run_is_cjk:
                flush()
            run_is_cjk = True
            run.append(char)
        elif char.isalnum():
            if run and run_is_cjk:
                flush()
            run.append(char)
        else:
            # 空白与标点都是分隔符。
            flush()
    flush()
    return tokens


def content_tokens(text: str) -> list[str]:
    """去掉虚词之后的查询词，用来算“这个问题被文档覆盖了多少”。"""
    kept = []
    for token in tokenize(text):
        if token in STOP_WORDS:
            continue
        if all(char in STOP_CHARS for char in token):
            continue
        kept.append(token)
    return kept
