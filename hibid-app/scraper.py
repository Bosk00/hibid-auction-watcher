"""
HiBid Ontario lot search - talks to HiBid's real GraphQL API directly.
"""

from __future__ import annotations

import re
import sys
import time
from datetime import datetime
from typing import Any, Optional

import requests

GRAPHQL_URL = "https://hibid.com/graphql"
SITE_SUBDOMAIN = "ontario.hibid.com"
PAGE_LENGTH = 100
REQUEST_TIMEOUT = 20
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2

LOT_SEARCH_QUERY = """
query LotSearch($auctionId: Int = null, $pageNumber: Int!, $pageLength: Int!, $category: CategoryId = null, $searchText: String = null, $zip: String = null, $miles: Int = null, $shippingOffered: Boolean = false, $countryName: String = null, $state: String = null, $status: AuctionLotStatus = null, $sortOrder: EventItemSortOrder = null, $filter: AuctionLotFilter = null, $isArchive: Boolean = false, $dateStart: DateTime, $dateEnd: DateTime, $countAsView: Boolean = true, $hideGoogle: Boolean = false, $eventItemIds: [Int!] = null) {
  lotSearch(
    input: {auctionId: $auctionId, category: $category, searchText: $searchText, zip: $zip, miles: $miles, shippingOffered: $shippingOffered, countryName: $countryName, state: $state, status: $status, sortOrder: $sortOrder, filter: $filter, isArchive: $isArchive, dateStart: $dateStart, dateEnd: $dateEnd, countAsView: $countAsView, hideGoogle: $hideGoogle, eventItemIds: $eventItemIds}
    pageNumber: $pageNumber
    pageLength: $pageLength
    sortDirection: DESC
  ) {
    pagedResults {
      pageLength
      pageNumber
      totalCount
      filteredCount
      results {
        auction {
          id
          eventName
          eventCity
          eventState
          eventZip
          eventDateBegin
          eventDateEnd
          bidOpenDateTime
          bidCloseDateTime
          distanceMiles
          auctioneer { name city state __typename }
          __typename
        }
        bidAmount
        bidQuantity
        description
        estimate
        featuredPicture { thumbnailLocation fullSizeLocation __typename }
        id
        itemId
        lead
        lotNumber
        lotState {
          bidCount highBid isClosed isLive isNotYetLive minBid
          priceRealized productUrl status timeLeft timeLeftSeconds
          timeLeftTitle __typename
        }
        pictureCount
        quantity
        shippingOffered
        site { domain subdomain __typename }
        distanceMiles
        __typename
      }
      __typename
    }
    __typename
  }
}
"""


class HiBidSearchError(RuntimeError):
    pass


def _post_lot_search(
    session: requests.Session,
    search_text: str,
    page_number: int,
    status: str = "OPEN",
    sort_order: str = "TIME_LEFT",
    zip_code: Optional[str] = None,
    miles: Optional[int] = None,
) -> dict[str, Any]:
    payload = {
        "operationName": "LotSearch",
        "variables": {
            "auctionId": None,
            "category": None,
            "searchText": search_text,
            "zip": zip_code,
            "miles": miles,
            "shippingOffered": False,
            "countryName": None,
            "state": None,
            "status": status,
            "sortOrder": sort_order,
            "filter": None,
            "isArchive": False,
            "dateStart": None,
            "dateEnd": None,
            "countAsView": False,
            "hideGoogle": False,
            "eventItemIds": None,
            "pageNumber": page_number,
            "pageLength": PAGE_LENGTH,
        },
        "query": LOT_SEARCH_QUERY,
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "SITE_SUBDOMAIN": SITE_SUBDOMAIN,
        "Origin": "https://hibid.com",
        "Referer": f"https://hibid.com/ontario/lots?q={requests.utils.quote(search_text)}",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
    }

    last_error: Optional[Exception] = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            resp = session.post(GRAPHQL_URL, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            if data.get("errors"):
                raise HiBidSearchError(f"GraphQL error on page {page_number}: {data['errors']}")
            return data
        except (requests.RequestException, HiBidSearchError) as exc:
            last_error = exc
            print(f"  [retry {attempt}/{RETRY_ATTEMPTS}] page {page_number} failed: {exc}", file=sys.stderr)
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)

    raise HiBidSearchError(f"Giving up on page {page_number} after {RETRY_ATTEMPTS} attempts: {last_error}")


def search_all_lots(
    search_text: str,
    status: str = "OPEN",
    sort_order: str = "TIME_LEFT",
    zip_code: Optional[str] = None,
    miles: Optional[int] = None,
    max_pages: int = 100,
) -> list[dict[str, Any]]:
    session = requests.Session()
    all_results: list[dict[str, Any]] = []
    page_number = 1

    while page_number <= max_pages:
        data = _post_lot_search(session, search_text, page_number, status, sort_order, zip_code, miles)
        paged = data["data"]["lotSearch"]["pagedResults"]
        results = paged["results"]
        filtered_count = paged["filteredCount"]

        print(f"  page {page_number}: got {len(results)} lots (filteredCount={filtered_count})")
        all_results.extend(results)

        if len(all_results) >= filtered_count or not results:
            break
        page_number += 1

    return all_results


def _closing_datetime(lot: dict[str, Any]) -> Optional[datetime]:
    raw = (lot.get("auction") or {}).get("bidCloseDateTime")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _slugify(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text or "").strip("-").lower()
    return text or "lot"


def flatten_lot(lot: dict[str, Any]) -> dict[str, Any]:
    auction = lot.get("auction") or {}
    lot_state = lot.get("lotState") or {}
    site = lot.get("site") or {}

    domain = site.get("domain") or "hibid.com"
    title = lot.get("lead") or lot.get("description") or ""

    product_url = lot_state.get("productUrl")
    if product_url and product_url.startswith("/"):
        product_url = f"https://{domain}{product_url}"

    item_id = lot.get("id") or lot.get("itemId")
    if not product_url and item_id:
        product_url = f"https://{domain}/lot/{item_id}/{_slugify(title)}"

    closing = _closing_datetime(lot)
    picture = lot.get("featuredPicture") or {}
    image_url = picture.get("thumbnailLocation") or picture.get("fullSizeLocation") or ""

    return {
        "lot_id": str(item_id),
        "title": title,
        "current_bid": lot_state.get("highBid") or lot.get("bidAmount"),
        "status": lot_state.get("status") or "OPEN",
        "time_left": lot_state.get("timeLeftTitle") or lot_state.get("timeLeft"),
        "closing_date": closing.isoformat() if closing else "",
        "distance_miles": lot.get("distanceMiles") or auction.get("distanceMiles"),
        "auction_name": auction.get("eventName"),
        "auction_city": auction.get("eventCity"),
        "auction_state": auction.get("eventState"),
        "url": product_url or "",
        "image_url": image_url,
    }
