from unittest.mock import MagicMock, patch

from dashboard.ui import (
    _render_recent_events,
    _render_risk_parameters,
    render_custom_dashboard,
)


class _DummyColumn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _mock_columns(spec):
    count = spec if isinstance(spec, int) else len(spec)
    return [_DummyColumn() for _ in range(count)]


def test_render_custom_dashboard_import_and_runs():
    strategy_config = {
        "chain": "polygon",
        "protocol": "pancakeswap_v3",
        "tokens": {
            "base": {"symbol": "USD1"},
            "quote": {"symbol": "USDT"},
        },
        "sizing": {"minPositionUsd": 100, "maxPositionUsd": 2000},
        "risk": {"stopLossPct": -0.1, "maxSlippageBps": 50},
        "exit": {"takeProfitPcts": [0.1, 0.2], "timeHorizonHours": 72},
    }

    with (
        patch("dashboard.ui.st.title") as title,
        patch("dashboard.ui.st.markdown"),
        patch("dashboard.ui.st.divider"),
        patch("dashboard.ui.st.subheader"),
        patch("dashboard.ui.st.columns", side_effect=_mock_columns),
        patch("dashboard.ui.st.metric"),
        patch("dashboard.ui.st.info"),
        patch("dashboard.ui.st.warning"),
        patch("dashboard.ui.st.success"),
    ):
        render_custom_dashboard(
            strategy_id="liq-frag-1",
            strategy_config=strategy_config,
            api_client=None,
            session_state={},
        )

    title.assert_called_once()


def test_render_risk_parameters_constructs_metrics():
    metrics = []

    def _capture_metric(label, value, **kwargs):
        metrics.append((label, value))

    with (
        patch("dashboard.ui.st.columns", side_effect=_mock_columns),
        patch("dashboard.ui.st.metric", side_effect=_capture_metric),
    ):
        _render_risk_parameters(
            {
                "sizing": {"minPositionUsd": 120, "maxPositionUsd": 2500},
                "risk": {"stopLossPct": -0.08, "maxSlippageBps": 40},
                "exit": {"takeProfitPcts": [0.12, 0.25], "timeHorizonHours": 48},
            }
        )

    labels = {label for label, _ in metrics}
    assert "Min Position" in labels
    assert "Max Position" in labels
    assert "Stop Loss" in labels
    assert "TP1" in labels
    assert "TP2" in labels
    assert "Time Exit" in labels


def test_recent_events_handles_api_failure():
    api_client = MagicMock()
    api_client.get_timeline.side_effect = RuntimeError("gateway unavailable")

    with patch("dashboard.ui.st.warning") as warning:
        _render_recent_events(api_client, "liq-frag-1")

    warning.assert_called_once()
