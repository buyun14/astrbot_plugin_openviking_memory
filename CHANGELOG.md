# Changelog

本文件记录社区维护分支相对上游 `t0saki/astrbot_plugin_openviking_memory` 的差异。

上游自 **2026-06-05** 起无代码提交；本分支在其基础上累计 **18 个提交**（`+3775 / -158`，19 个文件）。所有改动均**向后兼容**，可直接替换上游版本。

---

## v0.3.0 — 2026-09-27

### 新增：消息写入可靠性（Outbox）

`ov_client/outbox.py`（+329）

上游在写入失败时直接丢弃消息。本分支引入持久化 outbox：

- 写入失败时将消息落盘，按序重放，不丢不乱序
- 进程重启后可恢复未投递的消息
- 对应测试 `tests/test_outbox.py`（+513）

### 新增：召回分层降级（Context Face）

`ov_client/recall.py`（+408/-48）、`ov_client/recall_ledger.py`（+221）

- 改用 OpenViking **服务端 context face** 取召回结果，避免客户端自行拼装带来的口径不一致
- 服务端能力不可用时**分层降级**到原有检索路径，而不是整体失败
- 新增召回账本（`recall_ledger`）记录每次召回的来源与降级层级，便于排障
- 可选的 active-peer 补充检索（默认关闭）
- 对应测试 `tests/test_recall_context.py`（+738）

### 新增：召回内容注入到消息末尾

`ov_client/injection.py`（+73）

- 召回的上下文作为临时块追加到**当前用户消息末尾**
- **不写入对话历史、不改动 system prompt**，避免击穿 prefix cache（每轮对话历史重新计费）
- 对应测试 `tests/test_recall_injection.py`（+71）

> 注：上游 PR #1 的 review 中指出，**旧版 AstrBot** 上 `injection.py:39-43` 仍会回退到 `system_prompt` 追加路径。本分支沿用了该设计，若要彻底消除，需要在最低支持版本上验证 `after_message_sent` 的可用性。

### 安全：Admin Key 不再泄漏给租户 API

`ov_client/client.py`（+353/-33）、`ov_client/config.py`（+36/-2）

- Admin API key **绝不会**被发往 tenant 级 data API（`/api/v1/data/*`）
- 守卫覆盖**所有**来源：默认值、显式传参、venue 未解析时的回退路径
- venue 作用域下未解析的 venue **不会**继承 global key
- 仅在 `trusted_mode` 显式开启时例外
- `peer_scope=all` 仅在 venue 作用域下返回
- 对应测试 `tests/test_admin_key_guard.py`（+176）

### 可观测性

- `/ov_status` 区分「commit 请求被接受」与「记忆提取已完成」两个阶段，不再混为一谈
- **取消静默降级**：注入失败会以日志暴露，而非静默吞掉
- 恢复插件 logger（上游版本中丢失）
- 对应测试 `tests/test_commit_state.py`（+293）

### 健壮性修复

| 修复 | 说明 |
|---|---|
| session 计数 | 修复会话计数错误 |
| 关闭时 flush | 进程关闭前冲刷待写数据 |
| 响应体解析 | 对非 JSON / 空响应体做防御性解码，不再抛未捕获异常 |
| `search_list` 注解 | 允许 `None`，避免参数校验误拒 |

### 新增：测试体系

上游**无任何测试**。本分支新增 **1791 行**测试（5 个文件），覆盖 outbox 重放、context face 降级、注入位置、admin key 守卫、commit 状态机。CI 已接入（`.github/workflows/ci.yml`）。

---

## 关于上游

- 本分支为社区维护版本，上游仓库已停止维护
- 相对上游**无落后提交**（`main..upstream` 为空），可直接作为上游的超集使用
- 若上游恢复维护，欢迎讨论合并

[中文](README.md) | [English](README_EN.md)
