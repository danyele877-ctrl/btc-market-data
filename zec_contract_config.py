#!/usr/bin/env python3
"""Account-scoped, read-only contract metadata and risk policy for ZEC X-Perp."""
from __future__ import annotations
from typing import Any

INSTRUMENT_ID = "ZEC-USD_UM_XPERP-310530"
CTVAL_ZEC_PER_CONTRACT = 0.1  # Official contract size confirmed by OKX support
OFFICIAL_SPEC_CTVAL_ZEC_PER_CONTRACT = 0.1
ACCOUNT_API_CTVAL_ZEC_PER_CONTRACT = 0.01  # Raw account-scoped API/UI observation; not the confirmed contract size
CTVAL_RECONCILED = False
OFFICIAL_SUPPORT_CONFIRMATION_RECEIVED = True
OFFICIAL_SUPPORT_CONFIRMATION_DATE = "2026-11-16"
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


def build_contract_safety(instrument: dict[str, Any] | None, *, verified: bool = False, comparison_instrument: dict[str, Any] | None = None) -> dict[str, Any]:
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
    comparison = comparison_instrument if isinstance(comparison_instrument, dict) else {}
    comparison_valid = (
        comparison.get("instId") == INSTRUMENT_ID
        and comparison.get("state") == "live"
        and _number(comparison.get("ctVal")) is not None
        and comparison.get("ctValCcy") not in (None, "")
        and comparison.get("ctType") not in (None, "")
    )
    comparison_summary = {
        "instId": comparison.get("instId"),
        "ctVal": _number(comparison.get("ctVal")),
        "ctValCcy": comparison.get("ctValCcy"),
        "ctType": comparison.get("ctType"),
        "ctMult": _number(comparison.get("ctMult")),
        "state": comparison.get("state"),
    } if comparison else None
    api_face_value = (
        api_ctval * api_ctmult
        if api_ctval is not None and api_ctmult is not None and api_ctval_ccy == "ZEC"
        and api_ct_type == "linear" else None
    )
    global_face_value = (
        _number(comparison.get("ctVal")) * (_number(comparison.get("ctMult")) or 1.0)
        if comparison_valid and comparison.get("ctValCcy") == "ZEC"
        and comparison.get("ctType") == "linear" else None
    )
    ui_api_match = (
        verified and row.get("instId") == INSTRUMENT_ID and row.get("state") == "live"
        and api_ctval == ACCOUNT_API_CTVAL_ZEC_PER_CONTRACT and api_ctmult == CT_MULT
        and api_ctval_ccy == "ZEC" and bool(api_ct_type)
        and api_lot == LOT_SIZE and api_min == MIN_SIZE
        and api_tick == TICK_SIZE_USD and api_settle == SETTLE_CCY
    )
    reconciled = ui_api_match and CTVAL_RECONCILED
    return {
        "instrument_id": INSTRUMENT_ID,
        "ctVal_api_raw": api_ctval,
        "account_api_ctVal_observed_zec": ACCOUNT_API_CTVAL_ZEC_PER_CONTRACT,
        "ctValCcy_api": api_ctval_ccy,
        "ctType_api": api_ct_type,
        "ctVal_unit_interpretation": ("ZEC" if api_ctval_ccy == "ZEC" else "NON DETERMINATA: verificare ctValCcy" if not api_ctval_ccy else f"ctVal espresso in {api_ctval_ccy}, non convertito in ZEC"),
        "api_face_value_zec_diagnostic_only": api_face_value,
        "global_metadata_comparison": comparison_summary,
        "global_metadata_comparison_valid": bool(comparison_valid),
        "global_api_face_value_zec_diagnostic_only": global_face_value,
        "regional_api_values_match": bool(
            comparison_valid and api_ctval == _number(comparison.get("ctVal"))
            and api_ctval_ccy == comparison.get("ctValCcy")
            and api_ct_type == comparison.get("ctType")
            and api_ctmult == _number(comparison.get("ctMult"))
        ),
        "ctMult_api": api_ctmult,
        "lotSz_api": api_lot,
        "minSz_api": api_min,
        "tickSz_api_usd": api_tick,
        "settleCcy_api": api_settle,
        "ctVal_zec_per_contract": CTVAL_ZEC_PER_CONTRACT if reconciled else None,
        "ctVal_official_spec_zec": OFFICIAL_SPEC_CTVAL_ZEC_PER_CONTRACT,
        "official_support_confirmed_contract_size_zec": OFFICIAL_SPEC_CTVAL_ZEC_PER_CONTRACT,
        "official_support_confirmation_received": OFFICIAL_SUPPORT_CONFIRMATION_RECEIVED,
        "official_support_confirmation_date": OFFICIAL_SUPPORT_CONFIRMATION_DATE,
        "official_support_confirmation_scope": "OKX conferma la specifica pubblica per ZEC-USD_UM_XPERP: 0.1 ZEC/contratto; non conferma i campi API effettivi dell'account né la causa del valore 0.01",
        "ctVal_ui_api_match": bool(ui_api_match),
        "official_spec_zec_per_contract": OFFICIAL_SPEC_CTVAL_ZEC_PER_CONTRACT,
        "raw_metadata_complete": all(row.get(key) not in (None, "") for key in ("ctVal", "ctMult", "ctValCcy", "ctType", "lotSz", "minSz", "tickSz", "settleCcy")),
        "ctVal_status": (
            "RISOLTO" if reconciled else
            "SPECIFICA_UFFICIALE_0_1_CONFERMATA_API_ACCOUNT_DISCORDANTE" if ui_api_match
            else "SPECIFICA_UFFICIALE_0_1_CONFERMATA_API_NON_VERIFICATA"
        ),
        "confirmation_scope": "solo ZEC-USD_UM_XPERP-310530 dell'account mostrato; non generalizzare ad altre scadenze o account",
        "confirmation_evidence": (
            "Assistenza OKX: per ZEC-USD_UM_XPERP-310530 la specifica pubblica indica 0.1 ZEC/contratto e raccomanda di usare tale entità nei calcoli. La stessa risposta dichiara di non poter confermare ctVal/ctMult/ctValCcy/ctType effettivi dell'account né spiegare lo 0.01. UI/API account-scoped riportano 0.01 ZEC: discordanza non riconciliata."
            if ui_api_match else
            "Assistenza OKX conferma 0.1 ZEC/contratto nella specifica pubblica; metadati API account-scoped assenti, non freschi o non verificabili."
        ),
        "historical_document_note": (
            "OKX support ha confermato che l'entità contrattuale ufficiale da usare come riferimento è 0.1 ZEC/contratto. L'API account-scoped riporta ctVal=0.01 ZEC; l'assistenza non ha confermato i campi effettivi del conto né spiegato la discrepanza. Il riferimento ufficiale 0.1 è registrato, ma il sizing resta bloccato finché non è verificata la semantica operativa dell'API/account."
        ),
        "metadata_verified": bool(ui_api_match),
        "ctVal_reconciled": bool(reconciled),
        "sizing_allowed": bool(reconciled),
        "theoretical_sizing_permitted": bool(reconciled),
        "position_sizing_enabled": False,
        "position_sizing_note": "Bloccato: OKX support conferma 0.1 ZEC come entità contrattuale ufficiale, ma non conferma i campi API effettivi dell'account né spiega ctVal=0.01. Non tradurre manualmente ctVal API in quantità/contratti finché la semantica operativa non è verificata.",
        "max_risk_usdc": MAX_RISK_USDC,
        "margin_or_exposure_calculated": False,
        "orders_enabled": False,
    }
