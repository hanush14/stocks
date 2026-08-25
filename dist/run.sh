#!/usr/bin/env bash
set -u
cd "$(dirname "$0")"
echo "== 1/3 source reachability =="; python3 ledger.pyz status
echo; echo "== 2/3 offline self-test =="; python3 ledger.pyz selftest
echo; echo "== 3/3 live fetch =="; python3 ledger.pyz probe --year 2024
