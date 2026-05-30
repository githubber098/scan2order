"""ranker.py - Product comparison, relevance filtering, and ranking.

Algorithmic logic ported verbatim from scan2order2/server.py and
scan2order2/automators/base.py. Local Ollama LLM ranking is used as an
optional fallback when the algorithmic ranker finds no winner; the model
is selected via the OLLAMA_MODEL env var (default gemma4:e2b — the same
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

    model = os.getenv("OLLAMA_MODEL", "gemma4:e2b")

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
    from stores import bigbasket, blinkit, zepto

    _store_search = {
        "bigbasket": bigbasket.search_item_api,
        "blinkit": blinkit.search_item_api,
        "zepto": zepto.search_item_api,
    }

    search_query = f"{item.get('name', '')} {item.get('qty', '')}".strip()
    print(f"[compare-one] Searching: '{search_query}'")

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
    cheapest_dist = float("inf")
    cheapest_ppu = float("inf")
    selected_pid = None
    selected_name = None
    relevant_count = {}
    qty_str = (item.get("qty") or "").strip()

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
        "cheapest_price": cheapest_price if cheapest_app else None,
        "selected_pid": selected_pid,
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
    """Group a comparison list into {store: {items, total}} carts."""
    carts: dict = {}
    for i, entry in enumerate(comparison):
        app = entry.get("cheapest_app")
        if not app:
            continue
        if app not in carts:
            carts[app] = {"items": [], "total": 0}
        prod = selected_product(entry)
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
        })
        carts[app]["total"] += unit_price * count
    return carts
