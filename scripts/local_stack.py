"""Run the whole stack WITHOUT Docker (Windows fallback, used for the README numbers while Docker was missing).

    python scripts/local_stack.py          # Postgres 16 (pip package pgserver) + fake-payments :9000 + api :8000
    FAIL_RATE=0.2 TIMEOUT_RATE=0.1 python scripts/local_stack.py

If the api process dies (chaos.py --restart-cmd kills it), it is started again, like `restart: always`.
Prints the DATABASE_URL to use for the scripts. Ctrl+C stops everything.
"""
import os
import pathlib
import subprocess
import sys
import time

import pgserver
import psycopg

ROOT = pathlib.Path(__file__).resolve().parents[1]
PY = sys.executable
# uvicorn on Windows: psycopg async needs the selector event loop
API_CMD = [PY, "-c", "import asyncio, uvicorn; asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy());"
                     " uvicorn.run('app.main:app', host='127.0.0.1', port=8000, log_level='warning')"]

srv = pgserver.get_server(ROOT / ".pgdata", cleanup_mode=None)
if "shop" not in srv.psql("SELECT datname FROM pg_database;"):
    srv.psql("CREATE DATABASE shop;")
url = srv.get_uri("shop")
with psycopg.connect(url, autocommit=True) as conn:
    if conn.execute("SELECT to_regclass('products')").fetchone()[0] is None:
        conn.execute((ROOT / "db/schema.sql").read_text())

env = {**os.environ, "DATABASE_URL": url, "PAYMENTS_URL": "http://127.0.0.1:9000"}
pay = subprocess.Popen([PY, "-m", "uvicorn", "app:app", "--port", "9000", "--log-level", "warning"], cwd=ROOT / "fake-payments", env=env)
print(f"DATABASE_URL={url}", flush=True)
api = None
try:
    while True:
        if api is None or api.poll() is not None:
            if api is not None:
                print(f"api exited ({api.returncode}), restarting", flush=True)
            api = subprocess.Popen(API_CMD, cwd=ROOT / "api", env=env)
            (ROOT / ".api.pid").write_text(str(api.pid))
        time.sleep(0.2)
except KeyboardInterrupt:
    pass
finally:
    for p in (api, pay):
        p.terminate()
