"""Load, validate, and atomically persist tracker_state.json."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_VERSION = 2
HISTORY_CAP = 60
DEFAULT_STATE_PATH = Path(__file__).resolve().parent / "data" / "tracker_state.json"

REQUIRED_PRODUCT_FIELDS = ("id", "name", "site", "url", "target_price")


class StateError(ValueError):
    """Invalid tracker state."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _default_product_fields(product: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(product)
    out.setdefault("enabled", True)
    out.setdefault("match_keywords", [])
    out.setdefault("specs", {})
    out.setdefault("last_price", None)
    out.setdefault("lowest_seen", None)
    out.setdefault("last_checked_at", None)
    out.setdefault("in_stock", None)
    out.setdefault("consecutive_failures", 0)
    out.setdefault("last_alert", None)
    out.setdefault("history", [])
    return out


def _validate_product(product: dict[str, Any], index: int) -> dict[str, Any]:
    for field in REQUIRED_PRODUCT_FIELDS:
        if field not in product or product[field] in (None, ""):
            raise StateError(f"products[{index}] missing required field {field!r}")
    try:
        product["target_price"] = int(product["target_price"])
    except (TypeError, ValueError) as exc:
        raise StateError(f"products[{index}].target_price must be an integer") from exc
    if product.get("last_price") is not None:
        product["last_price"] = int(product["last_price"])
    product["site"] = str(product["site"]).lower()
    product["history"] = list(product.get("history") or [])[-HISTORY_CAP:]
    lowest = product.get("lowest_seen")
    if lowest is not None:
        if not isinstance(lowest, dict) or "price" not in lowest:
            raise StateError(f"products[{index}].lowest_seen must be {{price, at}}")
        lowest["price"] = int(lowest["price"])
    return product


def load_state(path: Path | str | None = None) -> dict[str, Any]:
    state_path = Path(path) if path else DEFAULT_STATE_PATH
    if not state_path.exists():
        raise StateError(f"state file not found: {state_path}")
    with state_path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise StateError("state root must be an object")
    version = data.get("version", 1)
    if version != STATE_VERSION:
        data["version"] = STATE_VERSION
    products = data.get("products")
    if not isinstance(products, list):
        raise StateError("state.products must be an array")
    ids: set[str] = set()
    validated: list[dict[str, Any]] = []
    for i, raw in enumerate(products):
        if not isinstance(raw, dict):
            raise StateError(f"products[{i}] must be an object")
        product = _validate_product(_default_product_fields(raw), i)
        pid = product["id"]
        if pid in ids:
            raise StateError(f"duplicate product id: {pid}")
        ids.add(pid)
        validated.append(product)
    data["products"] = validated
    return data


def save_state(state: dict[str, Any], path: Path | str | None = None) -> None:
    state_path = Path(path) if path else DEFAULT_STATE_PATH
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, indent=2, ensure_ascii=False) + "\n"
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, state_path)


def append_history(product: dict[str, Any], price: int, in_stock: bool | None, checked_at: str) -> None:
    history = list(product.get("history") or [])
    history.append({"price": price, "in_stock": in_stock, "at": checked_at})
    product["history"] = history[-HISTORY_CAP:]


def record_success(
    product: dict[str, Any],
    *,
    price: int,
    in_stock: bool | None,
    checked_at: str | None = None,
) -> None:
    checked_at = checked_at or utc_now()
    product["last_price"] = price
    product["in_stock"] = in_stock
    product["last_checked_at"] = checked_at
    product["consecutive_failures"] = 0
    lowest = product.get("lowest_seen")
    if lowest is None or price < int(lowest["price"]):
        product["lowest_seen"] = {"price": price, "at": checked_at}
    append_history(product, price, in_stock, checked_at)


def record_out_of_stock(product: dict[str, Any], *, checked_at: str | None = None) -> None:
    """OOS is a successful fetch; keep last_price and do not count as selector rot."""
    checked_at = checked_at or utc_now()
    product["in_stock"] = False
    product["last_checked_at"] = checked_at
    product["consecutive_failures"] = 0


def record_failure(product: dict[str, Any], *, checked_at: str | None = None) -> None:
    product["last_checked_at"] = checked_at or utc_now()
    product["consecutive_failures"] = int(product.get("consecutive_failures") or 0) + 1


def record_alert(product: dict[str, Any], *, price: int, reason: str, at: str | None = None) -> None:
    product["last_alert"] = {"price": price, "reason": reason, "at": at or utc_now()}


def find_product(state: dict[str, Any], product_id: str) -> dict[str, Any] | None:
    for product in state.get("products", []):
        if product.get("id") == product_id:
            return product
    return None
