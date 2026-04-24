import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Optional

from almanak.framework.intents import Intent
from almanak.framework.strategies import IntentStrategy, MarketSnapshot, almanak_strategy

logger = logging.getLogger(__name__)


@almanak_strategy(
    name="liquidation_fragility_usd1",
    description="BSC USD1 liquidation-fragility swing strategy with TP ladder and stop loss",
    version="1.0.0",
    author="Almanak",
    tags=["liquidation", "fragility", "bsc", "pancakeswap", "swap"],
    supported_chains=["bsc"],
    supported_protocols=["pancakeswap_v3"],
    intent_types=["SWAP", "HOLD"],
    default_chain="bsc",
)
class LiquidationFragilityUsd1Strategy(IntentStrategy):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        tokens = self.get_config("tokens", {})
        base_cfg = tokens.get("base", {}) if isinstance(tokens, dict) else {}
        quote_cfg = tokens.get("quote", {}) if isinstance(tokens, dict) else {}

        self.base_token_symbol = str(base_cfg.get("symbol", "USD1"))
        self.base_token_address = str(
            base_cfg.get("address", "0x8d0d000ee44948fc98c9b98a4fa4921476f08b0d")
        )
        self.quote_token_symbol = str(quote_cfg.get("symbol", "USDT"))

        sizing = self.get_config("sizing", {})
        risk = self.get_config("risk", {})
        exit_cfg = self.get_config("exit", {})

        self.portfolio_pct = Decimal(str(sizing.get("portfolioPct", "0.02")))
        self.max_position_usd = Decimal(str(sizing.get("maxPositionUsd", "2000")))
        self.min_position_usd = Decimal(str(sizing.get("minPositionUsd", "100")))

        self.stop_loss_pct = Decimal(str(risk.get("stopLossPct", "-0.1")))
        self.max_exposure_pct = Decimal(str(risk.get("maxExposurePct", "0.02")))
        self.max_slippage_bps = int(risk.get("maxSlippageBps", 50))

        take_profit_pcts = exit_cfg.get("takeProfitPcts", [0.1, 0.2])
        self.tp1_pct = Decimal(str(take_profit_pcts[0]))
        self.tp2_pct = Decimal(str(take_profit_pcts[1]))
        self.time_horizon_hours = int(exit_cfg.get("timeHorizonHours", 72))

        take_profit_fractions = exit_cfg.get("takeProfitFractions", [0.5, 0.5])
        self.tp1_fraction = Decimal(str(take_profit_fractions[0]))

        self.protocol = str(self.get_config("protocol", "pancakeswap_v3"))
        self.signal_active = bool(self.get_config("signal_active", True))
        self.dust_threshold_usd = Decimal(str(self.get_config("dust_threshold_usd", "5")))

        self.in_position = False
        self.entry_price: Optional[Decimal] = None
        self.entry_time: Optional[datetime] = None
        self.tp1_hit = False
        self.tp2_hit = False
        self._pending_action: Optional[str] = None
        self._pending_entry_price: Optional[Decimal] = None
        self._pending_entry_time: Optional[datetime] = None

    def _now(self) -> datetime:
        return datetime.now(UTC)

    def _max_slippage_decimal(self) -> Decimal:
        return Decimal(str(self.max_slippage_bps)) / Decimal("10000")

    def _get_base_balance(self, market: MarketSnapshot):
        try:
            return market.balance(self.base_token_address)
        except ValueError:
            return market.balance(self.base_token_symbol)

    def _get_base_price(self, market: MarketSnapshot) -> Decimal:
        try:
            return market.price(self.base_token_address)
        except ValueError:
            return market.price(self.base_token_symbol)

    def _reset_position(self) -> None:
        self.in_position = False
        self.entry_price = None
        self.entry_time = None
        self.tp1_hit = False
        self.tp2_hit = False

    def _build_swap(self, from_token: str, to_token: str, *, amount_usd=None, amount=None) -> Intent:
        return Intent.swap(
            from_token=from_token,
            to_token=to_token,
            amount_usd=amount_usd,
            amount=amount,
            max_slippage=self._max_slippage_decimal(),
            protocol=self.protocol,
            chain=self.chain,
        )

    def decide(self, market: MarketSnapshot) -> Optional[Intent]:
        try:
            quote_balance = market.balance(self.quote_token_symbol)
            base_balance = self._get_base_balance(market)
            base_price = self._get_base_price(market)
        except Exception as exc:
            return Intent.hold(reason=f"market data unavailable: {exc}")

        if base_price <= 0:
            return Intent.hold(reason="invalid base price")

        if self.in_position and base_balance.balance_usd <= self.dust_threshold_usd:
            self._reset_position()
            return Intent.hold(reason="position state reset from on-chain balance")

        if not self.in_position:
            if not self.signal_active:
                return Intent.hold(reason="signal inactive")

            portfolio_value = quote_balance.balance_usd + base_balance.balance_usd
            exposure_target = portfolio_value * self.max_exposure_pct
            portfolio_target = portfolio_value * self.portfolio_pct
            target_usd = min(
                exposure_target,
                portfolio_target,
                self.max_position_usd,
                quote_balance.balance_usd,
            )

            if target_usd < self.min_position_usd:
                return Intent.hold(reason="target size below minimum position")

            self._pending_action = "ENTRY"
            self._pending_entry_price = base_price
            self._pending_entry_time = self._now()

            return self._build_swap(
                from_token=self.quote_token_symbol,
                to_token=self.base_token_address,
                amount_usd=target_usd,
            )

        if self.entry_price is None or self.entry_time is None:
            return Intent.hold(reason="position metadata missing")

        pnl_pct = (base_price - self.entry_price) / self.entry_price

        if pnl_pct <= self.stop_loss_pct:
            self._pending_action = "EXIT_ALL"
            return self._build_swap(
                from_token=self.base_token_address,
                to_token=self.quote_token_symbol,
                amount="all",
            )

        if pnl_pct >= self.tp2_pct and not self.tp2_hit:
            self._pending_action = "EXIT_ALL"
            return self._build_swap(
                from_token=self.base_token_address,
                to_token=self.quote_token_symbol,
                amount="all",
            )

        if pnl_pct >= self.tp1_pct and not self.tp1_hit:
            amount = (base_balance.balance * self.tp1_fraction).quantize(Decimal("0.00000001"))
            if amount > 0:
                self._pending_action = "TP1"
                return self._build_swap(
                    from_token=self.base_token_address,
                    to_token=self.quote_token_symbol,
                    amount=amount,
                )

        if self._now() - self.entry_time >= timedelta(hours=self.time_horizon_hours):
            self._pending_action = "EXIT_ALL"
            return self._build_swap(
                from_token=self.base_token_address,
                to_token=self.quote_token_symbol,
                amount="all",
            )

        return Intent.hold(reason="holding position")

    def on_intent_executed(self, intent, success: bool, result):
        if not success:
            self._pending_action = None
            self._pending_entry_price = None
            self._pending_entry_time = None
            return
        if getattr(intent.intent_type, "value", "") != "SWAP":
            return

        if self._pending_action == "ENTRY":
            self.in_position = True
            self.entry_price = self._pending_entry_price
            self.entry_time = self._pending_entry_time
            self.tp1_hit = False
            self.tp2_hit = False
        elif self._pending_action == "TP1":
            self.tp1_hit = True
        elif self._pending_action == "EXIT_ALL":
            self._reset_position()

        self._pending_action = None
        self._pending_entry_price = None
        self._pending_entry_time = None

    def get_status(self) -> dict[str, Any]:
        return {
            "strategy": self.STRATEGY_NAME,
            "chain": self.chain,
            "protocol": self.protocol,
            "in_position": self.in_position,
            "entry_price": str(self.entry_price) if self.entry_price is not None else None,
            "entry_time": self.entry_time.isoformat() if self.entry_time else None,
            "tp1_hit": self.tp1_hit,
            "tp2_hit": self.tp2_hit,
        }

    def get_persistent_state(self):
        return {
            "in_position": self.in_position,
            "entry_price": str(self.entry_price) if self.entry_price is not None else None,
            "entry_time": self.entry_time.isoformat() if self.entry_time else None,
            "tp1_hit": self.tp1_hit,
            "tp2_hit": self.tp2_hit,
        }

    def load_persistent_state(self, state):
        if not state:
            return
        self.in_position = bool(state.get("in_position", False))
        entry_price = state.get("entry_price")
        self.entry_price = Decimal(str(entry_price)) if entry_price else None
        entry_time = state.get("entry_time")
        self.entry_time = datetime.fromisoformat(entry_time) if entry_time else None
        self.tp1_hit = bool(state.get("tp1_hit", False))
        self.tp2_hit = bool(state.get("tp2_hit", False))

    def get_open_positions(self):
        from almanak.framework.teardown import PositionInfo, PositionType, TeardownPositionSummary

        if not self.in_position:
            return TeardownPositionSummary.empty(self.strategy_id or self.STRATEGY_NAME)

        return TeardownPositionSummary(
            strategy_id=self.strategy_id or self.STRATEGY_NAME,
            timestamp=self._now(),
            positions=[
                PositionInfo(
                    position_type=PositionType.TOKEN,
                    position_id=f"{self.STRATEGY_NAME}:usd1",
                    chain=self.chain,
                    protocol=self.protocol,
                    value_usd=Decimal("0"),
                    details={
                        "symbol": self.base_token_symbol,
                        "address": self.base_token_address,
                        "quote_symbol": self.quote_token_symbol,
                    },
                )
            ],
        )

    def generate_teardown_intents(self, mode=None, market=None) -> list[Intent]:
        from almanak.framework.teardown import TeardownMode

        if not self.in_position:
            return []

        max_slippage = Decimal("0.03") if mode == TeardownMode.HARD else self._max_slippage_decimal()
        return [
            Intent.swap(
                from_token=self.base_token_address,
                to_token=self.quote_token_symbol,
                amount="all",
                max_slippage=max_slippage,
                protocol=self.protocol,
                chain=self.chain,
            )
        ]
