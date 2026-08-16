"""Silk Cloud credits checkout (design preview).

A self-contained checkout for buying Silk Cloud model credits, sized to match
the M0 gateway milestone in docs/CLOUD.md (§11.3): local users point at the
gateway, buy credits, and stop managing provider API keys. No real payment
rails exist yet, so a purchase here creates a *simulated* order — persisted
under ``$SILKCODE_HOME/billing.json`` — that the UI can display and tests can
assert against. When a real gateway/ledger lands (CLOUD.md §5, §6) this module
is the piece that talks to it; the GUI's checkout UI and its API contract
(DIST: credit packs + order receipt) are intentionally unchanged.
"""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Callable

from .config import config_dir

# Credit packs offered on the checkout page: ``credits`` is the balance added
# to the account and ``usd`` is the price shown. Credits are the unit the
# gateway meters (CLOUD.md §5 step 5), so a pack's real conversion rate lives
# here — a single place to adjust before it is mirrored by a live pricing API.
CREDIT_PACKS: list[dict] = [
    {"id": "starter", "label": "Starter", "credits": 500, "usd": 15.00},
    {"id": "pro", "label": "Pro", "credits": 2500, "usd": 60.00},
    {"id": "power", "label": "Power", "credits": 10000, "usd": 200.00},
]

# Order records are written under $SILKCODE_HOME so credentials and orders live
# with the same install the CLI and GUI already use.
BILLING_FILE = "billing.json"

_lock = threading.Lock()


def checkout_options() -> dict:
    """Describe the checkout page: the credit packs plus the order summary
    defaults the UI should render on first paint."""
    return {
        "packs": CREDIT_PACKS,
        "currency": "USD",
        "default_pack": CREDIT_PACKS[0]["id"],
    }


def _billing_path() -> Path:
    base = config_dir()
    if not base:
        raise RuntimeError("SILKCODE_HOME is not configured")
    return Path(base) / BILLING_FILE


def _load_orders() -> list[dict]:
    p = _billing_path()
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8")) or []
    except (json.JSONDecodeError, OSError):
        return []


def _save_orders(orders: list[dict]) -> None:
    p = _billing_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(orders, indent=2), encoding="utf-8")


def _mask_card(number: str) -> str:
    digits = "".join(ch for ch in number if ch.isdigit())[-4:]
    return "•••• " + digits if digits else ""


def build_receipt_email(order: dict) -> str:
    """A one-line receipt email shown on the success state of the checkout."""
    return (f"Order {order['order_id']} — {order['credits']:,} Silk credits "
            f"added · ${order['usd']:.2f}" + (f" · ending {order['card_last4']}"
                                              if order.get("card_last4") else ""))


def purchase(pack_id: str, card_number: str, now: Callable[[], int] | None = None) -> dict:
    """Buy a credit pack and record a simulated order.

    Validates the pack and card up front. On success returns a receipt that the
    UI can present; the order is appended (thread-safely) to the billing store
    so subsequent purchases and the Environment page can read it back.
    """
    pack = next((p for p in CREDIT_PACKS if p["id"] == pack_id), None)
    if pack is None:
        raise ValueError(f"unknown credit pack '{pack_id}'")

    digits = "".join(ch for ch in card_number if ch.isdigit())
    if len(digits) < 8:
        raise ValueError("card number is too short")

    ts = now() if now is not None else int(_now())
    order = {
        "order_id": f"silkord-{uuid.uuid4().hex[:12]}",
        "pack": pack["id"],
        "label": pack["label"],
        "credits": pack["credits"],
        "usd": pack["usd"],
        "card_last4": _mask_card(card_number),
        "at": ts,
    }
    with _lock:
        orders = _load_orders()
        orders.append(order)
        _save_orders(orders)

    receipt = {**order, "receipt_email": build_receipt_email(order)}
    return receipt


def orders() -> list[dict]:
    """Every recorded order, newest first — for the Environment page."""
    return list(reversed(_load_orders()))


def credit_balance() -> int:
    """Sum of purchased credits (CLOUD.md §6: balance as a materialized sum)."""
    return sum(o.get("credits", 0) for o in _load_orders())


def _now() -> float:
    import time

    return time.time()
