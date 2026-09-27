"""回归测试：一条对应一个真实修过的缺陷。

这里的每一条在修之前都是**红的**。和 `test_api.py` 不同，这一组不替换检索，
全部走真实的清洗库与真实索引——那些缺陷恰恰只在真实数据上才露出来
（元数据键名错位、缓存键没进内容、候选句排序反了，用假数据都测不出来）。

`DEBUG_LOG.md` 的「回归测试」一列引用的就是这里的用例名。
"""

from __future__ import annotations

import os
import types
from pathlib import Path

import pytest

from kbqa.retriever import Retriever

#: `conftest.py` 里那个替身是**给类打补丁**的（测试 API 时不想依赖知识库），
#: 装上之后整个进程的检索都变成固定返回。这里在模块导入时先抓住真身，
#: 后面只把补丁绑回自己这个实例上——不管两个测试文件谁先跑都不受影响。
_REAL_SEARCH = Retriever.search

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT.parent
KB_DIR = PROJECT / "knowledge_base"
SOURCE_DB = PROJECT / "data" / "pos.db"

pytestmark = pytest.mark.skipif(
    not SOURCE_DB.exists(), reason="没有 data/pos.db，跳过需要真实数据的回归测试"
)


@pytest.fixture(scope="session")
def settings(tmp_path_factory):
    """指向临时目录的一套配置，别动开发用的 var/。"""
    os.environ["VAR_DIR"] = str(tmp_path_factory.mktemp("var"))
    for key in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        os.environ.pop(key, None)
    from kbqa.config import load_settings

    return load_settings()
@pytest.fixture(scope="session")
def service(settings):
    from kbqa.service import Service

    instance = Service(settings)
    # 只给这一个实例绑回真检索：这一组的每条用例都必须在真实检索上跑，
    # 那些缺陷（过滤顺序、版本取代、候选排序）用固定返回是测不出来的。
    instance.retriever.search = types.MethodType(_REAL_SEARCH, instance.retriever)
    return instance
@pytest.fixture(scope="session")
def ask(service):
    """问一句，拿回回答。会话 id 每次不同，免得题目之间互相串。"""

    def run(question, session_id=None, **kwargs):
        sid = session_id or ("t-%d" % (abs(hash(question)) % 10**8))
        return service.chat(sid, question)

    return run
def test_date_range_is_inclusive_on_both_ends(service):
    """契约 §2：区间是闭区间。修之前用的是 `date > start AND date < end`。"""
    day = "2026-06-15"
    one_day = service.tools.query_metrics(day, day)
    assert one_day["orders"] > 0, "只查一天也应当有数据"

    wider = service.tools.query_metrics(day, "2026-06-16")
    assert wider["net_revenue"] > one_day["net_revenue"]
