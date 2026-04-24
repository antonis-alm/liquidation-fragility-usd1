from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import streamlit as st


def render_custom_dashboard(
    strategy_id: str,
    strategy_config: dict[str, Any],
    api_client: Any,
    session_state: dict[str, Any],
) -> None:
    st.title("Liquidation Fragility USD1 Dashboard")

    chain = strategy_config.get("chain", "polygon")
    protocol = strategy_config.get("protocol", "pancakeswap_v3")
    tokens = strategy_config.get("tokens", {})
    base_symbol = tokens.get("base", {}).get("symbol", "USD1") if isinstance(tokens, dict) else "USD1"
    quote_symbol = tokens.get("quote", {}).get("symbol", "USDT") if isinstance(tokens, dict) else "USDT"

    st.markdown(f"**Strategy ID:** `{strategy_id}`")
    st.markdown(f"**Pair:** {base_symbol}/{quote_symbol}")
    st.markdown(f"**Protocol:** {protocol}")
    st.markdown(f"**Chain:** {chain.upper()}")

    st.divider()
    st.subheader("Position State")
    _render_position_state(session_state)

    st.divider()
    st.subheader("Risk & Exit Parameters")
    _render_risk_parameters(strategy_config)

    st.divider()
    st.subheader("Recent Execution Events")
    _render_recent_events(api_client, strategy_id)


def _render_position_state(session_state: dict[str, Any]) -> None:
    in_position = bool(session_state.get("in_position", False))
    entry_price = Decimal(str(session_state.get("entry_price", "0") or "0"))
    tp1_hit = bool(session_state.get("tp1_hit", False))
    tp2_hit = bool(session_state.get("tp2_hit", False))

    entry_time_raw = session_state.get("entry_time")
    entry_time = "N/A"
    if entry_time_raw:
        try:
            parsed = datetime.fromisoformat(str(entry_time_raw).replace("Z", "+00:00")).astimezone(UTC)
            entry_time = parsed.strftime("%Y-%m-%d %H:%M:%S UTC")
        except Exception:
            entry_time = str(entry_time_raw)

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("In Position", "Yes" if in_position else "No")
    with col2:
        st.metric("Entry Price", f"${float(entry_price):.6f}" if entry_price > 0 else "N/A")
    with col3:
        st.metric("TP1 Hit", "Yes" if tp1_hit else "No")
    with col4:
        st.metric("TP2 Hit", "Yes" if tp2_hit else "No")

    st.markdown(f"**Entry Time:** {entry_time}")

    if in_position and not tp1_hit:
        st.info("Position open. Monitoring stop-loss, TP1, TP2, and time horizon exits.")
    elif in_position and tp1_hit and not tp2_hit:
        st.warning("TP1 executed. Position partially reduced and waiting for TP2/time-stop/stop-loss.")
    elif in_position and tp2_hit:
        st.success("TP2 hit state detected.")
    else:
        st.info("No active position. Strategy waits for active fragility signal and sizing checks.")


def _render_risk_parameters(strategy_config: dict[str, Any]) -> None:
    sizing = strategy_config.get("sizing", {}) if isinstance(strategy_config.get("sizing"), dict) else {}
    risk = strategy_config.get("risk", {}) if isinstance(strategy_config.get("risk"), dict) else {}
    exit_cfg = strategy_config.get("exit", {}) if isinstance(strategy_config.get("exit"), dict) else {}

    min_position_usd = Decimal(str(sizing.get("minPositionUsd", "100")))
    max_position_usd = Decimal(str(sizing.get("maxPositionUsd", "2000")))
    stop_loss_pct = Decimal(str(risk.get("stopLossPct", "-0.1"))) * Decimal("100")
    max_slippage_bps = Decimal(str(risk.get("maxSlippageBps", "50")))

    take_profit_pcts = exit_cfg.get("takeProfitPcts", [0.1, 0.2])
    tp1_pct = Decimal(str(take_profit_pcts[0] if len(take_profit_pcts) > 0 else "0.1")) * Decimal("100")
    tp2_pct = Decimal(str(take_profit_pcts[1] if len(take_profit_pcts) > 1 else "0.2")) * Decimal("100")
    time_horizon_hours = int(exit_cfg.get("timeHorizonHours", 72))

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Min Position", f"${float(min_position_usd):,.0f}")
        st.metric("Max Position", f"${float(max_position_usd):,.0f}")
    with col2:
        st.metric("Stop Loss", f"{float(stop_loss_pct):.1f}%")
        st.metric("Max Slippage", f"{float(max_slippage_bps):.0f} bps")
    with col3:
        st.metric("TP1", f"+{float(tp1_pct):.1f}%")
        st.metric("TP2", f"+{float(tp2_pct):.1f}%")
        st.metric("Time Exit", f"{time_horizon_hours}h")


def _render_recent_events(api_client: Any, strategy_id: str) -> None:
    if not api_client:
        st.info("No API client available. Event timeline is unavailable.")
        return

    try:
        events = api_client.get_timeline(strategy_id, limit=20)
    except Exception as exc:
        st.warning(f"Unable to load timeline: {exc}")
        return

    filtered = [
        event
        for event in events
        if str(event.get("event_type", "")).upper() in {"SWAP", "INTENT_EXECUTED", "HOLD"}
    ]

    if not filtered:
        st.info("No recent events yet.")
        return

    for event in filtered[:8]:
        timestamp = str(event.get("timestamp", "N/A"))
        event_type = str(event.get("event_type", "unknown")).upper()
        details = event.get("details", {}) or {}
        from_token = details.get("from_token")
        to_token = details.get("to_token")
        amount = details.get("amount") or details.get("amount_usd")

        message = f"- `{timestamp[:19] if len(timestamp) > 19 else timestamp}` **{event_type}**"
        if from_token and to_token:
            message += f": {from_token} → {to_token}"
        if amount is not None:
            message += f" (amount: {amount})"
        st.markdown(message)
