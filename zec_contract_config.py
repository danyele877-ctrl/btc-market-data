#!/usr/bin/env python3
"""Account-scoped, read-only contract metadata and risk policy for ZEC X-Perp."""
from __future__ import annotations
from typing import Any

INSTRUMENT_ID = "ZEC-USD_UM_XPERP-310530"
CTVAL_ZEC_PER_CONTRACT = 0.01
CT_MULT = 1.0
LOT_SIZE = 1.0
MIN_SIZE = 1.0
TICK_SIZE_USD = 0.01
SETTLE_CCY = "USD"
MAX_RISK_USDC = 20.0
FUNDING_INTERVAL_HOURS = 4.0
UI_CONFIRMATION_DATE = "2026-11-15"


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if number == number and abs(number) != float("inf") else None
    except (TypeError, ValueError):
        return None


def build_contract_safety(instrument: dict[str, Any] | None, *, verified: bool = False) -> dict[str, Any]:
    """Validate fresh EEA metadata against the user's exact-instrument UI confirmation."""
    row = instrument if isinstance(instrument, dict) else {}
    api_ctval = _number(row.get("ctVal"))
    api_ctmult = _number(row.get("ctMult"))
    api_lot = _number(row.get("lotSz"))
    api_min = _number(row.get("minSz"))
    api_tick = _number(row.get("tickSz"))
    api_settle = row.get("settleCcy")
    matches = (
        verified and row.get("instId") == INSTRUMENT_ID and row.get("state") == "live"
        and api_ctval == CTVAL_ZEC_PER_CONTRACT and api_ctmult == CT_MULT
        and api_lot == LOT_SIZE and api_min == MIN_SIZE
        and api_tick == TICK_SIZE_USD and api_settle == SETTLE_CCY
    )
    return {
        "instrument_id": INSTRUMENT_ID,
        "ctVal_api_zec": api_ctval,
        "ctMult_api": api_ctmult,
        "lotSz_api": api_lot,
        "minSz_api": api_min,
        "tickSz_api_usd": api_tick,
        "settleCcy_api": api_settle,
        "ctVal_zec_per_contract": CTVAL_ZEC_PER_CONTRACT if matches else None,
        "ctVal_status": "RISOLTO_UI_API_CONCORDI" if matches else "NON VERIFICATO",
        "confirmation_scope": "solo ZEC-USD_UM_XPERP-310530 dell'account mostrato; non generalizzare ad altre scadenze o account",
        "confirmation_evidence": (
            f"UI nativa OKX fornita dall'utente il {UI_CONFIRMATION_DATE} + metadati API OKX EEA correnti concordi"
            if matches else "metadati API EEA assenti, non freschi o non concordi con la conferma UI"
        ),
        "historical_document_note": (
            "La precedente tabella pubblica consultata riportava un valore differente; "
            "per questo account/instId la conferma UI nativa e l'API EEA concordano. "
            "Ricontrollare se cambia strumento, scadenza o account."
        ),
        "metadata_verified": bool(matches),
        "sizing_allowed": bool(matches),
        "theoretical_sizing_permitted": bool(matches),
        "position_sizing_enabled": False,
        "position_sizing_note": "Il motore non calcola quantità automaticamente; un calcolo teorico richiede entry, SL e costi verificati.",
        "max_risk_usdc": MAX_RISK_USDC,
        "margin_or_exposure_calculated": False,
        "orders_enabled": False,
    }
