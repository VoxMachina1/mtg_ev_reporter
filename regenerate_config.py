"""
Config Regeneration Script.

Downloads MTGJSON's AllSetFiles archive, extracts booster structures and
card metadata (without prices), and writes them to config/. Prices are
merged in fresh on each daily pipeline run.

Run this weekly (via GitHub Actions) or manually after a new set releases.
"""

import os
import sys
import json
import tarfile
import tempfile
import requests
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone

# Ensure emojis in print() don't crash on Windows CP1252 consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
CONFIG_DIR = ROOT / "config"

MTGJSON_META_URL = "https://mtgjson.com/api/v5/Meta.json"
MTGJSON_ALL_SETS_URL = "https://mtgjson.com/api/v5/AllSetFiles.tar.xz"


def fetch_version() -> str:
    print("Fetching MTGJSON version...")
    r = requests.get(MTGJSON_META_URL, timeout=30)
    r.raise_for_status()
    version = r.json().get("meta", {}).get("version", "unknown")
    print(f"   Version: {version}")
    return version


def download_and_process_sets() -> tuple[dict, list]:
    """Downloads AllSetFiles.tar.xz, extracts it, processes all set JSONs.
    Returns (booster_structures, card_rows).
    """
    print("Downloading AllSetFiles.tar.xz (this may take several minutes)...")
    with tempfile.TemporaryDirectory() as tmpdir:
        archive = os.path.join(tmpdir, "AllSetFiles.tar.xz")

        with requests.get(MTGJSON_ALL_SETS_URL, stream=True, timeout=600) as r:
            r.raise_for_status()
            total = 0
            with open(archive, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    f.write(chunk)
                    total += len(chunk)
        print(f"   Downloaded {total / 1_000_000:.1f} MB")

        print("Extracting...")
        sets_dir = os.path.join(tmpdir, "sets")
        os.makedirs(sets_dir)
        with tarfile.open(archive, "r:xz") as tf:
            tf.extractall(path=sets_dir)

        return _process_set_files(sets_dir)


def _process_set_files(sets_dir: str) -> tuple[dict, list]:
    # Walk recursively — the archive may extract into a subdirectory
    set_files = [
        os.path.join(root, f)
        for root, _, files in os.walk(sets_dir)
        for f in files
        if f.endswith(".json")
    ]
    print(f"   Processing {len(set_files)} set files...")

    booster_structures = {}
    card_rows = []

    for filepath in set_files:
        filename = os.path.basename(filepath)
        try:
            with open(filepath, encoding="utf-8") as f:
                set_data = json.load(f).get("data", {})

            set_code = set_data.get("code", "").upper()
            set_name = set_data.get("name", "")
            release_date = set_data.get("releaseDate", "")

            if not set_code:
                continue

            booster = set_data.get("booster")
            if booster:
                booster_structures[set_code] = {
                    "name": set_name,
                    "releaseDate": release_date,
                    "booster": booster,
                }

            for card in set_data.get("cards", []):
                uuid = card.get("uuid")
                if not uuid:
                    continue
                # Serialized cards are excluded from EV (unique 1-of printings, not pulled from standard packs)
                if "serialized" in card.get("promoTypes", []):
                    continue

                for finish in card.get("finishes", []):
                    if finish not in ("nonfoil", "foil", "etched"):
                        continue
                    card_rows.append({
                        "Set Code": set_code,
                        "Set Name": set_name,
                        "Card Name": card.get("name", ""),
                        "Card Number": card.get("number", ""),
                        "MTGJSON UUID": uuid,
                        "Scryfall ID": card.get("identifiers", {}).get("scryfallId", ""),
                        "Rarity": card.get("rarity", ""),
                        "Finish": finish.capitalize(),
                        "Frame Effects": ", ".join(card.get("frameEffects", [])),
                        "Border Color": card.get("borderColor", ""),
                        "Promo Types": ", ".join(card.get("promoTypes", [])),
                        "Is Full Art": str(card.get("isFullArt", "")),
                        "is_reprint": str(card.get("isReprint", "")),
                        "type": card.get("type", ""),
                        "mana_cost": card.get("manaCost", ""),
                        "cmc": card.get("convertedManaCost", 0.0),
                        "color_identity": ", ".join(card.get("colorIdentity", [])),
                    })

        except Exception as e:
            print(f"   ⚠️  Skipping {filename}: {e}")

    print(f"   ✅ {len(booster_structures)} sets with boosters, {len(card_rows):,} card rows")
    return booster_structures, card_rows


def main():
    print("=== Config Regeneration ===\n")
    CONFIG_DIR.mkdir(exist_ok=True)

    version = fetch_version()
    booster_structures, card_rows = download_and_process_sets()

    structures_path = CONFIG_DIR / "booster_structures.json"
    with open(structures_path, "w", encoding="utf-8") as f:
        json.dump(booster_structures, f)
    print(f"\n✅ {structures_path.name}: {len(booster_structures)} sets")

    # Price columns are intentionally absent — pipeline.py merges fresh prices each run
    df = pd.DataFrame(card_rows)
    metadata_path = CONFIG_DIR / "card_metadata.csv"
    df.to_csv(metadata_path, index=False)
    print(f"✅ {metadata_path.name}: {len(df):,} rows")

    meta_path = CONFIG_DIR / "meta.json"
    with open(meta_path, "w") as f:
        json.dump({
            "mtgjson_version": version,
            "last_regenerated": datetime.now(timezone.utc).isoformat(),
        }, f, indent=2)
    print(f"✅ {meta_path.name}: version {version}")

    print("\n=== Done ===")


if __name__ == "__main__":
    main()
