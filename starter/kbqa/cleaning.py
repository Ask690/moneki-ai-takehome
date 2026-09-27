"""把原始 sales 导进 var/clean.db，指标都查这张表。

清洗规则完全按知识库现行口径手册 KB-001《指标口径手册 v3》§2（规范化）与 §3（剔除）实现，
规则顺序与手册一致，先规范化、再按顺序剔除，第一条命中的规则决定这行记在哪个台账里。

KB-002（v2）是已被取代的旧口径，它的三条算法（退款行剔除、空金额回填、客单价分母用明细行数）
都不在这里实现；docs/API_CONTRACT.md 第 0 节要求数据口径以 KB-001 为准。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Optional

#: 金额里的 `¥` 与各类空白去掉再按数字解析（KB-001 §2.3）。
_CURRENCY = str.maketrans("", "", "¥￥ \t\r\n　 ")

#: KB-001 §2.2 接受的三种日期格式。
_ISO_DATE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_SLASH_DATE = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})$")
#: 旧 POS 导出格式：日在前、月在后。`25-07-2026` 是 2026 年 7 月 25 日。
_DMY_DATE = re.compile(r"^(\d{1,2})-(\d{1,2})-(\d{4})$")

REMOVAL_REASONS = (
    "1_unparseable_date",
    "2_empty_amount",
    "3_qty_le_zero",
    "4_store_not_in_stores",
    "5_product_not_in_products",
    "6_duplicate_row",
)

_MONTH_DAYS = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _valid_ymd(year: int, month: int, day: int) -> bool:
    if not (1 <= month <= 12) or day < 1:
        return False
    limit = _MONTH_DAYS[month - 1]
    if month == 2 and _is_leap(year):
        limit = 29
    return day <= limit


def normalize_date(value: Optional[str]) -> Optional[str]:
    """把三种写法统一成 `YYYY-MM-DD`；认不出来返回 None（KB-001 §2.2）。

    第三条规则最容易搞反：`DD-MM-YYYY` 是日在前，所以 `25-07-2026` 解析成 7 月 25 日。
    """
    text = (value or "").strip()
    if not text:
        return None
    match = _ISO_DATE.match(text) or _SLASH_DATE.match(text)
    if match:
        year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
    else:
        match = _DMY_DATE.match(text)
        if not match:
            return None
        day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
    if not _valid_ymd(year, month, day):
        return None
    return "%04d-%02d-%02d" % (year, month, day)


def normalize_code(value: Optional[str]) -> str:
    """门店号、商品号：去掉首尾空白并转大写（KB-001 §2.1）。

    `s01`、`S01 `、` s03` 都是可恢复的写法，规范化之后是合法编号，不是脏数据。
    """
    return (value or "").strip().upper()


def parse_amount(value: Optional[str]) -> tuple[Optional[int], str]:
    """返回 (分, 状态)。状态取值：`ok`、`empty`、`bad`。

    KB-001 §2.3 与 §3.2：`¥38.00` 与 `38.00` 是同一个金额；空金额直接剔除，**不回填**。
    带负号的行是可恢复的退款行，必须保留（§4）。
    """
    text = (value or "").translate(_CURRENCY)
    if not text:
        return None, "empty"
    try:
        cents = int((Decimal(text) * 100).to_integral_value())
    except (InvalidOperation, ValueError, ArithmeticError):
        return None, "bad"
    return cents, "ok"


def parse_qty(value: Optional[str]) -> Optional[int]:
    """KB-001 §2.4：按整数解析。解析不了的返回 None，按 §3.3 剔除。"""
    text = (value or "").translate(_CURRENCY)
    if not text:
        return None
    try:
        return int(Decimal(text))
    except (InvalidOperation, ValueError, ArithmeticError):
        return None


@dataclass
class CleaningReport:
    raw_rows: int = 0
    kept_rows: int = 0
    kept_sales_rows: int = 0
    kept_refund_rows: int = 0
    removed: dict[str, int] = field(default_factory=lambda: {k: 0 for k in REMOVAL_REASONS})
    note_unparseable_amount: int = 0

    def as_dict(self) -> dict:
        return {
            "raw_rows": self.raw_rows,
            "removed": dict(self.removed, note_unparseable_amount=self.note_unparseable_amount),
            "kept_rows": self.kept_rows,
            "kept_sales_rows": self.kept_sales_rows,
            "kept_refund_rows": self.kept_refund_rows,
        }


def open_readonly(path: Path) -> sqlite3.Connection:
    """以只读方式打开数据库。

    契约 §5 要求数据库不能有任何改动，所以连接走 `mode=ro`：
    即使上层误写 UPDATE/DELETE，SQLite 也会直接报错，而不是悄悄改掉源数据。
    """
    uri = "file:%s?mode=ro" % Path(path).as_posix().replace("?", "%3f").replace("#", "%23")
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def open_readwrite(path: Path) -> sqlite3.Connection:
    """只在重建清洗表时使用。"""
    conn = sqlite3.connect(Path(path).as_posix(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def clean_rows(
    rows: Iterable[sqlite3.Row],
    store_ids: Iterable[str],
    product_ids: Iterable[str],
) -> tuple[list[tuple], CleaningReport]:
    """按 KB-001 §2 规范化、再按 §3 的六条规则顺序剔除。

    返回清洗后的行与台账。行的形状：
    `(order_id, date, store_id, product_id, qty, amount_cents, payment, is_refund)`。
    """
    report = CleaningReport()
    valid_stores = {normalize_code(s) for s in store_ids}
    valid_products = {normalize_code(p) for p in product_ids}
    kept: list[tuple] = []
    seen: set[tuple] = set()

    for row in rows:
        report.raw_rows += 1

        # §2 规范化：先做，再判断是不是脏值。顺序反了会把 `s01`、`¥38.00` 这类可恢复值误删。
        normalized_date = normalize_date(row["date"])
        store_id = normalize_code(row["store_id"])
        product_id = normalize_code(row["product_id"])
        qty = parse_qty(row["qty"])
        cents, amount_status = parse_amount(row["amount"])
        payment = (row["payment"] or "").strip()

        # §3.1 日期无法解析。
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

        # §3.6 七个字段规范化后完全一致才算重复行。
        # 共用 order_id、只有商品不同的多行订单是合法的，必须全部留下。
        order_id = (row["order_id"] or "").strip()
        signature = (order_id, normalized_date, store_id, product_id, qty, cents, payment)
        if signature in seen:
            report.removed["6_duplicate_row"] += 1
            continue
        seen.add(signature)

        kept.append(
            (
                order_id,
                normalized_date,
                store_id,
                product_id,
                qty,
                cents,
                payment,
                1 if cents < 0 else 0,
            )
        )

    report.kept_rows = len(kept)
    # §4：退款行是剔除之后 amount < 0 的行，销售行是 amount > 0 的行。
    report.kept_refund_rows = sum(1 for row in kept if row[7])
    report.kept_sales_rows = report.kept_rows - report.kept_refund_rows
    return kept, report


_SCHEMA = """
CREATE TABLE stores (store_id TEXT PRIMARY KEY, store_name TEXT, category TEXT, district TEXT);
CREATE TABLE products (product_id TEXT PRIMARY KEY, product_name TEXT,
                       product_category TEXT, unit_price REAL);
CREATE TABLE sales_clean (
    order_id TEXT, date TEXT, store_id TEXT, product_id TEXT,
    qty INTEGER, amount_cents INTEGER, payment TEXT, is_refund INTEGER
);
CREATE INDEX idx_clean_date ON sales_clean(date);
CREATE INDEX idx_clean_store ON sales_clean(store_id);
CREATE INDEX idx_clean_product ON sales_clean(product_id);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


def build_clean_db(source: Path, target: Path) -> CleaningReport:
    """从只读的源库重建清洗表。返回清洗台账，供 `/api/health` 与数据质量面板使用。"""
    if not source.exists():
        raise FileNotFoundError("找不到源数据库：%s" % source)
    src = open_readonly(source)
    try:
        stores = [
            tuple(r)
            for r in src.execute(
                "SELECT store_id, store_name, category, district FROM stores"
            )
        ]
        products = [
            tuple(r)
            for r in src.execute(
                "SELECT product_id, product_name, product_category, unit_price FROM products"
            )
        ]
        rows, report = clean_rows(
            src.execute(
                "SELECT order_id, date, store_id, product_id, qty, amount, payment FROM sales"
            ),
            [r[0] for r in stores],
            [r[0] for r in products],
        )
    finally:
        src.close()

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    out = open_readwrite(target)
    try:
        out.executescript(_SCHEMA)
        out.executemany("INSERT INTO stores VALUES (?,?,?,?)", stores)
        out.executemany("INSERT INTO products VALUES (?,?,?,?)", products)
        out.executemany("INSERT INTO sales_clean VALUES (?,?,?,?,?,?,?,?)", rows)
        out.execute(
            "INSERT INTO meta VALUES ('cleaning_report', ?)",
            (json.dumps(report.as_dict(), ensure_ascii=False),),
        )
        out.execute("INSERT INTO meta VALUES ('source_db', ?)", (source.name,))
        out.commit()
    finally:
        out.close()
    return report
