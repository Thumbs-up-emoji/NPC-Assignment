import logging
from decimal import Decimal
from typing import Dict, List
import matplotlib.pyplot as plt
from hummingbot.core.data_type.common import OrderType, PriceType, TradeType
from hummingbot.core.data_type.order_candidate import OrderCandidate
from hummingbot.core.event.events import OrderFilledEvent
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase
from hummingbot.data_feed.candles_feed.candles_factory import CandlesFactory, CandlesConfig
from hummingbot.connector.connector_base import ConnectorBase
import os
import datetime

class MyPMM(ScriptStrategyBase):
    """
    NPC Group Assignment Submission - Vineet Krishna
    Checks for divergence between RSI and price to place orders.
    Uses NATR to adjust the spread.
    Inventory-aware spread skewing logic.
    """
    bid_spread = 0.0001
    ask_spread = 0.0001
    order_refresh_time = 10
    order_amount = 0.01
    create_timestamp = 0
    trading_pair = "ETH-USDT"
    exchange = "binance_paper_trade"
    # Here you can use for example the LastTrade price to use in your strategy
    price_source = PriceType.MidPrice

    # Candles params
    candle_exchange = "binance"
    candles_interval = "1m"
    candles_length = 30
    max_records = 1000
    # Number of candle rows to display in status
    display_rows = 5

    # Initializes candles
    candles = CandlesFactory.get_candle(CandlesConfig(connector=candle_exchange,
                                                      trading_pair=trading_pair,
                                                      interval=candles_interval,
                                                      max_records=max_records))

    # markets defines which order books (exchange / pair) to connect to. At least one exchange/pair needs to be instantiated
    markets = {exchange: {trading_pair}}

    # start the candles when the script starts
    def __init__(self, connectors: Dict[str, ConnectorBase]):
        super().__init__(connectors)
        self.candles.start()
        self.trade_history = []

    # stop the candles when the script stops
    async def on_stop(self):
        self.candles.stop()

    def on_tick(self):
        if self.create_timestamp <= self.current_timestamp:
            self.cancel_all_orders()
            proposal: List[OrderCandidate] = self.create_proposal()
            proposal_adjusted: List[OrderCandidate] = self.adjust_proposal_to_budget(proposal)
            self.place_orders(proposal_adjusted)
            self.create_timestamp = self.order_refresh_time + self.current_timestamp

    def get_candles_with_features(self):
        candles_df = self.candles.candles_df
        candles_df.ta.rsi(length=self.candles_length, append=True)
        candles_df.ta.natr(length=self.candles_length, append=True)
        return candles_df

    def create_proposal(self) -> List[OrderCandidate]:
        ref_price = self.connectors[self.exchange].get_price_by_type(self.trading_pair, self.price_source)
        inventory_ratio = self.get_inventory_ratio()
        candles_df = self.get_candles_with_features()

        rsi = candles_df["RSI_30"].iloc[-1]
        rsi_prev = candles_df["RSI_30"].iloc[-2]
        price = candles_df["close"].iloc[-1]
        price_prev = candles_df["close"].iloc[-2]
        natr = candles_df["NATR_30"].iloc[-1]

        # Initialize base spreads
        spread_multiplier = Decimal(1 + (natr / 100))  # Volatility-adjusted spread
        bid_spread = Decimal(self.bid_spread) * spread_multiplier
        ask_spread = Decimal(self.ask_spread) * spread_multiplier

        # Detect RSI divergence conditions
        bearish_divergence = rsi < rsi_prev and price > price_prev
        bullish_divergence = rsi > rsi_prev and price < price_prev
        print(inventory_ratio)
        # Inventory-Aware Spread Skewing Logic
        if inventory_ratio > 0.7 and bearish_divergence:
            ask_spread *= Decimal(0.9)
        elif inventory_ratio < 0.3 and bullish_divergence:
            bid_spread *= Decimal(0.9)

        buy_price = ref_price * Decimal(1 - bid_spread)
        sell_price = ref_price * Decimal(1 + ask_spread)

        buy_order = OrderCandidate(trading_pair=self.trading_pair, is_maker=True, order_type=OrderType.LIMIT,
                                   order_side=TradeType.BUY, amount=Decimal(self.order_amount), price=buy_price)

        sell_order = OrderCandidate(trading_pair=self.trading_pair, is_maker=True, order_type=OrderType.LIMIT,
                                    order_side=TradeType.SELL, amount=Decimal(self.order_amount), price=sell_price)

        return [buy_order, sell_order]

    def adjust_proposal_to_budget(self, proposal: List[OrderCandidate]) -> List[OrderCandidate]:
        proposal_adjusted = self.connectors[self.exchange].budget_checker.adjust_candidates(proposal, all_or_none=True)

        return proposal_adjusted

    def place_orders(self, proposal: List[OrderCandidate]) -> None:
        for order in proposal:
            self.place_order(connector_name=self.exchange, order=order)

    def place_order(self, connector_name: str, order: OrderCandidate):
        if order.order_side == TradeType.SELL:
            self.sell(connector_name=connector_name, trading_pair=order.trading_pair, amount=order.amount,
                      order_type=order.order_type, price=order.price)
        elif order.order_side == TradeType.BUY:
            self.buy(connector_name=connector_name, trading_pair=order.trading_pair, amount=order.amount,
                     order_type=order.order_type, price=order.price)

    def cancel_all_orders(self):
        for order in self.get_active_orders(connector_name=self.exchange):
            self.cancel(self.exchange, order.trading_pair, order.client_order_id)

    def did_fill_order(self, event: OrderFilledEvent):
        self.trade_history.append({
            "timestamp": self.current_timestamp,
            "type": event.trade_type.name,
            "price": event.price
        })
        msg = (f"{event.trade_type.name} {round(event.amount, 2)} {event.trading_pair} {self.exchange} at {round(event.price, 2)}")
        self.log_with_clock(logging.INFO, msg)
        self.notify_hb_app_with_timestamp(msg)

    def format_status(self) -> str:
        """
        Returns status of the current strategy and displays candles feed info
        """
        if not self.ready_to_trade:
            return "Market connectors are not ready."
        lines = []

        balance_df = self.get_balance_df()
        lines.extend(["", "  Balances:"] + ["    " + line for line in balance_df.to_string(index=False).split("\n")])

        try:
            df = self.active_orders_df()
            lines.extend(["", "  Orders:"] + ["    " + line for line in df.to_string(index=False).split("\n")])
        except ValueError:
            lines.extend(["", "  No active maker orders."])

        lines.extend(["\n----------------------------------------------------------------------\n"])
        candles_df = self.get_candles_with_features()
        lines.extend([f"  Candles: {self.candles.name} | Interval: {self.candles.interval}", ""])
        # Only display the last display_rows number of rows
        display_df = candles_df.tail(min(self.display_rows, self.candles_length)).iloc[::-1]
        lines.extend(["    " + line for line in display_df.to_string(index=False).split("\n")])

        plot_path = self.plot_candle_signals()
        if plot_path:
            lines.append(f"\n Plot saved to: {plot_path}")
        else:
            lines.append("\n Could not generate plot.")

        return "\n".join(lines)

    def get_inventory_ratio(self) -> float:
        base_asset, quote_asset = self.trading_pair.split("-")
        base_balance = self.connectors[self.exchange].get_balance(base_asset)
        quote_balance = self.connectors[self.exchange].get_balance(quote_asset)

        price = self.connectors[self.exchange].get_price_by_type(self.trading_pair, PriceType.MidPrice)

        base_value = base_balance * price
        total_value = base_value + quote_balance
        if total_value == 0:
            return 0.5  # Neutral
        return float(base_value / total_value)  # Between 0 (all quote) and 1 (all base)
    
    def plot_candle_signals(self, filename="strategy_plot.png"):
        if not self.trade_history:
            return None
        
        # Create figure and axis
        plt.figure(figsize=(12, 6))
        
        # Extract timestamps and prices from trade history
        buy_times = []
        buy_prices = []
        sell_times = []
        sell_prices = []
        
        for trade in self.trade_history:
            if trade["type"] == "BUY":
                buy_times.append(datetime.datetime.fromtimestamp(trade["timestamp"]))
                buy_prices.append(float(trade["price"]))
            elif trade["type"] == "SELL":
                sell_times.append(datetime.datetime.fromtimestamp(trade["timestamp"]))
                sell_prices.append(float(trade["price"]))
        
        # Plot trades
        if buy_times:
            plt.scatter(buy_times, buy_prices, color='green', label='Buy', marker='^', s=100)
        if sell_times:
            plt.scatter(sell_times, sell_prices, color='red', label='Sell', marker='v', s=100)
        
        # Add labels and title
        plt.xlabel('Time')
        plt.ylabel('Price')
        plt.title(f'Trade History for {self.trading_pair}')
        plt.xticks(rotation=45)
        plt.grid(True, alpha=0.3)
        plt.legend()
        plt.tight_layout()
        
        # Save the figure
        save_path = os.path.join(os.getcwd(), filename)
        plt.savefig(save_path)
        plt.close()
        
        return save_path
