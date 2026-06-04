"""ranker.py - Product comparison, relevance filtering, and ranking.

Algorithmic logic ported verbatim from scan2order2/server.py and
scan2order2/automators/base.py. Local Ollama LLM ranking is used as an
optional fallback when the algorithmic ranker finds no winner; the model
is selected via the OLLAMA_MODEL env var (default qwen2.5vl:3b — the same
multimodal model used for OCR) and reached via OLLAMA_HOST
(default http://ollama:11434 in docker-compose).
"""

import asyncio
import os
import re

_QTY_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(kg|g|gm|grams?|ml|l|liters?|litres?|pcs?|pieces?|pack)\b",
    re.IGNORECASE,
)

# Words too generic to use as a relevance signal.
_GENERIC_FOOD_WORDS = frozenset([
    'fresh', 'organic', 'natural', 'premium', 'indian', 'local', 'farm',
    'daily', 'best', 'quality', 'select', 'special', 'fine', 'pure',
    'raw', 'dried', 'cut', 'sliced', 'chopped', 'peeled', 'diced',
    'minced', 'grated', 'whole', 'halved', 'baby', 'mini', 'large',
    'small', 'medium', 'red', 'green', 'yellow', 'white', 'black',
    'brown', 'dark', 'light', 'sweet', 'sour', 'spicy', 'mild',
    'hot', 'cold', 'frozen', 'packed', 'loose', 'washed',
    # Generic food category words
    'vegetable', 'vegetables', 'fruit', 'fruits', 'dairy', 'milk',
    'oil', 'sauce', 'powder', 'mix', 'blend', 'paste', 'extract',
    'essence', 'spice', 'spices', 'herb', 'herbs', 'grain', 'grains',
    'rice', 'flour', 'salt', 'sugar', 'pepper', 'water', 'juice',
    # Qualifiers
    'whole', 'sliced', 'frozen', 'roasted', 'raw', 'hot', 'cold',
    'classic', 'regular', 'original', 'natural', 'pure', 'extra',
])


def _parse_qty(s: str):
    """Parse a quantity string into (numeric_grams_or_ml_or_count, unit_kind).
    Returns None if no parseable quantity found."""
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
    return _parse_qty(product.get("unit", "")) or _parse_qty(product.get("name", ""))


def _product_price(p: dict) -> float:
    return (p.get("sale_price") or p.get("price") or float("inf"))


def _price_per_unit(product: dict) -> float:
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
    pp = _product_qty(product)
    if pp is None:
        return True
    pval, pkind = pp
    base = {"mass": 2000, "volume": 2000, "count": 12}.get(pkind, 2000)
    pq = _parse_qty(query_qty) if query_qty else None
    if pq and pq[1] == pkind and pq[0] > base:
        base = pq[0] * 1.2
    return pval <= base


def _qty_multiplier(target_qty_str: str, product: dict) -> int | None:
    """Return N such that N × product_qty == target_qty (integer ≥ 1), or None.

    None means the product cannot be combined in whole-unit multiples to reach
    exactly the requested weight/volume, so it should be filtered out when a
    target is explicitly specified.

    Returns 1 (no-op) when target_qty_str is empty/unparseable so products
    without a size specification are kept in the running.
    """
    if not target_qty_str:
        return 1  # no target → order 1 unit, no filtering

    tq = _parse_qty(target_qty_str)
    if tq is None:
        return 1  # unparseable target → don't filter

    pq = _product_qty(product)
    if pq is None:
        return None  # target given but product has no size → exclude

    t_val, t_kind = tq
    p_val, p_kind = pq

    if t_kind != p_kind or p_val <= 0 or t_val <= 0:
        return None

    if p_val > t_val:
        return None  # single unit already exceeds target

    # t_val / p_val must be a positive integer (≤ tiny floating-point error)
    ratio = t_val / p_val
    n = int(round(ratio))
    if n >= 1 and abs(n * p_val - t_val) < 0.5:
        return n
    return None


def _effective_price(target_qty_str: str, product: dict) -> float:
    """Total cost to reach the target quantity using this product.

    Returns product.price × N where N = _qty_multiplier().
    Falls back to the raw product price when no qty multiplier applies.
    """
    n = _qty_multiplier(target_qty_str, product)
    if n is None:
        return float("inf")  # excluded product
    return _product_price(product) * n


def _qty_distance(query_qty: str, product: dict) -> float:
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


def filter_by_query_relevance(products: list, query: str) -> list:
    """Return only products whose name actually matches the query.

    Strict filter: if no match, returns [] so the caller can show all
    products in a swap modal rather than auto-picking an off-topic result.
    """
    if not products:
        return []
    query_words = [w.lower() for w in re.findall(r"[A-Za-z]+", query)
                   if len(w) >= 3]
    if not query_words:
        return products

    non_generic = [w for w in query_words if w not in _GENERIC_FOOD_WORDS]

    if not non_generic:
        return [p for p in products
                if any(w in (p.get("name") or "").lower() for w in query_words)]

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
        if all(any(s in name for s in stems) for stems in word_stems):
            kept.append(p)
        else:
            dropped.append(p.get("name", "")[:60])

    if dropped:
        req = " AND ".join(sorted(s)[0] for s in word_stems)
        print(f"[filter_relevance] Dropped {len(dropped)} not matching '{req}': {dropped[:3]}")
    return kept


# ---------------------------------------------------------------------------
# Shortlist builder — alternative products for the swap UI
# ---------------------------------------------------------------------------

def build_shortlist(
    app_results: dict,
    search_query: str,
    qty_str: str,
    winner_pid: str | None,
    winner_app: str | None,
    n: int = 15,
) -> list[dict]:
    """Return up to n ranked alternative products for the user to swap to.

    Surfaces "candidates removed at the nearest stage to the final pick":

    Tier 1 (best alternatives): passed both relevance and reasonable-size
        filters, not a gen-* placeholder, sorted by the same
        (qty_distance, price_per_unit, price) key used to select the winner.

    Tier 2 (fallback): passed relevance but failed the size filter (e.g. a
        5 kg bag when the user asked for 500 g). Included only when tier 1
        has fewer than n items.

    The winner (winner_pid on winner_app) is always excluded so the shortlist
    contains only swappable alternatives. Each item keeps all fields the store
    search function originally attached (including app / app_name).
    """
    tier1: list[dict] = []
    tier2: list[dict] = []
    seen: set[tuple] = set()
    if winner_pid:
        seen.add((str(winner_pid), winner_app or ""))

    for app_name, products in app_results.items():
        if not products:
            continue
        relevant = filter_by_query_relevance(products, search_query)
        sized = [
            p for p in relevant
            if _is_reasonable_size(p, qty_str)
            and not str(p.get("product_id") or "").startswith("gen-")
        ]

        for p in sized:
            pid = str(p.get("product_id") or "")
            key = (pid, app_name)
            if pid and key not in seen:
                seen.add(key)
                tier1.append(p)

        for p in relevant:
            if _is_reasonable_size(p, qty_str):
                continue  # already counted in tier1 (or excluded as winner)
            pid = str(p.get("product_id") or "")
            key = (pid, app_name)
            if pid and key not in seen:
                seen.add(key)
                tier2.append(p)

    def _sort_key(p: dict) -> tuple:
        return (_qty_distance(qty_str, p), _price_per_unit(p), _product_price(p))

    tier1.sort(key=_sort_key)
    tier2.sort(key=_sort_key)
    return (tier1 + tier2)[:n]


# ---------------------------------------------------------------------------
# Ollama fallback (optional — only runs when algorithmic ranker finds no winner)
# ---------------------------------------------------------------------------

async def _ollama_rank(original_item: str, products: list[dict]) -> list[dict]:
    """Ask the local Ollama model to pick the best product match.

    Returns the same products list with recommended=True on the best match.
    Falls back to returning products unchanged on any error.
    Requires OLLAMA_HOST env var (set automatically by docker-compose).
    """
    host = os.getenv("OLLAMA_HOST")
    if not host:
        return products

    model = os.getenv("OLLAMA_MODEL", "qwen2.5vl:3b")

    try:
        import httpx as _httpx
        product_list_str = "\n".join(
            f"{i+1}. {p.get('name')} ({p.get('unit','')}): ₹{_product_price(p):.0f} [{p.get('app_name','')}]"
            for i, p in enumerate(products[:10])
        )
        prompt = (
            f"A user searched for: \"{original_item}\"\n"
            f"These products were found:\n{product_list_str}\n\n"
            f"Return a JSON object with keys:\n"
            f"  best_index: (1-based index of the best match)\n"
            f"  reason: (one short sentence explaining why, 10 words max)\n"
            f"Only return valid JSON, no other text."
        )

        async with _httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{host}/v1/chat/completions",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0,
                    "max_tokens": 100,
                },
            )

        if resp.status_code != 200:
            return products

        content = resp.json()["choices"][0]["message"]["content"].strip()
        result = json_loads_safe(content)
        if not result:
            return products

        best_idx = int(result.get("best_index", 1)) - 1
        reason = result.get("reason", "")

        for i, p in enumerate(products):
            p["recommended"] = (i == best_idx)
            p["reason"] = reason if i == best_idx else ""

        return products
    except Exception as e:
        print(f"[ollama_rank] error: {e}")
        return products


def json_loads_safe(s: str) -> dict | None:
    import json as _json
    try:
        return _json.loads(s)
    except Exception:
        # Try to extract JSON object from response
        m = re.search(r'\{[^}]+\}', s, re.DOTALL)
        if m:
            try:
                return _json.loads(m.group(0))
            except Exception:
                pass
    return None


# ---------------------------------------------------------------------------
# Core comparison logic
# ---------------------------------------------------------------------------

async def compare_one_item(item: dict, user_id: str,
                           available_stores: list[str]) -> dict:
    """Compare a single {name, qty} item across available_stores in parallel.

    Returns one comparison entry:
        {
          item:           input {name, qty}
          search_query:   string sent to each store
          prices:         {store_name: [products sorted cheapest-first]}
          cheapest_app:   selected store (actual cheapest by per-unit price)
          cheapest_price: price of selected product
          selected_pid:   product_id of selected product
        }
    """
    from stores import bigbasket, blinkit, zepto, instamart, flipkart_minutes

    _store_search = {
        "bigbasket":        bigbasket.search_item_api,
        "blinkit":          blinkit.search_item_api,
        "zepto":            zepto.search_item_api,
        "instamart":        instamart.search_item_api,
        "flipkart_minutes": flipkart_minutes.search_item_api,
    }

    qty_str = (item.get("qty") or "").strip()
    # Search WITHOUT the quantity — the stores' text search is more reliable
    # without it, and quantity filtering/multiplication happens post-search.
    search_query = item.get("name", "").strip() or f"{item.get('name', '')} {qty_str}".strip()
    print(f"[compare-one] Searching: '{search_query}' (target qty: '{qty_str}')")

    app_results: dict = {}

    async def search_store(store_name: str):
        fn = _store_search.get(store_name)
        if not fn:
            return
        try:
            products = await fn(user_id, search_query)
            if products:
                app_results[store_name] = sorted(products, key=_product_price)
        except Exception as e:
            print(f"[compare-one][{store_name}] error: {e}")

    await asyncio.gather(*[search_store(s) for s in available_stores])

    cheapest_app = None
    cheapest_price = float("inf")
    cheapest_ppu = float("inf")
    cheapest_eff = float("inf")   # effective price (price × qty_count)
    selected_pid = None
    selected_name = None
    selected_qty_count = 1        # how many units to order
    relevant_count = {}

    # Target amount in base units (g / ml / count) — used to normalise the
    # relaxed fallback so a tiny pack can't masquerade as cheaper than a
    # legitimately-tiling product at another store.
    _tq = _parse_qty(qty_str) if qty_str else None
    target_amount = _tq[0] if _tq else 0

    # Two separate accumulators. The tiling candidate (whole-unit multiple of
    # the requested quantity) ALWAYS wins over a relaxed fallback, no matter the
    # raw price — buying 1×180ml is not a substitute for 1L.
    best_fb = None  # (eff, ppu, price, app, pid, name, n) — relaxed fallback

    for app_name, products in app_results.items():
        if not products:
            relevant_count[app_name] = 0
            continue
        relevant = filter_by_query_relevance(products, search_query)
        relevant_count[app_name] = len(relevant)
        if not relevant:
            continue

        sized = [p for p in relevant if _is_reasonable_size(p, qty_str)]
        if not sized:
            sized = relevant

        sized = [p for p in sized
                 if not str(p.get("product_id") or "").startswith("gen-")]
        if not sized:
            continue

        if qty_str:
            # PRIMARY: only products whose unit size tiles the target exactly.
            qty_valid = [(p, _qty_multiplier(qty_str, p)) for p in sized]
            filtered = [(p, n) for p, n in qty_valid if n is not None]
            if filtered:
                filtered.sort(key=lambda pn: (
                    _product_price(pn[0]) * pn[1],
                    _price_per_unit(pn[0]),
                ))
                best, best_n = filtered[0]
                eff_price = _product_price(best) * best_n
                if _product_price(best) > 0 and eff_price < cheapest_eff:
                    cheapest_eff = eff_price
                    cheapest_ppu = _price_per_unit(best)
                    cheapest_price = _product_price(best)
                    cheapest_app = app_name
                    selected_pid = best.get("product_id")
                    selected_name = best.get("name")
                    selected_qty_count = best_n

            # RELAXED FALLBACK: no exact-tiling product at this store. Score by
            # the cost to obtain the target quantity at this product's per-unit
            # rate (ppu × target) so it's comparable across stores. Only used if
            # NO store has a tiling product at all.
            else:
                sized.sort(key=lambda p: (
                    _qty_distance(qty_str, p), _price_per_unit(p), _product_price(p),
                ))
                fb = sized[0]
                fb_price = _product_price(fb)
                fb_ppu = _price_per_unit(fb)
                fb_eff = (fb_ppu * target_amount
                          if (fb_ppu < float("inf") and target_amount) else fb_price)
                if fb_price > 0 and (best_fb is None or fb_eff < best_fb[0]):
                    best_fb = (fb_eff, fb_ppu, fb_price, app_name,
                               fb.get("product_id"), fb.get("name"), 1)
            continue

        # No target quantity — rank purely by per-unit price.
        sized.sort(key=lambda p: (_price_per_unit(p), _product_price(p)))
        best = sized[0]
        eff_price = _product_price(best)
        if _product_price(best) > 0 and eff_price < cheapest_eff:
            cheapest_eff = eff_price
            cheapest_ppu = _price_per_unit(best)
            cheapest_price = _product_price(best)
            cheapest_app = app_name
            selected_pid = best.get("product_id")
            selected_name = best.get("name")
            selected_qty_count = 1

    # Use the relaxed fallback only when no store had an exact-tiling product.
    if cheapest_app is None and best_fb is not None:
        (cheapest_eff, cheapest_ppu, cheapest_price,
         cheapest_app, selected_pid, selected_name, selected_qty_count) = best_fb

    # No algorithmic winner — try Ollama as a fallback
    if not cheapest_app and os.getenv("OLLAMA_HOST") and app_results:
        all_products = []
        for store_name, prods in app_results.items():
            for p in prods:
                all_products.append({**p, "app_name": store_name})
        if all_products:
            ranked = await _ollama_rank(search_query, all_products)
            best = next((p for p in ranked if p.get("recommended")), None)
            if best:
                cheapest_app = best.get("app_name")
                cheapest_price = _product_price(best)
                selected_pid = best.get("product_id")
                selected_name = best.get("name")
                selected_qty_count = _qty_multiplier(qty_str, best) or 1
                cheapest_eff = cheapest_price * selected_qty_count
                print(f"[compare-one]   -> ollama picked: {cheapest_app} '{(selected_name or '')[:50]}'")

    rel_summary = ", ".join(
        f"{a}={relevant_count.get(a, 0)}/{len(app_results.get(a, []))}"
        for a in available_stores
    )
    if cheapest_app:
        print(f"[compare-one]   -> relevant={rel_summary}, "
              f"picked: {cheapest_app} '{(selected_name or '')[:50]}' @ ₹{cheapest_price}")
    else:
        print(f"[compare-one]   -> relevant={rel_summary}, NO auto-pick")

    return {
        "item": item,
        "search_query": search_query,
        "prices": app_results,
        "cheapest_app": cheapest_app,
        # cheapest_price = per-unit price of the selected product
        # cheapest_effective_price = total cost to meet the requested quantity
        "cheapest_price": cheapest_price if cheapest_app else None,
        "cheapest_effective_price": cheapest_eff if cheapest_app else None,
        "selected_pid": selected_pid,
        # qty_count: number of units to add to cart (>1 when multiplying to
        # reach target weight, e.g. 4× 250g to make 1kg).
        "qty_count": selected_qty_count,
        # Up to 15 ranked alternatives for the swap UI.
        "shortlist": build_shortlist(
            app_results, search_query, qty_str,
            winner_pid=selected_pid, winner_app=cheapest_app,
        ),
    }


def selected_product(entry: dict) -> dict:
    """Return the currently-selected product for a comparison entry, or {}."""
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


def build_carts_from_comparison(comparison: list) -> dict:
    """Group a comparison list into {store: {items, total}} carts.

    count = qty_count (units to buy to reach the target weight) × any explicit
    per-line count the user set. total uses unit_price × count so weight
    multipliers (e.g. 4× 250g for 1kg) are reflected in the basket total.
    """
    carts: dict = {}
    for i, entry in enumerate(comparison):
        app = entry.get("cheapest_app")
        if not app:
            continue
        if app not in carts:
            carts[app] = {"items": [], "total": 0}
        prod = selected_product(entry)
        try:
            user_count = max(1, int(entry.get("item", {}).get("count") or 1))
        except (TypeError, ValueError):
            user_count = 1
        qty_count = max(1, int(entry.get("qty_count") or 1))
        count = qty_count * user_count
        unit_price = entry["cheapest_price"] or 0
        carts[app]["items"].append({
            **entry["item"],
            "price": unit_price,
            "count": count,
            "product_id": prod.get("product_id"),
            "store_product_id": prod.get("store_product_id", ""),
            "listing_id": prod.get("listing_id") or prod.get("store_product_id", ""),
            "fc_id": prod.get("fc_id", ""),
            "search_query": entry["search_query"],
            "matched_name": prod.get("name", ""),
            "matched_unit": prod.get("unit", ""),
            "comparison_index": i,
        })
        carts[app]["total"] += unit_price * count
    return carts
