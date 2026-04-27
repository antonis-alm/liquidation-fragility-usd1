import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from almanak.framework.intents import Intent
from almanak.framework.strategies import IntentStrategy, MarketSnapshot, almanak_strategy

logger = logging.getLogger(__name__)


@almanak_strategy(
    name="polygon_pol_usdc_hf_momentum",
    description="MATIC/USDC micro-cap momentum strategy with net-of-cost exits",
    version="1.0.0",
    author="Almanak",
    tags=["momentum", "micro-cap", "polygon", "matic", "usdc"],
    supported_chains=["polygon"],
    supported_protocols=["uniswap_v3"],
    intent_types=["SWAP", "HOLD"],
    default_chain="polygon",
)
class PolygonPolUsdcHfMomentumStrategy(IntentStrategy):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.base_token = self._cfg("base_token", "MATIC")
        self.quote_token = self._cfg("quote_token", "USDC")

        self.primary_timeframe = self._cfg("primary_timeframe", "1m")
        self.fallback_timeframe = self._cfg("fallback_timeframe", "5m")
        self.rsi_period = int(self._cfg("rsi_period", 14))
        self.rsi_entry_min = self._d("rsi_entry_min", "52")
        self.rsi_entry_max = self._d("rsi_entry_max", "68")
        self.ema_fast_period = int(self._cfg("ema_fast_period", 9))
        self.ema_slow_period = int(self._cfg("ema_slow_period", 21))
        self.atr_period = int(self._cfg("atr_period", 14))
        self.noise_atr_percent_threshold = self._d("noise_atr_percent_threshold", "0.012")
        self.pullback_min_bps = self._d("pullback_min_bps", "20")

        self.reserve_usdc = self._d("reserve_usdc", "3")
        self.sizing_fraction = self._d("sizing_fraction", "0.70")
        self.min_trade_usd = self._d("min_trade_usd", "4")
        self.max_trade_usd = self._d("max_trade_usd", "12")

        self.dex_fee_bps = self._d("dex_fee_bps", "30")
        self.expected_slippage_bps_entry = self._d("expected_slippage_bps_entry", "20")
        self.expected_slippage_bps_exit = self._d("expected_slippage_bps_exit", "20")
        self.safety_buffer_usd = self._d("safety_buffer_usd", "0.15")
        self.max_roundtrip_cost_ratio = self._d("max_roundtrip_cost_ratio", "0.06")
        self.max_affordable_tp_bps = self._d("max_affordable_tp_bps", "180")
        self.max_gas_ratio = self._d("max_gas_ratio", "0.03")
        self.min_net_tp_usd = self._d("min_net_tp_usd", "0.20")

        self.hard_stop_net_usd = self._d("hard_stop_net_usd", "0.45")
        self.max_hold_minutes = int(self._cfg("max_hold_minutes", 30))
        self.cooldown_minutes = int(self._cfg("cooldown_minutes", 4))
        self.max_consecutive_losses = int(self._cfg("max_consecutive_losses", 3))
        self.loss_pause_minutes = int(self._cfg("loss_pause_minutes", 20))

        self.entry_max_slippage = self._d("entry_max_slippage_bps", "50") / Decimal("10000")
        self.exit_max_slippage = self._d("exit_max_slippage_bps", "70") / Decimal("10000")
        self.hard_stop_slippage = self._d("hard_stop_slippage_bps", "100") / Decimal("10000")

        self.position_state = "FLAT_USDC"
        self.entry_price = Decimal("0")
        self.entry_qty_pol = Decimal("0")
        self.entry_notional_usd = Decimal("0")
        self.entry_cost_usd_paid = Decimal("0")
        self.entry_ts = ""

        self.cooldown_until = ""
        self.paused_until = ""
        self.consecutive_losses = 0

        self._pending_entry_price = Decimal("0")
        self._pending_entry_notional_usd = Decimal("0")
        self._pending_entry_cost_usd = Decimal("0")
        self._pending_entry_ts = ""

        self.last_timeframe_used = self.primary_timeframe
        self.last_reason_code = "INIT"
        self._last_estimated_exit_net_pnl = Decimal("0")
        self._last_market_snapshot: MarketSnapshot | None = None

        self._last_price = Decimal("0")
        self._last_rsi: Decimal | None = None
        self._last_ema_fast: Decimal | None = None
        self._last_ema_slow: Decimal | None = None
        self._last_base_balance = Decimal("0")
        self._last_quote_balance = Decimal("0")
        self._price_history: list[tuple[str, float]] = []
        self._rsi_history: list[tuple[str, float]] = []
        self._buy_signals: list[tuple[str, float]] = []
        self._sell_signals: list[tuple[str, float]] = []

    def decide(self, market: MarketSnapshot) -> Intent:
        self._last_market_snapshot = market
        now = self._now(market)

        try:
            paused_until = self._parse_dt(self.paused_until)
            if paused_until and now < paused_until:
                return self._hold("Loss streak pause active", "LOSS_PAUSE_ACTIVE")

            cooldown_until = self._parse_dt(self.cooldown_until)
            if cooldown_until and now < cooldown_until:
                return self._hold("Cooldown active", "COOLDOWN_ACTIVE")

            quote_balance = market.balance(self.quote_token)
            base_balance = market.balance(self.base_token)
            self._last_quote_balance = self._to_decimal(getattr(quote_balance, "balance", "0"))
            self._last_base_balance = self._to_decimal(getattr(base_balance, "balance", "0"))

            if self.position_state == "LONG_POL":
                return self._decide_exit(market, base_balance, now)
            return self._decide_entry(market, quote_balance, now)
        except Exception as exc:
            logger.exception("decide() failed: %s", exc)
            return self._hold("Market data unavailable", "DATA_UNAVAILABLE")

    def _decide_entry(self, market: MarketSnapshot, quote_balance: Any, now: datetime) -> Intent:
        signal = self._entry_signal(market)
        self._record_dashboard_metrics(now, signal)
        if not signal["ok"]:
            logger.info(
                "entry_signal_debug timeframe=%s price=%s ema_fast=%s ema_slow=%s rsi=%s ema_trend_ok=%s pullback_ok=%s rsi_ok=%s data_ready=%s",
                signal.get("timeframe", self.last_timeframe_used),
                signal.get("price"),
                signal.get("ema_fast"),
                signal.get("ema_slow"),
                signal.get("rsi"),
                signal.get("ema_trend_ok"),
                signal.get("pullback_ok"),
                signal.get("rsi_ok"),
                signal.get("indicator_data_ready"),
            )
            return self._hold(
                "No valid entry signal",
                "NO_ENTRY_SIGNAL",
                reason_details={
                    "timeframe": signal.get("timeframe", self.last_timeframe_used),
                    "price": signal.get("price"),
                    "ema_fast": signal.get("ema_fast"),
                    "ema_slow": signal.get("ema_slow"),
                    "rsi": signal.get("rsi"),
                    "rsi_entry_min": self.rsi_entry_min,
                    "rsi_entry_max": self.rsi_entry_max,
                    "pullback_ok": signal.get("pullback_ok"),
                    "rsi_ok": signal.get("rsi_ok"),
                    "ema_trend_ok": signal.get("ema_trend_ok"),
                    "indicator_data_ready": signal.get("indicator_data_ready"),
                },
            )

        quote_usd = self._to_decimal(getattr(quote_balance, "balance_usd", "0"))
        available_usd = max(Decimal("0"), quote_usd - self.reserve_usdc)
        if available_usd <= Decimal("0"):
            return self._hold("Reserve protection", "INSUFFICIENT_AVAILABLE_USDC")

        sized = available_usd * self.sizing_fraction
        trade_notional = min(self.max_trade_usd, max(self.min_trade_usd, sized), available_usd)
        if trade_notional < self.min_trade_usd:
            return self._hold("Trade notional below minimum", "BELOW_MIN_TRADE")

        gas_cost = self._to_decimal(market.estimate_swap_gas_cost_usd(self.chain))
        costs = self._cost_model(trade_notional, gas_cost)

        if trade_notional > Decimal("0"):
            roundtrip_ratio = costs["roundtrip_total"] / trade_notional
            if roundtrip_ratio > self.max_roundtrip_cost_ratio:
                return self._hold("Roundtrip costs too high", "COST_TOO_HIGH")

        required_gross_bps = ((costs["roundtrip_total"] + self.min_net_tp_usd) / trade_notional) * Decimal("10000")
        if required_gross_bps > self.max_affordable_tp_bps:
            return self._hold("Required TP too large for micro size", "TP_UNREALISTIC")

        if not market.is_trade_worthwhile(
            amount_usd=trade_notional,
            chain=self.chain,
            max_gas_ratio=self.max_gas_ratio,
        ):
            return self._hold("Gas ratio gate rejected trade", "GAS_NOT_WORTH_IT")

        self._pending_entry_price = signal["price"]
        self._pending_entry_notional_usd = trade_notional
        self._pending_entry_cost_usd = costs["entry_total"]
        self._pending_entry_ts = now.isoformat()

        return Intent.swap(
            from_token=self.quote_token,
            to_token=self.base_token,
            amount_usd=trade_notional,
            max_slippage=self.entry_max_slippage,
            protocol="uniswap_v3",
            chain=self.chain,
        )

    def _decide_exit(self, market: MarketSnapshot, base_balance: Any, now: datetime) -> Intent:
        current_price = self._to_decimal(market.price(self.base_token))
        if current_price <= Decimal("0"):
            return self._hold("Invalid price", "DATA_UNAVAILABLE")

        position_qty = self.entry_qty_pol
        if position_qty <= Decimal("0"):
            position_qty = self._to_decimal(getattr(base_balance, "balance", "0"))
        if position_qty <= Decimal("0"):
            return self._hold("No position to manage", "NO_POSITION")

        current_notional = position_qty * current_price
        gas_cost = self._to_decimal(market.estimate_swap_gas_cost_usd(self.chain))
        costs = self._cost_model(max(self.entry_notional_usd, current_notional), gas_cost)

        gross_pnl_usd = (current_price - self.entry_price) * position_qty
        exit_cost_est = costs["exit_total"]
        net_pnl_usd = gross_pnl_usd - self.entry_cost_usd_paid - exit_cost_est
        self._last_estimated_exit_net_pnl = net_pnl_usd

        entry_dt = self._parse_dt(self.entry_ts)
        held_minutes = Decimal("0")
        if entry_dt:
            held_minutes = Decimal(str((now - entry_dt).total_seconds())) / Decimal("60")

        if net_pnl_usd <= -self.hard_stop_net_usd:
            return self._exit_swap(self.hard_stop_slippage)

        if held_minutes >= Decimal(str(self.max_hold_minutes)):
            return self._exit_swap(self.exit_max_slippage)

        required_gross_tp = self.min_net_tp_usd + self.entry_cost_usd_paid + exit_cost_est
        if gross_pnl_usd >= required_gross_tp and net_pnl_usd >= self.min_net_tp_usd:
            return self._exit_swap(self.exit_max_slippage)

        return self._hold("Net TP not reached", "NET_TP_NOT_REACHED")

    def _entry_signal(self, market: MarketSnapshot) -> dict[str, Any]:
        timeframe = self._select_timeframe(market)
        self.last_timeframe_used = timeframe

        price = self._to_decimal(market.price(self.base_token))
        try:
            ema_fast = self._indicator_value(market.ema(self.base_token, period=self.ema_fast_period, timeframe=timeframe))
            ema_slow = self._indicator_value(market.ema(self.base_token, period=self.ema_slow_period, timeframe=timeframe))
            rsi_value = self._indicator_value(market.rsi(self.base_token, period=self.rsi_period, timeframe=timeframe))
        except Exception:
            return {
                "ok": False,
                "timeframe": timeframe,
                "price": price,
                "ema_fast": None,
                "ema_slow": None,
                "rsi": None,
                "pullback_ok": False,
                "rsi_ok": False,
                "ema_trend_ok": False,
                "indicator_data_ready": False,
            }

        ema_trend_ok = ema_fast > ema_slow
        if not ema_trend_ok:
            return {
                "ok": False,
                "timeframe": timeframe,
                "price": price,
                "ema_fast": ema_fast,
                "ema_slow": ema_slow,
                "rsi": rsi_value,
                "pullback_ok": False,
                "rsi_ok": self.rsi_entry_min <= rsi_value <= self.rsi_entry_max,
                "ema_trend_ok": ema_trend_ok,
                "indicator_data_ready": True,
            }

        pullback_threshold = ema_fast * (Decimal("1") - (self.pullback_min_bps / Decimal("10000")))
        pullback_ok = price <= pullback_threshold and price > ema_slow
        rsi_ok = self.rsi_entry_min <= rsi_value <= self.rsi_entry_max

        return {
            "ok": pullback_ok and rsi_ok,
            "timeframe": timeframe,
            "price": price,
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
            "rsi": rsi_value,
            "pullback_ok": pullback_ok,
            "rsi_ok": rsi_ok,
            "ema_trend_ok": ema_trend_ok,
            "indicator_data_ready": True,
        }

    def _select_timeframe(self, market: MarketSnapshot) -> str:
        try:
            atr_data = market.atr(self.base_token, period=self.atr_period, timeframe=self.primary_timeframe)
            atr_pct = getattr(atr_data, "value_percent", None)
            if atr_pct is None:
                atr_value = self._indicator_value(atr_data)
                price = self._to_decimal(market.price(self.base_token))
                atr_pct = atr_value / price if price > Decimal("0") else Decimal("0")
            if self._to_decimal(atr_pct) > self.noise_atr_percent_threshold:
                return self.fallback_timeframe
            return self.primary_timeframe
        except Exception:
            return self.fallback_timeframe

    def _cost_model(self, notional_usd: Decimal, gas_cost_usd: Decimal) -> dict[str, Decimal]:
        fee_entry = notional_usd * self.dex_fee_bps / Decimal("10000")
        fee_exit = notional_usd * self.dex_fee_bps / Decimal("10000")
        slip_entry = notional_usd * self.expected_slippage_bps_entry / Decimal("10000")
        slip_exit = notional_usd * self.expected_slippage_bps_exit / Decimal("10000")

        entry_total = gas_cost_usd + fee_entry + slip_entry
        exit_total = gas_cost_usd + fee_exit + slip_exit + self.safety_buffer_usd
        return {
            "entry_total": entry_total,
            "exit_total": exit_total,
            "roundtrip_total": entry_total + exit_total,
        }

    def _exit_swap(self, slippage: Decimal) -> Intent:
        return Intent.swap(
            from_token=self.base_token,
            to_token=self.quote_token,
            amount="all",
            max_slippage=slippage,
            protocol="uniswap_v3",
            chain=self.chain,
        )

    def on_intent_executed(self, intent, success: bool, result):
        if not success:
            return

        if getattr(intent.intent_type, "value", "") != "SWAP":
            return

        now = datetime.now(UTC)
        from_token = getattr(intent, "from_token", "")
        to_token = getattr(intent, "to_token", "")

        if from_token == self.quote_token and to_token == self.base_token:
            entry_price = self._pending_entry_price
            entry_notional = self._pending_entry_notional_usd
            if entry_price > Decimal("0") and entry_notional > Decimal("0"):
                self.position_state = "LONG_POL"
                self.entry_price = entry_price
                self.entry_notional_usd = entry_notional
                self.entry_qty_pol = entry_notional / entry_price
                self.entry_cost_usd_paid = self._pending_entry_cost_usd
                self.entry_ts = self._pending_entry_ts or now.isoformat()
            self._record_trade_signal(now, "BUY")
            self._pending_entry_price = Decimal("0")
            self._pending_entry_notional_usd = Decimal("0")
            self._pending_entry_cost_usd = Decimal("0")
            self._pending_entry_ts = ""
            return

        if from_token == self.base_token and to_token == self.quote_token:
            self._record_trade_signal(now, "SELL")
            if self._last_estimated_exit_net_pnl < Decimal("0"):
                self.consecutive_losses += 1
            else:
                self.consecutive_losses = 0

            if self.consecutive_losses >= self.max_consecutive_losses:
                pause_until = now + timedelta(minutes=self.loss_pause_minutes)
                self.paused_until = pause_until.isoformat()

            cooldown_until = now + timedelta(minutes=self.cooldown_minutes)
            self.cooldown_until = cooldown_until.isoformat()

            self.position_state = "FLAT_USDC"
            self.entry_price = Decimal("0")
            self.entry_qty_pol = Decimal("0")
            self.entry_notional_usd = Decimal("0")
            self.entry_cost_usd_paid = Decimal("0")
            self.entry_ts = ""

    def get_status(self) -> dict[str, Any]:
        return {
            "strategy": "polygon_pol_usdc_hf_momentum",
            "chain": self.chain,
            "pair": f"{self.base_token}/{self.quote_token}",
            "position_state": self.position_state,
            "last_timeframe_used": self.last_timeframe_used,
            "consecutive_losses": self.consecutive_losses,
            "last_reason_code": self.last_reason_code,
            "base_price": str(self._last_price),
            "base_balance": str(self._last_base_balance),
            "quote_balance": str(self._last_quote_balance),
            "rsi_value": float(self._last_rsi) if self._last_rsi is not None else None,
            "rsi_data": self._rsi_history,
            "price_history": self._price_history,
            "buy_signals": self._buy_signals,
            "sell_signals": self._sell_signals,
            "ema_fast": str(self._last_ema_fast) if self._last_ema_fast is not None else None,
            "ema_slow": str(self._last_ema_slow) if self._last_ema_slow is not None else None,
        }

    def get_persistent_state(self) -> dict[str, Any]:
        return {
            "position_state": self.position_state,
            "entry_price": str(self.entry_price),
            "entry_qty_pol": str(self.entry_qty_pol),
            "entry_notional_usd": str(self.entry_notional_usd),
            "entry_cost_usd_paid": str(self.entry_cost_usd_paid),
            "entry_ts": self.entry_ts,
            "cooldown_until": self.cooldown_until,
            "paused_until": self.paused_until,
            "consecutive_losses": self.consecutive_losses,
            "last_timeframe_used": self.last_timeframe_used,
            "last_reason_code": self.last_reason_code,
            "last_price": str(self._last_price),
            "last_rsi": str(self._last_rsi) if self._last_rsi is not None else None,
            "last_ema_fast": str(self._last_ema_fast) if self._last_ema_fast is not None else None,
            "last_ema_slow": str(self._last_ema_slow) if self._last_ema_slow is not None else None,
            "last_base_balance": str(self._last_base_balance),
            "last_quote_balance": str(self._last_quote_balance),
            "price_history": self._price_history,
            "rsi_history": self._rsi_history,
            "buy_signals": self._buy_signals,
            "sell_signals": self._sell_signals,
        }

    def load_persistent_state(self, state: dict[str, Any]) -> None:
        if not state:
            return
        self.position_state = state.get("position_state", "FLAT_USDC")
        self.entry_price = self._to_decimal(state.get("entry_price", "0"))
        self.entry_qty_pol = self._to_decimal(state.get("entry_qty_pol", "0"))
        self.entry_notional_usd = self._to_decimal(state.get("entry_notional_usd", "0"))
        self.entry_cost_usd_paid = self._to_decimal(state.get("entry_cost_usd_paid", "0"))
        self.entry_ts = state.get("entry_ts", "")
        self.cooldown_until = state.get("cooldown_until", "")
        self.paused_until = state.get("paused_until", "")
        self.consecutive_losses = int(state.get("consecutive_losses", 0))
        self.last_timeframe_used = state.get("last_timeframe_used", self.primary_timeframe)
        self.last_reason_code = state.get("last_reason_code", "LOADED")
        self._last_price = self._to_decimal(state.get("last_price", "0"))
        self._last_rsi = self._opt_decimal(state.get("last_rsi"))
        self._last_ema_fast = self._opt_decimal(state.get("last_ema_fast"))
        self._last_ema_slow = self._opt_decimal(state.get("last_ema_slow"))
        self._last_base_balance = self._to_decimal(state.get("last_base_balance", "0"))
        self._last_quote_balance = self._to_decimal(state.get("last_quote_balance", "0"))
        self._price_history = self._normalize_point_series(state.get("price_history", []))
        self._rsi_history = self._normalize_point_series(state.get("rsi_history", []))
        self._buy_signals = self._normalize_point_series(state.get("buy_signals", []))
        self._sell_signals = self._normalize_point_series(state.get("sell_signals", []))

    def get_open_positions(self):
        from almanak.framework.teardown import PositionInfo, PositionType, TeardownPositionSummary

        balance = Decimal("0")
        if self._last_market_snapshot is not None:
            try:
                balance = self._to_decimal(self._last_market_snapshot.balance(self.base_token).balance)
            except Exception:
                balance = Decimal("0")
        if balance <= Decimal("0"):
            balance = self.entry_qty_pol

        positions = []
        if balance > Decimal("0"):
            positions.append(
                PositionInfo(
                    position_type=PositionType.TOKEN,
                    position_id="pol_position",
                    chain=self.chain,
                    protocol="uniswap_v3",
                    value_usd=Decimal("0"),
                    details={"asset": self.base_token, "balance": str(balance)},
                )
            )

        return TeardownPositionSummary(
            strategy_id=getattr(self, "strategy_id", "polygon_pol_usdc_hf_momentum"),
            timestamp=datetime.now(UTC),
            positions=positions,
        )

    def generate_teardown_intents(self, mode=None, market=None) -> list[Intent]:
        from almanak.framework.teardown import TeardownMode

        balance = Decimal("0")
        snapshots = [market, self._last_market_snapshot]
        for snapshot in snapshots:
            if snapshot is None:
                continue
            try:
                balance = self._to_decimal(snapshot.balance(self.base_token).balance)
                if balance > Decimal("0"):
                    break
            except Exception:
                continue

        if balance <= Decimal("0") and self.entry_qty_pol <= Decimal("0"):
            return []

        max_slippage = Decimal("0.03") if mode == TeardownMode.HARD else self.exit_max_slippage
        return [
            Intent.swap(
                from_token=self.base_token,
                to_token=self.quote_token,
                amount="all",
                max_slippage=max_slippage,
                protocol="uniswap_v3",
                chain=self.chain,
            )
        ]

    def _record_dashboard_metrics(self, now: datetime, signal: dict[str, Any]) -> None:
        price = signal.get("price")
        if price is not None:
            self._last_price = self._to_decimal(price)
            self._append_point(self._price_history, now.isoformat(), self._last_price)

        rsi_value = self._opt_decimal(signal.get("rsi"))
        if rsi_value is not None:
            self._last_rsi = rsi_value
            self._append_point(self._rsi_history, now.isoformat(), rsi_value)

        self._last_ema_fast = self._opt_decimal(signal.get("ema_fast"))
        self._last_ema_slow = self._opt_decimal(signal.get("ema_slow"))

    def _record_trade_signal(self, now: datetime, side: str) -> None:
        if self._last_price <= Decimal("0"):
            return
        target = self._buy_signals if side == "BUY" else self._sell_signals
        self._append_point(target, now.isoformat(), self._last_price, max_points=200)

    def _append_point(
        self,
        series: list[tuple[str, float]],
        timestamp: str,
        value: Decimal,
        max_points: int = 600,
    ) -> None:
        series.append((timestamp, float(value)))
        if len(series) > max_points:
            del series[:-max_points]

    def _normalize_point_series(self, raw: Any) -> list[tuple[str, float]]:
        if not isinstance(raw, list):
            return []
        normalized: list[tuple[str, float]] = []
        for item in raw:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                try:
                    normalized.append((str(item[0]), float(item[1])))
                except (TypeError, ValueError):
                    continue
            elif isinstance(item, dict) and "timestamp" in item and "value" in item:
                try:
                    normalized.append((str(item["timestamp"]), float(item["value"])))
                except (TypeError, ValueError):
                    continue
        return normalized[-600:]

    def _opt_decimal(self, value: Any) -> Decimal | None:
        if value is None:
            return None
        try:
            return self._to_decimal(value)
        except Exception:
            return None

    def _hold(self, reason: str, reason_code: str, reason_details: dict[str, Any] | None = None) -> Intent:
        self.last_reason_code = reason_code
        details = None
        if reason_details is not None:
            details = {k: self._serialize_reason_value(v) for k, v in reason_details.items() if v is not None}
        return Intent.hold(reason=reason, reason_code=reason_code, reason_details=details)

    def _cfg(self, key: str, default: Any) -> Any:
        if isinstance(self.config, dict):
            return self.config.get(key, default)
        return getattr(self.config, key, default)

    def _d(self, key: str, default: str) -> Decimal:
        return self._to_decimal(self._cfg(key, default))

    @staticmethod
    def _to_decimal(value: Any) -> Decimal:
        return Decimal(str(value))

    @staticmethod
    def _parse_dt(value: str) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    @staticmethod
    def _now(market: MarketSnapshot) -> datetime:
        timestamp = getattr(market, "timestamp", None)
        if isinstance(timestamp, datetime):
            if timestamp.tzinfo is None:
                return timestamp.replace(tzinfo=UTC)
            return timestamp
        return datetime.now(UTC)

    def _indicator_value(self, indicator_result: Any) -> Decimal:
        if hasattr(indicator_result, "value"):
            return self._to_decimal(indicator_result.value)
        return self._to_decimal(indicator_result)

    def _serialize_reason_value(self, value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, dict):
            return {k: self._serialize_reason_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._serialize_reason_value(v) for v in value]
        return value
