"""Illustrative execution costs for ZEC X-Perp plans and simulations."""

FEE_BPS_PER_SIDE = 5
ADVERSE_EXIT_SLIPPAGE_BPS = 2


def execution_pnl(side, entry, exit_price, *, exit_slippage_bps=0):
    """Return net P&L and fees in USD per ZEC, before any funding."""
    sign = 1 if side == "LONG" else -1
    execution = exit_price * (1 - sign * exit_slippage_bps / 10_000)
    fees = (entry + execution) * FEE_BPS_PER_SIDE / 10_000
    return sign * (execution - entry) - fees, fees


def limit_economics(plan):
    entry, stop, tp1 = (plan[key] for key in ("entry", "stop", "tp1"))
    profit, fees = execution_pnl(plan["side"], entry, tp1)
    loss, _ = execution_pnl(plan["side"], entry, stop, exit_slippage_bps=ADVERSE_EXIT_SLIPPAGE_BPS)
    return {
        "tp1_net_pct": profit / entry * 100,
        "stop_loss_net_pct": -loss / entry * 100,
        "tp1_fee_share_pct": fees / abs(tp1 - entry) * 100,
        "net_reward_risk": profit / -loss,
        "fee_bps_per_side": FEE_BPS_PER_SIDE,
        "exit_slippage_bps": ADVERSE_EXIT_SLIPPAGE_BPS,
    }
