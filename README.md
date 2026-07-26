# YiThume — One network. Every market.

The commerce network that reaches every market: a marketplace that also connects
marketplaces, powered by local runners who deliver where couriers won't go.
Buyers shop online and confirm on WhatsApp. See [POSITIONING.md](POSITIONING.md).

## What's here

| Surface | URL | What it does |
|---|---|---|
| **Storefront** | `/` | Browse products from all sellers & connected markets, basket, checkout → WhatsApp confirmation, order tracking, seller/runner applications |
| **Dashboard** | `/dashboard` | PIN-gated admin: stats, orders (confirm → assign runner → picked up → delivered), and full CRUD for products, sellers, runners, delivery nodes, and connected marketplaces |
| **API** | `/api/*` | JSON API over the database (`api/app.py`) |

## Run locally

```bash
pip install -r requirements.txt
python api/app.py
# open http://localhost:5000  (dashboard PIN: 1234)
```

With no `MONGO_URI` set, the app runs on a built-in in-memory demo store with
seed data (resets on restart). Set `MONGO_URI` to persist everything in MongoDB.

## Deploy (Vercel)

Already configured via `vercel.json`. Set these environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `MONGO_URI` | *(empty → demo store)* | MongoDB connection string (Atlas) |
| `MONGO_DB` | `yithume` | Database name |
| `ADMIN_SECRET` | `1234` | Dashboard PIN — **change in production** |
| `WHATSAPP_NUMBER` | `27600000000` | Business WhatsApp number for order confirmations |
| `DELIVERY_FEE` | `25.0` | Flat delivery fee (R) |
| `PLATFORM_FEE_RATE` | `0.10` | Platform take rate |
| `AUTO_SEED` | `true` | Seed demo data when the database is empty |

## Order lifecycle

`pending → confirmed → assigned (runner) → picked_up → delivered`
(cancellable until picked up). Assigning supports **auto-assign**: prefers
online runners in the buyer's area, then the least-loaded runner.

## Notes

- `api/app_legacy.py` is the previous backend (USSD flows, verification layer),
  kept for reference — the live app is `api/app.py`.
- Tailwind is self-hosted at `api/static/tailwind.js` so the UI works without
  external CDNs.
