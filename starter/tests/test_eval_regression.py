"""把评测挂进测试（第四关：eval-as-regression）。

跑法：起一个**没有 Key** 的服务（降级模式），把公开题库整份打完，断言总分不回落。

为什么用降级模式而不是配 Key 跑：

* 不需要外部服务、不花钱、不依赖网络，能进 CI；
* 降级路径是纯代码的，同样的输入给同样的输出，没有模型采样带来的抖动，
  所以地板线可以就定在满分——这不是苛刻，是"这条路径本来就不该掉分"。

整份题库跑完约 2~3 秒，可以跟着 `pytest starter/tests` 一起跑。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STARTER = ROOT / "starter"
EVAL = ROOT / "eval"
QUESTIONS = EVAL / "public_questions.jsonl"

#: 降级模式在公开题库上的满分。掉一分就说明改坏了，不是"差不多就行"。
FLOOR = 100.0


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _clean_env() -> dict:
    """把 Key 相关的变量摘干净，逼服务进降级模式。

    不能只在测试进程里 pop：uvicorn 起在子进程里，它读的是自己那份环境。
    """
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    for name in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
        env.pop(name, None)
    return env


def _wait_ready(port: int, timeout: float = 30.0) -> bool:
    import httpx

    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        try:
            if httpx.get("http://127.0.0.1:%d/api/health" % port, timeout=2.0).status_code == 200:
                return True
        except Exception:  # noqa: BLE001 - 还没起来就接着等
            time.sleep(0.3)
    return False


@pytest.mark.skipif(not QUESTIONS.exists(), reason="作业包的 eval/ 不在，跳过")
def test_public_question_set_does_not_regress(tmp_path):
    env = _clean_env()
    port = _free_port()
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "kbqa.server:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=str(STARTER),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    try:
        assert _wait_ready(port), "服务没能在 30 秒内起来"
        out = tmp_path / "eval_out"
        proc = subprocess.run(
            [
                sys.executable,
                str(EVAL / "run_eval.py"),
                "--base-url",
                "http://127.0.0.1:%d" % port,
                "--questions",
                str(QUESTIONS),
                "--kb",
                str(ROOT / "knowledge_base"),
                "--out",
                str(out),
            ],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
        report_path = out / "report.json"
        assert report_path.exists(), "评测没产出报告：\n%s\n%s" % (proc.stdout, proc.stderr)
        report = json.loads(report_path.read_text(encoding="utf-8"))
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - 收不掉就算了
            server.kill()

    # 两个都要断言，而且**判据是 `answer_engine`**：它是"这次真的走了哪条路"。
    # `llm_mode` 只是"有没有配 Key"，自从加了手动切换，光看它不足以说明
    # 这轮跑分没经过模型——虽然这条用例起的是自己的服务、开关拨不到它头上，
    # 但这里要证明的恰恰是"跑的是确定性的那条路"，就该按那个来断言。
    assert report["health"]["llm_mode"] == "mock", "这条用例必须跑在没配 Key 的服务上"
    assert report["health"]["answer_engine"] == "mock", "这条用例必须跑在降级路径上"

    total = report["total"]
    failed = [q["id"] for q in report["questions"] if not q["passed"]]
    assert total["earned"] >= FLOOR, "公开题库掉分了：%s，未通过 %s" % (
        json.dumps(total, ensure_ascii=False),
        failed,
    )
