"""Deterministic JSON serialization for hash-chain inputs.

Plan v4 §5: ``this_hash = sha256(prev_hash || canonical(row))``. The
canonical form must be byte-identical across machines, Python versions,
and architectures, otherwise a verifier on another host will reject every
chain. We use ``json.dumps`` with:

  - ``sort_keys=True``       — key ordering is deterministic
  - ``separators=(",", ":")``— no whitespace, no spaces after separators
  - ``default=str``           — fall back on str() for unknown types
                               (Decimal, datetime, sqlite3.Row, etc.)

Hash-only fields (``prev_hash``, ``this_hash``) are EXCLUDED from the
canonical form so that the hash of the *content* is unaffected by the
hash itself.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

# Fields excluded from canonicalization. Adding more here is a breaking
# change to the chain — every existing row would need re-hashing.
#
# - prev_hash / this_hash: hashing themselves into themselves is circular.
# - ledger_seq: the autoincrement counter is a property of the DB instance,
#   not the event. Excluding it lets the hash represent the *event content*
#   only; the chain (prev_hash + this_hash) still pins order.
_HASH_FIELDS = frozenset({"prev_hash", "this_hash", "ledger_seq"})


def canonical_json(row: Mapping[str, Any]) -> bytes:
    """Return the canonical UTF-8 byte representation of ``row``.

    ``row`` is any mapping (dict, sqlite3.Row coerced via dict()). Hash
    fields are stripped before serialization.
    """
    sanitized = {k: row[k] for k in row.keys() if k not in _HASH_FIELDS}
    text = json.dumps(sanitized, sort_keys=True, separators=(",", ":"), default=str)
    return text.encode("utf-8")


__all__ = ["canonical_json"]
