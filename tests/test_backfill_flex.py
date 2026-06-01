"""Unit tests for the Flex Query trade-backfill parser.

Covers the normalisation contract only (XML attrs -> get_executions dict shape).
The downstream reconcile_fills core is tested in the Phase B suite; this just
guards the parser's field mapping, level-of-detail filtering, sign handling, and
datetime parsing so a Flex export feeds the existing pipeline cleanly.
"""

from datetime import datetime

from scripts.backfill_flex_trades import parse_flex_trades, _parse_flex_datetime


_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse queryName="Trades" type="AF">
  <FlexStatements count="1">
    <FlexStatement accountId="DU111111" fromDate="20260101" toDate="20260530">
      <Trades>
        <Trade levelOfDetail="EXECUTION" assetCategory="STK" execID="0001.aaaa"
               conid="265598" symbol="AAPL" buySell="BUY" quantity="10"
               tradePrice="150.25" ibCommission="-1.00" orderType="LMT"
               orderID="123" accountId="DU111111" dateTime="20260515;143000"
               fifoPnlRealized="0"/>
        <Trade levelOfDetail="EXECUTION" assetCategory="STK" execID="0002.bbbb"
               conid="265598" symbol="AAPL" buySell="SELL" quantity="-10"
               tradePrice="155.50" ibCommission="-1.00" orderType="STP"
               orderID="124" accountId="DU111111" dateTime="20260520;150000"
               fifoPnlRealized="51.50"/>
        <Trade levelOfDetail="ORDER" assetCategory="STK" execID="0003.cccc"
               conid="265598" symbol="AAPL" buySell="BUY" quantity="10"
               tradePrice="150.25" ibCommission="-1.00" orderType="LMT"
               orderID="123" accountId="DU111111" dateTime="20260515;143000"/>
        <Trade levelOfDetail="EXECUTION" assetCategory="OPT" execID="0004.dddd"
               conid="999999" symbol="AAPL  260619C" buySell="BUY" quantity="1"
               tradePrice="2.50" ibCommission="-0.65" orderType="LMT"
               orderID="200" accountId="DU111111" dateTime="20260515;143000"/>
        <Trade levelOfDetail="EXECUTION" assetCategory="STK"
               conid="111" symbol="NOEXEC" buySell="BUY" quantity="5"
               tradePrice="10.0" ibCommission="-1.0" orderType="MKT"
               orderID="300" accountId="DU111111" dateTime="20260515;143000"/>
      </Trades>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>"""


def test_parses_execution_rows_only():
    """ORDER-level rows are dropped; only EXECUTION (and level-less) survive."""
    rows = parse_flex_trades(_SAMPLE)
    # 2 STK executions + 1 level-less STK row (NOEXEC has no execID -> dropped);
    # the ORDER row and the OPT row are filtered.
    exec_ids = {r["exec_id"] for r in rows}
    assert "0001.aaaa" in exec_ids
    assert "0002.bbbb" in exec_ids
    assert "0003.cccc" not in exec_ids   # ORDER level filtered
    assert "0004.dddd" not in exec_ids   # non-STK filtered
    # NOEXEC row has no execID attribute -> skipped (can't dedup it safely)
    assert all(r["symbol"] != "NOEXEC" for r in rows)
    assert len(rows) == 2


def test_dict_shape_matches_get_executions():
    """Every key get_executions emits must be present (reconciler depends on them)."""
    rows = parse_flex_trades(_SAMPLE)
    required = {
        "exec_id", "order_id", "perm_id", "parent_order_id", "account",
        "symbol", "conid", "side", "order_type", "shares", "price",
        "commission", "realized_pnl", "exec_time",
    }
    assert required.issubset(rows[0].keys())


def test_side_and_share_sign_normalisation():
    rows = {r["exec_id"]: r for r in parse_flex_trades(_SAMPLE)}
    buy = rows["0001.aaaa"]
    sell = rows["0002.bbbb"]
    assert buy["side"] == "BUY"
    assert sell["side"] == "SELL"
    # Flex emits negative quantity on sells; parser stores absolute share count.
    assert sell["shares"] == 10.0


def test_commission_sign_flipped_to_positive_cost():
    """Flex ibCommission is a signed debit; reconciler wants a positive cost."""
    buy = next(r for r in parse_flex_trades(_SAMPLE) if r["exec_id"] == "0001.aaaa")
    assert buy["commission"] == 1.00


def test_numeric_fields_typed():
    buy = next(r for r in parse_flex_trades(_SAMPLE) if r["exec_id"] == "0001.aaaa")
    assert buy["price"] == 150.25
    assert buy["conid"] == 265598
    assert buy["order_id"] == 123
    assert buy["shares"] == 10.0


def test_exec_time_parsed_to_naive_utc():
    buy = next(r for r in parse_flex_trades(_SAMPLE) if r["exec_id"] == "0001.aaaa")
    assert buy["exec_time"] == datetime(2026, 5, 15, 14, 30, 0)
    assert buy["exec_time"].tzinfo is None


def test_datetime_format_variants():
    assert _parse_flex_datetime("20260515;143000") == datetime(2026, 5, 15, 14, 30, 0)
    assert _parse_flex_datetime("20260515 143000") == datetime(2026, 5, 15, 14, 30, 0)
    assert _parse_flex_datetime("2026-05-15 14:30:00") == datetime(2026, 5, 15, 14, 30, 0)
    assert _parse_flex_datetime("") is None
    assert _parse_flex_datetime(None) is None
    assert _parse_flex_datetime("garbage") is None


def test_source_tz_converts_to_utc():
    """ET fills are shifted to UTC-naive so they match Phase B's convention."""
    rows = {r["exec_id"]: r for r in
            parse_flex_trades(_SAMPLE, source_tz="America/New_York")}
    # 2026-05-15 is EDT (UTC-4): 14:30 ET -> 18:30 UTC.
    assert rows["0001.aaaa"]["exec_time"] == datetime(2026, 5, 15, 18, 30, 0)
    assert rows["0001.aaaa"]["exec_time"].tzinfo is None


def test_source_tz_none_leaves_time_untouched():
    rows = {r["exec_id"]: r for r in parse_flex_trades(_SAMPLE, source_tz=None)}
    assert rows["0001.aaaa"]["exec_time"] == datetime(2026, 5, 15, 14, 30, 0)
