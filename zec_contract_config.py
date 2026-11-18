#!/usr/bin/env python3
"""Account-scoped, read-only contract metadata and risk policy for ZEC X-Perp."""
from __future__ import annotations
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

INSTRUMENT_ID = "ZEC-USD_UM_XPERP-310530"
OFFICIAL_SPEC_CTVAL_ZEC_PER_CONTRACT = 0.01
ACCOUNT_API_CTVAL_ZEC_PER_CONTRACT = 0.01
# Verified contract basis for diagnostics. This does not grant sizing permission.
CTVAL_ZEC_PER_CONTRACT: float | None = 0.01
CTVAL_RECONCILED = False
OFFICIAL_SUPPORT_CONFIRMATION_RECEIVED = False
OFFICIAL_SUPPORT_CONFIRMATION_DATE = None
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


def _decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _linear_zec_contract_value(instrument: dict[str, Any] | None, *, verified: bool) -> Decimal | None:
    """Return the verified diagnostic ZEC face value for the exact linear contract."""
    row = instrument if isinstance(instrument, dict) else {}
    if not verified or CTVAL_ZEC_PER_CONTRACT is None:
        return None
    ctval = _decimal(row.get("ctVal"))
    multiplier = _decimal(row.get("ctMult"))
    lot_size = _decimal(row.get("lotSz"))
    min_size = _decimal(row.get("minSz"))
    configured_value = _decimal(CTVAL_ZEC_PER_CONTRACT)
    if (row.get("instId") != INSTRUMENT_ID or row.get("state") != "live"
            or row.get("ctType") != "linear"
            or row.get("ctValCcy") != "ZEC" or row.get("settleCcy") != SETTLE_CCY
            or ctval != _decimal(OFFICIAL_SPEC_CTVAL_ZEC_PER_CONTRACT)
            or ctval != configured_value or multiplier != _decimal(CT_MULT)
            or ctval is None or multiplier is None or ctval <= 0 or multiplier <= 0
            or lot_size != _decimal(LOT_SIZE) or min_size != _decimal(MIN_SIZE)):
        return None
    return ctval * multiplier


def zec_for_contracts(contracts: float, instrument: dict[str, Any], *, verified: bool = False) -> float | None:
    """Diagnostic linear conversion; it does not authorize position sizing."""
    count = _decimal(contracts)
    value = _linear_zec_contract_value(instrument, verified=verified)
    row = instrument if isinstance(instrument, dict) else {}
    lot_size, min_size = _decimal(row.get("lotSz")), _decimal(row.get("minSz"))
    if (count is None or value is None or lot_size is None or min_size is None
            or count < min_size or count % lot_size != 0):
        return None
    return float(count * value)


def usd_notional_for_contracts(contracts: float, price_usd: float,
                               instrument: dict[str, Any], *, verified: bool = False) -> float | None:
    """Diagnostic linear USD notional: contracts × ctVal × ctMult × price."""
    price = _decimal(price_usd)
    zec = zec_for_contracts(contracts, instrument, verified=verified)
    return float(_decimal(zec) * price) if zec is not None and price is not None and price > 0 else None


def usd_pnl_for_contracts(contracts: float, entry_price: float, exit_price: float,
                          side: str, instrument: dict[str, Any], *, verified: bool = False) -> float | None:
    """Diagnostic gross linear PnL before fees/funding; never submits orders."""
    entry, exit_price = _decimal(entry_price), _decimal(exit_price)
    zec = zec_for_contracts(contracts, instrument, verified=verified)
    if zec is None or entry is None or exit_price is None or entry <= 0 or exit_price <= 0:
        return None
    if side not in ("LONG", "SHORT"):
        return None
    direction = Decimal("1") if side == "LONG" else Decimal("-1")
    return float(direction * (exit_price - entry) * _decimal(zec))


def trading_fee_usd_for_contracts(contracts: float, fill_price: float, fee_rate: float,
                                  instrument: dict[str, Any], *, verified: bool = False) -> float | None:
    """Diagnostic fee = linear USD notional × explicitly supplied account fee rate."""
    rate = _decimal(fee_rate)
    notional = usd_notional_for_contracts(contracts, fill_price, instrument, verified=verified)
    return float(_decimal(notional) * rate) if notional is not None and rate is not None and rate >= 0 else None


def funding_cashflow_usd_for_contracts(contracts: float, mark_price: float, funding_rate: float,
                                       side: str, instrument: dict[str, Any], *, verified: bool = False) -> float | None:
    """Diagnostic cash flow to the position: positive means received by that side."""
    if side not in ("LONG", "SHORT"):
        return None
    rate = _decimal(funding_rate)
    notional = usd_notional_for_contracts(contracts, mark_price, instrument, verified=verified)
    if notional is None or rate is None:
        return None
    transfer_to_long = -_decimal(notional) * rate
    return float(transfer_to_long if side == "LONG" else -transfer_to_long)


def initial_margin_usd_for_contracts(contracts: float, mark_price: float, leverage: float,
                                     instrument: dict[str, Any], *, verified: bool = False) -> float | None:
    """Formula-only initial-margin diagnostic; leverage must be supplied explicitly."""
    leverage_value = _decimal(leverage)
    notional = usd_notional_for_contracts(contracts, mark_price, instrument, verified=verified)
    if notional is None or leverage_value is None or leverage_value <= 0:
        return None
    return float(_decimal(notional) / leverage_value)


def maintenance_margin_usd_for_contracts(contracts: float, mark_price: float, maintenance_rate: float,
                                         instrument: dict[str, Any], *, verified: bool = False) -> float | None:
    """Formula-only maintenance-margin diagnostic using an explicitly supplied tier rate."""
    rate = _decimal(maintenance_rate)
    notional = usd_notional_for_contracts(contracts, mark_price, instrument, verified=verified)
    if notional is None or rate is None or rate < 0:
        return None
    return float(_decimal(notional) * rate)


def round_price_to_tick(price: float, instrument: dict[str, Any], *, rounding=ROUND_HALF_UP) -> float | None:
    """Quantize a price to the instrument tick; order-side policy remains external."""
    value = _decimal(price)
    tick = _decimal(instrument.get("tickSz")) if isinstance(instrument, dict) else None
    if value is None or tick is None or value <= 0 or tick <= 0:
        return None
    units = (value / tick).quantize(Decimal("1"), rounding=rounding)
    return float(units * tick)


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
    global_ctmult = _number(comparison.get("ctMult"))
    global_face_value = (
        _number(comparison.get("ctVal")) * global_ctmult
        if comparison_valid and comparison.get("ctValCcy") == "ZEC"
        and comparison.get("ctType") == "linear" and global_ctmult is not None and global_ctmult > 0 else None
    )
    ui_api_match = (
        verified and row.get("instId") == INSTRUMENT_ID and row.get("state") == "live"
        and api_ctval == ACCOUNT_API_CTVAL_ZEC_PER_CONTRACT and api_ctmult == CT_MULT
        and api_ctval_ccy == "ZEC" and api_ct_type == "linear"
        and api_lot == LOT_SIZE and api_min == MIN_SIZE
        and api_tick == TICK_SIZE_USD and api_settle == SETTLE_CCY
    )
    comparison_matches_spec = (
        not comparison
        or (comparison_valid and _number(comparison.get("ctVal")) == OFFICIAL_SPEC_CTVAL_ZEC_PER_CONTRACT
            and _number(comparison.get("ctMult")) == CT_MULT
            and comparison.get("ctValCcy") == "ZEC" and comparison.get("ctType") == "linear")
    )
    value_verified = (
        ui_api_match and api_ctval == OFFICIAL_SPEC_CTVAL_ZEC_PER_CONTRACT
        and api_ctmult == CT_MULT and CTVAL_ZEC_PER_CONTRACT == OFFICIAL_SPEC_CTVAL_ZEC_PER_CONTRACT
        and comparison_matches_spec
    )
    reconciled = bool(value_verified and CTVAL_RECONCILED)
    if not verified:
        ctval_status = "METADATI_API_NON_VERIFICATI"
    elif not ui_api_match:
        ctval_status = "METADATI_API_DISCORDANTI"
    elif not value_verified:
        ctval_status = "FONTI_CTVAL_DISCORDANTI"
    elif not reconciled:
        ctval_status = "CTVAL_VERIFICATO_SIZING_BLOCCATO"
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
        "ctVal_zec_per_contract": OFFICIAL_SPEC_CTVAL_ZEC_PER_CONTRACT if value_verified else None,
        "ctVal_okx_confirmed_zec": OFFICIAL_SPEC_CTVAL_ZEC_PER_CONTRACT,
        "ctVal_official_spec_zec": OFFICIAL_SPEC_CTVAL_ZEC_PER_CONTRACT,
        "ctVal_account_api_observed_zec": ACCOUNT_API_CTVAL_ZEC_PER_CONTRACT,
        "ctMult_account_api_observed": CT_MULT,
        "official_support_confirmed_contract_size_zec": OFFICIAL_SPEC_CTVAL_ZEC_PER_CONTRACT,
        "official_support_confirmation_received": OFFICIAL_SUPPORT_CONFIRMATION_RECEIVED,
        "official_support_confirmation_date": OFFICIAL_SUPPORT_CONFIRMATION_DATE,
        "official_support_confirmation_scope": "Nessuna nuova conferma dell'assistenza OKX; la verifica si basa sulla specifica pubblica OEM/EEA aggiornata e sui metadati API pubblici dell'esatto instId.",
        "ctVal_source_verified": bool(value_verified),
        "ctVal_ui_api_match": bool(ui_api_match),
        "official_spec_zec_per_contract": OFFICIAL_SPEC_CTVAL_ZEC_PER_CONTRACT,
        "raw_metadata_complete": all(row.get(key) not in (None, "") for key in ("ctVal", "ctMult", "ctValCcy", "ctType", "lotSz", "minSz", "tickSz", "settleCcy")),
        "ctVal_status": (
            ctval_status
        ),
        "confirmation_scope": "solo ZEC-USD_UM_XPERP-310530 secondo specifica OEM/EEA e metadati API pubblici freschi; non generalizzare ad altre scadenze o regioni",
        "confirmation_evidence": (
            "La specifica OKX OEM/EEA aggiornata il 2026-11-17 e le API pubbliche EEA/global dell'esatto instId concordano su 0.01 ZEC/contratto, ctMult=1. Il CTVAL è verificato come base matematica; CTVAL_RECONCILED=false e sizing/ordini restano disabilitati fino al completamento delle verifiche indipendenti."
            if value_verified else
            "CTVAL non verificato: mancano metadati freschi o le fonti non concordano. Conversioni, sizing, margine ed ordini restano disabilitati."
        ),
        "historical_document_note": (
            "La precedente specifica 0.1 ZEC/contratto è stata superata per OEM/EEA: la specifica pubblica aggiornata al 2026-11-17 riporta 0.01 ZEC. Il vecchio valore non è usato nel radar."
        ),
        "metadata_verified": bool(ui_api_match),
        "ctVal_reconciled": bool(reconciled),
        "sizing_allowed": False,
        "theoretical_sizing_permitted": False,
        "position_sizing_enabled": False,
        "position_sizing_note": "CTVAL documentato a 0.01 ZEC/contratto, ctMult=1; sizing comunque bloccato. CTVAL_RECONCILED=false e verifiche indipendenti su formule, fee, funding e margine non completano alcuna autorizzazione operativa.",
        "max_risk_usdc": MAX_RISK_USDC,
        "margin_or_exposure_calculated": False,
        "orders_enabled": False,
    }
