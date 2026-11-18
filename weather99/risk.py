"""Account vetoes are independent of signal quality."""

from .config import StrategyConfig
from .domain import SHANGHAI, RiskState, timestamp


def check_risk(state: RiskState | None, as_of: str, config: StrategyConfig) -> tuple[str, ...]:
    if state is None:
        return ("risk_state_missing",)
    reasons = []
    if state.trading_date != timestamp(as_of).astimezone(SHANGHAI).date().isoformat():
        reasons.append("risk_state_wrong_day")
    if state.daily_realized_usd <= config.daily_loss_limit_usd:
        reasons.append("daily_loss_limit")
    if state.consecutive_losses >= config.consecutive_loss_limit:
        reasons.append("consecutive_loss_limit")
    if 1 - state.equity_usd / state.peak_equity_usd >= config.drawdown_halt - 1e-12:
        reasons.append("drawdown_halt")
    return tuple(reasons)
