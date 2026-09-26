# astrbot_plugin_openviking_memory

中文 | [English](README_EN.md)

为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 提供 [OpenViking](https://github.com/volcengine/OpenViking) 长期记忆能力。

自动捕获群聊/私聊对话，在每次 LLM 请求前语义召回相关记忆。基于 OpenViking 的 **peer 记忆模型**：bot 是会话的「self」，每个人是一个「peer」，OV 会为每个说话的人单独建立画像，支持跨群跨会话的「记住每一个人」。

## 安装

1. 在 AstrBot WebUI → 插件市场搜索 **OpenViking Memory** 并安装；或从链接安装：`https://github.com/t0saki/astrbot_plugin_openviking_memory.git`
2. 填写插件配置（见下方）
3. 重载插件

### 前置条件

- AstrBot >= 4.23.1
- **OpenViking 服务端需为 peer-contract 版本（PR #2236 及之后）**，否则身份与召回行为不兼容
- 服务端以标准 `api_key` 模式运行：身份从 Bearer key 反解，插件不再发送 `X-OpenViking-*` 身份头（仅 `trusted_mode` 例外）
- 拥有 User API key（`global` 模式直接使用，推荐）或 Admin API key（`venue` 模式下自动创建 per-venue user）

## 功能概览

- **自动捕获**：每条用户消息和 bot 回复自动写入 OV session；群消息自动带 `[group:<群号> · 昵称(QQ)]` 前缀，便于区分来源
- **Peer 画像**：群成员的发言带上 `peer_id`（发送者），commit 时 OV 为每个人单独建立画像，存于 `viking://user/<bot>/peers/<sender_id>/`；bot 自己的回复与工具 I/O 归为 self
- **结构化工具调用**：工具调用与结果以独立 `tool` part 入库（带 `tool_name`/`tool_input`/`tool_status`），不拼进正文，服务端可分别处理
- **图片转写**：可把图片经视觉模型转成文字入库（见下「[图片转写](#图片转写)」）
- **自动召回**：每次 LLM 请求前，插件检索 self（bot/群上下文）+ 当前说话人 + 近期活跃成员的画像，作为临时内容块追加到当前用户消息**末尾**（不写入历史、不动 system prompt，避免击穿前缀缓存）
- **自动提交**：根据消息数、token 估算值或空闲超时自动 commit session，触发长期记忆提取
- **历史消化**：首次接入群聊时，自动拉取平台历史消息并入库（默认开启，可关闭）

### 与 AstrBot 内置知识库的关系

互补，而非替代：

- **内置知识库**：手动上传的稳态文档（手册、FAQ），仅 WebUI 管理，无 per-user 隔离
- **本插件**：对话流自动入库 + 语义召回 + 长期画像/偏好抽取，按 venue（群/私聊）隔离

## 配置

安装后在 AstrBot WebUI 中配置。

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `ov_base_url` | `http://localhost:1933` | OpenViking 服务端地址（需 peer-contract 版本） |
| `ov_user_api_key` | | User API key，`global` 模式下直接作为 bot self 的身份，**推荐** |
| `ov_admin_api_key` | | Admin API key，仅 `venue` 模式下用于创建 per-venue user |
| `ov_account_id` | | OV account ID（留空从 API key 自动解析，仅 `venue` 建用户时需要） |
| `self_scope` | `global` | 记忆归属（self）粒度，见下方说明 |
| `global_user_id` | `astrbot-global` | `global` 模式且只有 admin key 时，自动创建的 bot self 用户名 |
| `isolation_overrides` | `{}` | 按群号覆盖 `self_scope`，如 `{"123456": "venue"}` |
| `peer_enabled` | `true` | 给群成员消息打 `peer_id` 并在 commit 时启用 self+peer 提取（关闭则退化为只有 self 记忆） |
| `peer_recall_scope` | `speaker_plus_active` | 召回哪些 peer：`speaker`（self+当前说话人）/ `speaker_plus_active`（再加近期活跃成员）/ `none`（仅 self） |
| `peer_recall_active_window` | `5` | `speaker_plus_active` 下额外召回的近期活跃成员上限 |
| `trusted_mode` | `false` | 仅当 OV 以 `auth_mode=trusted`（受信网关后）运行时开启，会发送 `X-OpenViking-Account/User` 头 |
| `auto_recall_enabled` | `true` | 是否自动召回 |
| `recall_limit` | `8` | 最多召回条数（降级到 list/find 档时的上限） |
| `recall_min_score` | `0.35` | 语义匹配最低分 |
| `recall_token_budget` | `2000` | 注入上下文的 token 预算（context 档也用它作为服务端 `max_tokens`） |
| `recall_context_enabled` | `true` | 走服务端 context 组装（跨轮去重 + query 扩展 + 预算）。关闭则退回普通排序检索 |
| `recall_dedup_turns` | `3` | N 轮内已注入过的记忆不再重复注入（仅 context 档）；群聊可调小到 `1` |
| `recall_peer_scope` | `auto` | context 档可读哪些 peer：`auto`（`venue` 下为 `all`，`global` 下为 `actor`）/ `actor` / `all`。⚠️ `all` 会扫该 OV user 下所有 peer，仅当每群有独立 user（`venue`）时才安全 |
| `recall_query_expansion` | `true` | 允许服务端先扩展 query 再检索（对缩写、单词追问有效，增加延迟） |
| `recall_rewrite` | `false` | 要服务端生成重写摘要而非原始条目；明显更慢，开启时建议同步调大 `recall_timeout_ms` |
| `recall_timeout_ms` | `12000` | 召回请求超过此时长就放弃且不注入（毫秒） |
| `recall_include_active_peers` | `false` | context 档只能看到当前说话人（`peer_scope=actor`）时，额外用一次排序检索补上近期活跃成员的画像 |
| `commit_message_threshold` | `20` | 累积 N 条消息后自动 commit |
| `commit_token_threshold` | `4096` | 累积 token 超过此值后自动 commit |
| `commit_idle_seconds` | `1800` | 空闲 N 秒后自动 commit（也用作 peer 召回的「近期」时间窗） |
| `outbox_enabled` | `true` | OV 暂时收不下（限流、重启）时，把捕获写入落盘并在稍后按序重放，而不是丢消息 |
| `outbox_max_pending` | `200` | 每个 venue 的待重放上限，超出后丢最旧的 |
| `outbox_ttl_hours` | `24` | 待重放消息超过此时长就丢弃，避免长期故障把队列涨爆 |
| `outbox_flush_interval_seconds` | `60` | 后台重放循环的间隔 |
| `backfill_on_first_seen` | `true` | 首次接入群聊时拉取历史 |
| `backfill_max_messages` | `500` | 每群最多拉取历史条数 |
| `ingest_attachments` | `false` | 是否将图片/文件推送至 OV resources（需 VLM） |
| `capture_tool_io` | `true` | 以结构化 `tool` part 捕获工具调用输入/输出 |
| `capture_image_caption` | `true` | 抓取 AstrBot 对「发给 bot 的图」生成的转述文字（开 `caption_all_images` 时忽略） |
| `caption_all_images` | `false` | 主动转写**每一张**图（含群里互发、未 @bot 的）：每张图一次视觉模型调用，需配 provider |
| `image_caption_provider_id` | | 转写图片用的 provider id（留空 = 用 AstrBot 的 `default_image_caption_provider_id`） |
| `image_caption_prompt` | | 转写提示词（留空 = AstrBot 的 `image_caption_prompt` 或内置中文默认） |

> 旧版的 `isolation_mode`（`venue_user` / `venue_user_fanout` / `global_user`）仍可识别并自动映射到 `self_scope`（分别为 `venue` / `global` / `global`），同时打印 deprecation 日志。`venue_user_fanout` 已被 `global` + peer 取代。

## 隔离模式（self_scope）

记忆模型是 **一个 bot「self」 + 每个人一个「peer」**。`self_scope` 决定 self（记忆归属）的粒度，peer 始终按人区分：

| `self_scope` | self 映射 | peer 范围 | 行为 |
|------|-------------|------|------|
| `global`（默认） | 整个 bot 实例 = 1 个 self（OV user） | 跨群共享，每人一份画像 | bot 跨群「认识每一个人」；直接用 `ov_user_api_key`，无需 admin/建用户 |
| `venue` | 每群/私聊 = 1 个 self | 按群隔离 | 群间互相隔离，隐私优先；用 admin key 自动建 per-venue user |

### 跨人召回

所有 peer 都挂在同一个 bot self 空间下（`viking://user/<bot>/peers/*`）。默认召回 self + 当前说话人 + 近期活跃成员的画像；因此 A 在群里提问时，bot 也能召回最近活跃的 B、C 的画像（例如「Bob 喜欢什么」）。

召回的取值方式分两档：

- **context 档（默认）**：身份交给服务端解析，插件只传 `peer_scope`。`venue` 隔离下 OV user 就是本群，`all` = 本群所有 peer，正是想要的效果；`global` 下 user 跨群共享，`all` 会扫到别的群的 peer，所以**强制收窄为 `actor`**。若服务端拒绝 `peer_scope`，插件只会收窄到 `actor` 并告警，**绝不会放宽**。
- **降级档（list / find）**：服务端不提供身份解析，只能像以前那样**逐个显式点名** `target_uri`，点名范围由 `peer_recall_scope` 控制。

这也意味着 `peer_recall_scope` 只影响降级档；context 档看 `recall_peer_scope`。

> `global` 隔离下 `peer_scope` 强制为 `actor`，因此「A 问 Bob 喜欢什么」这类跨人召回会失失。要补回这个能力，把 `recall_include_active_peers` 设为 `true`：插件会在 context 结果之上再发一次**限定在活跃成员自身空间**的排序检索，URI 去重后合并进同一个块。代价是每轮多一次请求；`venue` 隔离下 `peer_scope=all` 已覆盖全部 peer，该项自动跳过。

## 写入可靠性（outbox）

捕获到的消息是插件**无法重建**的东西：唯一副本就是平台侧的场景，一旦丢就真的没了。因此所有捕获写入（文本、图片转写、工具 I/O、历史回填）统一经一个落盘队列：

- 写入失败时先落盘，再于后台按间隔重放；
- **严格保序**：某 venue 的头一条没送达时，后续消息不会绕过它，否则会话读起来是乱序的、抽取出的记忆也是错的；
- 只重试“稍后可能成功”的失败（超时 / 408 / 425 / 429 / 5xx / 连接错误）；4xx 这类确定性失败（如凭据错误）直接丢弃并唨 error，否则会永久堵住队列；
- 每个 venue 有上限、条目有 TTL，长时间故障不会把数据库涨爆；
- **队列不存凭据**：只存“说了什么”，Bearer 身份在重放时重新解析。

队列深度、已重放数、已丢弃数可在 `/ov_status` 的 `Outbox:` 行看到。

## 图片转写

把图片内容转成文字入库，两档：

- **`capture_image_caption`（默认开）**：复用 AstrBot 自带的图片转述。**仅覆盖发给 bot 且触发回复的图**，且要求 AstrBot 配了图片转述 provider、主模型非多模态（主模型多模态时 AstrBot 直接把图喂给它，不另生成文字）。
- **`caption_all_images`（默认关）**：插件主动对**每一张**图（含群里互发、没 @bot 的）调一次视觉模型转写，不依赖上面那些条件。代价是每张图一次 VLM 调用；需在 `image_caption_provider_id` 指定一个能看图的 provider（留空则用 AstrBot 的 `default_image_caption_provider_id`）。转写在后台进行，不阻塞消息处理。

入库格式为 `[group:<群号> · 昵称(QQ) · image] <转写文字>`，归到发送者的 peer 画像。回填的历史图不转写，仅清成 `[image]`。

## 推荐：添加 OV MCP 工具

强烈建议在 AstrBot WebUI → 插件 → MCP 页面添加 OpenViking MCP 服务，让 LLM 能主动调用 search、remember、read、list 等工具：

```json
{
  "transport": "streamable_http",
  "url": "http://localhost:1933/mcp",
  "headers": {
    "Authorization": "Bearer <你的 root_api_key>"
  },
  "timeout": 5,
  "sse_read_timeout": 300
}
```

将 `url` 和 `Authorization` 替换为实际的 OV 服务端地址和 API key。

> 由于 AstrBot 插件架构限制，插件无法自动注册 MCP 服务（需手动添加），也无法根据当前 venue 动态切换鉴权 header。因此 MCP 需要配置一个固定的 key：推荐使用 Root key，这样 LLM 能跨 venue 搜索所有用户的记忆；Admin key 只能看到 admin 自己的内容。代价是 LLM 可能搜到不相关 venue 的内容，需要通过 system prompt 引导其自行判断相关性。不添加 MCP 不影响插件的自动召回/捕获功能，但 LLM 将无法主动发起记忆搜索或写入。

## 命令

| 命令 | 权限 | 说明 |
|------|------|------|
| `/ov_status` | 所有人 | 查看插件连通性、pending 消息数、backfill 状态 |
| `/ov_backfill` | 管理员 | 强制重新执行当前群的历史消化 |

## License

MIT
