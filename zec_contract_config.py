#!/usr/bin/env python3
"""Account-scoped, read-only contract metadata and risk policy for ZEC X-Perp."""
from __future__ import annotations
from typing import Any

INSTRUMENT_ID = "ZEC-USD_UM_XPERP-310530"
OFFICIAL_SPEC_CTVAL_ZEC_PER_CONTRACT = 0.1
ACCOUNT_API_CTVAL_ZEC_PER_CONTRACT = 0.01
CTVAL_ZEC_PER_CONTRACT: float | None = None  # Do not use until OKX reconciles the discrepancy
CTVAL_RECONCILED = False
OFFICIAL_SUPPORT_CONFIRMATION_RECEIVED = True
OFFICIAL_SUPPORT_CONFIRMATION_DATE = "2026-11-16"
CT_MULT = 1.0
LOT_SIZE = 1.0
MIN_SIZE = 1.0
TICK_SIZE_USD = 0.01
SETTLE_CCY = "USD"
MAX_RISK_USDC = 15.0
FUNDING_INTERVAL_HOURS = 4.0
UI_CONFIRMATION_DATE = "2026-11-15"


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if number == number and abs(number) != float("inf") else None
    except (TypeError, ValueError):
        return None


def _linear_zec_contract_value(instrument: dict[str, Any] | None, *, verified: bool) -> float | None:
    """Return ZEC units per contract only for the exact linear ZEC instrument."""
    row = instrument if isinstance(instrument, dict) else {}
    if not verified or not CTVAL_RECONCILED or CTVAL_ZEC_PER_CONTRACT is None:
        return None
    ctval = _number(row.get("ctVal"))
    multiplier = _number(row.get("ctMult"))
    lot_size = _number(row.get("lotSz"))
    min_size = _number(row.get("minSz"))
    if (row.get("instId") != INSTRUMENT_ID or row.get("state") != "live"
            or row.get("ctType") != "linear"
            or row.get("ctValCcy") != "ZEC" or row.get("settleCcy") != SETTLE_CCY
            or ctval != CTVAL_ZEC_PER_CONTRACT or multiplier != CT_MULT
            or ctval <= 0 or multiplier <= 0 or lot_size is None or lot_size <= 0
            or min_size is None or min_size <= 0
            or lot_size != LOT_SIZE or min_size != MIN_SIZE):
        return None
    return ctval * multiplier


def zec_for_contracts(contracts: float, instrument: dict[str, Any], *, verified: bool = False) -> float | None:
    """Convert contract count to ZEC using the exact instrument API fields."""
    count = _number(contracts)
    value = _linear_zec_contract_value(instrument, verified=verified)
    row = instrument if isinstance(instrument, dict) else {}
    lot_size, min_size = _number(row.get("lotSz")), _number(row.get("minSz"))
    if (count is None or value is None or lot_size is None or min_size is None
            or count < min_size or abs(count / lot_size - round(count / lot_size)) > 1e-9):
        return None
    return count * value


def usd_notional_for_contracts(contracts: float, price_usd: float,
                               instrument: dict[str, Any], *, verified: bool = False) -> float | None:
    """Linear USD-quoted notional; this helper does not enable position sizing."""
    price = _number(price_usd)
    zec = zec_for_contracts(contracts, instrument, verified=verified)
    return zec * price if zec is not None and price is not None and price > 0 else None


def usd_pnl_for_contracts(contracts: float, entry_price: float, exit_price: float,
                          side: str, instrument: dict[str, Any], *, verified: bool = False) -> float | None:
    """Gross linear PnL before fees/funding; this helper never submits orders."""
    entry, exit_price = _number(entry_price), _number(exit_price)
    zec = zec_for_contracts(contracts, instrument, verified=verified)
    if zec is None or entry is None or exit_price is None or entry <= 0 or exit_price <= 0:
        return None
    if side not in ("LONG", "SHORT"):
        return None
    direction = 1.0 if side == "LONG" else -1.0
    return direction * (exit_price - entry) * zec


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
        if verified and api_ctval is not None and api_ctmult is not None and api_ctval_ccy == "ZEC"
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
    reconciled = ui_api_match and CTVAL_RECONCILED and CTVAL_ZEC_PER_CONTRACT is not None
    if not verified:
        ctval_status = "METADATI_API_NON_VERIFICATI"
    elif not ui_api_match:
        ctval_status = "METADATI_API_DISCORDANTI"
    elif not reconciled:
        ctval_status = "DISCREPANZA_CTVAL_NON_RISOLTA"
    else:
        ctval_status = "VALORI_OKX_CONFERMATI"
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
        "ctVal_okx_confirmed_zec": OFFICIAL_SPEC_CTVAL_ZEC_PER_CONTRACT,
        "ctVal_official_spec_zec": OFFICIAL_SPEC_CTVAL_ZEC_PER_CONTRACT,
        "ctVal_account_api_observed_zec": ACCOUNT_API_CTVAL_ZEC_PER_CONTRACT,
        "ctMult_account_api_observed": CT_MULT,
        "official_support_confirmed_contract_size_zec": OFFICIAL_SPEC_CTVAL_ZEC_PER_CONTRACT,
        "official_support_confirmation_received": OFFICIAL_SUPPORT_CONFIRMATION_RECEIVED,
        "official_support_confirmation_date": OFFICIAL_SUPPORT_CONFIRMATION_DATE,
        "official_support_confirmation_scope": "OKX conferma la specifica pubblica ctVal=0.1 ZEC/contratto. L'API account-specifica riporta ctVal=0.01 ZEC e ctMult=1; OKX non ha spiegato la discrepanza.",
        "ctVal_ui_api_match": bool(ui_api_match),
        "official_spec_zec_per_contract": OFFICIAL_SPEC_CTVAL_ZEC_PER_CONTRACT,
        "raw_metadata_complete": all(row.get(key) not in (None, "") for key in ("ctVal", "ctMult", "ctValCcy", "ctType", "lotSz", "minSz", "tickSz", "settleCcy")),
        "ctVal_status": (
            ctval_status
        ),
        "confirmation_scope": "solo ZEC-USD_UM_XPERP-310530 dell'account mostrato; non generalizzare ad altre scadenze o account",
        "confirmation_evidence": (
            "L'API fresca dell'esatto strumento riporta ctVal=0.01 ZEC e ctMult=1. La specifica pubblica OKX conferma 0.1 ZEC/contratto; la discrepanza non è chiarita e non abilita conversioni operative."
            if ui_api_match else
            "La specifica pubblica OKX conferma 0.1 ZEC/contratto. I metadati account-specifici sono mancanti, non freschi o discordanti; conversioni operative disabilitate."
        ),
        "historical_document_note": (
            "Discrepanza aperta: specifica pubblica OKX 0.1 ZEC/contratto; API account-specifica ctVal=0.01 ZEC e ctMult=1. Nessun valore è operativo finché OKX non spiega la differenza."
        ),
        "metadata_verified": bool(ui_api_match),
        "ctVal_reconciled": bool(reconciled),
        "sizing_allowed": False,
        "theoretical_sizing_permitted": False,
        "position_sizing_enabled": False,
        "position_sizing_note": "Bloccato: specifica pubblica OKX ctVal=0.1 ZEC e API account-specifica ctVal=0.01 ZEC discordano. CTVAL_RECONCILED=false; nessuna conversione operativa, quantità, margine o esposizione fino al chiarimento OKX.",
        "max_risk_usdc": MAX_RISK_USDC,
        "margin_or_exposure_calculated": False,
        "orders_enabled": False,
    }
