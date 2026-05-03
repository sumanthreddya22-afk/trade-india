"""Backward-compatibility shim — daemon was moved to trading_bot.shared.daemon.

Production launchd plists at ``ops/launchd/com.bharath.trading.daemon.{paper,live}.plist``
still reference ``python -m trading_bot.daemon`` and can keep doing so without
needing a reload. This shim re-exports the real module + provides a ``__main__``
entry point so the same invocation continues to work.

New code should import from ``trading_bot.shared.daemon`` directly.
"""
from __future__ import annotations

from trading_bot.shared.daemon import *  # noqa: F401,F403  re-export public surface
from trading_bot.shared.daemon import main  # noqa: F401  explicit, used by `-m`


if __name__ == "__main__":
    raise SystemExit(main())
