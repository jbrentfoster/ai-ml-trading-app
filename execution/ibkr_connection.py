"""
IBKR Connection Manager
=======================
Handles connecting to Interactive Brokers IB Gateway via ib_insync.

Supports:
  - Paper trading (simulation mode) and live trading
  - Automatic reconnection with exponential back-off
  - Account summary retrieval
  - Placing and cancelling orders (market, limit, bracket)
  - Real-time position and P&L monitoring
  - Clean shutdown

Usage:
    from execution.ibkr_connection import IBKRConnection
    from config.settings import config, TradingMode

    conn = IBKRConnection()
    await conn.connect()

    # Place a paper trade
    trade = await conn.place_market_order("AAPL", "BUY", 10)
    print(trade)

    await conn.disconnect()
"""

import asyncio
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from config.settings import config, TradingMode
from core.logger import get_logger

log = get_logger("execution.ibkr")

# ---------------------------------------------------------------------------
# ib_insync is an optional dependency at import time so the rest of the codebase
# can be imported and tested without a live IBKR session.  We'll raise a clear
# error only when an actual connection is attempted.
# ---------------------------------------------------------------------------
try:
    from ib_insync import (
        IB,
        Contract,
        LimitOrder,
        MarketOrder,
        Order,
        Stock,
        Trade,
        util,
    )
    _IB_AVAILABLE = True
except ImportError:
    _IB_AVAILABLE = False
    log.warning(
        "ib_insync is not installed. "
        "Run: pip install ib_insync  — then restart."
    )


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class AccountSummary:
    """Snapshot of key IBKR account metrics."""
    account_id: str = ""
    net_liquidation: float = 0.0
    total_cash: float = 0.0
    buying_power: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    gross_position_value: float = 0.0
    currency: str = "USD"
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __str__(self) -> str:
        return (
            f"Account {self.account_id} | "
            f"NLV: ${self.net_liquidation:,.2f} | "
            f"Cash: ${self.total_cash:,.2f} | "
            f"Buying Power: ${self.buying_power:,.2f} | "
            f"Unrealized P&L: ${self.unrealized_pnl:+,.2f}"
        )


@dataclass
class OrderResult:
    """Simplified result returned after placing an order."""
    order_id: int
    symbol: str
    action: str          # BUY | SELL
    quantity: float
    order_type: str      # MKT | LMT | STP | STP LMT | BRACKET
    limit_price: Optional[float]
    status: str
    stop_price: Optional[float] = None   # auxPrice for STP / STP LMT
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __str__(self) -> str:
        if self.order_type in ("STP", "STP LMT") and self.stop_price:
            price_str = f"@ stop ${self.stop_price:.2f}"
            if self.limit_price:
                price_str += f" / lmt ${self.limit_price:.2f}"
        elif self.order_type == "TRAIL":
            # For TRAIL orders, stop_price carries the trailing amount
            # (auxPrice — the $ distance below market the stop ratchets to).
            price_str = (
                f"@ trail ${self.stop_price:.2f}" if self.stop_price
                else "@ trail"
            )
        elif self.limit_price:
            price_str = f"@ ${self.limit_price:.2f}"
        else:
            price_str = "@ MKT"
        return (
            f"[{self.status}] {self.action} {self.quantity} {self.symbol} "
            f"{price_str} (id={self.order_id})"
        )


# ── Connection manager ────────────────────────────────────────────────────────

class IBKRConnection:
    """
    Async context-manager wrapper around ib_insync.IB.

    Example (async):
        async with IBKRConnection() as conn:
            summary = await conn.get_account_summary()
            print(summary)

    Example (manual):
        conn = IBKRConnection()
        await conn.connect()
        ...
        await conn.disconnect()
    """

    def __init__(self) -> None:
        if not _IB_AVAILABLE:
            raise ImportError(
                "ib_insync is required. Install with: pip install ib_insync"
            )

        self._cfg = config.ibkr
        self._mode = config.trading.mode
        self._ib: Optional[IB] = None
        self._connected = False
        self._reconnect_task: Optional[asyncio.Task] = None
        self._disconnecting = False   # prevents spurious reconnect on clean shutdown

        # Choose port based on mode
        self._port = (
            self._cfg.paper_port
            if self._mode == TradingMode.SIMULATION
            else self._cfg.live_port
        )

        log.info(
            "IBKRConnection initialised | mode=%s | host=%s | port=%d | client_id=%d",
            self._mode.value, self._cfg.host, self._port, self._cfg.client_id,
        )

    # ── Connection lifecycle ─────────────────────────────────────────────────

    async def connect(self) -> bool:
        """
        Connect to IB Gateway.
        Returns True on success, False if all retry attempts fail.
        """
        for attempt in range(1, self._cfg.max_reconnect_attempts + 1):
            try:
                log.info(
                    "Connecting to IBKR (attempt %d/%d) ...",
                    attempt, self._cfg.max_reconnect_attempts,
                )
                self._ib = IB()

                # Wire up event handlers
                self._ib.disconnectedEvent += self._on_disconnected
                self._ib.errorEvent += self._on_error

                await self._ib.connectAsync(
                    host=self._cfg.host,
                    port=self._port,
                    clientId=self._cfg.client_id,
                    timeout=self._cfg.connection_timeout,
                )

                self._connected = True
                accounts = self._ib.managedAccounts()
                log.info(
                    "Connected to IBKR | accounts=%s | mode=%s",
                    accounts, self._mode.value,
                )

                if self._mode == TradingMode.SIMULATION:
                    log.info("PAPER TRADING MODE - no real money at risk")
                else:
                    log.warning("LIVE TRADING MODE - real money is at risk!")

                return True

            except Exception as exc:  # noqa: BLE001
                log.warning("Connection attempt %d failed: %s", attempt, exc)
                if attempt < self._cfg.max_reconnect_attempts:
                    delay = self._cfg.reconnect_delay * attempt  # linear back-off
                    log.info("Retrying in %.1f seconds ...", delay)
                    await asyncio.sleep(delay)

        log.error(
            "Failed to connect after %d attempts. "
            "Is IB Gateway running on port %d?",
            self._cfg.max_reconnect_attempts, self._port,
        )
        return False

    async def disconnect(self) -> None:
        """Gracefully disconnect from IBKR."""
        self._disconnecting = True

        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()

        if self._ib and self._connected:
            self._ib.disconnect()
            self._connected = False
            log.info("Disconnected from IBKR.")

        self._disconnecting = False

    # ── Async context manager support ────────────────────────────────────────

    async def __aenter__(self) -> "IBKRConnection":
        success = await self.connect()
        if not success:
            raise ConnectionError("Could not connect to IBKR IB Gateway.")
        return self

    async def __aexit__(self, *_) -> None:
        await self.disconnect()

    # ── Account information ──────────────────────────────────────────────────

    async def get_account_summary(self) -> AccountSummary:
        """Pull key account metrics from IBKR."""
        self._require_connection()

        # accountSummaryAsync() is the recommended call in ib_insync 0.9.86+;
        # reqAccountSummaryAsync() fires the underlying request but returns None.
        vals = await self._ib.accountSummaryAsync()

        summary = AccountSummary()
        for v in vals:
            summary.account_id = v.account
            match v.tag:
                case "NetLiquidation":
                    summary.net_liquidation = float(v.value)
                case "TotalCashValue":
                    summary.total_cash = float(v.value)
                case "BuyingPower":
                    summary.buying_power = float(v.value)
                case "UnrealizedPnL":
                    summary.unrealized_pnl = float(v.value)
                case "RealizedPnL":
                    summary.realized_pnl = float(v.value)
                case "GrossPositionValue":
                    summary.gross_position_value = float(v.value)
            if v.currency:
                summary.currency = v.currency

        log.info("Account summary: %s", summary)
        return summary

    async def get_positions(self) -> list[dict]:
        """Return all current open positions with non-zero quantity.

        IBKR sometimes retains ghost records (qty=0) after a position is fully
        closed, until the next daily session reset.  These are filtered out here.
        """
        self._require_connection()
        positions = await self._ib.reqPositionsAsync()
        result = []
        for p in positions:
            if p.position == 0:
                log.debug("Skipping zero-quantity ghost position: %s", p.contract.symbol)
                continue
            result.append({
                "account": p.account,
                "symbol": p.contract.symbol,
                "exchange": p.contract.exchange,
                "currency": p.contract.currency,
                "quantity": p.position,
                "avg_cost": p.avgCost,
                "market_value": p.position * p.avgCost,  # approx until mkt price fetched
            })
            log.debug("Position: %s x%.0f @ %.2f", p.contract.symbol, p.position, p.avgCost)
        return result

    # ── Order placement ──────────────────────────────────────────────────────

    def _make_stock_contract(self, symbol: str, exchange: str = "SMART", currency: str = "USD") -> "Stock":
        return Stock(symbol, exchange, currency)

    async def place_market_order(
        self,
        symbol: str,
        action: str,          # "BUY" or "SELL"
        quantity: float,
        exchange: str = "SMART",
        currency: str = "USD",
    ) -> OrderResult:
        """
        Place a market order.
        In simulation mode this executes against IBKR's paper account.
        """
        self._require_connection()

        contract = self._make_stock_contract(symbol, exchange, currency)
        order = MarketOrder(action.upper(), quantity)

        log.info("Placing MARKET order: %s %s x%.0f", action.upper(), symbol, quantity)
        trade: Trade = self._ib.placeOrder(contract, order)

        # Brief wait to get initial status back from IBKR
        await asyncio.sleep(1)

        result = OrderResult(
            order_id=trade.order.orderId,
            symbol=symbol,
            action=action.upper(),
            quantity=quantity,
            order_type="MKT",
            limit_price=None,
            status=trade.orderStatus.status,
        )
        log.info("Market order result: %s", result)
        return result

    async def place_limit_order(
        self,
        symbol: str,
        action: str,
        quantity: float,
        limit_price: float,
        exchange: str = "SMART",
        currency: str = "USD",
    ) -> OrderResult:
        """Place a limit order."""
        self._require_connection()

        contract = self._make_stock_contract(symbol, exchange, currency)
        order = LimitOrder(action.upper(), quantity, limit_price)

        log.info(
            "Placing LIMIT order: %s %s x%.0f @ %.2f",
            action.upper(), symbol, quantity, limit_price,
        )
        trade: Trade = self._ib.placeOrder(contract, order)
        await asyncio.sleep(1)

        result = OrderResult(
            order_id=trade.order.orderId,
            symbol=symbol,
            action=action.upper(),
            quantity=quantity,
            order_type="LMT",
            limit_price=limit_price,
            status=trade.orderStatus.status,
        )
        log.info("Limit order result: %s", result)
        return result

    async def place_bracket_order(
        self,
        symbol: str,
        action: str,
        quantity: float,
        entry_price: float,
        stop_loss_price: float,
        take_profit_price: float,
        exchange: str = "SMART",
        currency: str = "USD",
    ) -> list[OrderResult]:
        """
        Place a bracket order (entry + stop-loss + take-profit).
        All three legs are linked — cancelling one cancels the others.
        """
        self._require_connection()

        contract = self._make_stock_contract(symbol, exchange, currency)

        # Round to $0.01 tick size — IBKR rejects with error 110 otherwise.
        # Float32 wire conversion inside ib_insync can drift a $202.52 limit to
        # 202.52000427246094, which fails the minimum-price-variation check.
        entry_price       = round(float(entry_price), 2)
        stop_loss_price   = round(float(stop_loss_price), 2)
        take_profit_price = round(float(take_profit_price), 2)

        bracket = self._ib.bracketOrder(
            action=action.upper(),
            quantity=quantity,
            limitPrice=entry_price,
            takeProfitPrice=take_profit_price,
            stopLossPrice=stop_loss_price,
        )

        log.info(
            "Placing BRACKET order: %s %s x%.0f | entry=%.2f | SL=%.2f | TP=%.2f",
            action.upper(), symbol, quantity,
            entry_price, stop_loss_price, take_profit_price,
        )

        # GTC so the bracket survives if the signal runner fires outside RTH
        # (limit legs would otherwise be cancelled immediately under DAY TIF).
        for leg in bracket:
            leg.tif = "GTC"

        def _clean_price(v):
            try:
                fv = float(v)
            except (TypeError, ValueError):
                return None
            if not math.isfinite(fv) or abs(fv) > 1e100 or fv == 0.0:
                return None
            return fv

        results = []
        for leg in bracket:
            trade: Trade = self._ib.placeOrder(contract, leg)
            await asyncio.sleep(0.5)
            results.append(OrderResult(
                order_id=trade.order.orderId,
                symbol=symbol,
                action=trade.order.action,
                quantity=quantity,
                order_type=trade.order.orderType,
                limit_price=_clean_price(getattr(trade.order, "lmtPrice", None)),
                stop_price=_clean_price(getattr(trade.order, "auxPrice", None)),
                status=trade.orderStatus.status,
            ))
            log.info("Bracket leg: %s", results[-1])

        return results

    async def place_trailing_stop(
        self,
        symbol: str,
        action: str,          # "SELL" to close a long, "BUY" to close a short
        quantity: float,
        trail_amount: float,  # $ below (SELL) / above (BUY) market the stop trails
        exchange: str = "SMART",
        currency: str = "USD",
    ) -> OrderResult:
        """
        Submit a standalone GTC trailing-stop order.

        For a long position, pass action="SELL" and trail_amount > 0 — IBKR
        ratchets the stop upward as price rises and triggers when price falls
        back by `trail_amount` from the peak.

        The trailing distance is rounded to the $0.01 tick size (same fix as
        place_bracket_order — IBKR rejects sub-tick prices with error 110).
        """
        self._require_connection()

        contract = self._make_stock_contract(symbol, exchange, currency)
        trail_amount = round(float(trail_amount), 2)

        order = Order()
        order.action = action.upper()
        order.orderType = "TRAIL"
        order.totalQuantity = quantity
        order.auxPrice = trail_amount
        order.tif = "GTC"

        log.info(
            "Placing TRAIL order: %s %s x%.0f | trail=$%.2f",
            action.upper(), symbol, quantity, trail_amount,
        )
        trade: Trade = self._ib.placeOrder(contract, order)
        await asyncio.sleep(1)

        def _clean_price(v):
            try:
                fv = float(v)
            except (TypeError, ValueError):
                return None
            if not math.isfinite(fv) or abs(fv) > 1e100 or fv == 0.0:
                return None
            return fv

        # stop_price carries auxPrice (the trailing distance) so downstream
        # views (Page 9, __str__) can surface it without a schema change.
        result = OrderResult(
            order_id=trade.order.orderId,
            symbol=symbol,
            action=action.upper(),
            quantity=quantity,
            order_type="TRAIL",
            limit_price=None,
            stop_price=_clean_price(getattr(trade.order, "auxPrice", None)),
            status=trade.orderStatus.status,
        )
        log.info("Trailing stop result: %s", result)
        return result

    async def get_open_orders(self) -> list[dict]:
        """Return all open orders as a list of plain dicts."""
        self._require_connection()
        trades = self._ib.openTrades()

        # ib_insync fills unused price fields with sys.float_info.max (~1.8e308)
        # rather than None. Treat any non-finite or >1e100 value as "no price".
        def _clean(v):
            try:
                fv = float(v)
            except (TypeError, ValueError):
                return None
            if not math.isfinite(fv) or abs(fv) > 1e100 or fv == 0.0:
                return None
            return fv

        result = []
        for t in trades:
            otype = t.order.orderType
            lmt   = _clean(getattr(t.order, "lmtPrice", None))
            stop  = _clean(getattr(t.order, "auxPrice", None))
            # STP / STP LMT carry the trigger price in auxPrice, not lmtPrice.
            if otype in ("STP", "STP LMT") and lmt is None:
                lmt = None  # keep limit blank; stop_price below carries the trigger
            result.append({
                "order_id":    t.order.orderId,
                "symbol":      t.contract.symbol,
                "action":      t.order.action,
                "quantity":    t.order.totalQuantity,
                "order_type":  otype,
                "limit_price": lmt,
                "stop_price":  stop,
                "status":      t.orderStatus.status,
                "filled":      t.orderStatus.filled,
                "remaining":   t.orderStatus.remaining,
            })
        return result

    async def cancel_order(self, order_id: int) -> bool:
        """Cancel an open order by ID. Returns True if the cancel was sent."""
        self._require_connection()

        open_trades = self._ib.openTrades()
        target = next(
            (t for t in open_trades if t.order.orderId == order_id), None
        )
        if not target:
            log.warning("cancel_order: order_id=%d not found in open trades.", order_id)
            return False

        self._ib.cancelOrder(target.order)
        log.info("Cancel request sent for order_id=%d", order_id)
        return True

    async def cancel_all_orders(self) -> int:
        """Cancel every open order. Returns count of cancels sent."""
        self._require_connection()
        open_trades = self._ib.openTrades()
        for trade in open_trades:
            self._ib.cancelOrder(trade.order)
        log.info("Cancelled %d open orders.", len(open_trades))
        return len(open_trades)

    # ── Market data ──────────────────────────────────────────────────────────

    async def get_last_price(self, symbol: str, exchange: str = "SMART", currency: str = "USD") -> Optional[float]:
        """
        Return the last traded price for a symbol using a three-tier fallback:

          1. IBKR live data (market data type 1) — requires real-time subscription
          2. IBKR 15-min delayed data (market data type 3) — free, no subscription needed
          3. yfinance — always available; returns the most recent close

        ib_insync populates the same ticker.last / ticker.close fields for both
        live and delayed data; ticker.marketPrice() returns the best non-NaN value.
        """
        self._require_connection()

        import math

        contract = self._make_stock_contract(symbol, exchange, currency)

        def _extract(ticker) -> Optional[float]:
            mp = ticker.marketPrice()
            if mp is not None and not math.isnan(mp) and mp > 0:
                return float(mp)
            for val in (ticker.last, ticker.close):
                if val is not None and not math.isnan(val) and val > 0:
                    return float(val)
            return None

        # ── Attempt 1: IBKR live data ────────────────────────────────────────
        self._ib.reqMarketDataType(1)
        ticker = self._ib.reqMktData(contract, snapshot=True)
        await asyncio.sleep(2)
        price = _extract(ticker)
        self._ib.cancelMktData(contract)

        if price is not None:
            log.debug("Last price %s = %.2f (IBKR live)", symbol, price)
            return price

        # ── Attempt 2: IBKR 15-min delayed data ─────────────────────────────
        log.info("No live quote for %s — trying IBKR delayed data ...", symbol)
        self._ib.reqMarketDataType(3)
        ticker = self._ib.reqMktData(contract, snapshot=False)
        await asyncio.sleep(3)
        price = _extract(ticker)
        self._ib.cancelMktData(contract)
        self._ib.reqMarketDataType(1)   # restore default

        if price is not None:
            log.debug("Last price %s = %.2f (IBKR delayed)", symbol, price)
            return price

        # ── Attempt 3: yfinance ──────────────────────────────────────────────
        log.info("No IBKR quote for %s — falling back to yfinance ...", symbol)
        try:
            import yfinance as yf
            hist = yf.Ticker(symbol).fast_info
            price = hist.get("last_price") or hist.get("previous_close")
            if price:
                log.debug("Last price %s = %.2f (yfinance)", symbol, price)
                return float(price)
        except Exception as exc:  # noqa: BLE001
            log.warning("yfinance fallback failed for %s: %s", symbol, exc)

        log.warning("Could not retrieve price for %s from any source", symbol)
        return None

    # ── Event handlers ───────────────────────────────────────────────────────

    def _on_disconnected(self) -> None:
        self._connected = False
        if self._disconnecting:
            return   # clean shutdown — do not attempt to reconnect
        log.warning("IBKR connection lost. Scheduling reconnect ...")
        self._reconnect_task = asyncio.ensure_future(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        for attempt in range(1, self._cfg.max_reconnect_attempts + 1):
            delay = self._cfg.reconnect_delay * attempt
            log.info("Reconnect attempt %d in %.1f s ...", attempt, delay)
            await asyncio.sleep(delay)
            try:
                await self._ib.connectAsync(
                    host=self._cfg.host,
                    port=self._port,
                    clientId=self._cfg.client_id,
                    timeout=self._cfg.connection_timeout,
                )
                self._connected = True
                log.info("Reconnected to IBKR.")
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("Reconnect attempt %d failed: %s", attempt, exc)

        log.error("Could not reconnect to IBKR after %d attempts.", self._cfg.max_reconnect_attempts)

    def _on_error(self, req_id: int, error_code: int, error_string: str, contract) -> None:
        # IBKR sends many informational codes — only log real errors
        informational = {
            2104,   # Market data farm connection OK
            2106,   # HMDS data farm connection OK
            2107,   # HMDS data farm inactive but available on demand
            2119,   # Market data farm is connecting
            2158,   # Sec-def data farm connection OK
            300,    # Can't find EId — benign race on cancel/resubscribe
            399,    # Order message
            10167,  # Switching to delayed market data (no real-time subscription)
            10197,  # No market data during competing session
            10349,  # Requested contract details not found
            202,    # Order Canceled — confirmation after cancelOrder, not a failure
        }
        if error_code in informational:
            log.debug("IBKR info [%d]: %s", error_code, error_string)
        else:
            log.error("IBKR error [%d] req_id=%d: %s", error_code, req_id, error_string)

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _require_connection(self) -> None:
        if not self._connected or not self._ib:
            raise RuntimeError(
                "Not connected to IBKR. Call await connect() first."
            )

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def ib(self) -> Optional["IB"]:
        """Direct access to the underlying ib_insync.IB instance if needed."""
        return self._ib
