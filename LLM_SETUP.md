# LLM 接入说明

## 1. 用了什么

| 项 | 值 |
|---|---|
| 协议 | **OpenAI 兼容 Chat Completions**（`POST {LLM_BASE_URL}/chat/completions`） |
| 厂商 / 模型 | DeepSeek 官方 API，模型 `deepseek-flash` |
| SDK | **没有用 SDK**，直接用 `httpx` 发 HTTP。见下面"为什么不用 SDK" |
| 依赖版本 | `httpx`（见 `starter/requirements.txt`），无额外依赖、无模型文件下载 |
| 思考模式 | **保持开启**（不传 `thinking` 参数）。理由见第 8 节 |

**为什么不用 SDK。** 契约 §7.2 要求"地址原样拼接：不要自己补 `/v1`，不要截掉路径"。SDK 会在内部拼 `base_url`，一旦评审让我们走一个带路径前缀的代理（§7.5），排查"到底发到哪个地址去了"就要翻 SDK 源码。直接 `httpx.post(self.endpoint, ...)` 的话，`endpoint` 就是 `"{LLM_BASE_URL}/chat/completions"` 一个字面量，预检里 P1 那项（请求是否原样发到 `LLM_BASE_URL`）是肉眼可核对的。另外少一层依赖，评审在断网环境里 `pip install` 也少一个包。

## 2. 配置从哪里读

全部从**环境变量**读，读取位置 `starter/kbqa/config.py` 的 `load_settings()`。

| 变量 | 含义 | 默认值 | 备注 |
|---|---|---|---|
| `LLM_BASE_URL` | 模型服务地址 | 空 | 原样使用，**不补 `/v1`、不截路径**，只去掉末尾的 `/` |
| `LLM_API_KEY` | 模型 Key | 空 | 只用于 `Authorization: Bearer` 请求头 |
| `LLM_MODEL` | 模型名 | 空 | 原样放进请求体的 `model` 字段 |
| `LLM_TIMEOUT` | 单次调用超时（秒） | `120` | 契约 §7.3 的下限 |
| `CHAT_BUDGET` | `/api/chat` 整体预算（秒） | `150` | 契约 §7.3 的 180 秒上限留了余量 |
| `DATA_DIR` / `KB_DIR` / `VAR_DIR` | 数据、知识库、产物目录 | 见 `config.py` | 换数据/知识库用 |
| `TODAY` | 系统的"今天" | `2026-09-01` | 契约固定值，留个口子只为测试 |

**三个变量必须同时有值才算"配好了"**（`Settings.live`）：缺任何一个都进降级模式。这是故意的——只给了 `LLM_BASE_URL` 却没有 Key 的情况，与其发一个注定 401 的请求，不如直接降级。

**启动时不做任何校验**：不检查 Key 的格式、不查余额、不列模型（契约 §7.2 明确要求）。配置只在真正调用时被用到。

## 3. 怎么换成你们的

改三个环境变量，**不需要改代码，也不需要重建索引**：

```bash
export LLM_BASE_URL=https://api.deepseek.com
export LLM_MODEL=deepseek-flash
export LLM_API_KEY=<你们的 Key>
```

然后**重启服务**（配置在进程启动时读一次）。重建命令**不用跑**——`kbqa.rebuild` 只处理数据清洗与文档索引，跟模型配置无关。

```bash
# 重启（Linux / macOS / Git Bash）
cd starter && LLM_BASE_URL=... LLM_MODEL=... LLM_API_KEY=... \
  ../.venv/Scripts/python -m uvicorn kbqa.server:app --host 127.0.0.1 --port 8000

# 或者用一键脚本
LLM_BASE_URL=... LLM_MODEL=... LLM_API_KEY=... bash start.sh
```

换完之后 `/api/health` 的 `llm_mode` 会从 `mock` 变成 `live`，这是确认切换成功的唯一信号。

**走代理也一样。** 如果评审让服务经过 `eval/llm_gateway.py proxy`，把 `LLM_BASE_URL` 设成代理打印的地址即可（含路径前缀），代码不会做任何改写。

## 4. 怎么看到发给模型的请求

两条路，任选。

**（a）走代理——推荐，评审用的就是这条。** 它把每一次请求和响应原样记成 JSONL：

```bash
python3 eval/llm_gateway.py proxy --upstream https://api.deepseek.com --log llm_traffic.jsonl
# 它打印出代理地址（形如 http://127.0.0.1:PORT/ds-gw），拿它当 LLM_BASE_URL 重启服务
```

**（b）看 trace——不依赖任何外部工具。** 每一次模型调用都被记进该次问答的 trace 里，
`GET /api/trace/{trace_id}` 或页面上的"调试面板"页签都能看到：

```json
{
  "kind": "llm",
  "model": "preflight-model-7f3a",
  "messages": 3,
  "tools": 6,
  "prompt": "[{\"role\": \"system\", \"content\": \"你是一家连锁餐饮公司的经营分析助手...",
  "status": 200,
  "took_ms": 2314.5,
  "has_reasoning": true,
  "finish": "tool_calls"
}
```

`prompt` 字段是**发给模型的完整 messages 的 JSON 原文**（封顶 4000 字符，超出部分标记截断），
包含系统提示词、工具定义轮次和每一轮的工具结果。这是契约 §7.2 "可观察"那一条的落地方式：
不接代理也能看清楚提示词长什么样。

**思考内容不会漏进回答。** `reasoning_content` 单独记在 trace 的 `raw_reasoning` 字段里（同样是封顶预览），
`answer` 只取 `message.content`。多轮工具调用时 assistant 消息是**整条**追加回 `messages` 的
（含 `reasoning_content`），不自己挑字段重组——契约 §7.3 明确说重组会 400。

**Key 只记长度，不记内容。** 日志与 trace 里都不会出现 Key 本身。

## 5. 没有 Key 时会怎样

服务**照常启动**，`/api/health` 报 `"llm_mode": "mock"`。四个接口：

| 接口 | 没有 Key 时的行为 |
|---|---|
| `GET /api/health` | 200，`llm_mode: "mock"`，其余字段与 live 模式一致 |
| `GET /api/metrics/summary`、`/api/metrics/daily` | 200，正常返回——这两个接口本来就不经过模型 |
| `POST /api/retrieve` | 200，正常返回 |
| `POST /api/chat` | 200。走**纯代码的抽取式作答**：数字题按口径现算、文档题检索原文摘句、混合题两边都做；覆盖不到的问题返回 `answer_type: "refusal"` 的说明 |

**没有做成"没 Key 就一律拒答"。** 降级路径是一条真的能答题的路径：它不需要模型，
所以同样的输入给同样的输出，公开题库在降级模式下能拿到满分（见 `EVAL_REPORT.md`）。
这样评审在干净环境里第一次跑（不配 Key）看到的是像样的回答，而不是一屏"暂不支持"。

live 模式下模型调用失败（超时、错误码、空回答……）时，也退回这条路径或返回结构化的 `refusal`，
`/api/chat` 在任何情况下都返回 HTTP 200（契约 §3）。

## 6. 依赖与安装

- **额外依赖：无。** `httpx` 本来就在 `requirements.txt` 里（FastAPI 的测试客户端也要它）。
- **模型文件：不下载。** 没有本地模型、没有向量权重。
- **首次启动耗时**：装依赖之外，`python -m kbqa.rebuild` 在本机约 **0.2 秒**
  （清洗 18628 行 + 建 139 个片段的索引）。仓库里已经带了重建产物，
  所以**直接起服务连这 0.2 秒都不用**。

## 7. 自测结果

先起一个只提供 `POST {前缀}/chat/completions` 的假模型服务，再用它打印的三个环境变量**重启**
本服务（端口 8000），然后由预检驱动 `/api/chat` 把 16 个场景各跑两遍。下面是完整输出，原文照贴：

```
==============================================================================
预检假模型已启动：http://127.0.0.1:54011/ds-gw
它只提供 POST http://127.0.0.1:54011/ds-gw/chat/completions，其余任何路径都会返回 404 并被记下来。

请用下面三个环境变量重启你的服务（模型名是故意取的怪名字，写死模型名会被查出来）：

  export LLM_BASE_URL=http://127.0.0.1:54011/ds-gw
  export LLM_API_KEY=preflight-key-3b9c1f
  export LLM_MODEL=preflight-model-7f3a

或者直接在启动命令前加上：
  LLM_BASE_URL=http://127.0.0.1:54011/ds-gw LLM_API_KEY=preflight-key-3b9c1f LLM_MODEL=preflight-model-7f3a <你的启动命令>
==============================================================================

开始检查 http://127.0.0.1:8000 ……
  [normal] 你们的退款规则是怎么规定的？ → HTTP 200，0.14 秒
  [normal] 最近一段时间的整体经营情况怎么样？ → HTTP 200，0.11 秒
  [thinking_starved] 你们的退款规则是怎么规定的？ → HTTP 200，0.11 秒
  [thinking_starved] 最近一段时间的整体经营情况怎么样？ → HTTP 200，0.09 秒
  [empty_content] 你们的退款规则是怎么规定的？ → HTTP 200，0.58 秒
  [empty_content] 最近一段时间的整体经营情况怎么样？ → HTTP 200，0.58 秒
  [json_empty] 你们的退款规则是怎么规定的？ → HTTP 200，0.12 秒
  [json_empty] 最近一段时间的整体经营情况怎么样？ → HTTP 200，0.11 秒
  [bad_tool_args] 你们的退款规则是怎么规定的？ → HTTP 200，0.08 秒
  [bad_tool_args] 最近一段时间的整体经营情况怎么样？ → HTTP 200，0.06 秒
  [content_filter] 你们的退款规则是怎么规定的？ → HTTP 200，0.06 秒
  [content_filter] 最近一段时间的整体经营情况怎么样？ → HTTP 200，0.06 秒
  [insufficient_resource] 你们的退款规则是怎么规定的？ → HTTP 200，0.58 秒
  [insufficient_resource] 最近一段时间的整体经营情况怎么样？ → HTTP 200，0.56 秒
  [aborted] 你们的退款规则是怎么规定的？ → HTTP 200，0.05 秒
  [aborted] 最近一段时间的整体经营情况怎么样？ → HTTP 200，0.05 秒
  [http_401] 你们的退款规则是怎么规定的？ → HTTP 200，0.05 秒
  [http_401] 最近一段时间的整体经营情况怎么样？ → HTTP 200，0.05 秒
  [http_402] 你们的退款规则是怎么规定的？ → HTTP 200，0.05 秒
  [http_402] 最近一段时间的整体经营情况怎么样？ → HTTP 200，0.03 秒
  [http_422] 你们的退款规则是怎么规定的？ → HTTP 200，0.05 秒
  [http_422] 最近一段时间的整体经营情况怎么样？ → HTTP 200，0.05 秒
  [http_429] 你们的退款规则是怎么规定的？ → HTTP 200，0.58 秒
  [http_429] 最近一段时间的整体经营情况怎么样？ → HTTP 200，0.58 秒
  [http_500] 你们的退款规则是怎么规定的？ → HTTP 200，0.58 秒
  [http_500] 最近一段时间的整体经营情况怎么样？ → HTTP 200，0.58 秒
  [http_503] 你们的退款规则是怎么规定的？ → HTTP 200，0.58 秒
  [http_503] 最近一段时间的整体经营情况怎么样？ → HTTP 200，0.58 秒
  [slow] 你们的退款规则是怎么规定的？ → HTTP 200，18.14 秒
  [slow] 最近一段时间的整体经营情况怎么样？ → HTTP 200，18.19 秒
  [hang] 你们的退款规则是怎么规定的？ → HTTP 200，120.06 秒
  [hang] 最近一段时间的整体经营情况怎么样？ → HTTP 200，120.05 秒

编号  检查项                                                            结果  说明
----------------------------------------------------------------------------------
P1    服务确实把请求发到了注入的 LLM_BASE_URL（含路径前缀）             通过  共观察到 60 次 POST /ds-gw/chat/completions。
P2    请求里的 model 等于注入的 LLM_MODEL                               通过  全部请求都用了 preflight-model-7f3a。
P3    注入的 Key 以 Authorization: Bearer 发送                          通过  全部请求都带了正确的 Bearer Key。
P4    只用了 DeepSeek 文档列出的顶层参数                                通过  只出现了 DeepSeek 文档列出的顶层参数。
P5    max_tokens 不设，或不小于 2048                                    通过  max_tokens 都不小于 2048。
P6    没有访问 {prefix}/chat/completions 之外的任何路径                 通过  只访问了 POST /ds-gw/chat/completions，没有碰任何别的路径。
P7    工具定义规范，且每一个工具调用都以 role=tool + tool_call_id 回传  通过  工具定义规范，44 个工具调用的结果都正确回传了。
P8    每个场景下 /api/chat 都返回 HTTP 200 与字段完整的合法 JSON        通过  32 次问答全部返回 200 和字段完整的 JSON。
P9    模型不可用时给出结构化 refusal，answer 从不是空串                 通过  模型不可用的场景下都给了结构化 refusal 或有据可查的回答，answer 从不是空串。
P10   思考内容没有漏进 answer / citations / data_evidence               通过  32 次回答里，思考标记都没有出现在任何对外字段里。
P11   /api/chat 在时限内返回（含长时间无响应的场景）                    通过  最慢的一次是 120.06 秒，都在 180 秒以内。
P12   注入环境变量后 /api/health 报告 llm_mode = live                   通过  llm_mode = live。
P13   多轮工具调用之间 reasoning_content 原样回传（没有触发 400）       通过  18 次多轮请求都原样回传了 reasoning_content。
P14   保持连接的空行与 SSE 注释没有把服务弄坏                           通过  正文前的空行和 SSE 的 `: keep-alive` 注释都被正确跳过了，slow 场景照常给出回答。

预检通过：在 OpenAI 兼容这条路线上，我们能原样接上你的服务。
报告已写入 run_preflight\preflight_report.md 与 run_preflight\preflight_report.json。
请把报告贴进 LLM_SETUP.md 第 7 节（自测结果）。
```

几点说明：

- **P1～P14 全部通过，退出码 0。** 最慢的一次 120.06 秒（`hang` 场景：假服务收下连接一直不回），
  在 180 秒的预算内——这是单次模型调用 120 秒超时在起作用。
- 输出里的 `preflight-key-3b9c1f` 是**预检自己生成的假 Key**，不是任何真实服务的凭据；
  `preflight-model-7f3a` 同理，是故意取的怪名字，用来查写死模型名的实现。两者都不入仓库。
- 这份输出是在**不设置任何真实 Key** 的机器上跑出来的：只注入了上面那三个环境变量。
- 跑法（`--port` 固定是为了能先把服务起好，`--no-wait` 是给脚本用的）：

  ```bash
  export LLM_BASE_URL=http://127.0.0.1:54011/ds-gw
  export LLM_API_KEY=preflight-key-3b9c1f
  export LLM_MODEL=preflight-model-7f3a
  bash start.sh
  python3 eval/llm_gateway.py preflight     --service-url http://127.0.0.1:8000 --port 54011 --no-wait --out run_preflight
  ```


## 8. 已知限制

1. **思考模式保持开启，代价是慢。** 契约 §7.3 允许传 `thinking: {"type": "disabled"}`。我选择不关：
   这道题的多轮工具调用场景下，开着思考规划更稳（模型更少编表名、更少在"还差一点"的时候反复查）。
   代价是单题耗时明显变长——公开题库里慢的题要 15～25 秒，关掉思考能快不少，
   而 180 秒的预算够用。**没有做过关掉思考的对照实验**，这是个基于零散观察的选择，不是量化的结论。
2. **`temperature` 在思考模式下不生效，所以 live 模式的得分有抖动。** 同一份代码重跑，
   公开题库的总分在 94～100 之间浮动（见 `EVAL_REPORT.md` 的多次运行记录）。
   抖动来自模型表达（多说了去年 618 的价、罗列了 21 个数字触发上限、少引用了一份文档），
   不是代码不稳定——降级模式同样的输入 100% 一致。**没有对题目做针对性调参**；
   我只把契约 §5 本来就写明的上限（1200 字 / 20 个不同数字 / 4 份文档）搬进了系统提示词。
3. **工具轮次上限是 4 轮**（`live.py: MAX_TOOL_ROUNDS`）。极端复杂的多跳问题可能 4 轮不够，
   用完之后会**摘掉工具**让模型用手上的结果作答，而不是报错。这个上限是拍的，没有实验依据。
4. **混合题的"原因"部分依赖检索能否命中。** 知识库里确实没有对应说明时，
   回答会明确说"没有找到能解释这件事的文档，只能确认数字本身"——这是刻意的，
   宁可少答也不编原因。但反过来说，**如果一个问题的原因散落在三份文档里，模型可能只找到其中一份**。
5. **没有做流式输出。** 契约 §7.3 提到流式（`delta.reasoning_content` 先到），
   这一版 `/api/chat` 是同步返回的，前端没有"思考中"的分步状态，只有一个等待态。
   合同没有硬性要求流式，但这是最明显的体验缺口。
6. **`history` 只回带最近 3 轮**，更早的上下文会被丢掉。对公开题库的多轮题够用，
   连续追问十轮以上可能丢失早期约束。
7. **降级路径的"达标"类问题不校验目标的生效范围。** 追问"618 当天……达到目标了吗？"之后接着问
   "那 7 月呢？"，门店/商品/指标都接得上，但目标值仍然取了 618 那一份（KB-023），
   于是出现"7 月整月 303 份 vs 只针对 6 月 18 日当天的 120 份"这种没有意义的比较。
   抽取式作答只按"文档里带『目标 N 份』的句子"去找，没有校验这份文档的生效区间是否盖住提问区间。
   想挡的话，"区间有交集"挡不住（KB-023 的元数据里只有 `effective_from`、没有 `effective_to`），
   "区间必须包含"又会误伤跨月有效的目标值，两者都判断不了，所以**没有拍一个规则，而是写在这里**。
   完整走查见 [`DEMO.md`](DEMO.md) 第 5 节。
