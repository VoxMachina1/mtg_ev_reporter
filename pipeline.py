"""
Daily MTG EV Pipeline.

Fetches fresh prices from MTGJSON, merges with committed card metadata,
calculates expected value for all booster types, validates output, and
writes dated CSV snapshots + updates the manifest.
"""

import sys
import json
import requests
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"

MTGJSON_META_URL = "https://mtgjson.com/api/v5/Meta.json"
MTGJSON_ALL_PRICES_URL = "https://mtgjson.com/api/v5/AllPricesToday.json"

_NOW = datetime.now(timezone.utc)
TODAY = _NOW.strftime("%Y_%m_%d")
DATE_STR = _NOW.strftime("%Y-%m-%d")
TIMESTAMP = _NOW.isoformat()


def download_prices() -> dict:
    print("Downloading AllPricesToday.json...")
    r = requests.get(MTGJSON_ALL_PRICES_URL, timeout=300, stream=True)
    r.raise_for_status()
    data = r.json()
    if not data.get("data"):
        raise ValueError("AllPricesToday.json has no 'data' key — may be malformed")
    return data


def build_price_map(price_data: dict) -> tuple[dict, str]:
    """Returns (price_map, price_date). price_map: uuid → {nonfoil, foil, etched}."""
    price_date = price_data.get("meta", {}).get("date")
    if not price_date:
        raise ValueError("AllPricesToday.json missing metadata date")

    price_map = {}
    for uuid, card in price_data.get("data", {}).items():
        tcg = card.get("paper", {}).get("tcgplayer", {})
        if not tcg:
            continue
        market = tcg.get("market", {})
        retail = tcg.get("retail", {})

        def get_price(finish):
            return float(
                (market.get(finish) or retail.get(finish) or {}).get(price_date, 0) or 0
            )

        nf, fo, et = get_price("normal"), get_price("foil"), get_price("etched")
        if nf or fo or et:
            price_map[uuid] = {"nonfoil": nf, "foil": fo, "etched": et}

    if not price_map:
        raise ValueError("Price map is empty — AllPricesToday.json may be stale or malformed")

    print(f"   ✅ Prices loaded for {price_date}: {len(price_map):,} cards")
    return price_map, price_date


def build_data_csv(price_map: dict) -> tuple[pd.DataFrame, Path]:
    """Merge today's prices into card_metadata.csv → data_YYYY_MM_DD.csv."""
    metadata_path = CONFIG_DIR / "card_metadata.csv"
    if not metadata_path.exists():
        raise FileNotFoundError("config/card_metadata.csv not found — run regenerate_config.py first")

    df = pd.read_csv(metadata_path, dtype=str)

    uuid_col = "MTGJSON UUID"
    df["Market Price Nonfoil"] = df[uuid_col].map(lambda u: price_map.get(u, {}).get("nonfoil", 0.0))
    df["Market Price Foil"] = df[uuid_col].map(lambda u: price_map.get(u, {}).get("foil", 0.0))
    df["Market Price Etched"] = df[uuid_col].map(lambda u: price_map.get(u, {}).get("etched", 0.0))

    out = DATA_DIR / f"data_{TODAY}.csv"
    df.to_csv(out, index=False)
    print(f"   ✅ {len(df):,} rows → {out.name}")
    return df, out


def _safe_float(v) -> float:
    try:
        return float(v) if v else 0.0
    except (ValueError, TypeError):
        return 0.0


_BOOSTER_PREFIX_MAP = {
    "default": "Standard", "draft": "Draft", "set": "Set", "play": "Play",
    "collector": "Collector", "prerelease": "Prerelease", "theme": "Theme",
    "arena": "Arena", "jumpstart": "Jumpstart", "bundle": "Bundle",
    "fat-pack": "Bundle", "box-topper": "Box Topper", "vip": "VIP",
    "six": "Six", "mtgo": "MTGO", "tournament": "Tournament",
    "starter": "Starter", "convention": "Convention", "compleat": "Compleat",
    "premium": "Premium", "treasure-chest": "Treasure Chest",
    "stainedglass": "Stained Glass",
}
_COLOR_SUFFIX = {
    "b": "Black", "u": "Blue", "g": "Green", "r": "Red", "w": "White", "c": "Colorless",
    "jp": "JP", "iwd": "IWD",
}


def _standardize_booster_type(raw: str) -> str:
    lower = raw.lower()

    # Exact match
    if lower in _BOOSTER_PREFIX_MAP:
        return f"{_BOOSTER_PREFIX_MAP[lower]} Booster"

    # Longest-prefix match — preserves suffix as a readable label
    best, best_len = None, 0
    for key in _BOOSTER_PREFIX_MAP:
        if lower.startswith(key + "-") and len(key) > best_len:
            best, best_len = key, len(key)

    if best:
        suffix = lower[best_len + 1:]
        suffix_label = _COLOR_SUFFIX.get(suffix, suffix.replace("-", " ").title())
        return f"{_BOOSTER_PREFIX_MAP[best]} Booster — {suffix_label}"

    return f"{raw.replace('-', ' ').title()} Booster"


def _calc_ev(booster_type_data: dict, price_df: pd.DataFrame, strategy_map: dict) -> tuple[float, float]:
    """Returns (ev, confidence) for a single booster type."""
    boosters = booster_type_data.get("boosters", [])
    sheets = booster_type_data.get("sheets", {})
    total_weight = sum(b.get("weight", 0) for b in boosters)
    if not boosters or not sheets or not total_weight:
        return 0.0, 0.0

    final_ev = total_priced = total_cards = 0.0

    for booster in boosters:
        b_weight = booster.get("weight", 0)
        if not b_weight:
            continue
        pack_ev = pack_priced = pack_count = 0.0

        for sheet_name, count in booster.get("contents", {}).items():
            card_weights = sheets.get(sheet_name, {}).get("cards", {})
            if not card_weights:
                continue

            price_col = strategy_map.get(sheet_name, "market_price_nonfoil")
            sheet_val = sheet_weight = priced_weight = 0

            for uuid, weight in card_weights.items():
                sheet_weight += weight
                if uuid in price_df.index:
                    row = price_df.loc[uuid]
                    val = _safe_float(row.get(price_col))
                    if val == 0:
                        val = max(
                            _safe_float(row.get("market_price_nonfoil")),
                            _safe_float(row.get("market_price_foil")),
                            _safe_float(row.get("market_price_etched")),
                        )
                    if val > 0:
                        sheet_val += val * weight
                        priced_weight += weight

            if sheet_weight:
                pack_ev += (sheet_val / sheet_weight) * count
                pack_priced += (priced_weight / sheet_weight) * count
                pack_count += count

        ratio = b_weight / total_weight
        final_ev += pack_ev * ratio
        total_priced += pack_priced * ratio
        total_cards += pack_count * ratio

    confidence = total_priced / total_cards if total_cards else 0.0
    return final_ev, confidence


def calculate_ev(price_map: dict) -> tuple[pd.DataFrame, Path]:
    """Calculate EV for all sets and booster types → ev_report_YYYY_MM_DD.csv (long format)."""
    for name in ["booster_structures.json", "booster_strategy.json"]:
        if not (CONFIG_DIR / name).exists():
            raise FileNotFoundError(f"config/{name} not found — run regenerate_config.py first")

    with open(CONFIG_DIR / "booster_structures.json") as f:
        booster_structures = json.load(f)
    with open(CONFIG_DIR / "booster_strategy.json") as f:
        strategy_map = json.load(f)

    price_df = pd.DataFrame([
        {
            "uuid": uuid,
            "market_price_nonfoil": v["nonfoil"],
            "market_price_foil": v["foil"],
            "market_price_etched": v["etched"],
        }
        for uuid, v in price_map.items()
    ]).set_index("uuid")

    rows = []
    for set_code, struct in booster_structures.items():
        set_name = struct.get("name", set_code)
        for booster_key, booster_data in struct.get("booster", {}).items():
            if not isinstance(booster_data, dict) or "boosters" not in booster_data:
                continue
            ev, conf = _calc_ev(booster_data, price_df, strategy_map)
            if ev > 0:
                rows.append({
                    "Set Name": set_name,
                    "Set Code": set_code,
                    "Pack Type": _standardize_booster_type(booster_key),
                    "Expected Value": round(ev, 2),
                    "Confidence": f"{conf:.1%}",
                    "Release Date": struct.get("releaseDate", ""),
                })

    if not rows:
        raise ValueError("No EV rows produced — possible data issue")

    df = pd.DataFrame(rows).sort_values(["Set Name", "Pack Type"]).reset_index(drop=True)
    out = DATA_DIR / f"ev_report_{TODAY}.csv"
    df.to_csv(out, index=False)
    print(f"   ✅ {len(df):,} pack types → {out.name}")
    return df, out


EV_DELTA_THRESHOLD = 0.50  # Flag if EV shifts more than 50% vs previous snapshot


def calculate_precons(price_map: dict) -> tuple[pd.DataFrame | None, Path | None]:
    """Process all deck files in config/decks/ → precons_YYYY_MM_DD.csv."""
    decks_dir = CONFIG_DIR / "decks"
    if not decks_dir.exists():
        print("   ⚠️  config/decks/ not found — run regenerate_config.py first")
        return None, None

    deck_files = [f for f in decks_dir.rglob("*.json")]
    if not deck_files:
        print("   ⚠️  No deck files found")
        return None, None

    rows = []
    for deck_file in deck_files:
        try:
            with open(deck_file, encoding="utf-8") as f:
                deck = json.load(f).get("data", {})

            deck_name = deck_file.stem.replace("_", " ").replace("-", " ")
            set_code = deck.get("code", "").upper()
            total_val = 0.0
            cards = []

            for card in deck.get("mainBoard", []):
                uuid = card.get("uuid")
                if not uuid:
                    continue
                prices = price_map.get(uuid, {})
                price = max(prices.get("nonfoil", 0), prices.get("foil", 0), prices.get("etched", 0))
                total_val += price
                cards.append((card.get("name", ""), price))

            cards.sort(key=lambda x: x[1], reverse=True)
            is_commander = "commander" in deck.get("type", "").lower() or bool(deck.get("commander"))

            rows.append({
                "Deck Name": deck_name,
                "Deck Type": deck.get("type", "Unknown"),
                "Category": "Commander" if is_commander else "Other",
                "Set Code": set_code,
                "Release Date": deck.get("releaseDate", ""),
                "Total Value": round(total_val, 2),
                "Top 5 Cards": ", ".join(c[0] for c in cards[:5]),
            })
        except Exception as e:
            print(f"   ⚠️  Skipping {deck_file.name}: {e}")

    if not rows:
        print("   ⚠️  No decks processed")
        return None, None

    df = pd.DataFrame(rows).sort_values(["Category", "Deck Name"]).reset_index(drop=True)
    out = DATA_DIR / f"precons_{TODAY}.csv"
    df.to_csv(out, index=False)
    print(f"   ✅ {len(df):,} decks → {out.name}")
    return df, out


def validate(data_df: pd.DataFrame, ev_df: pd.DataFrame) -> list[str]:
    errors = []

    if len(data_df) < 100_000:
        errors.append(f"data CSV: only {len(data_df):,} rows (expected ≥ 100,000)")
    for col in ["Set Code", "Set Name", "Card Name", "MTGJSON UUID"]:
        if col not in data_df.columns:
            errors.append(f"data CSV: missing required column '{col}'")

    if len(ev_df) < 100:
        errors.append(f"EV report: only {len(ev_df)} rows (expected ≥ 100)")
    for col in ["Set Name", "Pack Type", "Expected Value"]:
        if col not in ev_df.columns:
            errors.append(f"EV report: missing required column '{col}'")

    if "Expected Value" in ev_df.columns:
        evs = pd.to_numeric(ev_df["Expected Value"], errors="coerce").dropna()
        if (evs <= 0).any():
            errors.append(f"EV report: {(evs <= 0).sum()} rows have non-positive EV")

    errors.extend(_check_ev_delta(ev_df))
    return errors


def _check_ev_delta(ev_df: pd.DataFrame) -> list[str]:
    """Compare today's EVs against the most recent previous snapshot.
    Returns errors for any pack type whose EV shifted by more than EV_DELTA_THRESHOLD.
    Skipped entirely on the first run (no prior snapshot to compare against).
    """
    manifest_path = DATA_DIR / "manifest.json"
    if not manifest_path.exists():
        return []

    manifest = json.loads(manifest_path.read_text())
    snapshots = manifest.get("snapshots", [])
    # Exclude today's snapshot (not yet written) — use the most recent committed one
    prior = [s for s in snapshots if s["date"] != DATE_STR]
    if not prior:
        return []

    latest = max(prior, key=lambda s: s["date"])
    prior_path = DATA_DIR / latest["ev_report"]
    if not prior_path.exists():
        return []

    prior_df = pd.read_csv(prior_path)
    prior_lookup = {
        (row["Set Name"], row["Pack Type"]): float(row["Expected Value"])
        for _, row in prior_df.iterrows()
    }

    errors = []
    for _, row in ev_df.iterrows():
        key = (row["Set Name"], row["Pack Type"])
        prev_ev = prior_lookup.get(key)
        if prev_ev is None or prev_ev == 0:
            continue
        curr_ev = float(row["Expected Value"])
        delta = abs(curr_ev - prev_ev) / prev_ev
        if delta > EV_DELTA_THRESHOLD:
            errors.append(
                f"EV delta: {row['Set Name']} / {row['Pack Type']} "
                f"moved {delta:.0%} (${prev_ev:.2f} → ${curr_ev:.2f})"
            )

    return errors


def _apply_retention(snapshots: list) -> list:
    """Keep last 7 daily snapshots + first snapshot of each month for up to 24 months."""
    seen, unique = set(), []
    for s in sorted(snapshots, key=lambda s: s["date"], reverse=True):
        if s["date"] not in seen:
            unique.append(s)
            seen.add(s["date"])

    keep = set(s["date"] for s in unique[:7])

    # Oldest-first pass to identify the first snapshot of each month
    monthly = {}
    for s in reversed(unique):
        month = s["date"][:7]
        if month not in monthly:
            monthly[month] = s["date"]
    keep.update(sorted(monthly.values(), reverse=True)[:24])

    return [s for s in unique if s["date"] in keep]


def update_manifest(ev_filename: str, data_filename: str, precons_filename: str | None = None):
    manifest_path = DATA_DIR / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"snapshots": []}

    snapshot = {
        "date": DATE_STR,
        "ev_report": ev_filename,
        "data": data_filename,
        "timestamp": TIMESTAMP,
    }
    if precons_filename:
        snapshot["precons"] = precons_filename
    manifest["snapshots"].append(snapshot)
    manifest["snapshots"] = _apply_retention(manifest["snapshots"])
    manifest["last_updated"] = TIMESTAMP
    manifest_path.write_text(json.dumps(manifest, indent=2))

    # Prune CSV files no longer referenced in the manifest
    referenced = {f for s in manifest["snapshots"] for f in [s["ev_report"], s["data"], s.get("precons")] if f}
    for f in DATA_DIR.glob("*.csv"):
        if f.name not in referenced:
            print(f"   Pruning {f.name}")
            f.unlink()


def check_config_version():
    """Warn if MTGJSON has released a new version since the last config regeneration."""
    meta_path = CONFIG_DIR / "meta.json"
    if not meta_path.exists():
        return
    try:
        r = requests.get(MTGJSON_META_URL, timeout=30)
        r.raise_for_status()
        current = r.json().get("meta", {}).get("version")
        stored = json.loads(meta_path.read_text()).get("mtgjson_version")
        if current and current != stored:
            print(f"⚠️  MTGJSON version changed ({stored} → {current}). "
                  "New sets will appear after next weekly regeneration.")
    except Exception as e:
        print(f"⚠️  Could not check MTGJSON version: {e}")


def main():
    print(f"=== MTG EV Pipeline — {DATE_STR} ===\n")

    for name in ["card_metadata.csv", "booster_structures.json", "booster_strategy.json"]:
        if not (CONFIG_DIR / name).exists():
            print(f"FATAL: config/{name} missing — run regenerate_config.py first")
            sys.exit(1)

    check_config_version()

    print("\n[1/4] Prices")
    price_data = download_prices()
    price_map, _ = build_price_map(price_data)

    print("\n[2/4] Data CSV")
    data_df, data_path = build_data_csv(price_map)

    print("\n[3/4] EV Report")
    ev_df, ev_path = calculate_ev(price_map)

    print("\n[4/5] Precon Decks")
    _, precons_path = calculate_precons(price_map)

    print("\n[5/5] Validate & manifest")
    errors = validate(data_df, ev_df)
    if errors:
        print("VALIDATION FAILED:")
        for e in errors:
            print(f"  ✗ {e}")
        sys.exit(1)
    print("   ✅ Validation passed")
    update_manifest(ev_path.name, data_path.name, precons_path.name if precons_path else None)
    print("   ✅ Manifest updated")

    print(f"\n=== Complete ===")


if __name__ == "__main__":
    main()
