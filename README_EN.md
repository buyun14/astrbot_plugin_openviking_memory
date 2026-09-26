# astrbot_plugin_openviking_memory

[中文](README.md) | English

[OpenViking](https://github.com/volcengine/OpenViking) long-term memory integration for [AstrBot](https://github.com/AstrBotDevs/AstrBot).

Auto-captures conversations and performs semantic recall on every LLM request. Built on OpenViking's **peer memory model**: the bot is the session "self" and every person is a "peer", so OV builds a separate profile for each person the bot talks to — the bot "remembers everyone" across groups and sessions.

## How it works

- **Auto-capture**: Every user message and bot reply is written to an OpenViking session. Group messages are prefixed with `[group:<id> · name(qq)]` so the source is preserved.
- **Peer profiles**: Incoming group messages carry a `peer_id` (the sender); on commit OV builds a per-person profile under `viking://user/<bot>/peers/<sender_id>/`. The bot's own replies and tool I/O stay "self".
- **Structured tool calls**: Tool calls and results are recorded as standalone `tool` parts (`tool_name`/`tool_input`/`tool_status`), not folded into text, so the server can process them separately.
- **Image transcription**: Optionally transcribe images to text via a vision provider (see [Image transcription](#image-transcription)).
- **Auto-recall**: Before each LLM request, the plugin recalls self (bot/group context) + the current speaker + recently-active members and appends them as a provider-only content part at the **tail** of the current user message (never the system prompt, which would invalidate the provider's prefix cache for the whole history).
- **Auto-commit**: Sessions are committed (archived + memory extracted) based on message count, token threshold, or idle timeout.
- **Backfill**: On first encounter with a group, historical messages are pulled from the platform and ingested into OV.

## Relationship with AstrBot's built-in Knowledge Base

They are complementary:

- **Built-in KB**: Manually uploaded documents (manuals, FAQs). Admin-managed via WebUI. No per-user isolation.
- **This plugin**: Automatic conversation capture + semantic recall + per-person profile extraction (self + peer).

## Installation

1. In AstrBot WebUI, search **OpenViking Memory** in the Plugin Marketplace and install; or install from URL: `https://github.com/t0saki/astrbot_plugin_openviking_memory.git`
2. Fill in the plugin configuration (see below).
3. Reload the plugin.

## Configuration

All fields are configured via AstrBot WebUI after installation.

| Field | Default | Description |
|-------|---------|-------------|
| `ov_base_url` | `http://localhost:1933` | OpenViking server URL (must be a peer-contract build) |
| `ov_user_api_key` | | User API key, used directly as the bot self identity in `global` mode (**recommended**) |
| `ov_admin_api_key` | | Admin API key, only used in `venue` mode to mint per-venue users |
| `ov_account_id` | | OV account ID (auto-parsed from API key if empty; only needed when minting in `venue` mode) |
| `self_scope` | `global` | Memory-owner (self) granularity, see below |
| `global_user_id` | `astrbot-global` | Name of the minted bot-self user in `global` mode when only an admin key is available |
| `isolation_overrides` | `{}` | Per-group `self_scope` overrides `{"group_id": "venue"}` |
| `peer_enabled` | `true` | Tag incoming messages with `peer_id` and commit with self+peer policy (off = self-only memory) |
| `peer_recall_scope` | `speaker_plus_active` | Which peers to recall: `speaker` / `speaker_plus_active` / `none` |
| `peer_recall_active_window` | `5` | Max recently-active members also recalled in `speaker_plus_active` |
| `trusted_mode` | `false` | Send `X-OpenViking-Account/User` headers (only for OV servers in `auth_mode=trusted` behind a gateway) |
| `auto_recall_enabled` | `true` | Auto-recall on every LLM request |
| `recall_limit` | `8` | Max recalled entries |
| `recall_min_score` | `0.35` | Minimum semantic score |
| `recall_token_budget` | `2000` | Max tokens for injected context (also sent as the context-mode `max_tokens` budget) |
| `recall_context_enabled` | `true` | Use server-side context assembly (cross-turn dedup + query expansion + budgeting). Turn off to fall back to plain ranked search |
| `recall_dedup_turns` | `3` | Do not re-inject a memory served within this many turns (context mode only); use `1` in busy group chats |
| `recall_peer_scope` | `auto` | Which peers the context face may read: `auto` (`all` under `venue`, `actor` under `global`) / `actor` / `all`. ⚠️ `all` scans every peer of the OV user, so it is only safe when each venue has its own user |
| `recall_query_expansion` | `true` | Let the server expand the query before searching (helps with abbreviations; adds latency) |
| `recall_rewrite` | `false` | Ask for a rewritten digest instead of raw entries; noticeably slower, so raise `recall_timeout_ms` if enabled |
| `recall_timeout_ms` | `12000` | Give up on a recall request after this long and inject nothing (ms) |
| `recall_include_active_peers` | `false` | When the context face can only see the current speaker (`peer_scope=actor`), add one ranked search over the recently-active members' own spaces |
| `commit_message_threshold` | `20` | Auto-commit after N messages |
| `commit_token_threshold` | `4096` | Auto-commit when tokens exceed this |
| `commit_idle_seconds` | `1800` | Auto-commit after N seconds idle (also the "recent" window for peer recall) |
| `outbox_enabled` | `true` | Persist capture writes OpenViking could not accept (rate limits, restarts) and replay them in order instead of losing the messages |
| `outbox_max_pending` | `200` | Per-venue cap on queued messages; the oldest are dropped once exceeded |
| `outbox_ttl_hours` | `24` | Discard queued messages older than this, so a long outage cannot grow the queue forever |
| `outbox_flush_interval_seconds` | `60` | How often the background drainer retries queued messages |
| `backfill_on_first_seen` | `true` | Pull history on first group encounter |
| `backfill_max_messages` | `500` | Max messages to backfill |
| `ingest_attachments` | `false` | Push images/files to OV resources |
| `capture_tool_io` | `true` | Record tool inputs/outputs as structured `tool` parts |
| `capture_image_caption` | `true` | Capture AstrBot's caption for bot-directed images (ignored when `caption_all_images` is on) |
| `caption_all_images` | `false` | Actively transcribe **every** image (incl. ambient/non-@bot): one vision-model call per image, needs a provider |
| `image_caption_provider_id` | | Provider id for transcription (empty = AstrBot's `default_image_caption_provider_id`) |
| `image_caption_prompt` | | Transcription prompt (empty = AstrBot's `image_caption_prompt` or a built-in default) |

> Legacy `isolation_mode` values (`venue_user` / `venue_user_fanout` / `global_user`) are still recognized and auto-mapped onto `self_scope` (`venue` / `global` / `global`) with a deprecation log. `venue_user_fanout` is superseded by `global` + peer.

## Isolation modes (self_scope)

The model is **one bot "self" + one "peer" per person**. `self_scope` controls the self (memory-owner) granularity; peers are always keyed per person.

| `self_scope` | self mapping | peer scope | Behavior |
|------|--------------|------------|----------|
| `global` (default) | Entire bot = 1 self (OV user) | Shared across venues, one profile per person | The bot "knows everyone" across groups; uses `ov_user_api_key` directly, no admin/user minting |
| `venue` | Each group/DM = 1 self | Isolated per venue | Groups isolated from each other (privacy-first); mints per-venue users via the admin key |

### Cross-person recall

All peers live under the same bot-self space (`viking://user/<bot>/peers/*`). Recall by default pulls self + the current speaker + recently-active members, so when A asks something the bot can also recall B's and C's profiles (e.g. "what does Bob like?").

How the peer set is selected depends on the tier in use:

- **Context tier (default)**: the server resolves identity from the caller, so the plugin only passes `peer_scope`. Under `venue` scope the OV user *is* the group and `all` means "the people in this group"; under `global` the user is shared across groups, where `all` would reach other groups' peers, so it is forced down to `actor`. If the server rejects `peer_scope`, the plugin only ever narrows to `actor` (with a warning) — it never widens.
- **Degraded tier (list / find)**: no server-side identity resolution, so peers must still be named explicitly as `target_uri`, with `peer_recall_scope` controlling the range.

In other words `peer_recall_scope` only affects the degraded tier; the context tier reads `recall_peer_scope`.

> Under `global` scope `peer_scope` is forced down to `actor`, which costs cross-person recall ("what does Bob like?" asked by A). Set `recall_include_active_peers` to `true` to get it back: the plugin then runs one extra ranked search **scoped to the active members' own spaces** and merges it into the same block after URI dedup. The cost is one extra request per turn; under `venue` scope, where `peer_scope=all` already covers every peer, the supplement is skipped automatically.

## Write durability (outbox)

Captured messages are the one thing this plugin cannot re-derive: the bot's transcript is the only copy, so a lost message is lost for good. Every capture write (text, image transcripts, tool I/O, history backfill) therefore goes through a persisted queue:

- a failed write is stored and retried by a background drainer;
- **order is preserved**: while a venue's head message is undelivered, later ones do not overtake it — otherwise the session reads out of order and the extracted memory is wrong;
- only failures that can succeed later are retried (timeouts, 408/425/429/5xx, connection errors). Deterministic 4xx failures are dropped with an error log, since retrying them would block the queue forever;
- each venue's queue is bounded and entries expire, so a long outage cannot grow the database without limit;
- **credentials are never stored** — the queue holds only what was said, and the Bearer identity is resolved again at replay time.

Queue depth, replayed count and dropped count are shown on the `Outbox:` line of `/ov_status`.

## Observability (`/ov_status`)

Beyond the basics, `/ov_status` reports the things that used to be a black box:

| Line | Meaning |
|------|---------|
| `Last commit: … [state, task=…]` | **Commit is two-phase**: archiving (Phase 1) finishes before the call returns, memory extraction (Phase 2) keeps running in the background. States: `archived` (no task), `extracting` (accepted, still running), `extracted`, `extract_failed` (with reason), `extract_unknown` (task expired/gone), `commit_failed` (the request itself failed and pending work was kept for the next try). "Committed" therefore no longer implies "retrievable" |
| `Recall: …, peer_scope=…, dedup ring=…` | Active recall tier (context / degraded / reason it is unavailable), effective peer scope, and the size of this session's dedup ring |
| `Outbox: N pending …` | Queued capture writes, replayed count, dropped count |
| `Context inject: tail (fallbacks=N)` | Where the recall block went. `tail` means a content part, which keeps the prefix cache intact; a switch to `system_prompt` means the fallback fired and cache hits will drop, with the running total in `fallbacks` |

> Commit states and task polling live in memory and start empty after a restart (the messages themselves are safe — they are in the OV session).

## Image transcription

Two ways to turn image content into text memory:

- **`capture_image_caption` (default on)**: reuse AstrBot's own image captioning. **Only covers images sent to the bot that trigger a reply**, and only when AstrBot has an image-caption provider configured and the main model is *not* multimodal (a multimodal main model gets the image directly, so no text caption is generated).
- **`caption_all_images` (default off)**: the plugin actively calls a vision provider once per image — including ambient group images nobody @-mentioned the bot with — independent of the above conditions. Costs one VLM call per image; set `image_caption_provider_id` to a vision-capable provider (empty = AstrBot's `default_image_caption_provider_id`). Runs in the background, off the message path.

Stored as `[group:<id> · name(qq) · image] <transcription>`, attributed to the sender's peer profile. Backfilled historical images are not transcribed — just normalized to `[image]`.

## Recommended: Adding OV MCP tools

We strongly recommend adding the OpenViking MCP server in AstrBot WebUI → Plugins → MCP, so the LLM can proactively use tools like search, remember, read, and list:

```json
{
  "transport": "streamable_http",
  "url": "http://localhost:1933/mcp",
  "headers": {
    "Authorization": "Bearer <your_root_api_key>"
  },
  "timeout": 5,
  "sse_read_timeout": 300
}
```

Replace `url` and `Authorization` with your actual OV server address and API key.

> Due to AstrBot plugin architecture limitations, the plugin cannot register MCP servers automatically (manual setup required) and cannot dynamically switch auth headers based on the current venue. A fixed key must be configured: Root key is recommended so the LLM can search across all venue users' memories; Admin keys can only see the admin's own content. The trade-off is that the LLM may retrieve content from unrelated venues — guide it via system prompt to judge relevance. Without MCP, the plugin's auto-recall/capture still works, but the LLM won't be able to proactively search or write memories.

## Commands

| Command | Permission | Description |
|---------|-----------|-------------|
| `/ov_status` | Anyone | Show plugin connectivity, pending messages, backfill status |
| `/ov_backfill` | Admin | Force re-run backfill for the current venue |

## Requirements

- AstrBot >= 4.23.1 (for tool I/O capture hooks; core features work on >= 4.9.2)
- **OpenViking server on the peer contract (PR #2236 or later)**, running in standard `api_key` mode (identity is derived from the Bearer key; the plugin no longer sends `X-OpenViking-*` identity headers unless `trusted_mode` is set)
- A User API key (`global` mode, recommended) or Admin API key (`venue` mode, mints per-venue users)

## License

MIT
