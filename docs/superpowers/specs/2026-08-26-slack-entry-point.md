# Slack 接入 — 实现规格

**日期：** 2026-08-26
**范围：** 把 Slack 做成第一入口：bot 进告警频道，@ 即用，读得到 thread 里的告警上下文，
也支持直接对话。
**不改的：** `app/agent/`、`app/services/`、`app/core/`、`app/tools/`、`app/api/` 全部保持原样。
Slack 是新增的适配层，不是对现有 Agent 的重写。

---

## 1. 要解决的问题

现有系统有 Web UI，用户在页面上选「对话」还是「AIOps 诊断」。但告警是发在 Slack 里的，
排查讨论也在 Slack 里。让人从事故频道切到另一个网页去查，多一次上下文切换，而事故当中
最缺的就是这个。

所以：**把 Agent 放进告警发生的地方。**

三个具体要求：

1. bot 能被 @ 出来
2. @ 的时候它能读到 thread 里的告警原文和已有讨论
3. 也能直接对话，不是只有排查一种用法

---

## 2. 核心设计问题：一个入口，两种 Agent

### 2.1 冲突在哪

网页版可以把对话和运维做成两个入口，因为**用户自己会选**。Slack 里用户只会 @ 一句话——
所以入口必须合并。

但合并入口不等于合并执行：

- 「错误码 43 什么意思」走 Plan-Execute-Replan = 四次模型调用（planner / executor /
  replanner / respond），在 Slack 里体感明显慢
- 「排查这条 CPU 告警」走一问一答的对话 Agent = 丢掉规划和重规划能力

**两种形状都要保留。** 问题变成：谁来决定走哪个。

### 2.2 不能是一个单纯的分类器

最直接的做法是让模型读消息判断意图。但那意味着一个可以静默答"这条不用排查"的东西站在
所有排查前面——判错了没人看得见。

### 2.3 也不能是纯结构判断

第二个想法：thread 里有告警就一律排查。这个太钝。

告警在跑，工程师 @ 一句「data-sync-service 是干嘛的」——按纯结构判断要跑完整排查流程去回答一个
定义问题。慢、吵、答非所问。

### 2.4 采用的方案：两道闸

```
@oncall-agent
   │
① has_alert_context(event, thread) -> bool          代码
   │   读 root 消息的 bot_id / app_id / 频道配置
   │   —— 不是「我认不认识这条告警」，是「这条 thread 挂在谁发的消息下面」
   │
   ├── False ────────────────────────────────────► chat
   │
   └── True ──► ② classify_intent(text) -> investigate | ask     小模型
                  │
                  ├── investigate ──────────────► triage
                  ├── 异常 / 无法解析 / 空 @ ───► triage（偏向）
                  └── ask ──────────────────────► chat + offer_triage
```

**为什么①归代码：** Slack 元数据已经有答案了。问模型"这是不是告警"，它可能看错；查
`bot_id` 不会。而且读来源不读内容意味着**一条从没被登记过的告警照样触发完整排查**——
覆盖面不再依赖关键词表是否更新。

**为什么②归模型：** "是不是又是冷启动？"和"data-sync-service 是干嘛的？"都打在同一个 thread 里，
只有语义能分开。没有结构性替代。

**为什么②只在①为真时才调用：** 没有告警的 thread 根本没有排查这个选项，不存在误判空间。
大多数轮次因此不用付这次调用的成本。

---

## 3. 让语义闸门安全的三条

### 3.1 偏向写死在代码里

两种误判代价不对称：

| 误判 | 后果 |
|---|---|
| 提问 → 排查 | 慢、吵。**什么都没丢**，用户拿到答案外加一堆没要的证据 |
| 排查 → 提问 | 工程师**以为**排查过了。事故中的静默少交付 |

所以 `app/slack/intent.py` 里，以下情况全部返回 `investigate`：

- 分类器抛异常（模型不可达、超时）
- 返回值不是 `investigate` / `ask` 之一
- 消息文本为空（告警 thread 里的裸 @ 本身就是求助信号）

这些是代码分支，不是 prompt 里的"建议优先 investigate"。

### 3.2 判错必须可见

`Decision.offer_triage` 为真时，回复末尾附加：

> _我把这句理解成提问而不是排查请求，所以没有查这条告警。说一声 investigate 我就去查。_

误判本身不可怕，**静默的误判**才可怕。一句话能纠正的判错是麻烦；看不见的判错才是问题。

### 3.3 判断留在执行层外面

路由在 `app/slack/router.py` 里完成，`aiops_service` 和 `rag_agent_service` 都不参与。

"这次不用排查"这个结论一旦能由执行层自己下，就没人看得见它下错了。

---

## 4. 窄问题不缩小取证范围

一条容易踩反的规则，写进 `intent.py` 的 system prompt：

**"是不是又是冷启动？"→ `investigate`，不是 `ask`。**

它确实是在问这条告警，只是框得很窄。如果因为这句话就只查冷启动，正好错过真正的原因——
提问的人本来就是在猜。

判据是**这句话是不是在问这条告警**，不是**问得宽不宽**：

| 消息 | 判定 | 理由 |
|---|---|---|
| 「find root cause」 | investigate | 明确 |
| （空） | investigate | 告警 thread 里的裸 @ 就是求助 |
| 「是不是又是冷启动」 | investigate | 在问这条告警，只是框窄了 |
| 「CPU 看着挺正常啊」 | investigate | 是对这条告警的判断，需要验证 |
| 「为什么会这样」 | investigate | 模糊 → 偏向 |
| 「data-sync-service 是干嘛的」 | ask | 不是在问这条告警 |
| 「错误码 43 什么意思」 | ask | 同上 |

---

## 5. 路由的完整规则

`app/slack/router.py`

### 5.1 `has_alert_context()` — 短路，按顺序

| # | 判据 | 依据 |
|---|---|---|
| 2 | root 的 `bot_id` 在 `slack_alert_bot_ids` 里 | **来源** |
| 3 | root 是 bot 消息且频道在 `slack_alert_channels` 里 | 来源 |
| 4 | 本 thread 之前有过 triage | 连续性 |
| 5 | `match_alert(root.text)` 命中 | 关键词表，最后手段 |
| 6 | 以上都不是 | 没有告警 |

规则 2–3 是主力。规则 5 只服务"工程师把告警文本手动粘进来"。

`match_alert` 在 `app/slack/alerts.py`，告警名从 `aiops-docs/*.md` 里的 `**告警名**:`
**自动提取**（`HighCPUUsage` / `SlowResponse` / `HighMemoryUsage` / `ServiceUnavailable` /
`HighDiskUsage`），避免知识库和代码里各写一份、然后慢慢对不上。另外识别 `[FIRING]` /
`[RESOLVED]` / `alertmanager` 这类标记。

### 5.2 `decide()` 的完整优先级

```
1. 显式操作（Block Kit 按钮 / slash command） → writeup | rating
2. has_alert_context 为假                      → chat
3. has_alert_context 为真 + intent=ask         → chat, offer_triage=True
4. has_alert_context 为真 + 本 thread 已 triage → followup
5. has_alert_context 为真                      → triage
```

### 5.3 删掉的：靠词触发副作用

原实现有 `_wants_writeback()`，对消息文本做子串匹配（`"record this"`、`"resolved"`）。

**删掉了。** 一句话里出现"resolved"不该能开 PR。写回和打分要走显式操作（Block Kit 按钮，
带 `invocation_id`）。

---

## 6. 只读 thread，不读频道

| API | 范围 | 用 |
|---|---|---|
| `conversations.replies(channel, ts)` | 单条 thread | ✅ 默认，上限 `slack_thread_limit`（50） |
| `conversations.history(channel)` | 整个频道 | ❌ 不在默认路径 |

理由不只是省 token：事故频道同时可能有五条无关告警，全拉进来会稀释证据、制造"到底在问哪条"
的歧义。而且 §12 的原则是**被召唤才行动**，自作主张扩大范围是反过来的。

### 6.1 消息分类

`app/slack/thread.py`：

| 发送方 | 特征 |
|---|---|
| Incoming Webhook 告警 | `subtype="bot_message"` + `bot_id` + `username`，**没有 `user`** |
| Bot token 告警 | `bot_id` + `bot_profile` + `app_id`，无 `bot_message` subtype |
| 人类 | 有 `user`，无 `bot_id` |
| 我们自己 | `bot_id` 等于自己的 |

**`bot_id`（`B...`）和 `bot_user_id`（`U...`）是两个不同的 ID。** 流传很广的一个参考实现拿
这两个互相比较，所以它那段"跳到自己上一条消息为止"的循环其实是死代码。这里两个都在启动时
从 `auth.test` 解析、分开存。

### 6.2 顶层 @ 不是 thread

`thread_ts` 只在 thread 内的回复上存在。常见写法：

```python
thread_ts = event.get("thread_ts") or event["ts"]      # 寻址用，对
```

用来**寻址**没问题，但它会把"频道里的顶层 @"伪装成"一条只有自己的 thread"——而那条 thread 的
root 就是这条 @ 本身（一条人类消息），于是结构判断得出"没有告警"。结论是对的，但这个区别
被掩盖了。`in_thread(event)` 显式报告它。

顶层 @ 的两个例外：

- **告警文本直接贴在 @ 里** → 规则 5 命中，走排查，不读历史
- **顶层 @ 且像在指涉刚才那条告警**（限配置过的告警频道）→ 给一个**提议**：
  "这个频道 5 分钟前有条 HighCPUUsage，是这条吗？[查这条]"

  三个条件同时成立才触发，且只读最近的 bot 消息，产出的是提议不是假设。**尚未实现。**

---

## 7. 进程模型

```
┌── oncall-slackd ────────┐   ┌── oncall-api (uvicorn) ──────┐
│ AsyncApp                │   │ FastAPI: /api/chat /api/aiops │
│ AsyncSocketModeHandler  │   │ /api/upload /api/health       │
│ ack ≤3s → asyncio.Task  │   │ 静态 Web UI                   │
└─────────────────────────┘   └───────────────────────────────┘
            │                              │
            └──── 都调 ────────────────────┘
                     │
        aiops_service / rag_agent_service
        （各自持有 MemorySaver，按 thread_id 存会话）
                     │
        MCP servers (8003 cls / 8004 monitor) · Milvus 19530
```

**为什么两个进程：** bolt-python 维护者明确不建议 web app 和 WebSocket 客户端共用一个
event loop。社区的 FastAPI lifespan 变通方案在 `uvicorn --workers > 1` 下直接坏掉——每个
worker 开一条自己的 WebSocket，每个 Slack 事件被处理 N 次。

顺带好处：部署 API 不会断掉 Slack 连接。

**为什么 Socket Mode：** agent 的工具要连 Milvus、MCP server 和内网监控，进程本来就得待在
网内。Socket Mode 往外拨，不需要公网入口。

### 7.1 Socket Mode 的运维特性

- Slack 每几小时回收一次连接，约 10 秒预告（`refresh_requested`）；单 app 上限 10 条并发连接。
  建议 **≥2 副本**，靠 §9 的幂等让重复投递无害。
- **一小时内投递失败率超过 95% 会停用整个 app 的事件订阅**，需要手动去后台重启。所以 handler
  里的异常必须捕获并报进 thread，不能让它逃出去把容器打进崩溃循环。

---

## 8. 3 秒 ack

Socket Mode 和 Events API 一样有 3 秒死线。而一次排查要 60–120 秒。

```python
@app.event("app_mention")
async def on_mention(event, ack, client):
    await ack()                                   # 永远第一件事
    asyncio.create_task(run_slack_turn(...))      # 60-120s，离开 socket
```

ack 之前不碰 Milvus、不碰 MCP、不碰模型。

---

## 9. 一次 @ 一次排查

Slack 会重投未 ack 的事件，重连时也会重投；跑多副本时重复投递是常态而非异常。

`app/slack/dedupe.py`：`event_id` 作幂等键，`InMemoryDedupe` 和（可选）持久实现。

**认领要在这一轮被持久记录之后进行。** 反过来——先认领、再丢进内存队列——进程一重启，
认领记录还在，而那条本该重试的重投被它挡住了，等于永久丢失。

同一 thread 的并发 @ 由 `runtime.busy` 串行化，抢不到的那次回一句"还在处理上一个问题"，
而不是让两轮互相覆盖。

---

## 10. 进度

一个事故频道盯着 bot 沉默 90 秒会认为它挂了。但 `chat.update` 是 Tier 3（每方法每 workspace
50+/min），Slack 另外建议每频道约每秒一次。

`app/slack/progress.py`：

- 第一条**立刻发**——工程师需要看到它开始了
- 之后最快每 `slack_progress_interval`（1.5s）一次
- 最后一条**无条件发**
- 429 时按 `Retry-After` 拉长间隔，而不是丢更新

`chat.startStream` / `appendStream` 是 Tier 4（100+/min）且接受真 Markdown，实现在
`slack_use_streaming` 后面，**默认关**：官方文档说 thread 范围的 streaming 会返回
`invalid_thread_ts`，SDK 自己的示例又在传 `thread_ts`，两者矛盾，而值班 bot 就活在 thread 里。
代码在这个错误上回退到 `chat.update`，所以打开开关去真 workspace 试是安全的。

进度内容来自 `app/slack/dispatch.py` 对两个 service 事件流的规整。

---

## 11. dispatch：把两种 Agent 收敛成一条事件流

`app/slack/dispatch.py` 是唯一知道"有两种 Agent"的地方：

```python
async def run_turn(turn, *, question, alert_text, thread_id) -> AsyncIterator[dict]:
    if turn == "chat":
        # rag_agent_service.query_stream  → content / tool_call / error
    else:
        # aiops_service.execute           → plan / step_complete / report / complete
```

两边事件形状不同，统一规整成 `{type: progress|complete|error, message?, response?}`，
handlers 只认这一种。

**排查任务的构造**（`TRIAGE_TASK`）把告警原文和工程师的问题拼成一个任务描述，并写明要求：
先取证再下结论、空结果不等于健康、要区分故障源头和受害者、每条结论要指出依据。

对话侧也会 `yield progress`——对话 Agent 同样会调工具，不说的话等待期间看起来就是 bot 卡住了。

---

## 12. 被召唤，不主动推送

**不做**"每条告警自动排查并回帖"。

@ 本身就是信号：**有人读了告警，判断需要帮助**。对每条告警都不请自来地发一堆分析，会训练
人习惯性划过——这个习惯一旦形成很难逆转。

如果以后要做自动触发，应该限定在有实测记录的特定告警上，而不是全局打开。

---

## 13. 实现清单

| 文件 | 职责 |
|---|---|
| `app/slack/run.py` | `oncall-slackd` 入口，AsyncApp + AsyncSocketModeHandler |
| `app/slack/handlers.py` | `app_mention` / `message.im`，先 ack 再后台跑 |
| `app/slack/router.py` | `has_alert_context` / `in_thread` / `decide` / `route` |
| `app/slack/intent.py` | 意图分类，拿不准一律 investigate |
| `app/slack/dispatch.py` | 两个 service → 一条事件流 |
| `app/slack/thread.py` | `fetch_thread` / 消息分类 / `alert_message` / `thread_digest` |
| `app/slack/progress.py` | 进度合并写入 |
| `app/slack/alerts.py` | 从 `aiops-docs` 提取告警名 |
| `app/slack/dedupe.py` | `event_id` 幂等 |
| `app/slack/mrkdwn.py` | Markdown → Slack mrkdwn |

对现有代码的改动仅两处，都是修 bug 不是改架构：

- `app/config.py` — 新增 8 个 `slack_*` 配置项 + `get_settings()`
- `vector_embedding_service.py` / `vector_store_manager.py` — 模块级单例改延迟构造

后者是真实缺陷：构造 embedding 客户端和连 Milvus 都发生在 import 期，导致没有 API key、
没起 Milvus 时**整个进程连 import 都过不去**——包括根本不做检索的 Slack bot 和测试。

---

## 14. 测试

`tests/test_slack.py`，47 项，全部不需要网络。守住的是路由这一层的判断，因为那是最容易
悄悄退化的部分：

| 测试 | 守住什么 |
|---|---|
| `test_unknown_alert_from_configured_bot_still_routes_to_triage` | 没登记过的告警照样排查 |
| `test_a_narrow_hypothesis_is_still_an_investigation` | 窄问题不缩小取证 |
| `test_that_chat_answer_discloses_and_offers_to_triage` | 判错必须可见 |
| `test_an_unavailable_model_investigates` | 分类器挂了偏向排查 |
| `test_a_bare_mention_investigates_without_a_model` | 裸 @ 就是求助 |
| `test_intent_is_not_consulted_without_an_alert` | 没告警不调模型 |
| `test_a_top_level_mention_has_no_alert_context` | 顶层 @ 不读频道历史 |
| `test_message_text_never_triggers_a_writeback` | 词不能触发副作用 |
| `test_one_mention_is_one_investigation` | 重投不会跑两次 |
| `test_no_text_can_downgrade_an_alert_thread` | 措辞不能降级告警 thread |

---

## 15. 尚未完成

1. **顶层 @ 的告警提议**（§6.2）——当前顶层 @ 一律走对话
2. **写回和打分的 Block Kit 按钮**——路由里 `writeup` / `rating` 的分支在，操作还没接
3. **持久化去重**——当前是内存实现，多副本需要共享存储
4. **`chat.startStream` 实测**——开关在，默认关，要在真 workspace 里验
5. **`agents.sessions.setStatus`**——Slack 新的 AI app 状态指示（旧的 `assistant.threads.*`
   2027 年 2 月下线）。状态指示有两分钟超时，长排查需要周期性重发
