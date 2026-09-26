"""Shared logger for the ``ov_client`` modules.

Inside AstrBot, importing ``logger`` from ``astrbot.api`` yields a proxy that
routes every call to the *calling plugin's* dedicated logger
(``astrbot.plugin.<plugin_name>``). That is what makes the per-plugin log level
configured in the WebUI apply to this package as well, and it keeps these
messages on AstrBot's log pipeline instead of the plain root logger.

``ov_client`` is also imported directly by the test-suite (and by CI) without
AstrBot installed, so the import is guarded and degrades to a module logger.
"""

import logging

try:  # pragma: no cover - only taken when running inside AstrBot
    from astrbot.api import logger
except Exception:  # pragma: no cover - standalone test / CI runs
    logger = logging.getLogger("astrbot_plugin_openviking_memory")
