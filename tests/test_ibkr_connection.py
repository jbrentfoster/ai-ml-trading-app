"""
Unit tests for IBKRConnection.
These tests mock ib_insync so no live IBKR session is needed.

Run with:
    python -m pytest tests/test_ibkr_connection.py -v
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config.settings import TradingMode


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_ib():
    """Return a fully mocked IB instance."""
    ib = MagicMock()
    ib.connectAsync = AsyncMock()
    ib.reqAccountSummaryAsync = AsyncMock(return_value=[])
    ib.reqPositionsAsync = AsyncMock(return_value=[])
    ib.reqMktData = MagicMock(return_value=MagicMock(last=150.0, close=149.5))
    ib.cancelMktData = MagicMock()
    ib.placeOrder = MagicMock()
    ib.cancelOrder = MagicMock()
    ib.openTrades = MagicMock(return_value=[])
    ib.managedAccounts = MagicMock(return_value=["DU123456"])
    ib.disconnect = MagicMock()
    ib.disconnectedEvent = MagicMock()
    ib.errorEvent = MagicMock()
    ib.bracketOrder = MagicMock(return_value=[])
    return ib


@pytest.fixture
def mock_trade():
    """Return a mock Trade object as returned by ib.placeOrder."""
    trade = MagicMock()
    trade.order.orderId = 42
    trade.order.action = "BUY"
    trade.order.orderType = "MKT"
    trade.orderStatus.status = "PreSubmitted"
    return trade


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestIBKRConnectionConfig:
    """Test that config is read correctly."""

    def test_paper_port_selected_in_simulation_mode(self):
        with patch("execution.ibkr_connection._IB_AVAILABLE", True), \
             patch("execution.ibkr_connection.IB"):
            from config.settings import config
            from execution.ibkr_connection import IBKRConnection
            config.trading.mode = TradingMode.SIMULATION
            conn = IBKRConnection()
            assert conn._port == config.ibkr.paper_port

    def test_live_port_selected_in_live_mode(self):
        with patch("execution.ibkr_connection._IB_AVAILABLE", True), \
             patch("execution.ibkr_connection.IB"):
            from config.settings import config
            from execution.ibkr_connection import IBKRConnection
            config.trading.mode = TradingMode.LIVE
            conn = IBKRConnection()
            assert conn._port == config.ibkr.live_port
            config.trading.mode = TradingMode.SIMULATION   # reset


class TestConnection:
    """Test connect / disconnect lifecycle."""

    @pytest.mark.asyncio
    async def test_connect_success(self, mock_ib):
        with patch("execution.ibkr_connection._IB_AVAILABLE", True), \
             patch("execution.ibkr_connection.IB", return_value=mock_ib):
            from execution.ibkr_connection import IBKRConnection
            conn = IBKRConnection()
            result = await conn.connect()
            assert result is True
            assert conn.is_connected is True

    @pytest.mark.asyncio
    async def test_connect_failure_returns_false(self, mock_ib):
        mock_ib.connectAsync.side_effect = Exception("Connection refused")
        with patch("execution.ibkr_connection._IB_AVAILABLE", True), \
             patch("execution.ibkr_connection.IB", return_value=mock_ib):
            from execution.ibkr_connection import IBKRConnection
            conn = IBKRConnection()
            conn._cfg.max_reconnect_attempts = 1
            conn._cfg.reconnect_delay = 0
            result = await conn.connect()
            assert result is False
            assert conn.is_connected is False

    @pytest.mark.asyncio
    async def test_disconnect(self, mock_ib):
        with patch("execution.ibkr_connection._IB_AVAILABLE", True), \
             patch("execution.ibkr_connection.IB", return_value=mock_ib):
            from execution.ibkr_connection import IBKRConnection
            conn = IBKRConnection()
            await conn.connect()
            await conn.disconnect()
            mock_ib.disconnect.assert_called_once()
            assert conn.is_connected is False

    @pytest.mark.asyncio
    async def test_require_connection_raises_when_not_connected(self):
        with patch("execution.ibkr_connection._IB_AVAILABLE", True), \
             patch("execution.ibkr_connection.IB"):
            from execution.ibkr_connection import IBKRConnection
            conn = IBKRConnection()
            with pytest.raises(RuntimeError, match="Not connected"):
                conn._require_connection()


class TestOrders:
    """Test order placement and cancellation."""

    @pytest.mark.asyncio
    async def test_place_market_order(self, mock_ib, mock_trade):
        mock_ib.placeOrder.return_value = mock_trade

        with patch("execution.ibkr_connection._IB_AVAILABLE", True), \
             patch("execution.ibkr_connection.IB", return_value=mock_ib), \
             patch("execution.ibkr_connection.Stock"), \
             patch("execution.ibkr_connection.MarketOrder"), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            from execution.ibkr_connection import IBKRConnection
            conn = IBKRConnection()
            await conn.connect()
            result = await conn.place_market_order("AAPL", "BUY", 10)

            assert result.symbol == "AAPL"
            assert result.action == "BUY"
            assert result.quantity == 10
            assert result.order_type == "MKT"
            assert result.limit_price is None
            assert result.order_id == 42

    @pytest.mark.asyncio
    async def test_place_limit_order(self, mock_ib, mock_trade):
        mock_ib.placeOrder.return_value = mock_trade

        with patch("execution.ibkr_connection._IB_AVAILABLE", True), \
             patch("execution.ibkr_connection.IB", return_value=mock_ib), \
             patch("execution.ibkr_connection.Stock"), \
             patch("execution.ibkr_connection.LimitOrder"), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            from execution.ibkr_connection import IBKRConnection
            conn = IBKRConnection()
            await conn.connect()
            result = await conn.place_limit_order("AAPL", "BUY", 10, limit_price=150.0)

            assert result.order_type == "LMT"
            assert result.limit_price == 150.0

    @pytest.mark.asyncio
    async def test_cancel_order_not_found(self, mock_ib):
        mock_ib.openTrades.return_value = []

        with patch("execution.ibkr_connection._IB_AVAILABLE", True), \
             patch("execution.ibkr_connection.IB", return_value=mock_ib):
            from execution.ibkr_connection import IBKRConnection
            conn = IBKRConnection()
            await conn.connect()
            result = await conn.cancel_order(999)
            assert result is False

    @pytest.mark.asyncio
    async def test_cancel_all_orders(self, mock_ib, mock_trade):
        mock_ib.openTrades.return_value = [mock_trade, mock_trade]

        with patch("execution.ibkr_connection._IB_AVAILABLE", True), \
             patch("execution.ibkr_connection.IB", return_value=mock_ib):
            from execution.ibkr_connection import IBKRConnection
            conn = IBKRConnection()
            await conn.connect()
            count = await conn.cancel_all_orders()
            assert count == 2


class TestAccountSummary:
    """Test account data retrieval."""

    @pytest.mark.asyncio
    async def test_account_summary_parsed(self, mock_ib):
        def make_val(tag, value, account="DU123456", currency="USD"):
            v = MagicMock()
            v.tag = tag
            v.value = str(value)
            v.account = account
            v.currency = currency
            return v

        mock_ib.accountSummaryAsync = AsyncMock(return_value=[
            make_val("NetLiquidation",    100_000),
            make_val("TotalCashValue",     80_000),
            make_val("BuyingPower",       200_000),
            make_val("UnrealizedPnL",       1_500),
            make_val("RealizedPnL",           500),
            make_val("GrossPositionValue",  20_000),
        ])

        with patch("execution.ibkr_connection._IB_AVAILABLE", True), \
             patch("execution.ibkr_connection.IB", return_value=mock_ib):
            from execution.ibkr_connection import IBKRConnection
            conn = IBKRConnection()
            await conn.connect()
            summary = await conn.get_account_summary()

            assert summary.net_liquidation == 100_000
            assert summary.total_cash == 80_000
            assert summary.buying_power == 200_000
            assert summary.unrealized_pnl == 1_500
            assert summary.account_id == "DU123456"


class TestErrorHandling:
    """Test the IBKR error event handler logic."""

    def test_informational_errors_do_not_raise(self):
        with patch("execution.ibkr_connection._IB_AVAILABLE", True), \
             patch("execution.ibkr_connection.IB"):
            from execution.ibkr_connection import IBKRConnection
            conn = IBKRConnection()
            # Should silently handle informational codes (no exception)
            conn._on_error(0, 2104, "Market data farm connection is OK", None)
            conn._on_error(0, 2106, "HMDS data farm connection is OK", None)
