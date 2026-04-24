from datetime import UTC, datetime, timedelta
from decimal import Decimal

from almanak.framework.strategies import MarketSnapshot, TokenBalance
from almanak.framework.teardown import TeardownMode

from strategy import LiquidationFragilityUsd1Strategy


def make_config() -> dict:
    return {
        "chain": "bsc",
        "protocol": "pancakeswap_v3",
        "signal_active": True,
        "tokens": {
            "base": {
                "symbol": "USD1",
                "address": "0x8d0d000ee44948fc98c9b98a4fa4921476f08b0d",
            },
            "quote": {
                "symbol": "USDT",
                "address": "0x55d398326f99059ff775485246999027b3197955",
            },
        },
        "sizing": {
            "portfolioPct": 0.02,
            "maxPositionUsd": 2000,
            "minPositionUsd": 100,
        },
        "risk": {
            "stopLossPct": -0.1,
            "maxExposurePct": 0.02,
            "maxSlippageBps": 50,
        },
        "exit": {
            "takeProfitPcts": [0.1, 0.2],
            "takeProfitFractions": [0.5, 0.5],
            "timeHorizonHours": 72,
        },
    }


def make_strategy(config: dict | None = None) -> LiquidationFragilityUsd1Strategy:
    cfg = config or make_config()
    return LiquidationFragilityUsd1Strategy(
        config=cfg,
        chain="bsc",
        wallet_address="0x" + "1" * 40,
    )


def make_market(
    *,
    quote_usd: Decimal = Decimal("20000"),
    base_balance: Decimal = Decimal("0"),
    base_usd: Decimal = Decimal("0"),
    price: Decimal = Decimal("1"),
) -> MarketSnapshot:
    market = MarketSnapshot(chain="bsc", wallet_address="0x" + "1" * 40)
    market.set_balance(
        "USDT",
        TokenBalance(symbol="USDT", balance=quote_usd, balance_usd=quote_usd),
    )
    market.set_balance(
        "0x8d0d000ee44948fc98c9b98a4fa4921476f08b0d",
        TokenBalance(
            symbol="USD1",
            balance=base_balance,
            balance_usd=base_usd,
            address="0x8d0d000ee44948fc98c9b98a4fa4921476f08b0d",
        ),
    )
    market.set_price("0x8d0d000ee44948fc98c9b98a4fa4921476f08b0d", price)
    return market


def test_entry_swap_uses_pancakeswap_v3_and_spec_address():
    strategy = make_strategy()
    market = make_market(quote_usd=Decimal("20000"), price=Decimal("1"))

    intent = strategy.decide(market)

    assert intent.intent_type.value == "SWAP"
    assert intent.protocol == "pancakeswap_v3"
    assert intent.to_token == "0x8d0d000ee44948fc98c9b98a4fa4921476f08b0d"
    assert intent.amount_usd == Decimal("400")


def test_entry_hold_when_target_below_min_position():
    strategy = make_strategy()
    market = make_market(quote_usd=Decimal("1000"), price=Decimal("1"))

    intent = strategy.decide(market)

    assert intent.intent_type.value == "HOLD"


def test_stop_loss_exits_all():
    strategy = make_strategy()
    strategy.in_position = True
    strategy.entry_price = Decimal("1")
    strategy.entry_time = datetime.now(UTC)

    market = make_market(base_balance=Decimal("500"), base_usd=Decimal("450"), price=Decimal("0.9"))

    intent = strategy.decide(market)

    assert intent.intent_type.value == "SWAP"
    assert intent.from_token == "0x8d0d000ee44948fc98c9b98a4fa4921476f08b0d"
    assert intent.amount == "all"

    strategy.on_intent_executed(intent, success=True, result=None)
    assert strategy.in_position is False


def test_take_profit_1_sells_partial():
    strategy = make_strategy()
    strategy.in_position = True
    strategy.entry_price = Decimal("1")
    strategy.entry_time = datetime.now(UTC)

    market = make_market(base_balance=Decimal("200"), base_usd=Decimal("220"), price=Decimal("1.1"))

    intent = strategy.decide(market)

    assert intent.intent_type.value == "SWAP"
    assert intent.amount == Decimal("100.00000000")

    strategy.on_intent_executed(intent, success=True, result=None)
    assert strategy.tp1_hit is True
    assert strategy.in_position is True


def test_take_profit_2_exits_all():
    strategy = make_strategy()
    strategy.in_position = True
    strategy.entry_price = Decimal("1")
    strategy.entry_time = datetime.now(UTC)
    strategy.tp1_hit = True

    market = make_market(base_balance=Decimal("200"), base_usd=Decimal("240"), price=Decimal("1.2"))

    intent = strategy.decide(market)

    assert intent.intent_type.value == "SWAP"
    assert intent.amount == "all"

    strategy.on_intent_executed(intent, success=True, result=None)
    assert strategy.in_position is False


def test_time_horizon_exit_after_72h():
    strategy = make_strategy()
    strategy.in_position = True
    strategy.entry_price = Decimal("1")
    strategy.entry_time = datetime.now(UTC) - timedelta(hours=73)

    market = make_market(base_balance=Decimal("200"), base_usd=Decimal("205"), price=Decimal("1.01"))

    intent = strategy.decide(market)

    assert intent.intent_type.value == "SWAP"
    assert intent.amount == "all"


def test_state_self_heal_when_onchain_balance_missing():
    strategy = make_strategy()
    strategy.in_position = True
    strategy.entry_price = Decimal("1")
    strategy.entry_time = datetime.now(UTC)

    market = make_market(base_balance=Decimal("0"), base_usd=Decimal("0"), price=Decimal("1"))

    intent = strategy.decide(market)

    assert intent.intent_type.value == "HOLD"
    assert strategy.in_position is False


def test_persistence_round_trip():
    strategy = make_strategy()
    strategy.in_position = True
    strategy.entry_price = Decimal("1.05")
    strategy.entry_time = datetime.now(UTC)
    strategy.tp1_hit = True

    saved = strategy.get_persistent_state()

    restored = make_strategy()
    restored.load_persistent_state(saved)

    assert restored.get_persistent_state() == saved


def test_teardown_methods_for_open_position():
    strategy = make_strategy()
    strategy.in_position = True

    summary = strategy.get_open_positions()
    assert len(summary.positions) == 1
    assert summary.positions[0].protocol == "pancakeswap_v3"

    soft_intents = strategy.generate_teardown_intents(mode=TeardownMode.SOFT)
    hard_intents = strategy.generate_teardown_intents(mode=TeardownMode.HARD)

    assert len(soft_intents) == 1
    assert len(hard_intents) == 1
    assert hard_intents[0].max_slippage >= soft_intents[0].max_slippage
