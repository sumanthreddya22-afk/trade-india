"""Re-export shim: stocks pipeline intel → legacy aggregator."""
from trading_bot.intel.aggregator import *  # noqa: F401, F403
from trading_bot.intel.aggregator import (  # noqa: F401
    DEFAULT_SOURCE_WEIGHT,
    SOURCE_WEIGHTS,
    write_event,
)
