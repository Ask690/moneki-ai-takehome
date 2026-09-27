"""把各个部件接起来：规划、取数、检索、作答。"""

from __future__ import annotations

import re
import time
from datetime import date
from typing import Any, Optional

from .answerer import Answerer
from .schemas import Answer
from .cleaning import build_clean_db
from .docfacts import DocFacts
from .config import Settings, load_settings
from .entities import Catalog
from .index import load_index
from .live import LiveEngine
from .llm import LLMClient, LLMError
from .planner import Planner
from .retriever import Retriever
from .sessions import SessionStore
from .toolspec import TOOL_NAMES, TOOLS
from .tools import DataTools
from .trace import Trace, TraceStore

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_INT_PARAMS = {"top_k", "limit"}
#: 这三个不是给模型填的参数，而是规划器算出来的**版本上下文**（见 `retrieve`）。
#: 它们不在工具的 JSON schema 里——模型不该自己决定"哪一版现行"——
#: 但 `search_kb` 需要它们，所以在参数清洗时放行，取值原样保留（`as_of` 是 date 对象，
#: 转成字符串反而和 `Retriever.search` 的比较对不上）。
_CONTEXT_KEYS = ("year", "as_of", "historical")


class Service:
    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or load_settings()
        self.sessions = SessionStore()
        self.traces = TraceStore()
        #: 运行期的手动模式覆盖，见 `answer_engine`。只活在进程内存里。
        self._engine_override: Optional[str] = None
        self.rebuild(only_if_missing=True)

    # -- 引擎选择 ---------------------------------------------------------------

    @property
    def answer_engine(self) -> str:
        """下一次提问**实际会走**的引擎：`live` 或 `mock`。

        与 `settings.llm_mode` 分开：那个是**配置**（有没有 Key），这个是被手动
        开关覆盖之后的实际路由。契约 §1 的 `llm_mode` 一直指前者，预检 P12 认的
        也是它——所以拨开关不会把预检弄红。
        """
        if self._engine_override is not None:
            return self._engine_override
        return self.settings.llm_mode

    def engine_state(self) -> dict:
        """`/api/mode` 与 `/api/health` 一起报的那三个字段。"""
        return {
            "llm_mode": self.settings.llm_mode,
            "answer_engine": self.answer_engine,
            "llm_override": self._engine_override,
        }

    def set_engine(self, mode: str) -> dict:
        """手动切换引擎。`mode` 取 `live` / `mock` / `auto`（=`auto` 跟随环境）。

        只改内存：不落盘、不动环境变量、不改 `settings`。**重启即回到环境变量决定的
        模式**——演示时拨过头了，重启一下就好，不会留下一个说不清的持久状态。
        """
        wanted = (mode or "").strip().lower()
        if wanted == "auto":
            self._engine_override = None
        elif wanted in ("live", "mock"):
            if wanted == "live" and not self.settings.live:
                raise ValueError(
                    "没有配置 Key，切不到 live。三个环境变量（LLM_BASE_URL、LLM_API_KEY、"
                    "LLM_MODEL）要注入之后再启动服务。"
                )
            self._engine_override = wanted
        else:
            raise ValueError("mode 只能是 live、mock 或 auto，收到 %r" % (mode,))
        return self.engine_state()

    # -- 启动与重建 -------------------------------------------------------------

    def rebuild(self, only_if_missing: bool = False) -> None:
        settings = self.settings
        if not only_if_missing or not settings.clean_db.exists():
            build_clean_db(settings.source_db, settings.clean_db)
        self.tools = DataTools(settings.clean_db)
        self.index = load_index(settings.kb_dir, settings.index_path, rebuild=not only_if_missing)
        self.retriever = Retriever(self.index, settings.today)
        self.catalog = Catalog(
            stores=self.tools.stores(), products=self.tools.products(), aliases=self.index.aliases
        )
        self.data_period = self.tools.data_period()
        self.facts = DocFacts(self.index)
        self.answerer = Answerer(
            self.tools, self.retriever, self.catalog, settings.today, self.data_period, self.facts
        )
        self.planner = Planner(self.catalog, settings.today, self.data_period, self._scout)

    def _scout(self, text: str) -> tuple[float, float]:
        """给一句话探底：它的词在知识库里有多少、检索最高分多少。

        越界判断只看这两个数，不看话题词表：知识库真讲这件事就一定照答。
        """
        result = self.retriever.search(text, top_k=1)
        return self.facts.vocab_coverage(text), (result.hits[0].score if result.hits else 0.0)

    # -- 只读接口 ---------------------------------------------------------------

    def health(self) -> dict:
        report = self.tools.cleaning_report()
        return {
            "status": "ok",
            # `llm_mode` 是配置，`answer_engine` / `llm_override` 是运行期路由。
            # `eval/run_eval.py` 会把整个 health 快照进报告，所以哪次跑分是
            # 降级跑的，报告里自己写着，不靠人记。
            **self.engine_state(),
            # 契约 §1：数的是实际进了索引的文档，不是目录里的文件数。
            # 目录里的 README.md 没有 KB 编号，不算文档；数文件会多报。
            "kb_docs": len(self.index.docs_meta),
            "kb_chunks": len(self.index.chunks),
            "valid_sales_rows": self.tools.valid_sales_rows(),
            "today": self.settings.today.isoformat(),
            "data_period": self.data_period,
            "cleaning_report": report,
            "index_key": self.index.key[:12],
            "kb_warnings": self.index.warnings,
        }

    def metrics_summary(self, start: str, end: str, store_id=None, product_id=None) -> dict:
        return self.tools.query_metrics(start, end, store_id, product_id)

    def metrics_daily(self, start: str, end: str, store_id=None, product_id=None) -> dict:
        return self.tools.daily_metrics(start, end, store_id, product_id)

    def retrieve(
        self,
        query: str,
        top_k: int = 5,
        *,
        year: Optional[int] = None,
        as_of: Optional[date] = None,
        historical: bool = False,
    ) -> dict:
        """契约 §4：片段够就恰好给 top_k 条，不够才少给。

        `top_k` 大于索引里的片段总数时按总数封顶——这正是契约允许少给的那种情况。

        `year` / `as_of` / `historical` 是**版本上下文**，不是检索参数：
        契约 §4 要求"过滤元数据要在取 top_k 之前做"，而"哪一版现行"正是元数据过滤。
        `/api/retrieve` 端点不传它们（默认值 = 旧行为）；live 模式下由 `search_kb`
        把规划器算好的上下文带进来，见 `live.LiveEngine._tool_params`。
        """
        wanted = max(1, min(int(top_k or 5), len(self.index.chunks) or 1))
        result = self.retriever.search(
            query or "", top_k=wanted, year=year, as_of=as_of, historical=historical
        )
        return {"results": [hit.as_result() for hit in result.hits]}

    # -- 工具执行（live 模式下由模型驱动） ---------------------------------------

    def run_tool(self, name: str, params: dict) -> dict:
        if name not in TOOL_NAMES:
            return {"error": "没有这个工具：%s，可用工具：%s" % (name, "、".join(TOOL_NAMES))}
        schema = next(
            tool["function"]["parameters"] for tool in TOOLS if tool["function"]["name"] == name
        )
        cleaned: dict[str, Any] = {}
        for key, value in (params or {}).items():
            if key in _CONTEXT_KEYS:
                if value is not None:
                    cleaned[key] = value
                continue
            if key not in schema["properties"]:
                continue
            if key in _INT_PARAMS:
                try:
                    cleaned[key] = int(value)
                except (TypeError, ValueError):
                    return {"error": "参数 %s 应该是整数，收到 %r" % (key, value)}
                continue
            if value is None:
                continue
            text = str(value).strip()
            if key.startswith(("start", "end")) or key == "date":
                if not _ISO_DATE.match(text):
                    return {"error": "参数 %s 必须是 YYYY-MM-DD，收到 %r" % (key, value)}
            cleaned[key] = text
        for key in schema.get("required", []):
            if key not in cleaned:
                return {"error": "缺少必填参数 %s" % key}
        try:
            if name == "search_kb":
                return self.retrieve(
                    cleaned["query"],
                    cleaned.get("top_k", 5),
                    year=cleaned.get("year"),
                    as_of=cleaned.get("as_of"),
                    historical=bool(cleaned.get("historical")),
                )
            return getattr(self.tools, name)(**cleaned)
        except Exception as exc:  # noqa: BLE001 - 见下
            # 工具的异常必须在这里变成结构化的错误**还给模型**，不能往外抛。
            #
            # 契约 §3 要求 /api/chat 任何情况下都返回 200，所以异常不能穿透工具层；
            # 更关键的是模型**有能力自己改对**：它写 `SELECT ... FROM dim_product`
            # 是因为不知道真实的表叫什么，把 `no such table: dim_product` 原样告诉它，
            # 它下一轮就换成 `products` 了。之前这里只接 (TypeError, ValueError)，
            # SQL 执行本身的 sqlite3.OperationalError 会一路穿透到 service._answer
            # 的裸 except 里，整轮对话缩成一句「抱歉，我暂时无法回答。」——
            # 模型连自己错在哪都不知道，用户也拿不到本该拿到的数字。
            return {"error": "工具 %s 执行失败：%s：%s" % (name, type(exc).__name__, exc)}

    # -- 问答 -------------------------------------------------------------------

    def chat(self, session_id: Optional[str], question: str) -> dict:
        trace = Trace(
            trace_id=self.traces.new_id(self.settings.today.isoformat()),
            question=question or "",
            session_id=session_id,
        )
        answer = self._answer(trace, session_id, question or "")
        payload = {
            "answer": answer.answer,
            "answer_type": answer.answer_type,
            "citations": answer.citations,
            "data_evidence": answer.data_evidence,
            "trace_id": trace.trace_id,
        }
        trace.step("response", {"answer_type": answer.answer_type, "notes": answer.notes})
        self.traces.save(trace)
        return payload

    def _answer(self, trace: Trace, session_id: Optional[str], question: str) -> Answer:
        try:
            if not question.strip():
                return Answer(answer="没有收到问题内容，请再说一次。", answer_type="clarify")
            history = self.sessions.history(session_id)
            started = time.perf_counter()
            # history 必须传进规划器：追问还原（“那 7 月呢”）全靠它。
            # 之前这里漏传了，规划器永远以为没有上文，所有追问都被判成“缺少上下文”。
            plan = self.planner.plan(question, history)
            trace.step("plan", plan.as_trace(), started=started)
            answer = self._run_engine(plan, trace, history)
            self.sessions.append(
                session_id,
                {
                    "question": question,
                    "standalone": plan.standalone,
                    "slots": plan.slots,
                    "answer": answer.answer,
                    "answer_type": answer.answer_type,
                },
            )
            return answer
        except Exception as exc:  # noqa: BLE001 - 不管里面出什么事，接口都得给个像样的回答
            # 兜底可以吞掉异常，但不能吞掉**原因**：契约 §6 要求 trace 里
            # 看得到出现的错误，契约 §5 也要求把真实原因留在日志和 trace 里。
            # 之前这里什么都不记，于是线上只能看到一句「抱歉，我暂时无法回答。」，
            # 排查时既不知道是哪个工具、也不知道是报错还是超时。
            trace.error("unhandled", exc)
            trace.step("unhandled", {"type": type(exc).__name__, "message": str(exc)})
            return Answer(
                answer="抱歉，我暂时无法回答。",
                answer_type="refusal",
                notes=["未预期的错误：%s: %s" % (type(exc).__name__, exc)],
            )

    def _run_engine(self, plan, trace: Trace, history: list[dict]) -> Answer:
        # 判的是 `answer_engine` 而不是 `settings.live`：后者只是环境变量的快照，
        # 手动开关拨不动它。trace 里连**为什么**走这条路一起记下——
        # 否则一条 mock 的 trace 看起来永远像"没配 Key"。
        if self.answer_engine == "mock" or plan.intent == "refusal":
            started = time.perf_counter()
            answer = self.answerer.answer(plan, trace)
            reason = (
                "请求越界，不必调模型"
                if plan.intent == "refusal"
                else "手动切到降级模式"
                if self._engine_override == "mock"
                else "未配置 Key"
            )
            trace.step(
                "answer_mock",
                {"answer_type": answer.answer_type, "engine": "mock", "reason": reason},
                started=started,
            )
            return answer
        client = LLMClient(
            self.settings.llm_base_url,
            self.settings.llm_api_key,
            self.settings.llm_model,
            timeout=self.settings.llm_timeout,
        )
        engine = LiveEngine(
            client,
            self.answerer,
            self.run_tool,
            self.settings.today.isoformat(),
            self.data_period,
            budget=self.settings.chat_budget,
        )
        started = time.perf_counter()
        try:
            answer = engine.answer(plan, trace, history)
            trace.step("answer_live", {"answer_type": answer.answer_type}, started=started)
            return answer
        except LLMError as exc:
            trace.error("llm", exc)
            trace.step("answer_live_failed", {"kind": exc.kind, "detail": exc.detail}, started=started)
            return Answer(
                answer="模型服务这次没有正常返回（%s），为了不给出没有依据的数字，这个问题先不回答。"
                "可以稍后重试；失败的真实原因记在 trace 里。" % _reason_cn(exc),
                answer_type="refusal",
                notes=["live 模式失败：%s" % exc.detail],
            )

    # -- trace ------------------------------------------------------------------

    def get_trace(self, trace_id: str) -> Optional[dict]:
        return self.traces.get(trace_id)


def _reason_cn(exc: LLMError) -> str:
    mapping = {
        "timeout": "调用超时",
        "http_error": "接口返回错误码 %s" % (exc.status or ""),
        "empty_content": "返回了空回答",
        "length": "输出额度被思考耗尽",
        "content_filter": "被内容过滤拦截",
        "insufficient_system_resource": "服务端资源不足",
        "aborted": "请求被中止",
        "bad_tool_args": "工具参数无法解析",
        "bad_json": "返回的不是合法 JSON",
        "budget": "整体耗时接近时限",
        "transport": "网络异常",
        "tool_loop": "工具调用没有收敛",
    }
    return mapping.get(exc.kind, exc.kind)
