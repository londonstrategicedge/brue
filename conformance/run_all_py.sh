#!/bin/sh
# Conformance: the v2 corpus against the locked goldens, byte-exact stdout
# compare, plus direct assertions for the deliberate spec deviations.
# Self-contained: the bar fixtures are committed under v2/fixtures, generated
# once and deterministically, and frozen alongside the goldens they produced.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
CORPUS="$HERE/v2/corpus"
PY="${PYTHON:-python3}"
OUT="$HERE/out"
mkdir -p "$OUT"

for f in bars_small.csv bars_med.csv bars_big.csv; do
  [ -f "$OUT/$f" ] || cp "$HERE/v2/fixtures/$f" "$OUT/$f"
done

pass=0
fail=0
failed_cases=""
# Deliberate spec deviations (the python engine is the language of record).
# These cases assert the intended behavior directly rather than by golden
# comparison, because each one fixes a semantic that a plain byte-compare
# could not express (for example NaN-warmup handling, where the intended
# value is a number rather than NaN). The corpus below covers every
# construct of the language plus the loud-error cases.

# (a) the header is dead: strategy() anywhere is a loud parse error.
printf 'strategy("T", capital=10000)\nx = 1\n' > "$OUT/dev_header.brue"
if (cd "$ROOT" && "$PY" -m brue run "$OUT/dev_header.brue" "$OUT/bars_med.csv") 2>/dev/null \
    | grep -q "not part of the language"; then
  pass=$((pass+1))
else
  fail=$((fail+1)); failed_cases="$failed_cases v2_header_dead"
fi
# (d) NaN-warmup smoothing survives in v2: ema over tsi goes finite.
printf 't = tsi(close, 25, 13)\ns = ema(t, 13)\nlog(str(s))\n' > "$OUT/dev_nanwarm.brue"
if (cd "$ROOT" && "$PY" -m brue run "$OUT/dev_nanwarm.brue" "$OUT/bars_med.csv") 2>/dev/null \
    | grep -qE '"message":"-?[0-9]'; then
  pass=$((pass+1))
else
  fail=$((fail+1)); failed_cases="$failed_cases fix_nan_warmup"
fi
# (e) member access on a multi-value result stays a loud error in v2.
printf 'm = macd(close, 12, 26, 9)\nx = m.macd\n' > "$OUT/dev_member.brue"
if (cd "$ROOT" && "$PY" -m brue run "$OUT/dev_member.brue" "$OUT/bars_med.csv") 2>/dev/null \
    | grep -q 'destructure instead'; then
  pass=$((pass+1))
else
  fail=$((fail+1)); failed_cases="$failed_cases fix_member_loud"
fi
# (f) host string override for a numeric input coerces; garbage is loud.
if (cd "$ROOT" && "$PY" -c "
from brue.api import indicator_series
bars = [[1700000000000 + i*60000, 100, 101, 99, 100, 5] for i in range(30)]
src = 'lvl = input(101.5, \"Level\")\nplot(lvl, title=\"lvl\")\n'
out = indicator_series(src, bars, inputs={'input_0': '55.5'})
assert out['plots']['lvl'][-1] == 55.5, out['plots']['lvl'][-1]
try:
    indicator_series(src, bars, inputs={'input_0': 'junk'})
    raise SystemExit(1)
except Exception as e:
    assert 'cannot be read as' in str(e), e
") 2>/dev/null; then
  pass=$((pass+1))
else
  fail=$((fail+1)); failed_cases="$failed_cases fix_override_coerce"
fi
# (h) e12_no_strategy: a script has no header; the old "must start with
# strategy()" rule does not exist. This
# one places no orders, so it is an INDICATOR script and bare series are
# legal (the chart is its context): it now simply runs. The trading-script
# bare-series rejection is asserted separately below.
if (cd "$ROOT" && "$PY" -m brue run "$CORPUS/e12_no_strategy.brue" "$OUT/bars_small.csv") 2>/dev/null \
    | grep -q '"success":true'; then
  pass=$((pass+1))
else
  fail=$((fail+1)); failed_cases="$failed_cases fix_e12_v2_bare_series"
fi

# (i) the split rule: one order verb makes a script a TRADING script and
# bare series become loud; the same line is legal in an indicator script.
printf 'x = close\nbuy("EURUSD", 1)\n' > "$OUT/dev_v2_bare_trading.brue"
if (cd "$ROOT" && "$PY" -m brue run "$OUT/dev_v2_bare_trading.brue" "$OUT/bars_small.csv") 2>/dev/null \
    | grep -q "bare .close. does not exist in a trading script"; then
  pass=$((pass+1))
else
  fail=$((fail+1)); failed_cases="$failed_cases v2_bare_series_trading"
fi

# == corpus: every construct of the language plus the loud-error cases ==
# Each case in conformance/v2/cases.tsv runs with its canonical args and
# byte-compares against the locked v2 golden (error cases grep for the
# expected loud message). Regenerate goldens with V2_REGEN=1 after a
# deliberate, reviewed semantic change; never to silence a failure.
V2="$HERE/v2"
TAB="$(printf '\t')"
# Split each row by hand: `IFS=tab read a b c` collapses consecutive tabs
# (tab is IFS whitespace), so a row with an empty extra_args column would
# shift its expect value into $extra, run the engine with a stray argument
# and compare empty output against an empty golden.
while IFS= read -r line; do
  case "$line" in \#*|"") continue;; esac
  stem="${line%%"$TAB"*}"
  rest="${line#*"$TAB"}"
  [ "$rest" = "$line" ] && rest=""
  extra="${rest%%"$TAB"*}"
  expect="${rest#*"$TAB"}"
  [ "$expect" = "$rest" ] && expect=""
  out_v2="$OUT/v2_${stem}.json"
  (cd "$ROOT" && "$PY" -m brue run "conformance/v2/corpus/${stem}.brue" \
      "conformance/out/bars_med.csv" $extra) > "$out_v2" 2>/dev/null || true
  case "$expect" in
    error:*)
      want="${expect#error:}"
      if grep -q '"success":false' "$out_v2" && grep -q "$want" "$out_v2"; then
        pass=$((pass+1))
      else
        fail=$((fail+1)); failed_cases="$failed_cases v2_${stem}"
      fi;;
    *)
      if [ "${V2_REGEN:-0}" = "1" ]; then
        cp "$out_v2" "$V2/golden/${stem}.json"; pass=$((pass+1))
      elif cmp -s "$out_v2" "$V2/golden/${stem}.json"; then
        pass=$((pass+1))
      else
        fail=$((fail+1)); failed_cases="$failed_cases v2_${stem}"
      fi;;
  esac
done < "$V2/cases.tsv"

echo "conformance: pass=$pass fail=$fail"
[ -n "$failed_cases" ] && echo "failed:$failed_cases" | tr ' ' '\n' | head -40
[ "$fail" -eq 0 ]
