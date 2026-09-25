# HarborCart Checkout Service Runbook

## Overview
`checkout-api` serves the cart and order pages (`GET /cart`,
`GET /orders/{id}`) and cart-to-order conversion (`POST /checkout`). For a
checkout it validates the cart, opens a database transaction against
`checkout_db`, calls the external payment gateway, and commits the order on
a successful charge. All endpoints share one connection pool per instance.
It runs as 6 instances; the logs, traces, and metrics in this packet come
from `checkout-api-3`, the instance that paged first. `checkout_5xx_rate`
covers every endpoint on the instance.

## Dependencies
- PostgreSQL (`checkout_db`), accessed through a SQLAlchemy `QueuePool`
  with `pool_size=5`, `max_overflow=10` (15 connections total per
  instance), `pool_timeout=10` seconds.
- Payment gateway (`payments.harborcart.internal`), a third-party service
  outside our infrastructure. Its documented SLA is 99.9% availability;
  short error bursts during its own deploys are not unusual and normally
  self-resolve in under two minutes.

## Known alert: checkout 5xx rate above 2%
1. Check `checkout_5xx_rate` and `payment_gateway_5xx_rate` on the
   checkout dashboard. If the gateway rate is also elevated, note when it
   started and when it returned to zero.
2. Check `db_pool_in_use` against the pool capacity (15). Pool
   exhaustion produces 503s from `checkout-api` itself (connection checkout
   timeout), which look identical to the client as a generic 503 but
   originate from the application, not the gateway.
3. Compare when checkout 503s stop with when gateway 503s stop. A checkout
   503 tail that outlasts the gateway incident points at something inside
   checkout-api, not the gateway itself.
4. Check whether a deploy to `checkout-api` (not the gateway) preceded the
   incident.

## Connection handling contract
`checkout-api` acquires its database connection at the start of
`handle_checkout()` and holds it open through validation, the payment
gateway call, and the final commit. This is intentional: HarborCart wants
a single atomic transaction per order rather than a two-phase commit. The
tradeoff is that the connection's hold time is proportional to however
long the payment call takes, including any retries the HTTP client
performs before giving up.

## Rollback
`kubectl rollout undo deployment/checkout-api` reverts to the previous
image. Rolling back does not require a database migration.
