> **Superseded on 2026-10-11.** This document predates the 2026-10-01 call and assumed buying a Raspberry Pi 5 kit (board, Active Cooler, 27W power supply, microSD card).
> The board in use is now a borrowed Raspberry Pi 3 B+, the architecture is Pi -> laptop receiver -> Ditto, and the current plan is `notes/PI_TELEMETRY_SPEC_2026-10-11.md`.
> The text below is kept unchanged for history.

# Purchase Checklist — Raspberry Pi 5 Hardware

**NO ORDER HAS BEEN PLACED. Do not order until Prof. Siddiqui has seen the demo and explicitly approved funding. This document is preparation only.**

Prof. Siddiqui requires seeing the digital twin demonstrated successfully before she will agree to fund the physical hardware (see `notes/PROFESSOR_PRESENTATION_SCRIPT.md` for the demo this is gated on, and `notes/PROFESSOR_UPDATE_DRAFT.md` for the message requesting time for it). Nothing in this document should be read as a plan already in motion — it exists so that, if and when she approves, ordering can happen immediately on verified numbers rather than needing to be re-researched under time pressure.

**Prices and stock checked live: 2026-09-17, 04:13 UTC.** Every row below was fetched directly from the reseller's own product page in this session — not reused from the 2026-09-15 or 2026-09-16 checks, both of which are now superseded. Stock status has already been shown to change within a single day (the Pi 5 board itself went from "In Stock" to "Out of stock" at PiShop between the last two checks), so this snapshot should be treated as good for today only, and re-checked again once (and if) it's actually time to order.

Nothing below has been added to a cart, checked out, or paid for. Placing the order is a financial transaction and is Harrison's to perform himself, only after approval.

---

## Live stock matrix, right now

| Item | PiShop.us | CanaKit.com |
|---|---|---|
| Raspberry Pi 5, 4GB board | $110.00 — **Out of stock** (Notify Me only) | **$110.00 — In Stock** |
| Official Active Cooler | **$10.95 — In Stock** | $11.95 — In Stock (different SKU: `RSP-ACTIVE-COOLER`) |
| Official 27W USB-C power supply | **$11.95 — In Stock** | $12.95 — In Stock (Black or White, SKU `DCAR-RSP-PI-PD-BLK`/`-WHT`) |
| 32GB microSD w/ Raspberry Pi OS | **$19.95 — In Stock** (SanDisk-branded, Pi OS 64-bit) | $24.95 — In Stock (Samsung EVO+ branded, not the same card) |

The board is the only item not available everywhere: **PiShop does not have it right now; CanaKit does.** The three accessories are in stock at both, cheaper at PiShop in every case. This is not the "everything out of stock everywhere" ambiguous case — it's a clean, resolvable split.

---

## Option 1 — Two orders, lower total: $152.85

Buy the board at CanaKit (the only reseller that has it), the three accessories at PiShop (all cheaper there).

| Item | Price | Reseller | Exact URL |
|---|---:|---|---|
| Raspberry Pi 5, 4GB board | $110.00 | CanaKit | https://www.canakit.com/raspberry-pi-5-4gb.html |
| Official Active Cooler | $10.95 | PiShop | https://www.pishop.us/product/raspberry-pi-active-cooler/ |
| Official 27W USB-C power supply | $11.95 | PiShop | https://www.pishop.us/product/usb-c-pd-power-supply-for-raspberry-pi-5-27w-etl-listed/ |
| 32GB microSD w/ Raspberry Pi OS (SanDisk, 64-bit) | $19.95 | PiShop | https://www.pishop.us/product/class-10-microsd-card-with-raspberry-pi-os-bookworm-32gb/ |
| **Total** | **$152.85** | — | — |

**Tradeoff:** two separate packages, two tracking numbers, two shipping charges (both resellers' checkout pages should be checked for their actual shipping cost before comparing totals — that wasn't priced here since it depends on the shipping method chosen at checkout, not the item price). If the board and accessories arrive on different days, the board itself is still usable alone for initial OS flashing and SSH setup while the cooler/supply/card are in transit — the board needs the SD card and power supply to actually boot, so full bring-up waits for the slower of the two packages either way.

## Option 2 — One order, higher total: $159.85

Buy everything at CanaKit, accepting CanaKit's own prices on the three accessories.

| Item | Price | Reseller | Exact URL |
|---|---:|---|---|
| Raspberry Pi 5, 4GB board | $110.00 | CanaKit | https://www.canakit.com/raspberry-pi-5-4gb.html |
| Raspberry Pi 5 Active Cooler | $11.95 | CanaKit | https://www.canakit.com/raspberry-pi-5-active-cooler.html |
| Raspberry Pi 5 Power Supply (27W USB-C) | $12.95 | CanaKit | https://www.canakit.com/official-raspberry-pi-5-power-supply-27w-usb-c.html |
| Samsung EVO Plus microSD, 32GB, w/ Raspberry Pi OS | $24.95 | CanaKit | https://www.canakit.com/samsung-evo-plus-microsd-card-with-raspberry-pi-os.html |
| **Total** | **$159.85** | — | — |

**Tradeoff:** one package, one tracking number, one shipping charge, everything arrives together — costs **$7.00 more** than Option 1, and the SD card is a different brand (Samsung EVO+, not SanDisk) than every prior pricing pass in this repo's notes has been tracking, though it's an equivalent-purpose, Pi-OS-preloaded, Class 10 32GB card, not a downgrade.

## Option 3 — Wait for PiShop to restock the board

PiShop's board page offers a "Notify Me" email signup for restock but gives no ETA, and none was found elsewhere on the site. This is a real option but not a quantifiable one — there is no evidence-backed restock date to plan around, so this document does not assign it a timeline. Only worth choosing if avoiding a split order matters more than the delay of an unknown restock wait — a tradeoff to weigh whenever ordering is actually approved, not before.

---

## Recommendation (for whenever ordering is approved — not a decision to act on now)

**Option 1** ($152.85, split order) is the cheapest and doesn't require waiting on an unknown restock date. The two-package tradeoff is minor for a bring-up that needs all four pieces in hand before the Pi is even bootable — whichever package arrives first, the board can't do anything useful until the SD card and power supply are also in hand, so there's no real cost to the split.

If and when Prof. Siddiqui approves funding, re-verify the specific product page one more time immediately before checkout — this document is accurate as of 2026-09-17 04:13 UTC, not a standing guarantee, and pricing/stock have already been shown to shift within a single day.
