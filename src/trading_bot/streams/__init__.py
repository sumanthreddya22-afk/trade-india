"""Real-time stream producers for the dashboard.

Currently:
* ``alpaca_trade_stream`` — Alpaca paper trading websocket; pushes
  ``order.*`` and ``position.changed`` events into the bus.

Phase 3 will add ``file_watchers`` (watchdog-driven), and Phase 8 will
add the dashboard-side ``market_data_stream`` (gated, in-process price
ticks that never go through SQLite).
"""
