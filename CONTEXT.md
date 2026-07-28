# YiThume — Context Brief (paste this into any LLM to get it up to speed)

## What it is
YiThume is a South African (Eastern Cape-first) commerce startup. The positioning:
it's not "a marketplace" and not "a delivery app" — it's the **Connected Commerce
Network**: a marketplace that also connects *other* marketplaces (town markets,
spaza shops, independent sellers), delivered by a network of local **runners** who
reach places couriers won't go (townships, rural roads, informal settlements,
market stalls with no street address). Ordering is **WhatsApp-first** and
**USSD-capable** — works on a feature phone with no app and no data plan.

One-liner: *"Amazon built warehouses. We built a network of people. Warehouses
can't reach our markets — runners can."*

## The business model / unicorn thesis
- Market: Africa's informal retail economy is $1T+, mostly invisible to
  e-commerce because logistics stop at the townline. Whoever solves *reach*
  owns the transaction layer.
- Three-sided network effect: more runners → more coverage → more buyers →
  more order density → better runner earnings → more sellers apply → repeat.
- Moats: Delivery Nodes (local operators recruit runners, own a territory,
  earn commission — franchise economics, zero fleet capex for YiThume),
  WhatsApp/USSD channel lock-in (no app install needed to compete), and
  becoming the settlement/delivery layer *beneath* other marketplaces
  (infrastructure, not just a storefront).
- Five revenue layers on one distribution spine: (1) platform take rate
  (10%) + item margin (12%), (2) delivery fees with batch bonuses,
  (3) Retail OS SaaS for sellers (subscriptions + paywalled AI credits),
  (4) node commissions, (5) future: seller lending, B2B restocking, ads.
- Full narrative, audience-specific pitches, and milestone table live in
  `POSITIONING.md`.

## What's actually built (as of this session)
Repo: `luthandoquikmvp-cyber/YiThume`. Stack: Python/Flask backend, vanilla
HTML/JS + Tailwind (self-hosted, no CDN dependency) frontend, MongoDB (or an
in-memory demo store with seed data when no DB is configured), deployed via
Vercel (`vercel.json` routes everything through `api/app.py`).

**Three surfaces:**
1. **Storefront (`/`, `api/static/index.html`)** — buyers browse products
   from all sellers/markets in one grid, search/filter by category, build a
   basket, checkout. Checkout returns an order code (e.g. `YT-2RRTJ`) and a
   "Confirm on WhatsApp" deep link (`wa.me/...`) with the order pre-filled.
   Also has order tracking (by code or phone) and application forms for new
   sellers and runners to join the network.
2. **Admin dashboard (`/dashboard`, `api/static/dashboard.html`)** — PIN-gated
   (default PIN `1234` via `ADMIN_SECRET` env var). Literally "the DB with a
   UI on top": tabs for Overview (GMV, platform revenue, order status
   breakdown, top products), Orders (drive the delivery lifecycle), Products,
   Sellers, Runners, Nodes, Marketplaces — full CRUD on every entity.
3. **API (`/api/*`, `api/app.py`)** — one clean Flask app. Public endpoints:
   products/categories (with search), orders (create/track), seller/runner
   applications. Admin endpoints (require `X-Admin-Pin` header or `?pin=`):
   order status transitions, runner assignment (manual or `auto`, which
   prefers online runners in the buyer's area), generic CRUD for
   sellers/runners/nodes/marketplaces, stats.

**Order lifecycle** (guarded — invalid jumps rejected):
`pending → confirmed → assigned (runner) → picked_up → delivered`
(cancellable up until picked up).

**Entities in the DB:** products, sellers, runners, orders, nodes
(delivery-territory operators), marketplaces (connected external markets).

**Legacy:** the previous, much larger backend (7700+ lines, had USSD flows
and a verification layer) is preserved at `api/app_legacy.py` for reference
but is not wired into `vercel.json` — the live app is the new `api/app.py`.

## Key env vars (see README.md for full list)
`MONGO_URI` (empty = in-memory demo mode), `MONGO_DB`, `ADMIN_SECRET`
(dashboard PIN — must change from default in prod), `WHATSAPP_NUMBER`
(business WhatsApp number for order confirmations), `DELIVERY_FEE`,
`PLATFORM_FEE_RATE`, `AUTO_SEED`.

## Where things live
- `POSITIONING.md` — full unicorn narrative and pitch material.
- `README.md` — how to run locally / deploy to Vercel, env vars, order
  lifecycle diagram.
- `api/app.py` — current backend (read this first for any backend work).
- `api/app_legacy.py` — old backend, reference only.
- `api/static/index.html` — storefront.
- `api/static/dashboard.html` — admin dashboard.
- `api/static/tailwind.js` — self-hosted Tailwind build (avoids CDN dependency).

## Natural next steps (not yet built)
- Real WhatsApp Business API integration (today it's a `wa.me` deep link,
  not an automated bot flow).
- Payment links in checkout (Yoco/Ozow — mentioned in positioning, not
  wired into the new checkout flow yet).
- A runner-facing mobile view so runners can update delivery status
  themselves instead of admin doing it from the dashboard.
- Wiring the old USSD flows (in `app_legacy.py`) into the new backend for
  feature-phone ordering.
