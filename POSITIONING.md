# YiThume — Positioning

> **One-liner:** YiThume is the layer between hard-to-reach markets and their
> customers — software that lets anyone create their own marketplace, with a
> courier network already underneath it.

---

## 1. What we are, and what we are not

**We are not a delivery company.** We don't compete with couriers, and we don't
want to own trucks.

**We are not a marketplace.** We don't compete with the markets we serve, and we
don't want to own their customers.

**We are the runners app**: the infrastructure layer that makes a marketplace
possible in places where logistics stop at the townline. Someone local runs the
market. We run the road, the money, and the software.

The claim in one sentence:

> **"Anyone can start a market. Almost nobody can deliver to it. We already can."**

## 2. Why this shape

Three observations set the design.

### The long leg isn't ours to build

The Courier Guy's app isn't very good, and people use it happily anyway — because
the *infrastructure* behind it works. Depots, kiosks, pickup points, a prepaid habit
people already trust. Rebuilding that is a waste of money and years.

What existing couriers cannot do is the last five kilometres down a dirt road to a
house with no street address. That is the leg we own. So a courier is a **leg in a
YiThume order**, not a competitor: they carry the parcel into the town, a runner
takes it from the kiosk to the door — or the customer collects at the kiosk for
less, which is the cheapest option and the one people already like.

### Prepaid is a trust mechanism, not a payment method

People pay up front happily when they can see the money is held until they get the
goods. That single property does a lot of work at once: sellers get paid instantly
instead of waiting on a payout run, refunds become possible, and scamming someone
stops being profitable — the seller is not paid until the buyer's PIN proves
handover.

So the wallet isn't a convenience feature. It's the trust layer, and the ledger
under it is a core asset.

### Self-serve creation is an attack surface

"Anyone can create a marketplace in minutes" plus "there is money in the system"
means fake tenants and seller scams are a day-one design problem. Trust tiers,
escrow caps, payout holds and disputes are part of the product, not a later
hardening pass.

## 3. The runner network comes first

The order matters. We market and build the runner network **before** the
marketplaces, because it is the only part that can't be conjured on demand and the
only reason a brand-new market can deliver on its first day.

A new operator picks their areas and immediately sees how many runners already
cover them. They can recruit their own later — and those runners get first pick of
their jobs — but they never start from zero.

That is also why runners get treated properly:

- **They are paid for a wasted trip.** A buyer no-show used to mean the runner
  worked for free. Now a reserve, funded by a small levy on every order, pays them
  an attempt fee — and the cost falls on the buyer who caused it, or on the
  marketplace, or on us. Never on the runner.
- **Prepaid jobs reach them first.** Guaranteed money sorts above cash.
- **One pool, many markets.** A runner in the shared pool gets work from every
  marketplace in their area, not just one.

A network that leaks runners isn't a network.

## 4. Positioning by audience

**For people who want to start a market:**
> "Create your marketplace in minutes. You bring the sellers; the runners, the
> checkout and the payouts are already there."

**For buyers:**
> "Shop your local market and get it delivered — even if your door has no address.
> Your money is held until you have your goods."

**For sellers:**
> "Sell to your whole town, not just the people who walk past. You're paid the
> moment it's delivered, not next month."

**For runners:**
> "Deliver in the streets you know, for every market in your area. You get paid for
> the trip, even when the customer doesn't show."

**For couriers and kiosk operators:**
> "Your trucks stop where the tar stops. Hand the parcel to us there and we finish
> the job — or hold it at your counter and we send you the customers."

**For investors:**
> "The transaction and logistics layer for commerce in places logistics don't
> reach. Asset-light: no fleet, no warehouses, no inventory."

## 5. Where the money comes from

Five layers on one distribution spine:

1. **Network commission** — 2% of goods sold on every marketplace.
2. **Delivery economics** — our share of the delivery fee, better as density grows.
3. **Node commissions** — local operators run territories; we take a share.
4. **Float and payments** — money that lives in the wallet between top-up and
   payout.
5. **Later, data-enabled** — working capital for sellers, B2B restocking, ads.

Operators set their own commission on their own sellers. We deliberately don't
compete with them for it — our cut sits underneath, on volume.

## 6. What actually exists today

Being honest about this is more useful than a roadmap that reads as if it shipped.

**Built and tested:**
- Multi-tenant marketplaces: self-serve signup with phone OTP, per-tenant
  storefronts at `/m/<slug>`, operator console, platform admin.
- Shared runner pool with per-marketplace runners taking priority.
- Double-entry ledger with escrow, refunds, reserve accounts and an audit that
  recomputes every balance from the journal.
- Paystack rail for card/EFT in and bank transfers out, admin-approved.
- Failed-delivery waterfall that always pays the runner.
- Two-leg fulfilment with pickup points; The Courier Guy works today as a manual
  carrier.
- Trust tiers, disputes, seller strikes, withdrawal holds.
- WhatsApp notifications and a USSD flow for feature phones.

**Not built:**
- No live carrier API — waybills are entered by hand.
- Paystack is written to spec but has never run against real keys.
- Runner authentication is still phone-number-only.
- The regulatory question of who legally holds customer balances is open, and it
  needs answering before real money accumulates at scale.

## 7. The pitch (30 seconds)

A trillion rand of commerce happens in markets that Takealot, Amazon and every
courier ignore — not because there's no demand, but because trucks stop where the
tar stops. YiThume is the software that lets anyone in those places start their own
marketplace, with the delivery network already underneath it: local runners who
work the streets couriers won't drive down, kiosks for the long leg, and a wallet
that holds the buyer's money until they hand over their PIN. The operator runs the
market and keeps their own commission. We take two percent and run the road. We're
not building a delivery company — we're building the layer that makes every one of
those markets reachable.
