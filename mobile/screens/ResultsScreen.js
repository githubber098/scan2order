/**
 * ResultsScreen.js
 *
 * Shows multi-store comparison results from /api/compare.
 *
 * Layout:
 *   - Per-item rows: item name, cheapest store tag, price, matched product
 *   - Tap a row → expand to see all stores' results and swap selection
 *   - Cart summary section: per-store item lists + totals
 *   - "Add to all carts" button → /api/cart/add-all with progress
 *
 * Data shape (compareData from navigate params):
 * {
 *   comparison: [{item, search_query, prices:{store:[products]}, cheapest_app, cheapest_price, selected_pid}],
 *   carts: {store: {items:[...], total}},
 *   grand_total: float,
 *   summary: {total_items, items_found, apps_used}
 * }
 */

import {
  View, Text, StyleSheet, ScrollView, TouchableOpacity,
  ActivityIndicator, Alert,
} from 'react-native'
import { useState } from 'react'
import { api } from '../config'

const STORE_COLORS = {
  bigbasket: { bg: '#f0fdf4', text: '#15803d', label: 'BigBasket' },
  zepto:     { bg: '#eff6ff', text: '#1d4ed8', label: 'Zepto' },
  blinkit:   { bg: '#fffbeb', text: '#b45309', label: 'Blinkit' },
}

function StoreBadge({ store, small = false }) {
  const c = STORE_COLORS[store] || { bg: '#f1f5f9', text: '#475569', label: store }
  return (
    <View style={[styles.badge, { backgroundColor: c.bg }, small && styles.badgeSmall]}>
      <Text style={[styles.badgeText, { color: c.text }, small && styles.badgeTextSmall]}>
        {c.label}
      </Text>
    </View>
  )
}

export default function ResultsScreen({ route, navigation }) {
  const { compareData, userId } = route.params
  const { comparison = [], summary = {} } = compareData

  // Local state for user-swapped selections.
  // Key: comparison index, Value: {cheapest_app, selected_pid}
  const [selections, setSelections] = useState({})
  const [expanded, setExpanded] = useState({})
  const [addingToCart, setAddingToCart] = useState(false)
  const [cartProgress, setCartProgress] = useState({ done: 0, total: 0, current: '' })
  const [cartResults, setCartResults] = useState(null)

  function getSelection(i) {
    return selections[i] || {
      cheapest_app: comparison[i]?.cheapest_app,
      selected_pid: comparison[i]?.selected_pid,
    }
  }

  function getSelectedProduct(i) {
    const sel = getSelection(i)
    if (!sel.cheapest_app) return null
    const prods = (comparison[i]?.prices || {})[sel.cheapest_app] || []
    if (sel.selected_pid) {
      const found = prods.find(p => p.product_id === sel.selected_pid)
      if (found) return { ...found, _store: sel.cheapest_app }
    }
    return prods[0] ? { ...prods[0], _store: sel.cheapest_app } : null
  }

  // ── Build carts from current selections ─────────────────────
  function buildCarts() {
    const carts = {}
    comparison.forEach((entry, i) => {
      const sel = getSelection(i)
      if (!sel.cheapest_app) return
      const prod = getSelectedProduct(i)
      if (!prod) return
      if (!carts[sel.cheapest_app]) carts[sel.cheapest_app] = { items: [], total: 0 }
      const count = entry.item?.count || 1
      const price = prod.sale_price || prod.price || 0
      carts[sel.cheapest_app].items.push({
        ...entry.item,
        product_id: prod.product_id,
        search_query: entry.search_query,
        matched_name: prod.name,
        matched_unit: prod.unit,
        price,
        fc_id: prod.fc_id,
        store_product_id: prod.store_product_id,
        mrp_paise: prod.mrp_paise,
        discount_applicable: prod.discount_applicable,
        merchant_id: prod.merchant_id,
        _cart_item: prod._cart_item,
      })
      carts[sel.cheapest_app].total += price * count
    })
    return carts
  }

  // ── Add all to carts ─────────────────────────────────────────
  async function addAllToCarts() {
    const carts = buildCarts()
    if (!Object.keys(carts).length) {
      Alert.alert('Nothing to add', 'No items were matched to any store.')
      return
    }

    setAddingToCart(true)
    const totalItems = Object.values(carts).reduce((s, c) => s + c.items.length, 0)
    setCartProgress({ done: 0, total: totalItems, current: 'Starting…' })

    // Poll progress while the request runs.
    const poller = setInterval(async () => {
      try {
        const p = await api('/api/cart/progress', {}, userId)
        if (p.total) setCartProgress(p)
      } catch {}
    }, 800)

    try {
      const data = await api(
        '/api/cart/add-all',
        { method: 'POST', body: JSON.stringify({ carts }) },
        userId
      )
      clearInterval(poller)
      setCartResults(data.results || {})

      let added = 0, failed = 0
      Object.values(data.results || {}).forEach(r => {
        added += (r.added || []).length
        failed += (r.failed || []).length
      })
      Alert.alert(
        'Done!',
        `${added} item${added !== 1 ? 's' : ''} added to cart${failed ? `, ${failed} failed` : ''}.`
      )
    } catch (e) {
      clearInterval(poller)
      Alert.alert('Error', 'Could not add items to cart.')
    } finally {
      setAddingToCart(false)
    }
  }

  const carts = buildCarts()
  const grandTotal = Object.values(carts).reduce((s, c) => s + c.total, 0)

  return (
    <View style={{ flex: 1 }}>
      <ScrollView style={styles.scroll} contentContainerStyle={styles.container}>

        {/* Summary */}
        <View style={styles.summaryRow}>
          <Text style={styles.summaryText}>
            Found {summary.items_found || 0}/{summary.total_items || 0} items
            {summary.apps_used ? ` across ${summary.apps_used} store${summary.apps_used > 1 ? 's' : ''}` : ''}
          </Text>
          <Text style={styles.totalText}>₹{grandTotal.toFixed(0)}</Text>
        </View>

        {/* Comparison list */}
        <Text style={styles.sectionTitle}>Best prices</Text>
        {comparison.map((entry, i) => {
          const sel = getSelection(i)
          const prod = getSelectedProduct(i)
          const price = prod ? (prod.sale_price || prod.price || 0) : null
          const isExpanded = expanded[i]

          return (
            <View key={i} style={styles.itemCard}>
              {/* Main row */}
              <TouchableOpacity
                style={styles.itemMainRow}
                onPress={() => setExpanded(e => ({ ...e, [i]: !e[i] }))}
              >
                <View style={{ flex: 1 }}>
                  <Text style={styles.itemName}>
                    {entry.item?.name}{entry.item?.qty ? ` · ${entry.item.qty}` : ''}
                  </Text>
                  {prod ? (
                    <Text style={styles.itemProduct} numberOfLines={1}>
                      {prod.name} {prod.unit ? `· ${prod.unit}` : ''}
                    </Text>
                  ) : (
                    <Text style={styles.itemNotFound}>Not found</Text>
                  )}
                </View>
                <View style={{ alignItems: 'flex-end', gap: 4 }}>
                  {sel.cheapest_app && <StoreBadge store={sel.cheapest_app} small />}
                  {price != null ? (
                    <Text style={styles.itemPrice}>₹{price.toFixed(0)}</Text>
                  ) : null}
                  <Text style={styles.chevron}>{isExpanded ? '▲' : '▼'}</Text>
                </View>
              </TouchableOpacity>

              {/* Expanded: all store options */}
              {isExpanded && (
                <View style={styles.expandedSection}>
                  {Object.entries(entry.prices || {}).map(([store, products]) => {
                    if (!products?.length) return null
                    const isSelectedStore = store === sel.cheapest_app
                    return (
                      <View key={store}>
                        <View style={styles.storeHeader}>
                          <StoreBadge store={store} />
                          <Text style={styles.storeHeaderLabel}></Text>
                        </View>
                        {products.slice(0, 3).map((p, pi) => {
                          const isSelected = isSelectedStore && p.product_id === sel.selected_pid
                          const pPrice = p.sale_price || p.price || 0
                          return (
                            <TouchableOpacity
                              key={pi}
                              style={[styles.productOption, isSelected && styles.productOptionSelected]}
                              onPress={() => {
                                setSelections(s => ({
                                  ...s,
                                  [i]: { cheapest_app: store, selected_pid: p.product_id },
                                }))
                                setExpanded(e => ({ ...e, [i]: false }))
                              }}
                            >
                              <View style={{ flex: 1 }}>
                                <Text style={styles.optionName} numberOfLines={2}>{p.name}</Text>
                                <Text style={styles.optionUnit}>{p.unit || ''}</Text>
                              </View>
                              <Text style={styles.optionPrice}>₹{pPrice.toFixed(0)}</Text>
                              {isSelected && <Text style={styles.selectedMark}>✓</Text>}
                            </TouchableOpacity>
                          )
                        })}
                      </View>
                    )
                  })}
                </View>
              )}
            </View>
          )
        })}

        {/* Cart summary */}
        {Object.keys(carts).length > 0 && (
          <>
            <Text style={[styles.sectionTitle, { marginTop: 8 }]}>Cart breakdown</Text>
            {Object.entries(carts).map(([store, cart]) => (
              <View key={store} style={styles.cartBlock}>
                <View style={styles.cartHeader}>
                  <StoreBadge store={store} />
                  <Text style={styles.cartTotal}>₹{cart.total.toFixed(0)}</Text>
                </View>
                {cart.items.map((item, i) => (
                  <View key={i} style={styles.cartItemRow}>
                    <Text style={styles.cartItemName} numberOfLines={1}>
                      {item.matched_name || item.name}
                      {item.matched_unit ? ` · ${item.matched_unit}` : ''}
                    </Text>
                    <Text style={styles.cartItemPrice}>₹{(item.price || 0).toFixed(0)}</Text>
                  </View>
                ))}
              </View>
            ))}
          </>
        )}

        {/* Cart add progress */}
        {addingToCart && cartProgress.total > 0 && (
          <View style={styles.progressBlock}>
            <Text style={styles.progressText}>{cartProgress.current || 'Adding…'}</Text>
            <View style={styles.progressBar}>
              <View
                style={[
                  styles.progressFill,
                  { width: `${Math.round((cartProgress.done / cartProgress.total) * 100)}%` },
                ]}
              />
            </View>
            <Text style={styles.progressCount}>{cartProgress.done} / {cartProgress.total}</Text>
          </View>
        )}

        {/* Cart results summary */}
        {cartResults && (
          <View style={styles.cartResultBlock}>
            {Object.entries(cartResults).map(([store, r]) => (
              <Text key={store} style={styles.cartResultText}>
                {STORE_COLORS[store]?.label || store}: {(r.added || []).length} added
                {(r.failed || []).length ? `, ${r.failed.length} failed` : ''}
              </Text>
            ))}
          </View>
        )}

      </ScrollView>

      {/* Add-to-cart sticky button */}
      <View style={styles.stickyBottom}>
        <TouchableOpacity
          style={[styles.addBtn, addingToCart && styles.addBtnDisabled]}
          onPress={addAllToCarts}
          disabled={addingToCart}
        >
          {addingToCart
            ? <ActivityIndicator color="#fff" />
            : <Text style={styles.addBtnText}>
                Add all to cart · ₹{grandTotal.toFixed(0)}
              </Text>
          }
        </TouchableOpacity>
      </View>
    </View>
  )
}

const styles = StyleSheet.create({
  scroll: { flex: 1, backgroundColor: '#fff' },
  container: { padding: 16, paddingBottom: 100 },
  summaryRow: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 },
  summaryText: { fontSize: 13, color: '#555' },
  totalText: { fontSize: 18, fontWeight: '700', color: '#1a1a1a' },
  sectionTitle: { fontSize: 15, fontWeight: '700', color: '#1a1a1a', marginBottom: 10 },
  itemCard: {
    borderWidth: 1, borderColor: '#e2e8f0', borderRadius: 10, marginBottom: 10,
    overflow: 'hidden',
  },
  itemMainRow: { flexDirection: 'row', padding: 12, alignItems: 'center' },
  itemName: { fontSize: 14, fontWeight: '600', color: '#1a1a1a', marginBottom: 3 },
  itemProduct: { fontSize: 12, color: '#888' },
  itemNotFound: { fontSize: 12, color: '#ef4444' },
  itemPrice: { fontSize: 15, fontWeight: '700', color: '#1a1a1a' },
  chevron: { fontSize: 11, color: '#aaa' },
  expandedSection: { borderTopWidth: 1, borderTopColor: '#f1f5f9', backgroundColor: '#fafafa', padding: 10 },
  storeHeader: { flexDirection: 'row', alignItems: 'center', marginBottom: 6, marginTop: 6 },
  storeHeaderLabel: { fontSize: 12, color: '#888', marginLeft: 6 },
  productOption: {
    flexDirection: 'row', alignItems: 'center', padding: 10,
    backgroundColor: '#fff', borderRadius: 8, marginBottom: 6,
    borderWidth: 1, borderColor: '#e2e8f0',
  },
  productOptionSelected: { borderColor: '#2563eb', backgroundColor: '#eff6ff' },
  optionName: { fontSize: 13, color: '#1a1a1a', fontWeight: '500' },
  optionUnit: { fontSize: 11, color: '#888', marginTop: 1 },
  optionPrice: { fontSize: 13, fontWeight: '700', color: '#1a1a1a', marginLeft: 8 },
  selectedMark: { fontSize: 14, color: '#2563eb', marginLeft: 6 },
  badge: { paddingHorizontal: 8, paddingVertical: 3, borderRadius: 99 },
  badgeSmall: { paddingHorizontal: 6, paddingVertical: 2 },
  badgeText: { fontSize: 12, fontWeight: '600' },
  badgeTextSmall: { fontSize: 11 },
  cartBlock: {
    borderWidth: 1, borderColor: '#e2e8f0', borderRadius: 10, padding: 12, marginBottom: 10,
  },
  cartHeader: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 },
  cartTotal: { fontSize: 15, fontWeight: '700' },
  cartItemRow: { flexDirection: 'row', justifyContent: 'space-between', paddingVertical: 4 },
  cartItemName: { flex: 1, fontSize: 13, color: '#555' },
  cartItemPrice: { fontSize: 13, fontWeight: '600', color: '#1a1a1a', marginLeft: 8 },
  progressBlock: { backgroundColor: '#f8fafc', borderRadius: 10, padding: 14, marginTop: 8 },
  progressText: { fontSize: 13, color: '#555', marginBottom: 8 },
  progressBar: { height: 6, backgroundColor: '#e2e8f0', borderRadius: 99, overflow: 'hidden', marginBottom: 6 },
  progressFill: { height: '100%', backgroundColor: '#16a34a', borderRadius: 99 },
  progressCount: { fontSize: 12, color: '#888', textAlign: 'right' },
  cartResultBlock: { backgroundColor: '#f0fdf4', borderRadius: 10, padding: 12, marginTop: 8 },
  cartResultText: { fontSize: 13, color: '#166534', marginBottom: 3 },
  stickyBottom: {
    position: 'absolute', bottom: 0, left: 0, right: 0,
    padding: 16, backgroundColor: '#fff',
    borderTopWidth: 1, borderTopColor: '#e2e8f0',
  },
  addBtn: { backgroundColor: '#16a34a', padding: 16, borderRadius: 12, alignItems: 'center' },
  addBtnDisabled: { opacity: 0.6 },
  addBtnText: { color: '#fff', fontSize: 15, fontWeight: '700' },
})
