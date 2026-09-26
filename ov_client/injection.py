"""
Recall injection that does not fight the provider's prefix cache.

Recalled context is delivered as a provider-only content part on the current
user message. It deliberately stays out of the system prompt: that prompt sits
at the very front of every request, so rewriting it invalidates the cached
prefix for everything that follows — the entire conversation history is then
re-billed on each turn, even though only the recall changed.
"""

from __future__ import annotations

from typing import Any

from ._log import logger

try:  # content parts are exposed to plugins by current AstrBot releases
    from astrbot.core.agent.message import TextPart as _TextPart
except Exception:  # pragma: no cover - very old AstrBot without content parts
    _TextPart = None  # type: ignore[assignment]

# Times we had to inject through the system prompt because content parts were
# unavailable. Counted rather than swallowed: that path costs prefix-cache hits,
# so a silent fallback would look exactly like a recall-quality regression.
_fallback_count = 0
_fallback_warned = False


def injection_fallback_count() -> int:
    """How often recall had to fall back to rewriting the system prompt."""
    return _fallback_count


def inject_recall_block(
    req: Any,
    block: str,
    text_part_cls: Any = None,
) -> None:
    """Attach `block` to the tail of `req`'s current user message.

    Args:
        req: AstrBot ``ProviderRequest`` (anything with
            ``extra_user_content_parts`` and ``system_prompt`` attributes).
        block: The rendered recall block.
        text_part_cls: Content part class to use; defaults to AstrBot's
            ``TextPart``. Injectable for tests and for harnesses without
            content parts.
    """
    part_cls = _TextPart if text_part_cls is None else text_part_cls
    parts = getattr(req, "extra_user_content_parts", None)

    if part_cls is None or parts is None:
        # Last-resort fallback for AstrBot versions without content parts. This
        # costs prefix-cache hits, so it is counted and reported once instead of
        # degrading silently.
        global _fallback_count, _fallback_warned
        _fallback_count += 1
        if not _fallback_warned:
            _fallback_warned = True
            logger.warning(
                "[OV] recall injected via system_prompt (no content parts on this "
                "AstrBot build or request); prefix cache hits will drop. "
                "Reported once; see the Context inject line of /ov_status."
            )
        req.system_prompt = (req.system_prompt or "") + "\n\n" + block
        return

    part = part_cls(text=block)
    mark_as_temp = getattr(part, "mark_as_temp", None)
    if callable(mark_as_temp):
        # Provider-facing only: recalled context must not pile up in history.
        mark_as_temp()
    parts.append(part)
