"""v4 ledger — append-only, hash-chained, event-sourced.

Plan v4 §5. See ``README.md`` for the mandate; see ``schema.py`` for the
DDL; see the per-table writer modules for the append helpers.
"""
from __future__ import annotations

from trading_bot.ledger.connection import (
    DEFAULT_LEDGER_PATH,
    DEFAULT_MIRROR_PATH,
    WriterLockHeld,
    acquire_writer_lock,
    connect_reader,
    connect_writer,
)
from trading_bot.ledger.fill_event import append_fill_event
from trading_bot.ledger.hash_chain import (
    HashChainBroken,
    verify_all_chained,
    verify_chain,
)
from trading_bot.ledger.mirror import (
    init_mirror,
    mirror_event,
    mirror_order_master,
)
from trading_bot.ledger.order_master import (
    ACTIVE_STATES,
    TERMINAL_STATES,
    OrderIntent,
    check_idempotent,
    current_state,
    insert_order_master,
    lookup_by_client_order_id,
)
from trading_bot.ledger.orphan_recovery import (
    DEFAULT_ORPHAN_AGE_SECONDS,
    Orphan,
    find_orphans,
    recover_orphan,
)
from trading_bot.ledger.position_snapshot import (
    write_snapshot,
    write_snapshot_batch,
)
from trading_bot.ledger.reconciliation import (
    compute_recon,
    hash_position_vector,
    write_recon_proof,
)
from trading_bot.ledger.schema import (
    HASH_CHAINED_TABLES,
    SCHEMA_VERSION,
    create_ledger,
    ensure_schema,
    read_schema_version,
)
from trading_bot.ledger.state_event import (
    IllegalTransition,
    STATES,
    append_state_event,
)
from trading_bot.ledger.strategy_decision import write_decision

__all__ = [
    "ACTIVE_STATES",
    "DEFAULT_LEDGER_PATH",
    "DEFAULT_MIRROR_PATH",
    "DEFAULT_ORPHAN_AGE_SECONDS",
    "HASH_CHAINED_TABLES",
    "HashChainBroken",
    "IllegalTransition",
    "Orphan",
    "OrderIntent",
    "SCHEMA_VERSION",
    "STATES",
    "TERMINAL_STATES",
    "WriterLockHeld",
    "acquire_writer_lock",
    "append_fill_event",
    "append_state_event",
    "check_idempotent",
    "compute_recon",
    "connect_reader",
    "connect_writer",
    "create_ledger",
    "current_state",
    "ensure_schema",
    "find_orphans",
    "hash_position_vector",
    "init_mirror",
    "insert_order_master",
    "lookup_by_client_order_id",
    "mirror_event",
    "mirror_order_master",
    "read_schema_version",
    "recover_orphan",
    "verify_all_chained",
    "verify_chain",
    "write_decision",
    "write_recon_proof",
    "write_snapshot",
    "write_snapshot_batch",
]
