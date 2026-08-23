#!/bin/bash
set -e
cd "$(dirname "$0")"
PY=../.venv/bin/python
SCRIPT=ha_prevbreak_options_backtest.py

run() {
    label=$1; shift
    out="ha_prevbreak_options_results_${label}.json"
    $PY $SCRIPT --csv "$1" --label "$2" --entry-mode "$3" --same-bar "$4" --out "$out"
}

# 5-minute, full history
run 5m_close_target    ../data/NSEI_index_5m.csv 5m close target
run 5m_close_stop      ../data/NSEI_index_5m.csv 5m close stop
run 5m_break_target    ../data/NSEI_index_5m.csv 5m break target
run 5m_break_stop      ../data/NSEI_index_5m.csv 5m break stop
run 5m_break_heuristic ../data/NSEI_index_5m.csv 5m break heuristic

# 1-minute, 6 days
run 1m_close_target    ../data/NSEI_index_1m.csv 1m close target
run 1m_close_stop      ../data/NSEI_index_1m.csv 1m close stop
run 1m_break_target    ../data/NSEI_index_1m.csv 1m break target
run 1m_break_stop      ../data/NSEI_index_1m.csv 1m break stop
run 1m_break_heuristic ../data/NSEI_index_1m.csv 1m break heuristic
