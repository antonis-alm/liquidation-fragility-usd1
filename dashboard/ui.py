from typing import Any

from almanak.framework.dashboard.templates import get_rsi_config, render_ta_dashboard


def _build_dashboard_config(strategy_config: dict[str, Any]):
    return get_rsi_config(
        period=int(strategy_config.get("rsi_period", 14)),
        overbought=float(strategy_config.get("rsi_entry_max", 68)),
        oversold=float(strategy_config.get("rsi_entry_min", 52)),
    )


def render_custom_dashboard(
    strategy_id: str,
    strategy_config: dict[str, Any],
    api_client: Any,
    session_state: dict[str, Any],
) -> None:
    _ = api_client
    config = _build_dashboard_config(strategy_config)
    render_ta_dashboard(strategy_id, strategy_config, session_state, config)
