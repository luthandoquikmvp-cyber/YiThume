# YiThume — One network. Every market.

The commerce network that reaches every market: a marketplace that also connects
marketplaces, powered by local runners who deliver where couriers won't go.
Buyers shop online (or by USSD on a basic phone) and get updates on WhatsApp.
See [POSITIONING.md](POSITIONING.md).

## What's here

| Surface | URL | What it does |
|---|---|---|
| **Storefront** | `/` | Browse products from all sellers & connected markets, basket, checkout (cash on delivery or EFT) with OTP verification, order tracking, seller/runner applications |
| **Runner app** | `/runner` | Mobile-first view for runners: job queue, accept / collect / deliver (gated by the buyer's delivery PIN), cash-in-hand balance, "remit cash" deposit logging, incident reporting |
| **Dashboard** | `/dashboard` | PIN-gated admin: stats, order lifecycle, settlement (remittance reconciliation, flagged buyers), payout queue, fee settings, and full CRUD for products, sellers, runners, delivery nodes, and connected marketplaces |
| **USSD simulator** | `/ussd` | Phone-style simulator for the USSD flow (browse → order → track). `POST /api/ussd` speaks the aggregator protocol (Africa's Talking style), so a live short code is a config step later |
| **API** | `/api/*` | JSON API over the database (`api/app.py`) |

## Run locally (zero config)

```bash
pip install -r requirements.txt
python api/app.py
# open http://localhost:5000  (dashboard PIN: 1234)
```

With no `MONGO_URI` set, the app runs on a built-in in-memory demo store with
seed data (resets on restart). Set `MONGO_URI` to persist everything in MongoDB.

Everything works with zero external accounts:

- **OTP & delivery PIN** — with WhatsApp not configured, the checkout shows the
  buyer's one-time code on screen ("demo mode") and the delivery PIN appears in
  the order-tracking view. All messages are also logged to the server console.
- **Payments** — cash on delivery or manual EFT. No gateway needed.
- **USSD** — fully local via the simulator page.

## Order lifecycle

`awaiting_otp → pending → confirmed → assigned (runner) → picked_up → delivered`
(cancellable until delivered).

- **OTP**: an order is only accepted (`pending`) once the buyer enters the
  one-time code sent to their phone. USSD orders skip this (the phone number
  comes from the network). Admin cannot confirm an unverified order.
- **Delivery PIN**: generated when an order is confirmed and sent to the buyer.
  An order can only be marked `delivered` with that PIN — by the runner in
  `/runner` or by admin in the dashboard.
- **Auto-assign** prefers online runners in the buyer's area, then the
  least-loaded runner, and skips runners over the cash-in-hand limit.

## Anti-fraud controls

| Control | Behaviour | Config |
|---|---|---|
| Order OTP | 6-digit code confirms the order before it can cost a runner a trip; 5 wrong attempts cancels the order | — |
| Delivery PIN | 4-digit code proves the real buyer received the goods | — |
| Buyer strikes | No-show / refused-COD incidents (logged by runner or admin) count per phone number; at the limit the buyer becomes **prepay-only** (EFT required, COD blocked). Admin can reset or override per buyer | `strike_limit` (default 2) |
| COD value cap | Orders above the cap must be EFT-paid upfront | `cod_cap` (default R400) |
| Runner cash limit | A runner holding too much undeposited COD cash can't take or be assigned new jobs | `runner_cash_limit` (default R1000) |

## Settlement ledger & payouts

The fee breakdown is **frozen onto each order at checkout** (changing rates
later only affects new orders):

- platform commission — share of the item subtotal (`platform_rate`, default 10%)
- runner earning — share of the delivery fee (`runner_rate`, default 80%)
- node commission — share of the delivery fee, when a delivery node covers the
  buyer's area (`node_rate`, default 20%)
- seller payout — item subtotal minus platform commission, split per seller

All rates and fees are editable in **Dashboard → Settings** (env vars only set
the initial defaults).

Cash flow after delivery:

1. **COD**: the order's cash sits **with the runner** (their cash-in-hand
   balance, visible in `/runner` and the dashboard).
2. The runner deposits the cash at a bank and logs it via **Remit cash**
   (amount + reference) → a pending remittance.
3. Admin confirms the remittance in **Dashboard → Settlement** against the
   expected total → those orders become **settled**. (EFT-prepaid orders settle
   immediately on delivery — the money is already digital.)
4. Settled orders feed **Dashboard → Payouts**: what's owed to each seller,
   runner, and node, with a manual **Mark as paid** per payout. The actual
   transfer happens outside the app for now; this screen is where an automated
   payout API plugs in later.

## WhatsApp (Meta Cloud API, test mode)

Order notifications (OTP, confirmation + delivery PIN, status updates to the
buyer, job notices to the runner) are sent through the WhatsApp Cloud API when
configured, and fall back to console logging + a `wa.me` link when not.

To wire it up with a **free Meta developer test number** (no business
verification needed):

1. Create a Meta developer app with the WhatsApp product — the test number and
   a temporary access token are on the app's *WhatsApp → API Setup* page.
2. Add recipient numbers to the test number's allowed list (test mode only
   delivers to numbers you've added).
3. Set two environment variables and restart:

```bash
WHATSAPP_TOKEN=<your access token>
WHATSAPP_PHONE_NUMBER_ID=<the phone number ID, not the phone number>
```

Swapping to a production number later is the same two variables — no code
change. `GET /health` shows `"whatsapp": "cloud_api"` when configured.

## Deploy (Vercel)

Already configured via `vercel.json`. Environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `MONGO_URI` | *(empty → demo store)* | MongoDB connection string (Atlas) |
| `MONGO_DB` | `yithume` | Database name |
| `ADMIN_SECRET` | `1234` | Dashboard PIN — **change in production** |
| `WHATSAPP_TOKEN` | *(empty → console fallback)* | WhatsApp Cloud API access token |
| `WHATSAPP_PHONE_NUMBER_ID` | *(empty)* | WhatsApp Cloud API phone number ID |
| `WHATSAPP_NUMBER` | `27600000000` | Number used for the `wa.me` fallback link |
| `DELIVERY_FEE` | `25.0` | Initial delivery fee (R) |
| `PLATFORM_FEE_RATE` | `0.10` | Initial platform commission rate |
| `RUNNER_FEE_RATE` | `0.80` | Initial runner share of the delivery fee |
| `NODE_FEE_RATE` | `0.20` | Initial node share of the delivery fee |
| `COD_CAP` | `400.0` | Initial COD value cap (R) |
| `STRIKE_LIMIT` | `2` | Initial buyer strikes before prepay-only |
| `RUNNER_CASH_LIMIT` | `1000.0` | Initial runner cash-in-hand limit (R) |
| `EFT_DETAILS` | *(demo bank line)* | EFT instructions shown to buyers |
| `AUTO_SEED` | `true` | Seed demo data when the database is empty |

Fee/policy values are initial defaults only — once running, edit them in
**Dashboard → Settings**.

## Future work

- **Live payments (Yoco)** — the payment step is behind a small provider
  interface (`api/payments.py`). A Yoco (or other gateway) provider slots in by
  implementing `validate_method` / `initiate` / `on_delivered`, without touching
  the order lifecycle.
- **Automated payouts** — the payout queue (`/api/payouts` +
  Dashboard → Payouts) is the integration point; replace "Mark as paid" with a
  payout API call per row.
- **Live USSD short code** — `POST /api/ussd` already accepts aggregator-style
  requests (`sessionId`, `phoneNumber`, `text`) and answers `CON`/`END`. Going
  live means a telecom/aggregator contract (e.g. Africa's Talking) pointed at
  that endpoint, plus IP allowlisting/signature checks.
- **SMS fallback for OTP/PIN** — for buyers without WhatsApp; slot it into
  `api/whatsapp.py`'s fallback path.

## Notes

- `api/app_legacy.py` is the previous backend, kept for reference only — the
  live app is `api/app.py`.
- Tailwind is self-hosted at `api/static/tailwind.js` so the UI works without
  external CDNs.
