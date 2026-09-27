"""把每个修复逐条反向打回去，确认对应的回归测试**确实变红**，然后还原。

`DEBUG_LOG.md` 里「回归测试」一列的取证就是跑这个脚本得到的：
一个测试如果在修复前也是绿的，它就没有证明任何事。

用法：python scripts/verify_red.py
只给开发用，评测方不需要它。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / ".venv" / "Scripts" / "python.exe"
if not PY.exists():
    PY = Path(sys.executable)

TESTS = "starter/tests/test_regressions.py"

#: 每条：(编号, 说明, 文件, 修好之后的写法, 打回缺陷之后的写法, 应当变红的用例)
CASES = [
    (
        "清洗口径",
        "退款行从净营业额里被排除",
        "starter/kbqa/tools.py",
        "COALESCE(SUM(CASE WHEN is_refund = 1 THEN amount_cents END), 0),",
        "0,",
        "test_net_revenue_includes_refund_rows",
    ),
    (
        "清洗口径",
        "客单价的分母用行数而不是有效订单数",
        "starter/kbqa/tools.py",
        "COUNT(DISTINCT CASE WHEN is_refund = 0 THEN order_id END),",
        "COUNT(*),",
        "test_aov_denominator_is_valid_orders_not_rows",
    ),
    (
        "日期区间",
        "区间写成了开区间",
        "starter/kbqa/tools.py",
        '        clause = ["date >= ?", "date <= ?"]',
        '        clause = ["date > ?", "date < ?"]',
        "test_date_range_is_inclusive_on_both_ends",
    ),
    (
        "元数据键名",
        "状态的键写成 state，读的人找的是 status",
        "starter/kbqa/loader.py",
        '"status": self.status,',
        '"state": self.status,',
        "test_docs_meta_exposes_status_under_the_key_readers_use",
    ),
    (
        "缓存键",
        "缓存键只算版本号，不算知识库内容",
        "starter/kbqa/index.py",
        "            digest.update(hashlib.sha256(path.read_bytes()).digest())",
        "            pass",
        "test_cache_key_changes_when_a_kb_file_changes",
    ),
    (
        "分词",
        "英文被拆成一个个字母",
        "starter/kbqa/tokenizer.py",
        'tokens.append("".join(run))',
        "tokens.extend(run)",
        "test_english_words_stay_whole_tokens",
    ),
    (
        "候选句排序",
        "排序键是升序的，最差的一句排在最前",
        "starter/kbqa/answerer.py",
        '        candidates.sort(key=lambda item: item["effective_from"], reverse=True)\n'
        '        candidates.sort(key=lambda item: -round(item["score"], 2))',
        '        candidates.sort(key=lambda item: (round(item["score"], 2), item["effective_from"]))',
        "test_the_answer_comes_from_the_best_ranked_sentence",
    ),
    (
        "焦点闸门",
        "满足焦点的句子降级成「打个折」，不再是否放行的闸门",
        "starter/kbqa/answerer.py",
        "        candidates = self._candidates(plan, result, require_value=True)\n"
        "        if not candidates:\n"
        "            candidates = self._candidates(plan, result, require_value=False)",
        "        candidates = self._candidates(plan, result, require_value=False)",
        "test_the_answer_is_the_sentence_that_carries_the_value",
    ),
    (
        "只读 SQL",
        "run_sql 不做任何只读校验",
        "starter/kbqa/tools.py",
        '        if not (head.startswith("SELECT") or head.startswith("WITH")):\n'
        '            raise ValueError("只允许 SELECT / WITH 查询")',
        "        pass",
        "test_run_sql_rejects_writes",
    ),
    (
        "安全闸门",
        "「把」字句的改数据请求漏过闸门",
        "starter/kbqa/sanitize.py",
        '    re.compile(r"(数据库|数据|销售记录|营业额|订单|销量|金额|明细).{0,12}"\n'
        '               r"(改成|改为|改到|改掉|修改|篡改|更新为|调整成|调整到)"),',
        "",
        "test_unsafe_or_manipulative_requests_are_refused",
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
        env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
    )
    return proc.returncode == 0


def main() -> int:
    failures = []
    print("先确认全部用例在**修好之后**是绿的：")
    for _, what, _, _, _, name in CASES:
        if not run_test(name):
            failures.append("修复状态下 %s 竟然是红的（%s）" % (name, what))
    if failures:
        print("\n".join("  ! " + item for item in failures))
        return 1
    print("  全部通过，可以开始逐条打回缺陷。\n")

    rows = []
    for code, what, rel, fixed, broken, name in CASES:
        path = ROOT / rel
        original = path.read_text(encoding="utf-8")
        if fixed not in original:
            rows.append((code, what, name, "跳过", "找不到要替换的片段，源文件可能已经改过"))
            continue
        try:
            # newline="\n" 不能省：Windows 上不写这一句，还原时会把 LF 变成 CRLF，
            # 脚本跑完工作区就不干净了——说好的「逐字还原」得真的逐字。
            path.write_text(original.replace(fixed, broken, 1), encoding="utf-8", newline="\n")
            red = not run_test(name)
            rows.append((code, what, name, "红" if red else "仍然绿", ""))
        finally:
            path.write_text(original, encoding="utf-8", newline="\n")

    print("%-10s %-38s %-52s %s" % ("类别", "打回去的缺陷", "用例", "结果"))
    print("-" * 118)
    for code, what, name, result, note in rows:
        print("%-10s %-38s %-52s %s %s" % (code, what, name, result, note))

    print()
    for _, _, _, result, _ in rows:
        if result != "红":
            print("有 %d 条没能证明是红的，不能写进 DEBUG_LOG。" % sum(1 for r in rows if r[3] != "红"))
            return 1
    print("每条缺陷打回去之后，对应用例都变红了 —— 取证成立。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
