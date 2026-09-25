#!/bin/bash
# Все тесты. Без аргументов — на SQLite. С PG_URL=postgresql://user@host:port — каждый набор
# на своей чистой базе t_<набор> (нужны права CREATE DATABASE).
cd "$(dirname "$0")/.."
PY=${PY:-venv/bin/python}
rc=0
for t in tests/smoke.py tests/gate*.py; do
  n=$(basename "$t" .py)
  if [ -n "$PG_URL" ]; then
    psql "$PG_URL/postgres" -qc "DROP DATABASE IF EXISTS t_$n" -c "CREATE DATABASE t_$n" >/dev/null || exit 1
    out=$(DATABASE_URL="$PG_URL/t_$n" timeout 600 "$PY" "$t" 2>&1); code=$?
  else
    out=$(timeout 600 "$PY" "$t" 2>&1); code=$?
  fi
  fails=$(grep -c '^\[FAIL' <<<"$out")
  echo "$([ $code -eq 0 ] && echo OK || echo FAIL)  $n  (fail-проверок: $fails)"
  [ $code -eq 0 ] || { rc=1; tail -5 <<<"$out" | sed 's/^/    /'; }
done
exit $rc
