"""backend/ranker.py - Product ranking and comparison utilities.

Two ranking strategies:
  1. Algorithmic (primary): qty-distance + per-unit price + absolute price sort.
     Source: scan2order2/server.py + automators/base.py.
  2. Groq LLM fallback (optional): llama-3.3-70b-versatile for edge cases.
     Source: scan2order1/backend/agents/ranking_agent.py.

Public API:
    filter_by_query_relevance(products, query) -> list[dict]
    rank_products(items_with_app_results) -> sorted by best value
    _compare_one_item(item, store_results) -> comparison entry dict
    _selected_product(entry) -> dict
    _build_carts_from_comparison(comparison) -> {app: {items, total}}
    rank_with_groq(original_item, products) -> list[dict]   (optional)
"""

import os
import re
import json

import httpx  # lightweight dep check - Groq uses its own client


# ── Generic food words (exact match skiplist for relevance filter) ─

_GENERIC_FOOD_WORDS = frozenset([
    'sauce', 'oil', 'rice', 'dal', 'ghee', 'butter', 'milk', 'flour',
    'salt', 'sugar', 'atta', 'water', 'cream', 'soup', 'masala',
    'juice', 'jam', 'honey', 'vinegar', 'ketchup', 'mustard',
    'mayo', 'mayonnaise', 'pickle', 'chutney', 'spread',
    'seed', 'leaf', 'leaves', 'fresh', 'organic', 'premium',
    'powder', 'paste', 'mix', 'pack', 'large', 'small',
    'whole', 'sliced', 'frozen', 'roasted', 'raw', 'hot', 'cold',
    'classic', 'regular', 'original', 'natural', 'pure', 'extra',
])


def filter_by_query_relevance(products: list, query: str) -> list:
    """Return only products whose name actually matches the query.

    Strategy:
      1. Extract query words >=3 chars.
      2. Identify "non-generic" words - those NOT in the generic-food skiplist.
         For "Worcester Sauce", that's just "Worcester" (sauce is generic).
      3. ALL non-generic words must appear in the product name (stem-aware
         substring match). "Cherry Tomatoes" requires both "cherr*" and
         "tomato*" to be present.
      4. If the whole query is generic ("Hot Sauce", "Olive Oil"), require
         any of its words to appear in the product name.

    Returns [] if no products match. Caller decides what to do (typically:
    don't auto-pick, but still show all products in swap-result modal).
    """
    if not products:
        return []
    query_words = [w.lower() for w in re.findall(r"[A-Za-z]+", query)
                   if len(w) >= 3]
    if not query_words:
        return products  # nothing to filter on

    non_generic = [w for w in query_words if w not in _GENERIC_FOOD_WORDS]

    if not non_generic:
        # Whole query is generic; any-word match.
        return [p for p in products
                if any(w in (p.get("name") or "").lower() for w in query_words)]

    # Build stem sets for each non-generic word.
    word_stems = []
    for w in non_generic:
        stems = {w}
        if w.endswith('s'):
            stems.add(w[:-1])
        if w.endswith('es') and len(w) > 4:
            stems.add(w[:-2])
        word_stems.append(stems)

    kept = []
    dropped = []
    for p in products:
        name = (p.get("name") or "").lower()
        matched_all = all(
            any(s in name for s in stems) for stems in word_stems
        )
        if matched_all:
            kept.append(p)
        else:
            dropped.append(p.get("name", "")[:60])

    if dropped:
        req = " AND ".join(sorted(s)[0] for s in word_stems)
        print(f"[filter_relevance] Dropped {len(dropped)} not matching '{req}': "
              f"{dropped[:3]}")
    return kept


# ── Quantity parsing ─────────────────────────────────────────────

_QTY_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(kg|g|gm|grams?|ml|l|liters?|litres?|pcs?|pieces?|pack)\b",
    re.IGNORECASE,
)


def _parse_qty(s: str):
    """Parse a quantity string into (numeric_value, unit_kind) or None.

    unit_kind is one of 'mass', 'volume', 'count'.

      '500g'   -> (500.0, 'mass')
      '1 kg'   -> (1000.0, 'mass')
      '1.5 L'  -> (1500.0, 'volume')
      '4 pcs'  -> (4.0, 'count')
      '1 pack' -> (1.0, 'count')
      'Tomato' -> None
    """
    if not s:
        return None
    m = _QTY_RE.search(str(s).replace(",", ""))
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2).lower()
    if unit == "kg":
        return (val * 1000, "mass")
    if unit in ("g", "gm", "gram", "grams"):
        return (val, "mass")
    if unit in ("l", "liter", "litre", "liters", "litres"):
        return (val * 1000, "volume")
    if unit == "ml":
        return (val, "volume")
    if unit in ("pc", "pcs", "piece", "pieces", "pack"):
        return (val, "count")
    return None


def _product_qty(product: dict):
    """Parse product qty from its unit field, falling back to its name."""
    return _parse_qty(product.get("unit", "")) or _parse_qty(product.get("name", ""))


def _product_price(p: dict) -> float:
    """Effective price: sale_price if positive, else price."""
    return (p.get("sale_price") or p.get("price") or float("inf"))


def _price_per_unit(product: dict) -> float:
    """Per-gram (or per-ml or per-piece) price for sorting by value."""
    pp = _product_qty(product)
    if pp is None:
        return float("inf")
    qval, _ = pp
    if qval <= 0:
        return float("inf")
    price = _product_price(product)
    if price >= float("inf"):
        return float("inf")
    return price / qval


def _is_reasonable_size(product: dict, query_qty: str) -> bool:
    """Drop bulk packs the user almost certainly didn't want.

    Defaults: mass <= 2kg, volume <= 2L, count <= 12.
    Raised to 1.2x the query qty if user explicitly asked for bulk.
    Products with no parseable qty are kept (can't tell).
    """
    pp = _product_qty(product)
    if pp is None:
        return True
    pval, pkind = pp
    base = {"mass": 2000, "volume": 2000, "count": 12}.get(pkind, 2000)
    pq = _parse_qty(query_qty) if query_qty else None
    if pq and pq[1] == pkind and pq[0] > base:
        base = pq[0] * 1.2
    return pval <= base


def _qty_distance(query_qty: str, product: dict) -> float:
    """Distance between the user's query qty and a product's qty.

    0 = exact match, larger = further off, +inf = can't compare.
    Uses ratio: max(a, b) / min(a, b) - 1.
    """
    pq = _parse_qty(query_qty)
    if pq is None:
        return float("inf")
    pp = _parse_qty(product.get("unit", "")) or _parse_qty(product.get("name", ""))
    if pp is None or pp[1] != pq[1]:
        return float("inf")
    qv, _ = pq
    pv, _ = pp
    if qv <= 0 or pv <= 0:
        return float("inf")
    return max(qv, pv) / min(qv, pv) - 1.0


# ── Comparison logic ─────────────────────────────────────────────

def _compare_one_item(item: dict, store_results: dict) -> dict:
    """Compare a single item across store search results.

    Args:
        item: {"name": str, "qty": str, "count": int}
        store_results: {store_name: [product_dicts sorted cheapest-first]}

    Returns:
        {
          "item": item,
          "search_query": str,
          "prices": {store_name: [products]},
          "cheapest_app": str | None,
          "cheapest_price": float | None,
          "selected_pid": str | None,
        }
    """
    search_query = f"{item.get('name', '')} {item.get('qty', '')}".strip()
    qty_str = (item.get("qty") or "").strip()

    cheapest_app = None
    cheapest_price = float("inf")
    cheapest_dist = float("inf")
    cheapest_ppu = float("inf")
    selected_pid = None
    selected_name = None
    relevant_count = {}

    for app_name, products in store_results.items():
        if not products:
            relevant_count[app_name] = 0
            continue

        relevant = filter_by_query_relevance(products, search_query)
        relevant_count[app_name] = len(relevant)
        if not relevant:
            continue

        # Drop unreasonably large bulk packs unless user asked for one.
        sized = [p for p in relevant if _is_reasonable_size(p, qty_str)]
        if not sized:
            sized = relevant

        # Exclude gen-XXXX pids (generic scraper output - no reliable cart-add).
        sized = [p for p in sized if not str(p.get("product_id") or "").startswith("gen-")]
        if not sized:
            continue

        # Sort by: qty distance → per-unit price → absolute price.
        sized.sort(key=lambda p: (
            _qty_distance(qty_str, p),
            _price_per_unit(p),
            _product_price(p),
        ))
        best = sized[0]
        price = _product_price(best)
        dist = _qty_distance(qty_str, best)
        ppu = _price_per_unit(best)

        if price > 0 and (dist, ppu, price) < (cheapest_dist, cheapest_ppu, cheapest_price):
            cheapest_dist = dist
            cheapest_ppu = ppu
            cheapest_price = price
            cheapest_app = app_name
            selected_pid = best.get("product_id")
            selected_name = best.get("name")

    rel_summary = ", ".join(
        f"{a}={relevant_count.get(a, 0)}/{len(store_results.get(a, []))}"
        for a in store_results
    )
    if cheapest_app:
        print(f"[compare-one] '{search_query}' -> relevant={rel_summary}, "
              f"picked: {cheapest_app} '{(selected_name or '')[:50]}' @ ₹{cheapest_price}")
    else:
        print(f"[compare-one] '{search_query}' -> relevant={rel_summary}, "
              f"NO auto-pick (user can swap to choose)")

    return {
        "item": item,
        "search_query": search_query,
        "prices": store_results,
        "cheapest_app": cheapest_app,
        "cheapest_price": cheapest_price if cheapest_app else None,
        "selected_pid": selected_pid,
    }


def _selected_product(entry: dict) -> dict:
    """Return the currently-picked product for a comparison entry, or {}."""
    app = entry.get("cheapest_app")
    if not app:
        return {}
    products = (entry.get("prices") or {}).get(app) or []
    if not products:
        return {}
    pid = entry.get("selected_pid")
    if pid is not None:
        for p in products:
            if p.get("product_id") == pid:
                return p
    return products[0]


def _build_carts_from_comparison(comparison: list) -> dict:
    """Group comparison entries into {app: {items, total}} carts."""
    carts: dict = {}
    for i, entry in enumerate(comparison):
        app = entry.get("cheapest_app")
        if not app:
            continue
        if app not in carts:
            carts[app] = {"items": [], "total": 0}
        prod = _selected_product(entry)
        try:
            count = max(1, int(entry.get("item", {}).get("count") or 1))
        except (TypeError, ValueError):
            count = 1
        unit_price = entry["cheapest_price"] or 0
        carts[app]["items"].append({
            **entry["item"],
            "price": unit_price,
            "product_id": prod.get("product_id"),
            "search_query": entry["search_query"],
            "matched_name": prod.get("name", ""),
            "matched_unit": prod.get("unit", ""),
            "comparison_index": i,
            # Blinkit needs the full cart_item from search for add-to-cart.
            "_cart_item": prod.get("_cart_item"),
            "merchant_id": prod.get("merchant_id"),
            # Zepto needs these for enrichment.
            "store_product_id": prod.get("store_product_id"),
            "mrp_paise": prod.get("mrp_paise"),
            "discount_applicable": prod.get("discount_applicable"),
            # BigBasket needs fc_id.
            "fc_id": prod.get("fc_id"),
        })
        carts[app]["total"] += unit_price * count
    return carts


# ── Groq LLM fallback ────────────────────────────────────────────

def rank_with_groq(original_item: dict, products: list) -> list:
    """Optional Groq LLM ranking for edge cases.

    Takes the original item {name, qty/quantity} and a list of products,
    returns the top-3 products with a 'recommended' flag and 'reason' on
    the best match. Falls back to products[:3] on any error.

    Requires GROQ_API_KEY env var. Returns [] immediately if not configured.
    """
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key or not products:
        return products[:3]

    try:
        from groq import Groq
        client = Groq(api_key=api_key)

        product_list = "\n".join([
            f"{i+1}. {p.get('name', '')} - {p.get('unit', p.get('weight', ''))} - "
            f"₹{p.get('sale_price') or p.get('price', '')}"
            for i, p in enumerate(products)
        ])

        item_desc = original_item.get("name", "")
        quantity = original_item.get("qty") or original_item.get("quantity", "")

        prompt = f"""The user wants to buy: "{item_desc}" (quantity: {quantity})

Here are the search results (each variant is listed separately):
{product_list}

Your job:
1. Pick the 3 most relevant products from the list
2. From those 3, pick 1 as the best match

Consider:
- Product name must match what the user wants (no wrong flavours, no unrelated products)
- Weight should be closest to what they asked for
- If quantity has no unit (e.g. "4 gatorade"), it means 4 units, not 400ml
- If the user wants plain/unflavoured, reject flavoured versions

Reply ONLY with a JSON object, no other text:
{{
  "top_3": [1, 5, 12],
  "best_match": 5,
  "reason": "brief reason why"
}}

Use the numbers from the list above."""

        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150,
        )
        raw = response.choices[0].message.content.strip()
        cleaned = (
            raw.removeprefix("```json")
               .removeprefix("```")
               .removesuffix("```")
               .strip()
        )
        result = json.loads(cleaned)

        top_3_indices = [int(x) - 1 for x in result["top_3"]]
        best_idx = int(result["best_match"]) - 1
        reason = result.get("reason", "")

        top_products = []
        for i in top_3_indices:
            if 0 <= i < len(products):
                p = products[i].copy()
                p["recommended"] = (i == best_idx)
                p["reason"] = reason if i == best_idx else ""
                top_products.append(p)

        return top_products

    except Exception as e:
        print(f"[ranker] Groq fallback failed: {e}")
        return products[:3]
