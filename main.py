"""
AstrBot OpenViking Memory Plugin — main entry point.

Star subclass that registers hooks for auto-capture, auto-recall, commit
scheduling, and backfill.

Memory model (peer contract, OpenViking PR #2236+): the bot is the session
"self" (an OV user); each person is a "peer" keyed by sender_id. Incoming
messages carry peer_id; the bot's own replies and tool I/O stay self. Commit
sends memory_policy {self, peer} so OV builds a per-person profile under
viking://user/<bot>/peers/<sender_id>/.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType, PermissionType
from astrbot.api.star import Context, Star

from .ov_client.backfill import BackfillManager
from .ov_client.client import OVClient
from .ov_client.commit_scheduler import CommitScheduler
from .ov_client.config import PluginConfig, data_api_key, user_api_key
from .ov_client.identity import (
    derive_ov_user_id,
    derive_session_id,
    derive_venue,
    get_effective_self_scope,
    safe_peer_id,
    venue_is_group,
)
from .ov_client.injection import inject_recall_block, injection_fallback_count
from .ov_client.outbox import Outbox
from .ov_client.parts import (
    assistant_text_part,
    build_message,
    estimate_tokens,
    file_placeholder_part,
    image_caption_part,
    image_placeholder_part,
    parse_image_captions,
    tool_call_part,
    tool_result_part,
    user_text_part,
)
from .ov_client.presence import PresenceTracker
from .ov_client.recall import recall_and_format
from .ov_client.recall_ledger import RecallLedger


class OpenVikingMemoryPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        # ``Star.__init__`` already installs this plugin's dedicated logger
        # (``astrbot.plugin.<plugin_name>``); re-pointing it at the global
        # "astrbot" logger would defeat the per-plugin log level in the WebUI.
        raw_config = dict(config) if config else {}
        self.cfg = PluginConfig(raw_config)

        account_id = self.cfg.ov_account_id
        # The client-wide Bearer is the *user* key. An admin/root key is refused
        # by tenant data APIs ("ROOT API keys cannot access tenant-scoped data
        # APIs"), so shipping it here turned every capture write into a 403 that
        # the outbox dropped. The admin key stays a parameter of the admin API
        # (``_mint_user_key`` → ``OVClient.create_user``). Account resolution
        # still prefers the admin key, which is the one that carries it.
        effective_key = data_api_key(self.cfg)
        if not account_id:
            account_id = _parse_account_from_key(
                self.cfg.ov_admin_api_key or effective_key
            )

        self.ov = OVClient(
            base_url=self.cfg.ov_base_url,
            api_key=effective_key,
            account_id=account_id,
            trusted_mode=self.cfg.trusted_mode,
        )
        import hashlib

        url_hash = hashlib.md5(self.cfg.ov_base_url.encode()).hexdigest()[:8]
        self._kv_prefix = f"ov_{url_hash}_"

        self.scheduler = CommitScheduler(self.ov, self.cfg)
        self.presence = PresenceTracker(
            window=self.cfg.peer_recall_active_window,
            ttl_seconds=float(self.cfg.commit_idle_seconds),
        )
        # venue_id -> (api_key, fallback_user_id). fallback_user_id is only used
        # for X-OpenViking-User assertion in trusted_mode.
        self._venue_auth: dict[str, tuple[str, str]] = {}
        self.outbox = Outbox(
            kv_get=self._kv_get,
            kv_put=self._kv_put,
            client=self.ov,
            prefix=self._kv_prefix,
            auth_resolver=self._auth,
            on_delivered=self._on_outbox_delivered,
            max_pending=self.cfg.outbox_max_pending,
            ttl_seconds=self.cfg.outbox_ttl_hours * 3600,
        )
        self._drainer: asyncio.Task | None = None
        self.backfill = BackfillManager(
            self.ov,
            self.cfg,
            kv_get=self._kv_get,
            kv_put=self._kv_put,
            kv_prefix=self._kv_prefix,
            outbox=self.outbox,
        )
        self.recall_ledger = RecallLedger(
            kv_get=self._kv_get,
            kv_put=self._kv_put,
            prefix=self._kv_prefix,
        )

    async def _kv_get(self, key: str, default: Any = None) -> Any:
        return await self.get_kv_data(key, default)

    async def _kv_put(self, key: str, value: Any) -> None:
        await self.put_kv_data(key, value)

    # -- auth helpers ---------------------------------------------------------

    async def _mint_user_key(self, user_id: str, cache_key: str) -> str:
        """Mint (or load cached) an OV user key via the admin API. '' on failure."""
        cached = await self._kv_get(cache_key)
        if cached:
            return cached
        if not self.cfg.ov_admin_api_key:
            return ""
        self.logger.info("[OV] creating user %s (account=%s)", user_id, self.ov.account_id)
        result, err = await self.ov.create_user(user_id, self.cfg.ov_admin_api_key)
        if result and "user_key" in result:
            key = result["user_key"]
            await self._kv_put(cache_key, key)
            self.logger.info("[OV] created user %s OK", user_id)
            return key
        self.logger.warning("[OV] create_user %s failed: %s", user_id, err)
        return ""

    async def _ensure_self_auth(self, venue_id: str, group_id: str, ov_user_id: str):
        """Resolve the Bearer identity (the bot self) for a venue.

        global scope → one bot self for the whole instance (user key, or a single
        minted global user). venue scope → one minted self per venue.
        """
        if venue_id in self._venue_auth:
            return

        scope = get_effective_self_scope(self.cfg, group_id)

        if scope == "global":
            user_key = user_api_key(self.cfg)
            if user_key:
                self._venue_auth[venue_id] = (user_key, "")
                return
            cache_key = f"{self._kv_prefix}gkey::{self.cfg.global_user_id}"
            key = await self._mint_user_key(self.cfg.global_user_id, cache_key)
            self._venue_auth[venue_id] = self._auth_or_fallback(key, self.cfg.global_user_id)
            return

        # venue scope: mint a per-venue self.
        key = await self._mint_user_key(ov_user_id, f"{self._kv_prefix}key::{venue_id}")
        self._venue_auth[venue_id] = self._auth_or_fallback(key, ov_user_id)

    def _auth_or_fallback(self, key: str, user_id: str) -> tuple[str, str]:
        if key:
            return (key, "")
        if self.cfg.trusted_mode:
            # Gateway asserts identity via X-OpenViking-User using the root/admin key.
            return ("", user_id)
        self.logger.warning(
            "[OV] no user key for %s (mint failed, not trusted_mode) — memory disabled",
            user_id,
        )
        return ("", "")

    def _auth(self, venue_id: str) -> dict[str, str | None]:
        """Request auth for a venue.

        A venue whose key could not be resolved must not inherit the client-wide
        key by accident: in api_key mode that is the *global* user key, so the
        write would land under the wrong self instead of failing. Resolving it
        here keeps the decision visible — and lets the client tell "unset"
        (``None``, use the default) apart from "no Bearer" (``""``).
        """
        api_key, user_id = self._venue_auth.get(venue_id, ("", ""))
        if not api_key and self.cfg.trusted_mode:
            # Trusted mode authenticates with the client-wide key (the admin key
            # when no user key is set) and asserts identity via the headers.
            return {"api_key": None, "user_id": user_id or None}
        return {"api_key": api_key or "", "user_id": user_id or None}

    def _extract_event_info(self, event: AstrMessageEvent) -> dict:
        platform = getattr(event, "get_platform_name", lambda: "unknown")()
        group_id = getattr(event, "get_group_id", lambda: "")() or ""
        sender_id = getattr(event, "get_sender_id", lambda: "")() or ""
        sender_name = getattr(event, "get_sender_name", lambda: "")() or ""
        text = getattr(event, "message_str", "") or ""
        return {
            "platform": str(platform),
            "group_id": str(group_id),
            "sender_id": str(sender_id),
            "sender_name": str(sender_name),
            "text": str(text),
        }

    def _peer_id_for(self, sender_id: str) -> str | None:
        return safe_peer_id(sender_id) if self.cfg.peer_enabled else None

    # -- capture writes -------------------------------------------------------

    async def _capture(
        self,
        venue_id: str,
        session_id: str,
        message: dict[str, Any],
        *,
        peer_id: str | None = None,
    ) -> bool:
        """Write one capture message, queueing it when OV cannot take it now.

        Returns:
            True when the message is in OV; False when it is queued for replay
            or was rejected outright.
        """
        self._ensure_drainer()
        if not self.cfg.outbox_enabled:
            return await self.ov.add_message(
                session_id, message, peer_id=peer_id, **self._auth(venue_id)
            )
        return await self.outbox.send(venue_id, session_id, message, peer_id=peer_id)

    async def _capture_message(
        self,
        venue_id: str,
        session_id: str,
        message: dict[str, Any],
        *,
        text: str,
        peer_id: str | None = None,
    ) -> bool:
        """Capture a message and, only if it reached OV, count it toward a commit.

        Delivery and accounting are kept in one place on purpose: a queued or
        rejected write must not advance the commit counters, or the session would
        be committed while it is still missing those messages. Replayed writes are
        counted once they land, in ``_on_outbox_delivered``.
        """
        ok = await self._capture(venue_id, session_id, message, peer_id=peer_id)
        if ok:
            self.scheduler.set_auth(session_id, self._auth(venue_id))
            await self.scheduler.record_message(session_id, estimate_tokens(text))
        return ok

    async def _on_outbox_delivered(
        self, venue_id: str, session_id: str, message: dict[str, Any]
    ) -> None:
        """Count a replayed write, so a queue that drained still commits."""
        self.scheduler.set_auth(session_id, self._auth(venue_id))
        await self.scheduler.record_message(session_id, estimate_tokens(_message_text(message)))

    def _ensure_drainer(self) -> None:
        """Start the background loop if it is not running yet.

        Called from the capture path, from ``on_astrbot_loaded`` and from the LLM
        request hook. The last one matters because ``on_astrbot_loaded`` does not
        fire again on a hot reload, and because the loop also polls commit tasks —
        which is needed whether or not the outbox is enabled.
        """
        if self._drainer is not None and not self._drainer.done():
            return
        self._drainer = asyncio.create_task(self._drain_loop())

    async def _drain_loop(self) -> None:
        interval = max(5, int(self.cfg.outbox_flush_interval_seconds))
        while True:
            await asyncio.sleep(interval)
            try:
                # Always polled: a commit only reports that archiving finished, so
                # extraction is observable only through the task API. That has
                # nothing to do with whether the outbox is switched on.
                await self.scheduler.poll_commit_tasks()
                if self.cfg.outbox_enabled:
                    await self.outbox.flush_all()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("[OV] background drain failed")

    # -- hook: on_astrbot_loaded ----------------------------------------------

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        await self.outbox.load()
        self._ensure_drainer()
        ok = await self.ov.health()
        if ok:
            self.logger.info(
                "[OV] server reachable at %s (account=%s)",
                self.cfg.ov_base_url,
                self.ov.account_id or "(not set)",
            )
        else:
            self.logger.warning("[OV] server NOT reachable at %s", self.cfg.ov_base_url)

    # -- hook: capture user messages ------------------------------------------

    @filter.event_message_type(EventMessageType.ALL)
    async def on_user_message(self, event: AstrMessageEvent):
        info = self._extract_event_info(event)
        # Don't capture our own commands as memory, and don't let them double-fire
        # backfill alongside the command handler.
        if _is_self_command(info["text"]):
            return

        venue_id = derive_venue(info["platform"], info["group_id"], info["sender_id"])
        if self.cfg.is_bypassed(venue_id):
            return

        msg_chain = getattr(event, "message_obj", None)
        images = (
            self._collect_images(msg_chain) if (self.cfg.caption_all_images and msg_chain) else []
        )
        has_text = bool(info["text"].strip())
        if not has_text and not images:
            return

        ov_user_id = derive_ov_user_id(
            self.cfg, info["platform"], info["group_id"], info["sender_id"]
        )
        await self._ensure_self_auth(venue_id, info["group_id"], ov_user_id)
        auth = self._auth(venue_id)
        session_id = derive_session_id(venue_id)
        is_group = venue_is_group(venue_id)
        peer_id = self._peer_id_for(info["sender_id"])
        self.presence.record(venue_id, peer_id)

        if has_text:
            parts = [
                user_text_part(
                    info["text"],
                    info["sender_name"],
                    info["sender_id"],
                    is_group,
                    group_id=info["group_id"],
                )
            ]
            if msg_chain:
                self._append_media_placeholders(msg_chain, parts)
            await self._capture_message(
                venue_id,
                session_id,
                build_message("user", parts),
                text=info["text"],
                peer_id=peer_id,
            )

        # Actively transcribe every image (not just bot-directed ones) in the
        # background so the VLM call doesn't block message handling.
        if images:
            asyncio.create_task(
                self._caption_images(images, venue_id, session_id, peer_id, info, is_group)
            )

        await self.backfill.maybe_trigger(
            venue_id,
            info["platform"],
            info["group_id"],
            auth,
            event=event,
        )

    def _collect_images(self, msg_chain: Any) -> list:
        chain = getattr(msg_chain, "message", None) or []
        return [c for c in chain if type(c).__name__ == "Image"]

    def _astrbot_provider_setting(self, key: str, default: str = "") -> str:
        try:
            cfg = self.context.get_config()
            return (cfg.get("provider_settings", {}) or {}).get(key, default)
        except Exception:
            return default

    def _image_caption_provider(self):
        pid = self.cfg.image_caption_provider_id or self._astrbot_provider_setting(
            "default_image_caption_provider_id", ""
        )
        if not pid:
            self.logger.warning("[OV] caption_all_images on but no image caption provider set")
            return None
        try:
            return self.context.get_provider_by_id(pid)
        except Exception:
            self.logger.warning("[OV] image caption provider %s not found", pid)
            return None

    def _image_caption_prompt(self) -> str:
        return (
            self.cfg.image_caption_prompt
            or self._astrbot_provider_setting("image_caption_prompt", "")
            or "请用中文描述这张图片，尽量包含其中的文字内容。"
        )

    async def _caption_images(self, images, venue_id, session_id, peer_id, info, is_group):
        provider = self._image_caption_provider()
        if provider is None:
            return
        # Runs as a background task, i.e. possibly after the hook that spawned it
        # returned: it resolves its own auth instead of relying on that ordering.
        # Resolved after the early return so captions-off installs never pay for
        # a mint round-trip.
        ov_user_id = derive_ov_user_id(
            self.cfg, info["platform"], info["group_id"], info["sender_id"]
        )
        await self._ensure_self_auth(venue_id, info["group_id"], ov_user_id)
        prompt = self._image_caption_prompt()
        for comp in images:
            try:
                path = await comp.convert_to_file_path()
                resp = await provider.text_chat(prompt=prompt, image_urls=[path])
                cap = (getattr(resp, "completion_text", "") or "").strip()
            except Exception:
                self.logger.exception("[OV] image caption failed")
                continue
            if not cap:
                continue
            part = image_caption_part(
                cap, info["sender_name"], info["sender_id"], is_group, info["group_id"]
            )
            await self._capture_message(
                venue_id, session_id, build_message("user", [part]), text=cap, peer_id=peer_id
            )

    def _append_media_placeholders(self, msg_chain: Any, parts: list):
        chain = getattr(msg_chain, "message", None) or []
        for comp in chain:
            comp_type = type(comp).__name__
            if comp_type == "Image":
                url = getattr(comp, "url", "") or getattr(comp, "file", "") or ""
                if url:
                    parts.append(image_placeholder_part(url))
            elif comp_type == "File":
                name = getattr(comp, "name", "") or getattr(comp, "file", "") or ""
                if name:
                    parts.append(file_placeholder_part(name))

    # -- hook: recall on LLM request ------------------------------------------

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: Any):
        info = self._extract_event_info(event)
        venue_id = derive_venue(info["platform"], info["group_id"], info["sender_id"])
        if self.cfg.is_bypassed(venue_id):
            return

        ov_user_id = derive_ov_user_id(
            self.cfg, info["platform"], info["group_id"], info["sender_id"]
        )
        await self._ensure_self_auth(venue_id, info["group_id"], ov_user_id)
        auth = self._auth(venue_id)
        session_id = derive_session_id(venue_id)
        peer_id = self._peer_id_for(info["sender_id"])
        # Every turn is a chance to (re)start the background loop: on_astrbot_loaded
        # does not fire on a hot reload, and the loop also polls commit tasks, so it
        # must not depend on a capture write happening first.
        self._ensure_drainer()

        # AstrBot turns images into a <image_caption>…</image_caption> text part on
        # the request (req built before this hook fires). Image-only messages have
        # empty message_str, so on_user_message skipped them — capture the caption
        # here as the image's textual content.
        # When caption_all_images is on, on_user_message already transcribes every
        # image (including this one) — don't also capture AstrBot's caption here.
        if self.cfg.capture_image_caption and not self.cfg.caption_all_images:
            is_group = venue_is_group(venue_id)
            for cap in _extract_image_captions(req):
                part = image_caption_part(
                    cap, info["sender_name"], info["sender_id"], is_group, info["group_id"]
                )
                await self._capture_message(
                    venue_id,
                    session_id,
                    build_message("user", [part]),
                    text=cap,
                    peer_id=peer_id,
                )

        if not self.cfg.auto_recall_enabled or not info["text"].strip():
            return

        active = self.presence.active(venue_id, exclude=peer_id)
        await self.recall_ledger.load()
        block = await recall_and_format(
            self.ov,
            self.cfg,
            info["text"],
            venue_id,
            ov_user_id,
            speaker_id=info["sender_id"],
            active_member_ids=active,
            session_id=session_id,
            self_scope=get_effective_self_scope(self.cfg, info["group_id"]),
            ledger=self.recall_ledger,
            **auth,
        )
        if block:
            inject_recall_block(req, block)

    # -- hook: capture LLM response -------------------------------------------

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: Any):
        info = self._extract_event_info(event)
        venue_id = derive_venue(info["platform"], info["group_id"], info["sender_id"])
        if self.cfg.is_bypassed(venue_id):
            return

        reply_text = ""
        if hasattr(resp, "completion_text"):
            reply_text = resp.completion_text or ""
        elif hasattr(resp, "text"):
            reply_text = resp.text or ""
        elif hasattr(resp, "result_chain"):
            chain = resp.result_chain or []
            reply_text = " ".join(getattr(c, "text", str(c)) for c in chain if hasattr(c, "text"))

        if not reply_text.strip():
            return

        # This hook captures, so it resolves the venue's Bearer itself:
        # bot-initiated turns (cron pushes, command replies, spontaneous
        # messages) reach it for venues no earlier hook has primed, and an
        # unprimed venue used to fall back to the client-wide key.
        ov_user_id = derive_ov_user_id(
            self.cfg, info["platform"], info["group_id"], info["sender_id"]
        )
        await self._ensure_self_auth(venue_id, info["group_id"], ov_user_id)

        # The bot's own reply is the session owner (self) — no peer_id.
        session_id = derive_session_id(venue_id)
        parts = [assistant_text_part(reply_text)]
        payload = build_message("assistant", parts)
        await self._capture_message(venue_id, session_id, payload, text=reply_text)

    # -- hook: tool I/O capture -----------------------------------------------

    @filter.on_using_llm_tool()
    async def on_tool_call(self, event: AstrMessageEvent, *args, **kwargs):
        # AstrBot signature: (event, tool: FunctionTool, tool_args: dict | None)
        if not self.cfg.capture_tool_io:
            return
        info = self._extract_event_info(event)
        venue_id = derive_venue(info["platform"], info["group_id"], info["sender_id"])
        if self.cfg.is_bypassed(venue_id):
            return
        tool = kwargs.get("tool", args[0] if args else None)
        tool_args = kwargs.get("tool_args", args[1] if len(args) > 1 else None)
        t_name = _tool_name(tool)
        if not t_name:
            return
        ov_user_id = derive_ov_user_id(
            self.cfg, info["platform"], info["group_id"], info["sender_id"]
        )
        await self._ensure_self_auth(venue_id, info["group_id"], ov_user_id)
        session_id = derive_session_id(venue_id)
        payload = build_message("assistant", [tool_call_part(t_name, tool_args)])
        # Tool traffic is part of the session: it has to be counted too, or a
        # tool-heavy turn never reaches the commit thresholds.
        await self._capture_message(venue_id, session_id, payload, text=f"{t_name} {tool_args}")

    @filter.on_llm_tool_respond()
    async def on_tool_respond(self, event: AstrMessageEvent, *args, **kwargs):
        # AstrBot signature: (event, tool, tool_args, tool_result: CallToolResult | None)
        if not self.cfg.capture_tool_io:
            return
        info = self._extract_event_info(event)
        venue_id = derive_venue(info["platform"], info["group_id"], info["sender_id"])
        if self.cfg.is_bypassed(venue_id):
            return
        tool = kwargs.get("tool", args[0] if args else None)
        tool_result = kwargs.get("tool_result", args[2] if len(args) > 2 else None)
        t_name = _tool_name(tool)
        if not t_name:
            return
        ov_user_id = derive_ov_user_id(
            self.cfg, info["platform"], info["group_id"], info["sender_id"]
        )
        await self._ensure_self_auth(venue_id, info["group_id"], ov_user_id)
        session_id = derive_session_id(venue_id)
        result_text = _tool_result_text(tool_result)
        payload = build_message("assistant", [tool_result_part(t_name, result_text)])
        # Counted for the same reason as the tool-call part above.
        await self._capture_message(venue_id, session_id, payload, text=result_text)

    # -- hook: after message sent → commit eval -------------------------------

    @filter.after_message_sent()
    async def after_sent(self, event: AstrMessageEvent):
        info = self._extract_event_info(event)
        venue_id = derive_venue(info["platform"], info["group_id"], info["sender_id"])
        session_id = derive_session_id(venue_id)
        await self.scheduler.evaluate(session_id)

    # -- commands -------------------------------------------------------------

    @filter.command("ov_status", alias={"ov-status"})
    async def cmd_status(self, event: AstrMessageEvent):
        info = self._extract_event_info(event)
        venue_id = derive_venue(info["platform"], info["group_id"], info["sender_id"])
        session_id = derive_session_id(venue_id)

        healthy = await self.ov.health()
        sched = self.scheduler.get_status(session_id)
        bf_status = await self.backfill.get_status(venue_id)
        scope = get_effective_self_scope(self.cfg, info["group_id"])

        ov_user_id = derive_ov_user_id(
            self.cfg, info["platform"], info["group_id"], info["sender_id"]
        )
        api_key, fallback_uid = self._venue_auth.get(venue_id, ("", ""))
        if api_key and scope == "global" and self.cfg.ov_user_api_key:
            key_status = "user key (global self)"
        elif api_key:
            key_status = "minted user key"
        elif fallback_uid:
            key_status = f"trusted header (user={fallback_uid})"
        else:
            key_status = "no auth"

        peer_status = f"on ({self.cfg.peer_recall_scope})" if self.cfg.peer_enabled else "off"

        await self.recall_ledger.load()
        snap = self.recall_ledger.snapshot()
        effective_scope = self.recall_ledger.resolve_peer_scope(
            self.cfg.recall_peer_scope, self_scope=scope
        )
        if not self.cfg.recall_context_enabled:
            recall_status = "ranked only (recall_context_enabled=false)"
        elif snap["context_face"] == "unsupported":
            recall_status = (
                f"context unavailable ({snap['context_face_reason'] or 'server refused'})"
            )
        else:
            recall_status = "context"
        recalled_uris = await self.recall_ledger.ring_size(session_id)
        outbox_snap = self.outbox.snapshot()
        outbox_pending = outbox_snap["venues"].get(venue_id, 0)
        commit_line = f"Last commit: {_fmt_ts(sched['last_commit_ts'])}"
        if sched["commit_state"]:
            task = sched["extract_task_id"]
            detail = sched["commit_detail"]
            commit_line += f" [{sched['commit_state']}"
            if task:
                commit_line += f", task={task[:8]}"
            if detail:
                commit_line += f", {detail[:60]}"
            commit_line += "]"
        supplement = ""
        if self.cfg.recall_include_active_peers and effective_scope == "actor":
            supplement = ", active-peer supplement=on"
        elif self.cfg.recall_include_active_peers:
            supplement = ", active-peer supplement=redundant (peer_scope=all)"

        lines = [
            "OpenViking Memory Plugin v0.2.0",
            f"Server: {self.cfg.ov_base_url} ({'OK' if healthy else 'UNREACHABLE'})",
            f"Account: {self.ov.account_id or '(not set)'}",
            f"Self scope: {scope}",
            f"OV self user: {ov_user_id}",
            f"Auth: {key_status}",
            f"Peer memory: {peer_status}",
            f"Venue: {venue_id}",
            f"Pending: {sched['pending_messages']} msgs / ~{sched['pending_tokens']} tokens",
            commit_line,
            f"Backfill: {bf_status}",
            f"Context inject: {'tail' if injection_fallback_count() == 0 else 'system_prompt'}"
            f" (fallbacks={injection_fallback_count()})",
            f"Recall: {recall_status}, peer_scope={effective_scope}, "
            f"dedup ring={recalled_uris}{supplement}",
            f"Outbox: {outbox_pending} pending (all venues: {sum(outbox_snap['venues'].values())}, "
            f"sent={outbox_snap['sent']}, dropped={outbox_snap['dropped']})",
            f"Active peers: {len(self.presence.active(venue_id))}",
            f"Venues: {len(self._venue_auth)}",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("ov_backfill", alias={"ov-backfill"})
    async def cmd_backfill(self, event: AstrMessageEvent):
        info = self._extract_event_info(event)
        venue_id = derive_venue(info["platform"], info["group_id"], info["sender_id"])
        ov_user_id = derive_ov_user_id(
            self.cfg, info["platform"], info["group_id"], info["sender_id"]
        )
        await self._ensure_self_auth(venue_id, info["group_id"], ov_user_id)
        auth = self._auth(venue_id)

        await self.backfill.force_backfill(
            venue_id,
            info["platform"],
            info["group_id"],
            auth,
            event=event,
        )
        yield event.plain_result(f"Backfill triggered for {venue_id}")

    # -- lifecycle ------------------------------------------------------------

    async def terminate(self):
        if self._drainer is not None and not self._drainer.done():
            self._drainer.cancel()
            try:
                await self._drainer
            except asyncio.CancelledError:
                pass
            except Exception:
                self.logger.exception("[OV] outbox drainer stopped with an error")
        # Last chance to empty the outbox before the HTTP client goes away.
        try:
            await self.outbox.flush_all()
        except Exception:
            self.logger.exception("[OV] outbox flush on shutdown failed")
        await self.scheduler.flush_all()
        await self.ov.close()
        self.logger.info("[OV] plugin terminated, all sessions flushed")


_SELF_COMMANDS = ("ov_backfill", "ov-backfill", "ov_status", "ov-status")


def _is_self_command(text: str) -> bool:
    t = text.strip().lstrip("/").lower()
    return any(t == c or t.startswith(c + " ") for c in _SELF_COMMANDS)


def _tool_name(tool: Any) -> str:
    """Extract a tool's name from AstrBot's FunctionTool (or a fallback)."""
    if tool is None:
        return ""
    name = getattr(tool, "name", None)
    return str(name) if name else str(tool)


def _tool_result_text(result: Any) -> str:
    """Best-effort text from an AstrBot/MCP CallToolResult."""
    if result is None:
        return ""
    content = getattr(result, "content", None)
    if content is None:
        return str(result)
    blocks = content if isinstance(content, list) else [content]
    texts = []
    for b in blocks:
        t = getattr(b, "text", None)
        texts.append(t if t else (b if isinstance(b, str) else str(b)))
    joined = "\n".join(s for s in texts if s)
    return joined or str(result)


def _message_text(payload: dict) -> str:
    """Best-effort text of a captured message, for token accounting on replay."""
    content = payload.get("content")
    if isinstance(content, str):
        return content
    chunks: list[str] = []
    for part in payload.get("parts") or []:
        if not isinstance(part, dict):
            continue
        for key in ("text", "tool_output"):
            value = part.get(key)
            if isinstance(value, str):
                chunks.append(value)
        tool_input = part.get("tool_input")
        if isinstance(tool_input, dict):
            chunks.append(json.dumps(tool_input, ensure_ascii=False, default=str))
    return " ".join(chunks)


def _extract_image_captions(req: Any) -> list[str]:
    """Pull AstrBot image captions out of req.extra_user_content_parts."""
    captions: list[str] = []
    for part in getattr(req, "extra_user_content_parts", None) or []:
        captions.extend(parse_image_captions(getattr(part, "text", "") or ""))
    return captions


def _fmt_ts(ts: float) -> str:
    if ts <= 0:
        return "never"
    import datetime

    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _parse_account_from_key(api_key: str) -> str:
    import base64

    parts = api_key.split(".")
    if len(parts) >= 2:
        try:
            account = base64.b64decode(parts[0] + "==").decode("utf-8")
            if account:
                return account
        except Exception:
            pass
    return ""
