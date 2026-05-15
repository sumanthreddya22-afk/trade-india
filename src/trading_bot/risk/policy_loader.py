"""Load + hash-verify the v4 policy locks.

Plan v4 §0: at startup the kernel reads ``policy/HASHES``, recomputes
SHA-256 of each ``.lock`` file, and refuses to start on any mismatch.

Plan v4 §4: loosening any threshold requires a new dated lock version
AND a 7-day cooldown before the system honors it. ``honor_cooldown`` is
the helper that gates "is this lock effective yet?".

For Phase 2, the loader produces a frozen ``PolicyBundle`` that every
risk check reads. Mutating ``PolicyBundle`` is intentionally not
supported — a new lock is a new file, hashed and committed.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_POLICY_DIR = REPO_ROOT / "policy"
DEFAULT_HASHES_PATH = DEFAULT_POLICY_DIR / "HASHES"

# Lock files the bundle expects. Each maps to an attribute on PolicyBundle.
LOCK_FILES = {
    "validation_policy": "validation_policy.lock",
    "risk_policy": "risk_policy.lock",
    "pdt_policy": "pdt_policy.lock",
    "lane_caps": "lane_caps.lock",
    "cost_model": "cost_model.lock",
    "role_personas": "role_personas.lock",
    "source_reliability": "source_reliability.lock",
    "data_freshness": "data_freshness.lock",
    "short_policy": "short_policy.lock",
    "live_capital": "live_capital.lock",
}

# Lock-version pattern: YYYY-MM-DD[.<tag>]
_LOCK_VERSION_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


class PolicyHashMismatch(Exception):
    """Raised when a lock file's content does not match its entry in
    ``policy/HASHES``."""


class CooldownNotElapsed(Exception):
    """Raised when a loosened lock is presented to the kernel before its
    7-day cooldown has passed."""


@dataclass(frozen=True)
class PolicyBundle:
    """Every ``.lock`` file loaded into memory + their combined hash.

    Every risk check receives one of these. The bundle is immutable; new
    policy means a new bundle loaded from new lock files.
    """

    validation_policy: Mapping[str, Any]
    risk_policy: Mapping[str, Any]
    pdt_policy: Mapping[str, Any]
    lane_caps: Mapping[str, Any]
    cost_model: Mapping[str, Any]
    role_personas: Mapping[str, Any]
    source_reliability: Mapping[str, Any]
    data_freshness: Mapping[str, Any]
    short_policy: Mapping[str, Any]
    live_capital: Mapping[str, Any]
    combined_hash: str = ""
    """SHA-256 over the canonical JSON of every lock, written into
    ``strategy_decision.policy_hash`` for every risk decision."""


def _read_text(path: Path) -> str:
    return path.read_text()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_hashes_manifest(path: Path) -> dict[str, str]:
    """``policy/HASHES`` shape: each line ``<sha256>  <relpath>``."""
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        sha, rel = line.split(None, 1)
        out[rel.strip()] = sha
    return out


def verify_policy_hashes(
    policy_dir: Path = DEFAULT_POLICY_DIR,
    hashes_path: Path = DEFAULT_HASHES_PATH,
) -> dict[str, str]:
    """Recompute every lock file's SHA-256 and compare to ``policy/HASHES``.

    Returns the verified manifest on success. Raises ``PolicyHashMismatch``
    on the first mismatch (or missing file).
    """
    manifest = _parse_hashes_manifest(hashes_path)
    for _key, fname in LOCK_FILES.items():
        rel = f"policy/{fname}"
        if rel not in manifest:
            raise PolicyHashMismatch(f"{rel} missing from HASHES")
        path = policy_dir / fname
        if not path.exists():
            raise PolicyHashMismatch(f"{rel} missing on disk")
        actual = _sha256(_read_text(path))
        if actual != manifest[rel]:
            raise PolicyHashMismatch(
                f"{rel}: HASHES says {manifest[rel][:16]}..., "
                f"file is {actual[:16]}..."
            )
    return manifest


def load_policy(
    policy_dir: Path = DEFAULT_POLICY_DIR,
    *,
    verify: bool = True,
) -> PolicyBundle:
    """Load + (optionally) hash-verify every lock file.

    ``verify=True`` (default) refuses to load if any lock has been
    modified without a HASHES regen. Tests may pass ``verify=False`` to
    sidestep the hash check when loading fixture locks from a tmp dir.
    """
    if verify:
        verify_policy_hashes(policy_dir=policy_dir,
                             hashes_path=policy_dir / "HASHES")

    loaded: dict[str, Mapping[str, Any]] = {}
    combined_payload: list[str] = []
    for key, fname in LOCK_FILES.items():
        text = _read_text(policy_dir / fname)
        loaded[key] = json.loads(text)
        combined_payload.append(_sha256(text))
    combined_hash = hashlib.sha256(
        "\n".join(combined_payload).encode("utf-8")
    ).hexdigest()

    return PolicyBundle(
        validation_policy=loaded["validation_policy"],
        risk_policy=loaded["risk_policy"],
        pdt_policy=loaded["pdt_policy"],
        lane_caps=loaded["lane_caps"],
        cost_model=loaded["cost_model"],
        role_personas=loaded["role_personas"],
        source_reliability=loaded["source_reliability"],
        data_freshness=loaded["data_freshness"],
        short_policy=loaded["short_policy"],
        live_capital=loaded["live_capital"],
        combined_hash=combined_hash,
    )


def parse_lock_version_date(version: str) -> Optional[dt.date]:
    """Extract the YYYY-MM-DD date prefix from a ``lock_version`` string."""
    m = _LOCK_VERSION_DATE.match(version or "")
    if not m:
        return None
    return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def honor_cooldown(
    *,
    new_lock_version: str,
    new_is_looser: bool,
    today: dt.date,
    cooldown_days: int = 7,
) -> bool:
    """Plan §4 asymmetric cooldown.

    Tightening (``new_is_looser=False``) takes effect on the next kernel
    cycle. Loosening must wait ``cooldown_days`` after the lock's dated
    version before the system honors it.

    Returns True iff the new lock is currently honoured.
    """
    if not new_is_looser:
        return True
    locked_at = parse_lock_version_date(new_lock_version)
    if locked_at is None:
        # Bad date format — fail closed.
        return False
    elapsed = (today - locked_at).days
    return elapsed >= cooldown_days


__all__ = [
    "CooldownNotElapsed",
    "DEFAULT_POLICY_DIR",
    "DEFAULT_HASHES_PATH",
    "LOCK_FILES",
    "PolicyBundle",
    "PolicyHashMismatch",
    "honor_cooldown",
    "load_policy",
    "parse_lock_version_date",
    "verify_policy_hashes",
]
