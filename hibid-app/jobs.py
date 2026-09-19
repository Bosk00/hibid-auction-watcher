from __future__ import annotations

import os
from datetime import datetime, timezone

import db
import ha_webhook
import scoring
from scraper import flatten_lot, search_all_lots

DEFAULT_ZIP = os.environ.get("HIBID_ZIP", "")
DEFAULT_MILES = int(os.environ.get("HIBID_MILES", "100")) # can select from "25, 50, 100, 250 and 500 miles"


def run_all_searches() -> dict:
    """Scrape every unique saved term, score for every profile that uses
    it, persist everything, and notify on brand-new matches."""
    now_iso = datetime.now(timezone.utc).isoformat()

    term_rows = db.list_all_terms()
    if not term_rows:
        print("No saved search terms yet - nothing to do.")
        return {"terms_run": 0, "lots_seen": 0, "new_matches": 0}

    # Group by term so we only hit HiBid once per unique search string,
    # even if multiple profiles search the same thing.
    profiles_by_term: dict[str, list] = {}
    for row in term_rows:
        profiles_by_term.setdefault(row["term"], []).append(row)

    total_lots_seen = 0
    new_match_count = 0

    for term, rows in profiles_by_term.items():
        print(f"Searching HiBid for: {term!r}")
        try:
            raw_lots = search_all_lots(term, zip_code=DEFAULT_ZIP, miles=DEFAULT_MILES)
        except Exception as exc:  # noqa: BLE001 - keep the scheduler alive
            print(f"  search failed for {term!r}: {exc}")
            continue

        for raw_lot in raw_lots:
            lot = flatten_lot(raw_lot)
            if not lot["lot_id"]:
                continue
            db.upsert_lot(lot, now_iso)
            total_lots_seen += 1

            for term_row in rows:
                profile_id = term_row["profile_id"]
                weights = db.get_word_weights(profile_id, term)
                score, qty_found = scoring.score_lot(
                    lot["title"], term_row["target_quantity"], weights
                )
                is_new = db.upsert_match(
                    lot["lot_id"], profile_id, term, qty_found, score, now_iso
                )
                if is_new:
                    new_match_count += 1

    # Notify on everything not yet notified (new matches from this run,
    # or any left over from a previous run that failed to send).
    for match in db.unnotified_matches():
        ha_webhook.notify_new_match(
            match["profile_name"], dict(match), match["term"], match["score"]
        )
        db.mark_notified(match["lot_id"], match["profile_id"], match["term"])

    print(f"Done. {total_lots_seen} lots seen, {new_match_count} new matches.")
    return {
        "terms_run": len(profiles_by_term),
        "lots_seen": total_lots_seen,
        "new_matches": new_match_count,
    }
