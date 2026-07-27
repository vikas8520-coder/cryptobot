#!/usr/bin/env python3
"""
crypto_tax_analysis_test.py — test harness for crypto tax calculations (one-shot, not auto-run).

WHY: crypto_tax_analysis.py has zero test coverage despite handling financial calculations
that must be exact (India tax regime: 30% on gains + 1% TDS on every sell + no loss offset).
This file tests the core functions deterministically with synthetic data.

Checks:
  (a) spot_realized_after_tax() calculates TDS and gain tax correctly per trade
  (b) brake_signals() generates correct 200DMA cross signals
  (c) metrics_from_equity() computes CAGR and drawdown accurately
  (d) edge cases: zero pnl, negative pnl, empty results, division safety

Prints PASS/FAIL with actual numbers. Exit 0 on PASS, 1 on FAIL.
"""
import os
import sys
import sqlite3
import tempfile
import pandas as pd
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import crypto_tax_analysis as cta


def test_spot_realized_after_tax():
    """Test (a): spot ledger tax calculation accuracy with synthetic trades."""
    print("[a] Testing spot_realized_after_tax() with synthetic ledger")
    
    # Create temporary SQLite with test trades
    with tempfile.NamedTemporaryFile(mode='w', suffix='.sqlite', delete=False) as f:
        db_path = f.name
    
    try:
        con = sqlite3.connect(db_path)
        con.execute("""
            CREATE TABLE trades (
                pair TEXT, open_rate REAL, close_rate REAL, amount REAL,
                close_profit_abs REAL, fee_open_cost REAL, fee_close_cost REAL,
                exit_reason TEXT, is_open INTEGER, close_date TEXT
            )
        """)
        
        # Trade 1: profitable trade ($100 pnl)
        con.execute("""
            INSERT INTO trades VALUES 
            ('BTC-USD', 50000.0, 51000.0, 1.0, 100.0, 5.0, 5.0, 'signal', 0, '2024-01-01')
        """)
        
        # Trade 2: losing trade (-$50 pnl, should not be taxed)
        con.execute("""
            INSERT INTO trades VALUES 
            ('ETH-USD', 3000.0, 2900.0, 1.0, -50.0, 3.0, 3.0, 'stop', 0, '2024-01-02')
        """)
        
        # Trade 3: break-even trade ($0 pnl)
        con.execute("""
            INSERT INTO trades VALUES 
            ('SOL-USD', 100.0, 100.0, 10.0, 0.0, 1.0, 1.0, 'signal', 0, '2024-01-03')
        """)
        
        con.commit()
        con.close()
        
        result = cta.spot_realized_after_tax(db_path)
        
        if result is None:
            print("FAIL (a): spot_realized_after_tax returned None")
            return 1
        
        if result['n'] != 3:
            print(f"FAIL (a): expected 3 trades, got {result['n']}")
            return 1
        
        # Trade 1: $100 pnl, exit_value = 1.0 * 51000 = $51000
        # TDS = 0.01 * 51000 = $510
        # gain_tax = 0.30 * 100 = $30
        # net = 100 - 510 - 30 = -$440
        trade1 = result['trades'][0]
        expected_tds = 510.0
        expected_gain_tax = 30.0
        expected_net = -440.0
        
        if abs(trade1['tds'] - expected_tds) > 0.01:
            print(f"FAIL (a): trade1 TDS expected {expected_tds}, got {trade1['tds']}")
            return 1
        if abs(trade1['gain_tax'] - expected_gain_tax) > 0.01:
            print(f"FAIL (a): trade1 gain_tax expected {expected_gain_tax}, got {trade1['gain_tax']}")
            return 1
        if abs(trade1['net'] - expected_net) > 0.01:
            print(f"FAIL (a): trade1 net expected {expected_net}, got {trade1['net']}")
            return 1
        
        # Trade 2: -$50 pnl, should have 0 gain tax (no loss offset)
        trade2 = result['trades'][1]
        if trade2['gain_tax'] != 0.0:
            print(f"FAIL (a): trade2 gain_tax should be 0 for loss, got {trade2['gain_tax']}")
            return 1
        
        # Trade 3: $0 pnl, should have 0 gain tax
        trade3 = result['trades'][2]
        if trade3['gain_tax'] != 0.0:
            print(f"FAIL (a): trade3 gain_tax should be 0 for break-even, got {trade3['gain_tax']}")
            return 1
        
        # Check totals
        expected_pre_total = 100.0 + (-50.0) + 0.0  # 50.0
        expected_post_total = -440.0 + (-50.0 - 0.01 * 2900.0) + (0.0 - 0.01 * 1000.0)
        
        if abs(result['pre'] - expected_pre_total) > 0.01:
            print(f"FAIL (a): pre_total expected {expected_pre_total}, got {result['pre']}")
            return 1
        
        print(f"[a] PASS: {result['n']} trades, pre-tax ${result['pre']:.2f}, "
              f"after-tax ${result['post']:.2f}, tax drag ${result['pre']-result['post']:.2f}")
        return 0
        
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_spot_realized_after_tax_empty():
    """Test (a) edge case: empty ledger returns None."""
    print("[a-edge] Testing spot_realized_after_tax() with empty ledger")
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.sqlite', delete=False) as f:
        db_path = f.name
    
    try:
        con = sqlite3.connect(db_path)
        con.execute("""
            CREATE TABLE trades (
                pair TEXT, open_rate REAL, close_rate REAL, amount REAL,
                close_profit_abs REAL, fee_open_cost REAL, fee_close_cost REAL,
                exit_reason TEXT, is_open INTEGER, close_date TEXT
            )
        """)
        con.commit()
        con.close()
        
        result = cta.spot_realized_after_tax(db_path)
        
        if result is not None:
            print("FAIL (a-edge): expected None for empty ledger, got result")
            return 1
        
        print("[a-edge] PASS: empty ledger returns None")
        return 0
        
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_spot_realized_after_tax_missing_db():
    """Test (a) edge case: missing database file returns None."""
    print("[a-edge2] Testing spot_realized_after_tax() with missing database")
    
    result = cta.spot_realized_after_tax("/nonexistent/path/to/db.sqlite")
    
    if result is not None:
        print("FAIL (a-edge2): expected None for missing database, got result")
        return 1
    
    print("[a-edge2] PASS: missing database returns None")
    return 0


def test_spot_realized_after_tax_invalid_data():
    """Test (a) edge case: invalid data is skipped gracefully."""
    print("[a-edge3] Testing spot_realized_after_tax() with invalid data")
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.sqlite', delete=False) as f:
        db_path = f.name
    
    try:
        con = sqlite3.connect(db_path)
        con.execute("""
            CREATE TABLE trades (
                pair TEXT, open_rate REAL, close_rate REAL, amount REAL,
                close_profit_abs REAL, fee_open_cost REAL, fee_close_cost REAL,
                exit_reason TEXT, is_open INTEGER, close_date TEXT
            )
        """)
        
        # Valid trade
        con.execute("""
            INSERT INTO trades VALUES 
            ('BTC-USD', 50000.0, 51000.0, 1.0, 100.0, 5.0, 5.0, 'signal', 0, '2024-01-01')
        """)
        
        # Invalid trade: negative close_rate
        con.execute("""
            INSERT INTO trades VALUES 
            ('ETH-USD', 3000.0, -100.0, 1.0, -50.0, 3.0, 3.0, 'stop', 0, '2024-01-02')
        """)
        
        # Invalid trade: zero amount
        con.execute("""
            INSERT INTO trades VALUES 
            ('SOL-USD', 100.0, 100.0, 0.0, 0.0, 1.0, 1.0, 'signal', 0, '2024-01-03')
        """)
        
        con.commit()
        con.close()
        
        result = cta.spot_realized_after_tax(db_path)
        
        if result is None:
            print("FAIL (a-edge3): expected result with 1 valid trade, got None")
            return 1
        
        if result['n'] != 1:
            print(f"FAIL (a-edge3): expected 1 valid trade, got {result['n']}")
            return 1
        
        print("[a-edge3] PASS: invalid data skipped, 1 valid trade processed")
        return 0
        
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_brake_signals():
    """Test (b): 200DMA brake signal generation."""
    print("[b] Testing brake_signals() with synthetic price data")
    
    # Create synthetic price data with known crossover points
    # Need 300+ days for 200DMA to be valid
    dates = pd.date_range('2024-01-01', periods=400, freq='D')
    prices = []
    
    # First 200 days: price at 100 (establish SMA baseline)
    for i in range(200):
        prices.append(100.0)
    
    # Days 200-210: price rises above SMA (should generate entry signal)
    for i in range(10):
        prices.append(150.0)
    
    # Days 210-350: price above SMA (should stay in position)
    for i in range(140):
        prices.append(160.0)
    
    # Days 350-400: price falls below SMA (should generate exit signal)
    for i in range(50):
        prices.append(90.0)
    
    df = pd.DataFrame({'Date': dates, 'Close': prices})
    
    entry, exit_ = cta.brake_signals(df)
    
    # Check that entry signals are generated at the crossover
    entry_points = entry[entry].index.tolist()
    if len(entry_points) == 0:
        print("FAIL (b): no entry signals generated")
        return 1
    
    # First entry should be around day 200-201 (when price crosses above SMA)
    first_entry = entry_points[0]
    if first_entry < 195 or first_entry > 205:
        print(f"FAIL (b): first entry at {first_entry}, expected around 200")
        return 1
    
    # Check that exit signals are generated
    exit_points = exit_[exit_].index.tolist()
    if len(exit_points) == 0:
        print("FAIL (b): no exit signals generated")
        return 1
    
    # First exit should be around day 350-351 (when price crosses below SMA)
    first_exit = exit_points[0]
    if first_exit < 345 or first_exit > 355:
        print(f"FAIL (b): first exit at {first_exit}, expected around 350")
        return 1
    
    print(f"[b] PASS: entry at {first_entry}, exit at {first_exit}")
    return 0


def test_metrics_from_equity():
    """Test (c): equity metrics calculation accuracy."""
    print("[c] Testing metrics_from_equity() with synthetic equity curve")
    
    # Create synthetic equity curve: $1000 -> $1500 over 1 year (50% return)
    dates = pd.date_range('2024-01-01', periods=365, freq='D')
    equity_values = np.linspace(1000.0, 1500.0, 365)
    
    # Add a drawdown in the middle
    equity_values[100:150] = np.linspace(1500.0, 1200.0, 50)
    equity_values[150:200] = np.linspace(1200.0, 1500.0, 50)
    
    equity = pd.Series(equity_values, index=dates)
    
    metrics = cta.metrics_from_equity(equity)
    
    # Total return should be ~50%
    expected_total = 50.0
    if abs(metrics['total_return_pct'] - expected_total) > 1.0:
        print(f"FAIL (c): total_return_pct expected {expected_total}, got {metrics['total_return_pct']}")
        return 1
    
    # CAGR should be ~50% (1 year)
    expected_cagr = 50.0
    if abs(metrics['cagr_pct'] - expected_cagr) > 1.0:
        print(f"FAIL (c): cagr_pct expected {expected_cagr}, got {metrics['cagr_pct']}")
        return 1
    
    # Max drawdown should be ~20% (from 1500 to 1200)
    expected_dd = -20.0
    if abs(metrics['max_drawdown_pct'] - expected_dd) > 2.0:
        print(f"FAIL (c): max_drawdown_pct expected {expected_dd}, got {metrics['max_drawdown_pct']}")
        return 1
    
    print(f"[c] PASS: total {metrics['total_return_pct']:.2f}%, "
          f"CAGR {metrics['cagr_pct']:.2f}%, maxDD {metrics['max_drawdown_pct']:.2f}%")
    return 0


def test_metrics_edge_cases():
    """Test (c) edge cases: empty equity, single point, zero time."""
    print("[c-edge] Testing metrics_from_equity() edge cases")
    
    # Empty equity (should return zeros gracefully)
    empty_equity = pd.Series([], index=pd.DatetimeIndex([]))
    try:
        metrics = cta.metrics_from_equity(empty_equity)
        if metrics['total_return_pct'] != 0.0 or metrics['cagr_pct'] != 0.0:
            print(f"FAIL (c-edge): empty equity should return zeros, got {metrics}")
            return 1
        print("[c-edge] PASS: empty equity handled gracefully")
    except Exception as e:
        print(f"FAIL (c-edge): empty equity raised exception: {e}")
        return 1
    
    # Single point equity (should not crash)
    dates = pd.date_range('2024-01-01', periods=1, freq='D')
    equity = pd.Series([1000.0], index=dates)
    
    try:
        metrics = cta.metrics_from_equity(equity)
        # With insufficient data, should return zeros
        if metrics['total_return_pct'] != 0.0 or metrics['cagr_pct'] != 0.0:
            print(f"FAIL (c-edge): single point should return zeros, got {metrics}")
            return 1
        print("[c-edge] PASS: single point handled gracefully")
    except Exception as e:
        print(f"FAIL (c-edge): single point raised exception: {e}")
        return 1
    
    # None equity (should return zeros gracefully)
    try:
        metrics = cta.metrics_from_equity(None)
        if metrics['total_return_pct'] != 0.0 or metrics['cagr_pct'] != 0.0:
            print(f"FAIL (c-edge): None equity should return zeros, got {metrics}")
            return 1
        print("[c-edge] PASS: None equity handled gracefully")
    except Exception as e:
        print(f"FAIL (c-edge): None equity raised exception: {e}")
        return 1
    
    return 0


def test_brake_signals_edge_cases():
    """Test (b) edge cases: empty DataFrame, missing columns, insufficient data."""
    print("[b-edge] Testing brake_signals() edge cases")
    
    # Empty DataFrame
    empty_df = pd.DataFrame()
    entry, exit_ = cta.brake_signals(empty_df)
    if not entry.empty or not exit_.empty:
        print("FAIL (b-edge): empty DataFrame should return empty Series")
        return 1
    print("[b-edge] PASS: empty DataFrame handled")
    
    # DataFrame without Close column
    bad_df = pd.DataFrame({'Open': [100.0, 101.0], 'High': [102.0, 103.0]})
    entry, exit_ = cta.brake_signals(bad_df)
    if not entry.empty or not exit_.empty:
        print("FAIL (b-edge): missing Close column should return empty Series")
        return 1
    print("[b-edge] PASS: missing Close column handled")
    
    # Insufficient data (<200 rows)
    short_df = pd.DataFrame({'Close': [100.0] * 150})
    entry, exit_ = cta.brake_signals(short_df)
    if not entry.empty or not exit_.empty:
        print("FAIL (b-edge): insufficient data should return empty Series")
        return 1
    print("[b-edge] PASS: insufficient data handled")
    
    return 0


def main():
    print("=" * 72)
    print("CRYPTO TAX ANALYSIS TEST SUITE")
    print("=" * 72)
    print()
    
    failures = []
    
    # Run all tests
    tests = [
        test_spot_realized_after_tax,
        test_spot_realized_after_tax_empty,
        test_spot_realized_after_tax_missing_db,
        test_spot_realized_after_tax_invalid_data,
        test_brake_signals,
        test_brake_signals_edge_cases,
        test_metrics_from_equity,
        test_metrics_edge_cases,
    ]
    
    for test in tests:
        try:
            result = test()
            if result != 0:
                failures.append(test.__name__)
        except Exception as e:
            print(f"EXCEPTION in {test.__name__}: {e}")
            failures.append(test.__name__)
        print()
    
    # Summary
    print("=" * 72)
    if failures:
        print(f"FAIL: {len(failures)} test(s) failed: {', '.join(failures)}")
        return 1
    else:
        print(f"PASS: all {len(tests)} tests passed")
        return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
