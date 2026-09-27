"""把工作区里已经完成并验证过的改动，整理成一份可读的提交历史。

作业要求看到「先加一个会红的测试，再修」这个顺序，所以有三条走的是严格的两步：
先把缺陷打回代码里、只提交测试（此时代码里缺陷还在，用例是红的），
再提交修复（用例转绿）。每一步都真的跑一次对应用例并把结果打出来，
红了就是红了，绿了就是绿了，不靠嘴说。

三条的顺序是有讲究的：区间 → 退款口径 → 候选排序。
前两条同住在 `tools.py`，后一条在 `answerer.py`；排序题只有在区间与口径都修好之后
跑出来的红/绿才是干净的红/绿，不会被别的缺陷带着走。

只给开发用，评测方不需要它。
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / ".venv" / "Scripts" / "python.exe"
if not PY.exists():
    PY = Path(sys.executable)

GIT = ["git", "-c", "http.proxy=", "-c", "https.proxy="]
TESTS = "starter/tests/test_regressions.py"
CACHE = Path("starter/.cache/index.json")

TRAILER = "\n\nCo-Authored-By: Claude Code <noreply@anthropic.com>"

LOG: list[str] = []


# -- git ---------------------------------------------------------------------------

def git(*args: str) -> str:
    proc = subprocess.run(GIT + list(args), cwd=str(ROOT), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise SystemExit("git %s 失败：\n%s\n%s" % (" ".join(args), proc.stdout, proc.stderr))
    return proc.stdout.strip()


def changed(*paths: str) -> bool:
    return bool(git("status", "--porcelain", "--", *paths).strip())


def commit(message: str, *paths: str) -> None:
    if not changed(*paths):
        raise SystemExit("这些路径没有改动，提交会是空的：%s" % ", ".join(paths))
    git("add", "--", *paths)
    git("commit", "-q", "-m", message + TRAILER)
    head = git("rev-parse", "--short", "HEAD")
    subject = message.splitlines()[0]
    print("  %s  %s" % (head, subject))
    LOG.append("%s  %s" % (head, subject))


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def write(rel: str, text: str) -> None:
    (ROOT / rel).write_text(text, encoding="utf-8", newline="\n")


# -- 跑用例 -------------------------------------------------------------------------

def run_test(name: str) -> bool:
    """返回 True 表示通过。每次都先删索引缓存，别让上一次的缓存遮住缺陷。"""
    if (ROOT / CACHE).exists():
        (ROOT / CACHE).unlink()
    proc = subprocess.run(
        [str(PY), "-m", "pytest", "%s::%s" % (TESTS, name), "-q", "--no-header", "-x"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    return proc.returncode == 0


def expect(name: str, want_green: bool) -> None:
    green = run_test(name)
    ok = green == want_green
    print("     %-56s → %s%s" % (name, "绿" if green else "红",
                                 "" if ok else "   <<< 与预期不符，中止"))
    if not ok:
        raise SystemExit("取证失败：%s 应当是%s的" % (name, "绿" if want_green else "红"))
    LOG.append("    %s → %s" % (name, "绿" if green else "红"))


# -- 测试文件的分段 ------------------------------------------------------------------
#
# 三条取证各自只需要自己那条用例，所以提交出去的测试文件是「模块头 + 三个夹具 + 已引入的用例」。
# 到收尾那一步才补成全集。这样每个提交里的测试都是自洽的，不会出现「这条用例引用了还不存在的东西」。

FIXTURES = ("settings", "service", "ask")


def _blocks(full: str) -> dict:
    """按语法树切出每个顶层定义（连同它上面的装饰器）。

    一开始是用正则按行切的，结果 `@pytest.mark.parametrize(...)` 会被落在上一块里：
    装饰器一丢，参数化的用例就报 `fixture 'sql' not found`。行号交给 ast 更靠谱。
    """
    lines = full.splitlines(keepends=True)
    tree = ast.parse(full)
    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)]

    def first_line(node: ast.FunctionDef) -> int:
        """装饰器也算这个定义的一部分。"""
        return min([node.lineno] + [d.lineno for d in node.decorator_list]) - 1

    out = {"__head__": "".join(lines[:first_line(funcs[0])])}
    for node in funcs:
        out[node.name] = "".join(lines[first_line(node):node.end_lineno]).rstrip("\n") + "\n"
    return out


_FULL: str | None = None
_INTRODUCED: list[str] = []


def add_tests(names) -> None:
    """names=None 表示写回全集；否则写出「夹具 + 累计已引入的用例」。"""
    global _FULL
    if _FULL is None:
        _FULL = read(TESTS)
    if names is None:
        write(TESTS, _FULL)
        return
    _INTRODUCED.extend(names)
    blocks = _blocks(_FULL)
    missing = [n for n in _INTRODUCED if n not in blocks]
    if missing:
        raise SystemExit("测试文件里找不到这些用例：%s" % ", ".join(missing))
    body = [blocks["__head__"]]
    body += [blocks[name] for name in FIXTURES]
    body += [blocks[name] for name in _INTRODUCED]
    write(TESTS, "".join(body))


# -- 三条缺陷的两种形态 ---------------------------------------------------------------

DEFECTS = {
    "bounds": (
        "starter/kbqa/tools.py",
        '        clause = ["date >= ?", "date <= ?"]',
        '        clause = ["date >= ?", "date < ?"]',
    ),
    "readonly": (
        "starter/kbqa/tools.py",
        # 修好之后：先判只读，再执行；越权的写法抛 ValueError。
        '    def run_sql(self, sql: str) -> dict:\n'
        '        """执行一条只读 SQL。工具覆盖不到的查法，让模型自己写。\n'
        "\n"
        "        契约 §5：`data_evidence.sql` 只能是一条 `SELECT` / `WITH` 开头、确实查了表（有 `FROM`）\n"
        "        的只读查询。连接本身也是 `mode=ro` 打开的，越权的写操作在 SQLite 层就会被拒绝。\n"
        '        """\n'
        '        text = (sql or "").strip().rstrip(";").strip()\n'
        '        head = text.lstrip("( \\t\\r\\n").upper()\n'
        '        if not (head.startswith("SELECT") or head.startswith("WITH")):\n'
        '            raise ValueError("只允许 SELECT / WITH 查询")\n'
        '        if " FROM " not in (" " + text.upper().replace("\\n", " ") + " "):\n'
        '            raise ValueError("查询必须 FROM 一张表")\n'
        '        if ";" in text:\n'
        '            raise ValueError("一次只允许一条查询")\n'
        "        cursor = self.conn.execute(text)\n"
        "        rows = [dict(row) for row in cursor.fetchall()] if cursor.description else []\n"
        '        return {"sql": text, "rows": rows[:50], "row_count": len(rows)}',
        # 原始形态：不判，直接执行，还带一句 commit()。
        '    def run_sql(self, sql: str) -> dict:\n'
        '        """执行一条 SQL。工具覆盖不到的查法，让模型自己写。"""\n'
        "        cursor = self.conn.execute(sql)\n"
        "        rows = [dict(row) for row in cursor.fetchall()] if cursor.description else []\n"
        "        self.conn.commit()\n"
        '        return {"sql": sql, "rows": rows[:50], "row_count": len(rows)}',
    ),
    "refund": (
        "starter/kbqa/tools.py",
        '        """营业额、退款、订单数、客单价、销量，口径见 KB-001 §4。"""\n'
        "        where, params = self._where(start, end, store_id, product_id)\n"
        "        row = self.conn.execute(\n"
        '            """\n'
        "            SELECT COALESCE(SUM(amount_cents), 0),\n"
        "                   COALESCE(SUM(CASE WHEN is_refund = 1 THEN amount_cents END), 0),\n"
        "                   COUNT(DISTINCT CASE WHEN is_refund = 0 THEN order_id END),\n"
        "                   COALESCE(SUM(CASE WHEN is_refund = 0 THEN qty ELSE -qty END), 0)\n"
        "            FROM sales_clean WHERE %s\n"
        '            """\n'
        "            % where,\n"
        "            params,\n"
        "        ).fetchone()\n"
        "        net_cents, refund_cents, orders, qty = int(row[0]), int(row[1]), int(row[2]), int(row[3])\n"
        "        # KB-001 §4：净营业额 = 销售行之和 + 退款行之和（退款金额本身为负，效果是相减）。\n"
        "        # 退款金额取其绝对值；有效订单数只数销售行的不同 order_id，退款行不单独计单。\n"
        "        # 客单价的分母是有效订单数，不是明细行数（v2 的算法已被取代）。",
        '        """营业额、退款、订单数、客单价、销量。客单价 = 营业额 ÷ 明细行数。"""\n'
        "        where, params = self._where(start, end, store_id, product_id)\n"
        "        # 退款行不是营业，直接排掉，省得把营业额算少了。\n"
        "        row = self.conn.execute(\n"
        '            """\n'
        "            SELECT COALESCE(SUM(amount_cents), 0),\n"
        "                   0,\n"
        "                   COUNT(*),\n"
        "                   COALESCE(SUM(qty), 0)\n"
        "            FROM sales_clean WHERE %s AND is_refund = 0\n"
        '            """\n'
        "            % where,\n"
        "            params,\n"
        "        ).fetchone()\n"
        "        net_cents, refund_cents, orders, qty = int(row[0]), int(row[1]), int(row[2]), int(row[3])",
    ),
    "sort": (
        "starter/kbqa/answerer.py",
        '        candidates.sort(key=lambda item: item["effective_from"], reverse=True)\n'
        '        candidates.sort(key=lambda item: -round(item["score"], 2))',
        '        candidates.sort(key=lambda item: (round(item["score"], 2), item["effective_from"]))',
    ),
}


def pair_bodies(key: str) -> tuple[str, str, str]:
    """返回（文件, 带着缺陷的正文, 修好之后的正文）。

    两个方向都要认：`bounds` 那条在轮到它取证时，代码里已经是修好的形态，
    所以从「修复」推出「缺陷」；`refund` 和 `sort` 所在的文件要在分组提交里先交上去，
    初始化时就把它们打回了缺陷形态，所以反过来从「缺陷」推出「修复」。
    """
    rel, fixed, broken = DEFECTS[key]
    now = read(rel)
    if fixed in now:
        return rel, now.replace(fixed, broken, 1), now
    if broken in now:
        return rel, now, now.replace(broken, fixed, 1)
    raise SystemExit("%s 里既没有 %s 的修复形态、也没有缺陷形态" % (rel, key))


def strip_for_grouped_phase(key: str) -> str:
    """把某个缺陷打回代码里。用在开头——它所在的文件马上要在分组提交里交上去，
    而这条修复要留到后面单独取证。"""
    rel, fixed, broken = DEFECTS[key]
    body = read(rel)
    if fixed not in body:
        raise SystemExit("%s 里找不到 %s 对应的修复片段" % (rel, key))
    write(rel, body.replace(fixed, broken, 1))
    return rel


# -- 提交计划 -----------------------------------------------------------------------

GROUPS = [
    ("chore: 忽略本机产物与虚拟环境", (".gitignore",)),
    ("chore(dev): 重启 / 重建 / 评测的开发辅助脚本", ("scripts/dev.sh",)),
    (
        "fix(cleaning): 按 KB-001 §2/§3 实现规范化与六条剔除规则\n\n"
        "原来 clean_rows 是个直通车：金额解析不了按 0 记、数量为空按 0 记、日期照抄、\n"
        "重复行照收，18,628 行原样进库，六条规则一条都没执行。所有指标都建在这张表上。",
        ("starter/kbqa/cleaning.py",),
    ),
    (
        "fix(tokenizer): 中文按二元组切，英文按整词留\n\n"
        "原来是 normalise(text).split()——按空白切。中文句子中间没有空格，\n"
        "整句被当成一个 token，BM25 基本失效。",
        ("starter/kbqa/tokenizer.py",),
    ),
    (
        "fix(chunker): 结构化切块，补回被丢掉的尾部",
        ("starter/kbqa/chunker.py",),
    ),
    (
        "fix(loader): 支持 txt/html，GBK 回退，HTML 去标签\n\n"
        "SUPPORTED_SUFFIXES 原来只有 md，交接说明里写明的 txt/html 根本没进索引；\n"
        "旧 OA 导出的 txt 是 GBK，按 UTF-8 解码整篇变乱码；\n"
        "HTML 按原文入库，引用里会混进标签，逐字核对过不了。",
        ("starter/kbqa/loader.py",),
    ),
    (
        "fix(index): 缓存键纳入知识库内容\n\n"
        "原来只哈希三个版本号。整体替换 knowledge_base/ 时版本号没变，\n"
        "随仓库分发的那份 .cache/index.json 会被当成有效缓存继续用。",
        ("starter/kbqa/index.py",),
    ),
    (
        "fix(retriever): 先过滤再取 top_k；doc_id 取自命中的那个片段\n\n"
        "allowed 原来是全部片段：被取代的版本先占掉 top_k 的格子、再被剔掉，\n"
        "结果既不满条也不是最相关的。doc_id 原来按「第几条命中」另取一篇文档，\n"
        "某篇因为「每篇只留一格」被跳过时就与 chunk_id 对不上。",
        ("starter/kbqa/retriever.py",),
    ),
    ("fix(docfacts): 版本说明改走 doc_status", ("starter/kbqa/docfacts.py",)),
    (
        "fix(sessions): 按 session_id 隔离对话历史\n\n"
        "原来 _turns 是一份共享列表，session_id 收下就扔了，所有会话共用一段上文——\n"
        "A 会话问过 S01，B 会话里的「那 7 月呢」会接着 A 答。",
        ("starter/kbqa/sessions.py",),
    ),
    (
        "fix(service): kb_docs 数进了索引的文档；history 传进规划器\n\n"
        "kb_docs 原来数的是目录里的文件数（含没有 KB 编号的 README），与契约 §1 对不上；\n"
        "history 原来没往下传，规划器永远以为没有上文，追问一律被判成缺少上下文。",
        ("starter/kbqa/service.py",),
    ),
    (
        "fix(planner): 去掉推翻分类的兜底路由，安全闸门前置\n\n"
        "末尾原来有一条「有『多少 / 多久 / 几』就一律改成取数」，把前面刚判好的分类推翻：\n"
        "「外卖订单多久内可以申请退款」的「多久」说的是政策时限，\n"
        "「618 达到目标了吗」两边都要查——都被改成纯取数，于是拿一段营业额去答制度问题。",
        ("starter/kbqa/planner.py",),
    ),
    (
        "fix(answerer): 回答不再拼接整篇文档；指令式句子不进引用\n\n"
        "原来 _context() 把首篇命中的整篇文档拼在答案前面，动辄两三千字、数字几十个，\n"
        "撞上契约 §5 的长度与数字上限，真正那句话淹没在公文抬头里。\n"
        "sanitize() 早就写好了却从没被调用过，KB-060 里埋的指令句会进引用。",
        ("starter/kbqa/answerer.py",),
    ),
    (
        "fix(sanitize): 补齐「把」字句的改数据模式\n\n"
        "中文把字句宾语在前（「把 6 月的营业额改成 100 万」），原来只认动词在前的写法。",
        ("starter/kbqa/sanitize.py",),
    ),
    (
        "fix(live): trace 记下工具返回了什么\n\n"
        "契约 §6 要求 trace 能看到工具执行了什么、返回了什么。\n"
        "单次 SQL 可能几十行，超过 4000 字改记 {truncated, size, preview}。",
        ("starter/kbqa/live.py",),
    ),
    (
        "feat(web): 经营看板与调试面板\n\n"
        "纯静态单页，三标签：数据看板 / 问答助手 / 调试面板。\n"
        "新增只读端点 GET /api/catalog 与 GET /api/metrics/top_products，\n"
        "并在所有 /api/* 路由之后托管静态文件。",
        ("starter/kbqa/server.py", "starter/web"),
    ),
]

#: (缺陷, 测试提交说明, 引入的用例, 修复提交说明)
#: 顺序即提交顺序：每条的前提是「它前面的都修好了、它后面的都还带着缺陷」，
#: 这样每条修复提交的 diff 里只会出现它自己那一处改动。
#: 所以开头的 strip_for_grouped_phase 要把除第一条以外的全部打回缺陷形态。
PAIRS = [
    (
        "bounds",
        "test(tools): 日期区间是闭区间，两端都要算进来\n\n"
        "契约 §2 写的是闭区间，此时右端少算一天，这条是红的。",
        ["test_date_range_is_inclusive_on_both_ends"],
        "fix(tools): 日期区间右端改为闭区间\n\n"
        "原来是 date >= ? AND date < ?，右端少算一天，只查一天会返回空。",
    ),
    (
        "readonly",
        "test(tools): 任何请求都不许改动数据库，被拒绝时要给结构化的错误\n\n"
        "此时 run_sql 不判只读就直接执行，这条是红的。",
        ["test_run_sql_rejects_writes"],
        "fix(tools): run_sql 先判只读再执行\n\n"
        "原来拿到 SQL 就丢给 sqlite 执行，末尾还带一句 commit()。\n"
        "连接是 mode=ro 打开的，写操作确实写不进去，但抛出的 sqlite3.OperationalError\n"
        "不在 run_tool 接的 (TypeError, ValueError) 里，会**穿透工具层**把整轮对话打断——\n"
        "契约 §3 要求 /api/chat 任何情况下都返回 200。改成先校验、越权抛 ValueError，\n"
        "模型拿到的就是一条 {\"error\": ...}，可以自己换个查法重试。",
    ),
    (
        "refund",
        "test(tools): 净营业额必须含退款行、客单价分母是有效订单数\n\n"
        "KB-001 §4 的口径，此时退款行被排除、分母用的是明细行数，两条都是红的。",
        ["test_net_revenue_includes_refund_rows",
         "test_aov_denominator_is_valid_orders_not_rows"],
        "fix(tools): 退款行计入净营业额；客单价分母改用有效订单数\n\n"
        "SQL 里写死了 AND is_refund = 0，退款行被整体排除，净营业额偏高；\n"
        "订单数用的是 COUNT(*)（明细行数），一个订单有多行时客单价被低估。\n"
        "旁边原来还留着一句给错误行为背书的注释：「退款行不是营业，直接排掉」。",
    ),
    (
        "sort",
        "test(answerer): 候选句要按分数从高到低取",
        ["test_the_answer_comes_from_the_best_ranked_sentence"],
        "fix(answerer): 候选句排序改为按分数降序\n\n"
        "排序键原来是 (round(score, 2), effective_from) 这样一个**升序**元组，\n"
        "分数最低的那句排在最前，而取答案是从第一个开始取的——\n"
        "于是长期在挑「最不像答案的那一句」，best / best_score 参照的也是它。\n"
        "改成先按生效日期排、再按分数排（sort 是稳定的，分数因此成为主序）。",
    ),
]


def main() -> int:
    if not git("status", "--porcelain").strip():
        raise SystemExit("工作区是干净的，没有可整理的东西。")
    if git("branch", "--list", "feat/kbqa-hardening"):
        raise SystemExit("分支 feat/kbqa-hardening 已存在，先删掉或换个名字。")

    print("建分支…")
    git("checkout", "-q", "-b", "feat/kbqa-hardening")
    git("config", "user.name", "Ask690")
    git("config", "user.email", "ask690@users.noreply.github.com")

    # 除第一条之外，其余每条的缺陷都先打回代码里：这样 `answerer.py` 在分组提交里
    # 带的是它的其余改动，而 `tools.py` 的每一条修复提交，diff 里都只有它自己那一处。
    print("\n先把后面要单独取证的几条打回缺陷形态：")
    for key in ("readonly", "refund", "sort"):
        print("  已打回：%s（%s）" % (strip_for_grouped_phase(key), key))

    print("\n分组提交：")
    for message, paths in GROUPS:
        commit(message, *paths)

    print("\n逐条取证（先红后绿）：")
    for key, test_msg, introduced, fix_msg in PAIRS:
        rel, bad, good = pair_bodies(key)
        print("\n  [%s]" % key)
        write(rel, bad)
        add_tests(introduced)
        commit(test_msg, TESTS)
        for name in introduced:
            expect(name, want_green=False)
        write(rel, good)
        commit(fix_msg, rel)
        for name in introduced:
            expect(name, want_green=True)

    print("\n收尾：回归测试全集与取证脚本")
    add_tests(None)
    commit(
        "test: 回归测试全集与缺陷取证脚本\n\n"
        "scripts/verify_red.py 把每个修复逐条打回代码里，确认对应用例确实变红、再还原——\n"
        "一个在修复前也是绿的测试，证明不了任何事。\n"
        "scripts/make_history.py 是整理这份提交历史用的，一并留在这里备查。",
        TESTS, "scripts/verify_red.py", "scripts/make_history.py",
        "starter/tests/conftest.py",
    )

    # 索引缓存每次跑测试都会被重建，所以放到最后再交——上面每跑一次用例，
    # 它的内容都会跟着当前代码变一遍。
    print("\n索引缓存：")
    if not (ROOT / CACHE).exists():
        expect("test_cleaning_removes_exactly_what_kb001_says", want_green=True)
    commit("chore(cache): 用修好之后的代码重建索引缓存", str(CACHE))

    leftover = git("status", "--porcelain")
    print("\n提交历史：")
    for line in LOG:
        print("  " + line)
    print("\n工作区剩余改动：" + (leftover if leftover else "（干净）"))
    if leftover:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
