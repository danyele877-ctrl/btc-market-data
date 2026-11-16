#!/usr/bin/env python3
"""Account-scoped, read-only contract metadata and risk policy for ZEC X-Perp."""
from __future__ import annotations
from typing import Any

INSTRUMENT_ID = "ZEC-USD_UM_XPERP-310530"
CTVAL_ZEC_PER_CONTRACT = 0.01
OFFICIAL_SPEC_CTVAL_ZEC_PER_CONTRACT = 0.1
CTVAL_RECONCILED = False
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
    # ctVal is a raw API value; its unit is defined by ctValCcy and must not
    # be relabelled as ZEC unless OKX explicitly reports that currency.
    api_ctval = _number(row.get("ctVal"))
    api_ctval_ccy = row.get("ctValCcy")
    api_ct_type = row.get("ctType")
    api_ctmult = _number(row.get("ctMult"))
    api_lot = _number(row.get("lotSz"))
    api_min = _number(row.get("minSz"))
    api_tick = _number(row.get("tickSz"))
    api_settle = row.get("settleCcy")
    ui_api_match = (
        verified and row.get("instId") == INSTRUMENT_ID and row.get("state") == "live"
        and api_ctval == CTVAL_ZEC_PER_CONTRACT and api_ctmult == CT_MULT
        and api_ctval_ccy == "ZEC"
        and api_lot == LOT_SIZE and api_min == MIN_SIZE
        and api_tick == TICK_SIZE_USD and api_settle == SETTLE_CCY
    )
    reconciled = ui_api_match and CTVAL_RECONCILED
    return {
        "instrument_id": INSTRUMENT_ID,
        "ctVal_api_raw": api_ctval,
        "ctValCcy_api": api_ctval_ccy,
        "ctType_api": api_ct_type,
        "ctVal_unit_interpretation": ("ZEC" if api_ctval_ccy == "ZEC" else "NON DETERMINATA: verificare ctValCcy" if not api_ctval_ccy else f"ctVal espresso in {api_ctval_ccy}, non convertito in ZEC"),
        "ctMult_api": api_ctmult,
        "lotSz_api": api_lot,
        "minSz_api": api_min,
        "tickSz_api_usd": api_tick,
        "settleCcy_api": api_settle,
        "ctVal_zec_per_contract": CTVAL_ZEC_PER_CONTRACT if reconciled else None,
        "ctVal_official_spec_zec": OFFICIAL_SPEC_CTVAL_ZEC_PER_CONTRACT,
        "ctVal_ui_api_match": bool(ui_api_match),
        "official_spec_zec_per_contract": OFFICIAL_SPEC_CTVAL_ZEC_PER_CONTRACT,
        "raw_metadata_complete": all(row.get(key) not in (None, "") for key in ("ctVal", "ctMult", "ctValCcy", "ctType", "lotSz", "minSz", "tickSz", "settleCcy")),
        "ctVal_status": (
            "RISOLTO" if reconciled else "NON RISOLTO_SPECIFICA_UFFICIALE" if ui_api_match else "NON VERIFICATO"
        ),
        "confirmation_scope": "solo ZEC-USD_UM_XPERP-310530 dell'account mostrato; non generalizzare ad altre scadenze o account",
        "confirmation_evidence": (
            f"UI nativa OKX ({UI_CONFIRMATION_DATE}) e API EEA concordano su 0.01 ZEC, ma la specifica pubblica OKX EEA riporta 0.1 ZEC; sizing bloccato finché la differenza non è riconciliata"
            if ui_api_match else "metadati API EEA assenti, non freschi o non concordi con la conferma UI"
        ),
        "historical_document_note": (
            "La specifica pubblica OKX EEA riporta 0.1 ZEC, mentre UI/API account-scoped riportano 0.01 ZEC; "
            "non usare il dato account-scoped per dimensionare finché OKX non chiarisce formalmente la differenza."
        ),
        "metadata_verified": bool(ui_api_match),
        "ctVal_reconciled": bool(reconciled),
        "sizing_allowed": bool(reconciled),
        "theoretical_sizing_permitted": bool(reconciled),
        "position_sizing_enabled": False,
        "position_sizing_note": "Bloccato fino alla riconciliazione formale tra 0.01 UI/API e 0.1 ZEC della specifica pubblica e alla verifica dell’unita ctValCcy.",
        "max_risk_usdc": MAX_RISK_USDC,
        "margin_or_exposure_calculated": False,
        "orders_enabled": False,
    }
