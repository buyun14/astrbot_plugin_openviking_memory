"""
An admin key must never reach a tenant data API.

OV answers 403 PERMISSION_DENIED ("ROOT API keys cannot access tenant-scoped
data APIs") when a tenant data call carries an admin/root Bearer. The plugin
used to hand the admin key to the client as its default Bearer
(``admin or user``), so any capture that could not resolve a venue key wrote
with an admin Bearer: OV rejected it, the outbox treated the rejection as
non-retryable and dropped the message — a hole in the session instead of a
visible failure.

Three layers are pinned here: the helper that picks the data key, the client's
refusal to fall back to an admin key, and the invariant that every hook in
``main.py`` that captures a message also resolves the venue's auth first.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib

import pytest

from ov_client.client import OVClient
from ov_client.config import PluginConfig, data_api_key, user_api_key

MAIN_PY = pathlib.Path(__file__).resolve().parent.parent / "main.py"

ADMIN_KEY = "ov_4a8db146b64deadbeef"
USER_KEY = "aG9tZQ.YWRtaW4.c2lnbmF0dXJl"


def _client(api_key: str = "", *, trusted_mode: bool = False) -> OVClient:
    return OVClient("http://x", api_key=api_key, account_id="acme", trusted_mode=trusted_mode)


# -- config: the user key is the data key ------------------------------------


def test_user_key_wins_over_admin_key():
    cfg = PluginConfig({"ov_admin_api_key": ADMIN_KEY, "ov_user_api_key": USER_KEY})
    assert data_api_key(cfg) == USER_KEY


def test_admin_key_alone_never_becomes_the_data_key():
    cfg = PluginConfig({"ov_admin_api_key": ADMIN_KEY})
    assert data_api_key(cfg) == ""


def test_user_key_is_trimmed():
    cfg = PluginConfig({"ov_user_api_key": f"  {USER_KEY}  "})
    assert data_api_key(cfg) == USER_KEY
    assert user_api_key(cfg) == USER_KEY


def test_trusted_mode_defaults_to_the_admin_key():
    # Trusted mode authenticates the gateway with the admin key and asserts
    # identity through X-OpenViking-User, so the admin key *is* its Bearer.
    cfg = PluginConfig({"trusted_mode": True, "ov_admin_api_key": ADMIN_KEY})
    assert data_api_key(cfg) == ADMIN_KEY


def test_trusted_mode_still_prefers_a_user_key():
    cfg = PluginConfig(
        {
            "trusted_mode": True,
            "ov_admin_api_key": ADMIN_KEY,
            "ov_user_api_key": USER_KEY,
        }
    )
    assert data_api_key(cfg) == USER_KEY


# -- client: no silent admin fallback ----------------------------------------


def test_explicit_user_key_is_sent():
    c = _client(ADMIN_KEY)
    assert c._headers(api_key=USER_KEY)["Authorization"] == f"Bearer {USER_KEY}"


def test_explicit_admin_key_is_still_allowed():
    # ``create_user`` passes the admin key explicitly; only the implicit
    # client-wide fallback is refused.
    c = _client(USER_KEY)
    assert c._headers(api_key=ADMIN_KEY)["Authorization"] == f"Bearer {ADMIN_KEY}"


def test_client_wide_admin_key_is_refused():
    c = _client(ADMIN_KEY)
    with pytest.raises(ValueError, match="admin key"):
        c._headers()


def test_unresolved_venue_write_fails_loudly_instead_of_403():
    # The shape of the production bug: an unprimed venue resolves to no key, so
    # the client falls back to its own. With an admin key configured that was a
    # 403 plus a dropped write; now it raises before the request is built.
    c = _client(ADMIN_KEY)
    with pytest.raises(ValueError):
        asyncio.run(c.add_message("sess", {"role": "user", "content": []}))


def test_explicit_empty_key_means_no_bearer():
    # ``_auth`` resolves an unresolved venue to "" on purpose: inheriting the
    # client-wide key would write under the global self. An explicit "" must
    # therefore mean "no Bearer", not "use the default".
    c = _client(USER_KEY)
    assert "Authorization" not in c._headers(api_key="")
    assert "Authorization" in c._headers()


def test_trusted_mode_may_default_to_the_admin_key():
    c = _client(ADMIN_KEY, trusted_mode=True)
    assert c._headers()["Authorization"] == f"Bearer {ADMIN_KEY}"


def test_health_probe_is_contained_when_the_default_is_an_admin_key():
    # health() swallows the misconfiguration into a False rather than raising
    # inside a probe; the data path is where it has to be loud.
    assert asyncio.run(_client(ADMIN_KEY).health()) is False


def test_user_key_default_and_trusted_mode_still_work():
    assert "Authorization" in _client(USER_KEY)._headers()
    trusted = _client("", trusted_mode=True)._headers(user_id="peer")
    assert "Authorization" not in trusted
    assert trusted["X-OpenViking-User"] == "peer"


# -- every capturing hook resolves the venue's auth ---------------------------


def _calls(node: ast.AST, attr: str) -> bool:
    return any(
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == attr
        for call in ast.walk(node)
    )


def test_capture_hooks_prime_auth_they_use():
    """A hook that captures must resolve auth first, or the write silently
    falls back to the client-wide key.

    Checked against the source because ``main.py`` imports the AstrBot runtime
    (relative imports plus ``astrbot.api``) and cannot be imported here.
    """
    tree = ast.parse(MAIN_PY.read_text(encoding="utf-8"))
    missing = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and _calls(node, "_capture_message")
        and not _calls(node, "_ensure_self_auth")
    ]
    assert missing == [], f"capture hooks that never prime auth: {missing}"
