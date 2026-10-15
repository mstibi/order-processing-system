// M7 load test: N virtual users create orders for one product with plenty of stock.
//   seed:  docker compose exec -T postgres psql -U app -d shop -f - < load/seed.sql
//   run:   k6 run load/orders.js                                  (k6 on the host, smoke only)
//          docker compose --profile load run --rm k6              (k6 inside the compose network: numbers for the README)
// Env: BASE_URL, VUS (default 50), DURATION (default 30s), OUT (default results/load.json).
// Run fake-payments with FAIL_RATE=0 TIMEOUT_RATE=0 for headline numbers, else 3 s timeouts dominate p99.
import http from 'k6/http';
import { check } from 'k6';

const BASE = __ENV.BASE_URL || 'http://localhost:8000';
const VUS = Number(__ENV.VUS || 50);
const DURATION = __ENV.DURATION || '30s';
const RUN = Date.now(); // keeps Idempotency-Keys unique across runs

export const options = {
  scenarios: { orders: { executor: 'constant-vus', vus: VUS, duration: DURATION } },
  summaryTrendStats: ['avg', 'med', 'p(50)', 'p(95)', 'p(99)', 'max'],
  thresholds: { checks: ['rate>0.99'] }, // correctness gate only; latency is reported, not enforced (noisy runners)
};

export default function () {
  const body = JSON.stringify({ customer_id: __VU, items: [{ product_id: 1000000, qty: 1 }] });
  const res = http.post(`${BASE}/orders`, body, {
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': `${RUN}-${__VU}-${__ITER}` },
  });
  check(res, { 'order is PAID (201)': (r) => r.status === 201 });
}

export function handleSummary(data) {
  const d = data.metrics.http_req_duration.values;
  const out = {
    vus: VUS, duration: DURATION,
    requests: data.metrics.http_reqs.values.count,
    rps: Math.round(data.metrics.http_reqs.values.rate),
    p50_ms: d['p(50)'], p95_ms: d['p(95)'], p99_ms: d['p(99)'], max_ms: d.max,
    checks_passed_rate: data.metrics.checks.values.rate,
  };
  const json = JSON.stringify(out, null, 2);
  return { [__ENV.OUT || 'results/load.json']: json, stdout: json + '\n' };
}
