"""把每个修复逐条反向打回去，确认对应的回归测试**确实变红**，然后还原。

`DEBUG_LOG.md` 里「回归测试」一列的取证就是跑这个脚本得到的：
一个测试如果在修复前也是绿的，它就没有证明任何事。

用法：python scripts/verify_red.py [用例名片段]
只给开发用，评测方不需要它。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / ".venv" / "Scripts" / "python.exe"
if not PY.exists():
    PY = Path(sys.executable)

TESTS = "starter/tests/test_regressions.py"

# 每条缺陷一处或多处替换：`edits` 里是 (文件, 修好之后的写法, 打回缺陷之后的写法)。
# 一处缺陷要同时改两个文件时（比如键名错位既在写的一端也在读的一端），
# 必须一起打回去，只改一半的话两边仍然自洽，用例照样是绿的。
CASES: list[tuple[str, str, str, list[tuple[str, str, str]]]] = [
    # -- 清洗 -------------------------------------------------------------------
    (
        "清洗口径",
        "六条剔除规则整段略过，原始行原样进库",
        "test_cleaning_removes_exactly_what_kb001_says",
        [
            (
                "starter/kbqa/cleaning.py",
                """        # §3.1 日期无法解析。
        if normalized_date is None:
            report.removed["1_unparseable_date"] += 1
            continue
        # §3.2 amount 为空。不回填，直接剔除。
        if amount_status == "empty":
            report.removed["2_empty_amount"] += 1
            continue
        # 非空但解析不了的金额属于畸形值，同样不能进统计，单独记一笔以便对账。
        if amount_status != "ok":
            report.note_unparseable_amount += 1
            report.removed["2_empty_amount"] += 1
            continue
        # §3.3 qty ≤ 0。退款行的 qty 是正数、金额为负，不受这条影响。
        if qty is None or qty <= 0:
            report.removed["3_qty_le_zero"] += 1
            continue
        # §3.4 / §3.5 规范化之后再判断外键，顺序反了会误删真实订单。
        if store_id not in valid_stores:
            report.removed["4_store_not_in_stores"] += 1
            continue
        if product_id not in valid_products:
            report.removed["5_product_not_in_products"] += 1
            continue
""",
                """        # §3.1 日期无法解析。
        if normalized_date is None:
            report.removed["1_unparseable_date"] += 1
            continue
        # §3.2–§3.6 整段略过：金额解析不了的按 0，不判外键，不去重。
        if amount_status != "ok":
            cents = 0
        qty = qty or 0
""",
            )
        ],
    ),
    (
        "清洗口径",
        "退款行从净营业额里被排除",
        "test_net_revenue_includes_refund_rows",
        [
            (
                "starter/kbqa/tools.py",
                "COALESCE(SUM(CASE WHEN is_refund = 1 THEN amount_cents END), 0),",
                "0,",
            )
        ],
    ),
    (
        "清洗口径",
        "客单价的分母用行数而不是有效订单数",
        "test_aov_denominator_is_valid_orders_not_rows",
        [
            (
                "starter/kbqa/tools.py",
                "COUNT(DISTINCT CASE WHEN is_refund = 0 THEN order_id END),",
                "COUNT(*),",
            )
        ],
    ),
    (
        "日期区间",
        "区间写成了开区间",
        "test_date_range_is_inclusive_on_both_ends",
        [
            (
                "starter/kbqa/tools.py",
                '        clause = ["date >= ?", "date <= ?"]',
                '        clause = ["date > ?", "date < ?"]',
            )
        ],
    ),
    # -- 索引 -------------------------------------------------------------------
    (
        "元数据键名",
        "状态的键写成 state，读的人找的是 status",
        "test_docs_meta_exposes_status_under_the_key_readers_use",
        [
            (
                "starter/kbqa/loader.py",
                '            "status": self.status,',
                '            "state": self.status,',
            )
        ],
    ),
    (
        "元数据键名",
        "键名错位 + 读的一端绕过 doc_status，取代关系静默失效",
        "test_superseded_version_is_excluded_for_the_current_rule",
        [
            (
                "starter/kbqa/loader.py",
                '            "status": self.status,',
                '            "state": self.status,',
            ),
            (
                "starter/kbqa/retriever.py",
                '        if doc_status(meta) == "已废止" and ends and as_of.isoformat() >= ends:',
                '        if meta.get("status") == "已废止" and ends and as_of.isoformat() >= ends:',
            ),
        ],
    ),
    (
        "缓存键",
        "缓存键只算版本号，不算知识库内容",
        "test_cache_key_changes_when_a_kb_file_changes",
        [
            (
                "starter/kbqa/index.py",
                "            digest.update(hashlib.sha256(path.read_bytes()).digest())",
                "            pass",
            )
        ],
    ),
    (
        "分词",
        "英文被拆成一个个字母",
        "test_english_words_stay_whole_tokens",
        [
            (
                "starter/kbqa/tokenizer.py",
                '            tokens.append("".join(run))',
                "            tokens.extend(run)",
            )
        ],
    ),
    # -- 检索 -------------------------------------------------------------------
    (
        "过滤顺序",
        "先取 top_k 再过滤，被挡掉的版本先占掉格子",
        "test_top_k_is_filled_after_filtering_not_before",
        [
            (
                "starter/kbqa/retriever.py",
                """        allowed = {
            position
            for position, chunk in enumerate(self.index.chunks)
            if chunk.doc_id not in excluded
        }""",
                "        allowed = set(range(len(self.index.chunks)))",
            )
        ],
    ),
    (
        "命中归属",
        "doc_id 按「第几条命中」另取一篇文档",
        "test_every_hit_reports_its_own_chunk",
        [
            (
                "starter/kbqa/retriever.py",
                "            hits.append(self._hit(position, score, filtered))",
                """            ordered = [self.index.chunks[pos] for _, pos in adjusted]
            hit = self._hit(position, score, filtered)
            hit.doc_id = ordered[len(hits)].doc_id
            hits.append(hit)""",
            )
        ],
    ),
    # -- 工具 -------------------------------------------------------------------
    (
        "只读 SQL",
        "run_sql 不校验语句形状，非 SELECT / 无 FROM 的查询照过",
        # 注意这里用的**不是** test_run_sql_rejects_writes：连接以 mode=ro 打开，
        # 写操作在 SQLite 层就被挡住，那条用例把缺陷打回去也照样是绿的，
        # 它是护栏不是证据（见 DEBUG_LOG.md 第 11 条）。
        "test_run_sql_only_returns_selects_with_a_from",
        [
            (
                "starter/kbqa/tools.py",
                """        if not (head.startswith("SELECT") or head.startswith("WITH")):
            raise ValueError("只允许 SELECT / WITH 查询")
        if " FROM " not in (" " + text.upper().replace("\\n", " ") + " "):
            raise ValueError("查询必须 FROM 一张表")
        if ";" in text:
            raise ValueError("一次只允许一条查询")""",
                "        pass",
            )
        ],
    ),
    # -- 安全闸门 ---------------------------------------------------------------
    (
        "安全闸门",
        "「把」字句的改数据请求漏过闸门",
        "test_unsafe_or_manipulative_requests_are_refused",
        [
            (
                "starter/kbqa/sanitize.py",
                """    re.compile(r"(数据库|数据|销售记录|营业额|订单|销量|金额|明细).{0,12}"
               r"(改成|改为|改到|改掉|修改|篡改|更新为|调整成|调整到)"),""",
                "    # 打回缺陷：这条「把」字句模式被拿掉。",
            )
        ],
    ),
    (
        "文档注入",
        "文档里埋的指令句不再跳过，进了候选句",
        "test_instruction_sentences_never_become_candidates",
        [
            (
                "starter/kbqa/answerer.py",
                """                if is_instruction_like(unit.text):
                    continue
""",
                "",
            )
        ],
    ),
    # -- 工具层与 live 引擎 -------------------------------------------------------
    (
        "工具异常",
        "工具抛出的异常只接 (TypeError, ValueError)，SQL 报错穿透工具层",
        "test_tool_errors_come_back_as_structured_errors",
        [
            (
                "starter/kbqa/service.py",
                "        except Exception as exc:  # noqa: BLE001 - 见下",
                "        except (TypeError, ValueError) as exc:",
            )
        ],
    ),
    (
        "工具轮次",
        "工具轮次用完就抛错，而不是用手上的结果作答",
        "test_tool_loop_exhaustion_still_produces_an_answer",
        [
            (
                "starter/kbqa/live.py",
                '        messages.append({"role": "user", "content": _FORCE_ANSWER})\n'
                "        reply = self.client.chat_with_retry(messages, None, budget=remaining, on_call=trace.llm)\n"
                '        trace.step("tool_loop_forced_answer", {"rounds": MAX_TOOL_ROUNDS})\n'
                "        return self._finalise(plan, reply.content, evidence, retrieved, trace)",
                '        raise LLMError("tool_loop", "工具调用超过 %d 轮仍未给出回答" % MAX_TOOL_ROUNDS)',
            )
        ],
    ),
    (
        "证据裁剪",
        "证据整包塞进 data_evidence，数字总数撞上 60 的上限",
        "test_data_evidence_stays_within_the_number_budget",
        [
            (
                "starter/kbqa/live.py",
                "    wanted = _numbers_in(text)\n    scored = []",
                "    return evidence\n    wanted = _numbers_in(text)\n    scored = []",
            ),
            (
                "starter/kbqa/live.py",
                "            data_evidence=_prune_evidence(evidence, text),",
                "            data_evidence=evidence,",
            )
        ],
    ),
    # -- 作答 -------------------------------------------------------------------
    (
        "作答长度",
        "回答前面拼接首篇命中的整篇文档",
        "test_answers_stay_within_the_contract_limits",
        [
            (
                "starter/kbqa/answerer.py",
                '        return Answer(answer=body, answer_type="doc", citations=citations)',
                """        context = "".join(
            chunk.text
            for hit in result.hits[:1]
            for chunk in self.retriever.index.chunks_of(hit.doc_id)
        )
        return Answer(answer=context + body, answer_type="doc", citations=citations)""",
            )
        ],
    ),
    (
        "候选句排序",
        "排序键是升序的，最差的一句排在最前",
        "test_the_answer_comes_from_the_best_ranked_sentence",
        [
            (
                "starter/kbqa/answerer.py",
                '        candidates.sort(key=lambda item: item["effective_from"], reverse=True)\n'
                '        candidates.sort(key=lambda item: -round(item["score"], 2))',
                '        candidates.sort(key=lambda item: (round(item["score"], 2), item["effective_from"]))',
            )
        ],
    ),
    (
        "焦点闸门",
        "满足焦点的句子降级成「打个折」，不再是否放行的闸门",
        "test_the_answer_is_the_sentence_that_carries_the_value",
        [
            (
                "starter/kbqa/answerer.py",
                "        candidates = self._candidates(plan, result, require_value=True)\n"
                "        if not candidates:\n"
                "            candidates = self._candidates(plan, result, require_value=False)",
                "        candidates = self._candidates(plan, result, require_value=False)",
            )
        ],
    ),
    # -- 会话 -------------------------------------------------------------------
    (
        "会话隔离",
        "所有 session_id 共用一段上文",
        "test_sessions_do_not_bleed_into_each_other",
        [
            (
                "starter/kbqa/sessions.py",
                """        # 没给 session_id 就当成无状态：不读历史也不写历史。
        # 随便给个 key 会让两个互不相关的调用共用上文。
        return key or None""",
                '        return "shared"',
            )
        ],
    ),
    (
        "追问上下文",
        "history 没传进规划器，追问永远判成缺少上下文",
        "test_follow_up_within_one_session_keeps_the_context",
        [
            (
                "starter/kbqa/service.py",
                "            plan = self.planner.plan(question, history)",
                "            plan = self.planner.plan(question)",
            )
        ],
    ),
    # -- 自己加的额外题库带出来的两处 ---------------------------------------------
    (
        "价格意图",
        "PRICE_WORDS 不认「成交价/均价」，整句落到纯文档分支",
        "test_average_price_questions_go_to_the_database",
        [
            (
                "starter/kbqa/entities.py",
                """    # “成交价/均价/实收价”问的是**数据库里的实收金额**，按 KB-001 §5 排在文档之前。
    # 少了这几个词，这类问句会被判成纯文档题，拿一段调价通知去回答一个该算出来的数字。
    "成交价", "均价", "平均价", "实收价",
""",
                "",
            )
        ],
    ),
    (
        "隐私闸门",
        "没有隐私闸门，索要某个人的私人联系方式照答",
        "test_requests_for_a_person_s_private_contact_are_refused",
        [
            (
                "starter/kbqa/sanitize.py",
                """    for pattern in _PERSONAL_INFO_PATTERNS:
        if pattern.search(text):
            return "这问的是某个人的私人联系方式，不属于经营数据"
""",
                "",
            )
        ],
    ),
    (
        "版本上下文",
        "live 模式下 search_kb 只把 query 递给检索器，版本上下文全丢",
        "test_live_mode_search_keeps_the_version_context",
        [
            (
                "starter/kbqa/live.py",
                "                result = self.run_tool(name, self._tool_params(plan, name, params))",
                "                result = self.run_tool(name, params)",
            )
        ],
    ),
]


def run_test(name: str) -> bool:
    """返回 True 表示通过。

    每次先删掉索引缓存：缓存键只覆盖「知识库内容 + 算法版本号」，
    不覆盖元数据的**形状**。只把 `meta()` 的键名改回去的话内容没变、
    缓存键就没变，磁盘上那份老索引会被继续当成有效缓存——
    于是「键名错位」这个缺陷在缓存后面藏得好好的，测试照样是绿的。
    """
    cache = ROOT / "starter" / ".cache" / "index.json"
    if cache.exists():
        cache.unlink()
    proc = subprocess.run(
        [str(PY), "-m", "pytest", "%s::%s" % (TESTS, name), "-q", "--no-header", "-x"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    return proc.returncode == 0


def apply_edits(edits) -> str | None:
    """把一处缺陷的所有替换一次性写下去。返回 None 表示成功，否则是原因。

    必须**先全部检查再全部写**：一处缺陷的两个替换里只要有一个锚点找不到，
    写下去的那一半就会留在树上，脚本跑完工作区就不干净了。
    """
    plans = []
    for rel, fixed, broken in edits:
        path = ROOT / rel
        original = path.read_text(encoding="utf-8")
        if fixed not in original:
            return "%s 里找不到要替换的片段" % rel
        plans.append((path, original, original.replace(fixed, broken, 1)))
    for path, _, mutated in plans:
        # newline="\n" 不能省：Windows 上不写这一句，还原时会把 LF 变成 CRLF，
        # 脚本跑完工作区就不干净了——说好的「逐字还原」得真的逐字。
        path.write_text(mutated, encoding="utf-8", newline="\n")
    return None


def restore(originals: dict[Path, str]) -> None:
    for path, text in originals.items():
        path.write_text(text, encoding="utf-8", newline="\n")


def main() -> int:
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    cases = [c for c in CASES if only in c[2]] if only else CASES
    if not cases:
        print("没有匹配的用例：%s" % only)
        return 1

    print("先确认全部用例在**修好之后**是绿的：")
    broken_green = []
    for _, what, name, _ in cases:
        if not run_test(name):
            broken_green.append("%s（%s）在修复状态下竟然是红的" % (name, what))
    if broken_green:
        print("\n".join("  ! " + item for item in broken_green))
        return 1
    print("  %d 条全部通过，可以开始逐条打回缺陷。\n" % len(cases))

    rows = []
    for code, what, name, edits in cases:
        originals = {ROOT / rel: (ROOT / rel).read_text(encoding="utf-8") for rel, _, _ in edits}
        try:
            problem = apply_edits(edits)
            if problem:
                rows.append((code, what, name, "跳过", problem))
                continue
            red = not run_test(name)
            rows.append((code, what, name, "红" if red else "仍然绿", ""))
        finally:
            restore(originals)

    print("%-10s %-44s %-56s %s" % ("类别", "打回去的缺陷", "用例", "结果"))
    print("-" * 130)
    for code, what, name, result, note in rows:
        print("%-10s %-44s %-56s %s %s" % (code, what, name, result, note))

    failed = [row for row in rows if row[3] != "红"]
    print()
    if failed:
        print("有 %d 条没能证明是红的，不能写进 DEBUG_LOG。" % len(failed))
        return 1
    print("每条缺陷打回去之后，对应用例都变红了 —— 取证成立。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
