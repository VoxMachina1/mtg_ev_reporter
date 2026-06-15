# MTG EV Reporter

Automated daily expected value (EV) tracker for Magic: The Gathering booster packs and preconstructed decks. Prices are pulled from MTGJSON every morning and published as CSV snapshots consumed by the web UI.

## What it does

- Calculates the **expected value of every booster type** (Draft, Play, Collector, Set, etc.) for every set in print, weighted by actual card slot probabilities from MTGJSON booster data
- Calculates the **total resale value of every preconstructed deck** (Commander, precons, Jumpstart, etc.) along with the top 5 most valuable cards
- Publishes dated CSV snapshots to `data/` and maintains a rolling manifest with 7-day daily retention + monthly snapshots going back 24 months
- Cards priced below **$1.00** contribute $0 to EV — the output reflects realizable sell value, not theoretical total card value

## Automation schedule

| Workflow | Schedule | What it does |
|----------|----------|--------------|
| `daily_ev.yml` | 3 AM UTC daily | Downloads prices, calculates EV, writes `data/` CSVs, commits |
| `weekly_regenerate.yml` | 1 AM UTC every Sunday | Rebuilds `config/` from MTGJSON (new sets, updated card lists) |

Both workflows can also be triggered manually via **Actions → Run workflow**.

## Repository structure

```
├── pipeline.py              # Daily EV calculation (run by GitHub Actions)
├── regenerate_config.py     # Weekly config rebuild from MTGJSON
├── index.html               # Web UI — loads CSVs from data/ via manifest
├── requirements.txt
│
├── config/
│   ├── booster_structures.json  # Pack compositions and card slot weights (from MTGJSON)
│   ├── booster_strategy.json    # Which price finish to use per slot (nonfoil/foil/etched)
│   ├── card_metadata.csv        # Card names, set info, rarity (from MTGJSON)
│   ├── precon_metadata.json     # Precon deck lists and metadata (from MTGJSON)
│   └── meta.json                # MTGJSON version tracking (stale-config detection)
│
└── data/
    ├── manifest.json            # Index of available snapshots + retention policy
    ├── data_YYYY_MM_DD.csv      # Full card price data per snapshot
    ├── ev_report_YYYY_MM_DD.csv # EV per set/booster type per snapshot
    └── precons_YYYY_MM_DD.csv   # Precon deck values per snapshot
```

## EV calculation

**Booster EV** is a probability-weighted average across all possible pack configurations for a given booster type. Each card slot (sheet) contributes:

```
sheet_EV = (sum of card_price × card_weight for cards ≥ $1.00) / total_sheet_weight × cards_per_slot
```

Pack EV sums all slot EVs, then weights across booster variants by their print frequency.

**Confidence** is the fraction of pack weight represented by cards with any valid price in the database — a data-quality signal, not a hit-rate indicator. Low confidence means MTGJSON price coverage is incomplete for that set.

**Precon total value** sums the prices of all cards in the deck's mainboard that are priced ≥ $1.00. Top 5 cards are always drawn from the full card list regardless of the bulk threshold.

## Running locally

```bash
pip install -r requirements.txt

# Build/refresh config (needed after new set releases or first run)
python regenerate_config.py

# Run the daily pipeline
python pipeline.py
```

`pipeline.py` writes to `data/` and updates `manifest.json`. No API keys required — all data is from public MTGJSON endpoints.

## Config regeneration

`regenerate_config.py` rebuilds `config/` from scratch by downloading the latest MTGJSON data. Run it when:
- A new set has released and isn't appearing in EV output
- MTGJSON has published a new version (pipeline will warn you via `⚠️ MTGJSON version changed`)
- The weekly GitHub Action hasn't run yet and you need fresh data now

## Data source

All card data and prices come from [MTGJSON](https://mtgjson.com) (`AllPricesToday.json` for TCGPlayer market prices, set files for booster structures). No affiliation with MTGJSON or TCGPlayer.
