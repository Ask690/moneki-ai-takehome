"""手动切换问答引擎：`POST /api/mode`。

契约 §1 里的 `llm_mode` 说的是**配置**（有没有 Key），不是"下一次提问会走哪条路"。
这个开关是运行期的手动覆盖，两者必须分开报：

- `llm_mode`      —— 还是环境变量决定的那个值，预检 P12 认的就是它；
- `answer_engine` —— 下一次提问实际会走的引擎；
- `llm_override`  —— 手动覆盖，`null` 表示跟随环境。

分开报的好处是：不管开关拨在哪一边，预检 P12 都照过；而 `eval/run_eval.py`
会把 `/api/health` 整个快照进报告，所以**哪次跑分是降级跑的，报告里自己写着**，
不靠人记。
"""

from __future__ import annotations

import os
from dataclasses import replace

import pytest

#: 一个连不上的地址。切到降级之后**根本不该发起任何请求**；
#: 万一路由写错了，这里会立刻「连接被拒」，而不是挂满 120 秒。
DEAD_URL = "http://127.0.0.1:9"


@pytest.fixture(scope="module")
def keyed(tmp_path_factory):
    """一套"配了 Key"的服务：用来测有 Key 时也能手动切到降级。"""
    os.environ["VAR_DIR"] = str(tmp_path_factory.mktemp("var"))
    for key in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        os.environ.pop(key, None)

    from kbqa.config import load_settings
    from kbqa.service import Service

    settings = replace(
        load_settings(),
        llm_base_url=DEAD_URL,
        llm_api_key="test-key-not-real",
        llm_model="test-model",
    )
    return Service(settings)


# -- 两个值必须分开报 ---------------------------------------------------------


def test_health_reports_the_engine_beside_the_configuration(client):
    """没配 Key 时：配置是 mock，实际引擎也是 mock，没有手动覆盖。"""
    body = client.get("/api/health").json()
    assert body["llm_mode"] == "mock"
    assert body["answer_engine"] == "mock"
    assert body["llm_override"] is None


def test_the_switch_returns_both_values(client):
    response = client.post("/api/mode", json={"mode": "mock"})
    assert response.status_code == 200
    body = response.json()
    assert body["llm_mode"] == "mock", "llm_mode 说的是配置，开关不该改它"
    assert body["answer_engine"] == "mock"
    assert body["llm_override"] == "mock"


def test_setting_the_mode_twice_is_not_an_error(client):
    """按钮会被连点。同一个模式设两遍，第二遍也该是 200。"""
    assert client.post("/api/mode", json={"mode": "mock"}).status_code == 200
    assert client.post("/api/mode", json={"mode": "mock"}).status_code == 200


# -- 拨不过去的情况要给出原因 -------------------------------------------------


def test_live_without_a_key_is_refused_with_a_reason(client):
    """没有 Key 就没有 live 这条路可走。不能默默答应下来什么都不做。"""
    response = client.post("/api/mode", json={"mode": "live"})
    assert response.status_code == 400
    assert "LLM_API_KEY" in response.json()["error"]


def test_an_unknown_mode_is_refused(client):
    response = client.post("/api/mode", json={"mode": "turbo"})
    assert response.status_code == 400
    assert "turbo" in response.json()["error"]


def test_a_missing_mode_is_refused(client):
    """`{}` 不当作"切回跟随环境"，那是 `{"mode": "auto"}` 的活儿。"""
    response = client.post("/api/mode", json={})
    assert response.status_code == 400


# -- 拨得动的情况 -------------------------------------------------------------


def test_a_keyed_service_switches_both_ways(keyed):
    try:
        assert keyed.answer_engine == "live", "配了 Key，默认就该走模型"
        assert keyed.settings.llm_mode == "live"

        keyed.set_engine("mock")
        assert keyed.answer_engine == "mock"
        assert keyed.settings.llm_mode == "live", "开关不改变配置本身"

        keyed.set_engine("auto")
        assert keyed.answer_engine == "live", "auto 就是跟随环境"
        assert keyed.engine_state()["llm_override"] is None
    finally:
        keyed.set_engine("auto")


def test_switching_to_mock_actually_skips_the_model(keyed):
    """**这个开关唯一真正的行为断言。**

    切到降级之后，配着 Key 也不能再去调模型——地址指向一个连不上的端口，
    路由写对了就一次请求都不会发出去。所以这里断言的不是"回答好看"，
    而是"根本没联过网"：答案里没有那句模型失败的话，trace 里也只有 mock 那一步。

    修之前 `_run_engine` 判的是 `self.settings.live`（只看环境变量），
    开关拨到 mock 它照样会去调 `DEAD_URL`，这条用例就是红的。
    """
    keyed.set_engine("mock")
    try:
        body = keyed.chat("mode-switch-test", "2026 年 7 月全公司净营业额是多少？")
    finally:
        keyed.set_engine("auto")

    assert "模型服务这次没有正常返回" not in body["answer"], (
        "切到降级之后还在调模型：%s" % body["answer"][:160]
    )
    steps = [step["step"] for step in keyed.get_trace(body["trace_id"])["steps"]]
    assert "answer_mock" in steps, steps
    assert "answer_live" not in steps, steps
