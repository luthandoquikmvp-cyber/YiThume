# YiThume — create your marketplace. The runners are already there.

YiThume is the layer between hard-to-reach markets and their customers. It is **not
a delivery business** — it is software that lets anyone stand up their own
marketplace, with a courier network already underneath it.

The runner network is the part that already exists, and it is what makes a
brand-new marketplace able to deliver on its first day. Marketplaces are what
people build on top of it.

See [POSITIONING.md](POSITIONING.md).

## What's here

| Surface | URL | What it does |
|---|---|---|
| **Landing** | `/` | The pitch, live coverage, and self-serve marketplace creation |
| **Directory** | `/markets` | Every live marketplace, searchable by town |
| **Storefront** | `/m/<slug>` | A tenant's branded shop: their sellers, their prices, wallet/COD/EFT checkout, order tracking, seller applications |
| **Operator console** | `/console` | Phone + PIN. Setup checklist, orders, products, sellers, money, storefront settings |
| **Platform admin** | `/dashboard` | PIN-gated. Every tenant, the runner network, carriers, pickup points, ledger audit, withdrawals, disputes |
| **Runner app** | `/runner` | Job queue across every marketplace the runner serves, accept / collect / deliver by PIN, cash balance, remittances |
| **Kiosk** | `/kiosk` | Pickup-point operator: mark parcels arrived, hand over against the buyer's PIN |
| **USSD** | `/ussd` | Simulator for the feature-phone flow (choose market → shop → order → track → wallet) |
| **API** | `/api/*` | JSON API over the database (`api/app.py`) |

## Run locally

```bash
pip install -r requirements.txt
python api/app.py
# open http://localhost:5000
```

With no `MONGO_URI` set, the app runs on a built-in in-memory demo store with seed
data (resets on restart). Set `MONGO_URI` to persist in MongoDB.

**Demo logins** (seeded):

| Who | Where | Credentials |
|---|---|---|
| Platform admin | `/dashboard` | PIN `1234` |
| Mthatha Town Market (verified) | `/console` | `27720000001` / `1234` |
| Kei Fresh Market (unverified) | `/console` | `27720000002` / `4321` |
| Shared-pool runner | `/runner` | `27730000001` |
| Kiosk operator | `/kiosk` | `27750000001` |

Don't run the dev server with `FLASK_DEBUG=1` on anything public — Werkzeug's
debugger both shadows `/console` and exposes a remote Python shell.

## Tenancy

A **marketplace is the tenant**. Products, sellers, orders, nodes and pickup points
all carry `marketplace_id`, and every protected endpoint resolves one actor:

- **platform** — `X-Admin-Pin` header matching `ADMIN_SECRET`; sees every tenant.
- **operator** — `X-Console-Token` from `POST /api/console/login`; sees exactly one.

`current_actor()` / `scoped()` / `owns()` in `api/app.py` do this once, so handlers
share a code path instead of having platform and operator copies. The admin PIN is
**header-only** — it is not accepted as a query parameter.

Anyone can create a marketplace from `/`: name, areas, phone, PIN → OTP → the
storefront is live at `/m/<slug>` and the operator is signed in.

## The runner network

`runners.marketplace_id == ""` means the **shared YiThume pool** — the default, and
the reason a new marketplace has delivery immediately. A marketplace can also
recruit its own runners, who get first pick of its jobs.

A runner may take an order when they belong to that marketplace, or when they are
in the shared pool and the marketplace has `use_shared_runners` on. Job queues sort
by: own marketplace → prepaid → local area → oldest.

## Carriers and pickup points

Existing couriers already move parcels between towns and into kiosks. YiThume
treats that as a leg rather than a competitor, and owns the part they can't do —
the last mile to a house with no street address.

Orders carry a `fulfilment` block with legs:

```
confirmed → in_transit → at_point → assigned → picked_up → delivered
                       → at_point → collected            (collect at the point)
```

**The Courier Guy works today** as a manual carrier: an operator books on the
carrier's own system and records the waybill (`POST /api/orders/<id>/waybill`). No
API or partnership needed. `api/rails.py` shows the pattern a live carrier API
would follow.

Collecting at a point drops the last-mile leg and costs the buyer less — which is
why the prepaid-kiosk model is popular. The **same delivery PIN** proves handover at
a counter as at a door.

## Money

**Paystack is the rail** — not Stripe. Stripe cannot settle to South African bank
accounts (it requires a US/EU legal entity), so the payout half of the loop has
nowhere to land. Stripe's Africa arm is Paystack, which does the whole loop in ZAR:
card/EFT in, and a Transfers API that pays out to any SA bank.

```bash
export PAYSTACK_SECRET_KEY=sk_test_...   # test keys work the moment you sign up
export PAYSTACK_PUBLIC_KEY=pk_test_...
export PAYSTACK_WEBHOOK_SECRET=...       # optional; defaults to the secret key
```

Test mode is available before any compliance review. Going live needs a CIPC
certificate, a bank confirmation letter and director details (or a Sole
Proprietorship account while you register), and **Transfers must be enabled** on the
account before payouts work.

### The ledger

`api/ledger.py` is a double-entry journal. The rail holds one pooled balance; the
ledger records whose money it is. Two invariants are enforced, not trusted:

1. **Every post balances** — debits equal credits, or it raises.
2. **Every post is idempotent** on `kind:ref`, so a replayed webhook or a
   double-clicked button cannot move money twice.

`balance` and `held` on an account are a cache; the journal is the truth.
`GET /api/ledger/audit` recomputes one from the other and reports drift.

### The loop

1. **Top up** — buyer pays by card/EFT; the wallet is credited only after the rail
   confirms, never on the browser's word.
2. **Checkout** — a wallet order moves funds to `held` as escrow. Short balance is
   not a rejection: checkout offers to top up exactly the shortfall.
3. **Delivery** — the buyer's PIN releases escrow, split across seller, marketplace,
   runner, node, reserve and platform. Internal, instant, no bank run per order.
4. **Withdrawal** — requested → trust/hold/cooldown checks → **admin approves** →
   the Transfers API pays the bank account → webhook confirms. A failed transfer
   reverses cleanly.

Fees are **frozen onto each order at checkout**, so changing rates later only
affects new orders:

| Share | Of | Default | Set by |
|---|---|---|---|
| `marketplace_rate` | item subtotal | 10% | the operator |
| `network_rate` | item subtotal | 2% | platform |
| `runner_rate` | delivery fee | 80% | platform |
| `node_rate` | delivery fee | 19% | platform |
| `reserve_rate` | delivery fee | 1% | platform |

`runner_rate + node_rate + reserve_rate` may not exceed the delivery fee, and the
two commissions must leave the seller at least 10%. Both are validated on save.

## Who pays for a failed delivery

Previously the runner did: the trip happened, the goods travelled, and a buyer
no-show only earned the buyer a strike. That charges the side that makes the
network work, and it is how you lose runners.

Now the cost falls in order:

1. the buyer's wallet, if there is anything in it;
2. otherwise their balance goes negative and they are blocked from ordering **on
   any marketplace** until it clears;
3. the marketplace's **reserve** (funded by `reserve_rate` on every order) pays the
   runner their attempt fee immediately, either way;
4. an empty reserve goes negative — the platform backstop, recovered from that
   tenant's future earnings. This is why COD is capped hard for new marketplaces.

The runner is paid for a genuine attempt in every branch.

COD also carries a surcharge because it genuinely costs more to serve, and wallet
orders sort ahead of COD in the runner queue — so the protected path is the cheaper
and faster one rather than just the recommended one.

## Anti-fraud

| Control | Behaviour |
|---|---|
| Marketplace signup | Phone OTP required; one live marketplace per number; per-IP rate limit |
| Trust tiers | `unverified` → `verified` (bank details + 5 clean deliveries) → `trusted` (50+, dispute rate < 2%). Tier gates escrow caps and withdrawals, and shows on the storefront |
| Escrow | The seller is not paid until the buyer's PIN proves handover |
| Disputes | Opening one freezes the release; resolving for the buyer refunds them |
| Withdrawals | Admin approves every one; bank-detail changes freeze withdrawals 48h |
| Buyer strikes | No-show / refused-COD counts per phone number, network-wide; at the limit the buyer is prepay-only |
| COD cap | Orders above the cap must be paid from the wallet |
| Runner cash limit | A runner holding too much undeposited cash can't take new jobs |
| Order OTP | 6 digits; 5 wrong attempts cancels the order |
| Webhooks | HMAC-SHA512 signature checked; unsigned payloads rejected |

## Tests

```bash
python money_loop_test.py   # the R20 round trip: in → around → out
python smoke_test.py        # tenant isolation, runner scoping, frozen fees, fail-safes
```

`money_loop_test.py` prints every account's balance after each step. Paystack is
stubbed at the HTTP boundary so it runs offline — the code under test is the same
`PaystackRail` that runs in production, signature verification included.

## Cash settlement (COD)

COD cash still sits with the runner after delivery, and runners deposit and log it
via **Remit cash**; platform admin confirms the deposit against the expected total
in **Dashboard → Settlement**. Wallet orders skip all of this — the money is already
digital.

## WhatsApp (Meta Cloud API)

Order notifications (OTP, confirmation + delivery PIN, status updates, runner job
notices) go through the WhatsApp Cloud API when configured, and fall back to console
logging plus a `wa.me` link when not. With it unconfigured, one-time codes are shown
on screen so every flow still works.

```bash
WHATSAPP_TOKEN=<access token>
WHATSAPP_PHONE_NUMBER_ID=<phone number ID, not the phone number>
```

## Deploy (Vercel)

Configured via `vercel.json`.

| Variable | Default | Purpose |
|---|---|---|
| `MONGO_URI` | *(empty → demo store)* | MongoDB connection string |
| `MONGO_DB` | `yithume` | Database name |
| `ADMIN_SECRET` | `1234` | Platform admin PIN — **change in production** |
| `PAYSTACK_SECRET_KEY` | *(empty)* | `sk_test_…` or `sk_live_…` |
| `PAYSTACK_PUBLIC_KEY` | *(empty)* | Public key |
| `PAYSTACK_WEBHOOK_SECRET` | *(secret key)* | Webhook signing secret |
| `PUBLIC_URL` | *(empty)* | Base URL for payment callbacks |
| `WHATSAPP_TOKEN` / `WHATSAPP_PHONE_NUMBER_ID` | *(empty)* | WhatsApp Cloud API |
| `DELIVERY_FEE` | `25.0` | Initial delivery fee (R) |
| `MARKETPLACE_FEE_RATE` | `0.10` | Initial operator commission |
| `NETWORK_FEE_RATE` | `0.02` | YiThume's cut |
| `RUNNER_FEE_RATE` / `NODE_FEE_RATE` / `RESERVE_RATE` | `0.80` / `0.19` / `0.01` | Shares of the delivery fee |
| `COD_CAP` / `COD_SURCHARGE` | `400.0` / `10.0` | COD limits |
| `FAILED_DELIVERY_FEE` / `FAILED_ATTEMPT_FEE_RATE` | `25.0` / `0.60` | Failed-delivery economics |
| `STRIKE_LIMIT` | `2` | Buyer strikes before prepay-only |
| `RUNNER_CASH_LIMIT` | `1000.0` | Runner cash-in-hand limit (R) |
| `FLOAT_MIN` | `500.0` | Warn when the rail balance drops below this |
| `AUTO_SEED` | `true` | Seed demo data when the database is empty |

Fee values are initial defaults only — edit them in the console or dashboard once
running.

## Known gaps

- **Runner auth is phone-number-only** (`_require_runner`). Anyone who knows a
  runner's number can accept jobs and file cash remittances as them. With a wallet
  in the system this matters more, not less — it is the first thing to fix.
- **Paystack is unverified against the live API.** It is written to Paystack's
  documented request and response shapes and tested against stubs; nobody has run it
  with real keys yet. Expect to correct a field name or two on the first live run.
- **Holding customer balances is a regulated activity.** A wallet people top up and
  spend is close to e-money, which in South Africa sits under the National Payment
  System and is normally done under a licensed partner. The ledger is built to be
  audit-ready, but "who is legally holding this money" needs answering before real
  balances accumulate.
- **No live carrier API** — carriers are manual-waybill only.
- `api/app_legacy.py` and `api/verification_layer.py` are dead code kept for
  reference; the live app is `api/app.py`.
