/**
 * HomeScreen.js
 *
 * Main screen after stores are connected.
 *
 * Flow:
 *   1. Take/pick a photo of your grocery list
 *   2. Tap "Scan & Compare" → OCR → items list appears for review
 *   3. Optionally edit items, then tap "Compare prices"
 *   4. Navigates to ResultsScreen with comparison data
 *
 * Also:
 *   - "Share Code" → generates a link code for the web UI
 *   - "Manage Stores" → back to ConnectScreen
 */

import {
  View, Text, StyleSheet, TouchableOpacity, Image, ScrollView,
  Alert, TextInput, ActivityIndicator, Modal,
} from 'react-native'
import { useState, useEffect } from 'react'
import * as ImagePicker from 'expo-image-picker'
import AsyncStorage from '@react-native-async-storage/async-storage'
import { api, BACKEND_URL, apiHeaders } from '../config'

export default function HomeScreen({ navigation }) {
  const [userId, setUserId] = useState(null)
  const [image, setImage] = useState(null)
  const [items, setItems] = useState([])        // [{name, qty, count}]
  const [ocrLoading, setOcrLoading] = useState(false)
  const [compareLoading, setCompareLoading] = useState(false)
  const [compareStep, setCompareStep] = useState('')
  const [linkCode, setLinkCode] = useState(null)
  const [linkModalVisible, setLinkModalVisible] = useState(false)
  const [connectedStores, setConnectedStores] = useState([])

  useEffect(() => {
    AsyncStorage.getItem('scan2order_user_id').then(id => {
      setUserId(id)
      if (id) loadStores(id)
    })
  }, [])

  async function loadStores(uid) {
    try {
      const data = await api('/api/stores', {}, uid)
      const stores = data.stores || {}
      setConnectedStores(
        Object.entries(stores)
          .filter(([, info]) => info.connected)
          .map(([k]) => k)
      )
    } catch {}
  }

  // ── Photo capture / pick ─────────────────────────────────────

  async function takePhoto() {
    const { status } = await ImagePicker.requestCameraPermissionsAsync()
    if (status !== 'granted') {
      Alert.alert('Permission needed', 'Camera access is required to scan grocery lists.')
      return
    }
    const result = await ImagePicker.launchCameraAsync({
      mediaTypes: ['images'],
      quality: 0.85,
    })
    if (!result.canceled) {
      setImage(result.assets[0])
      setItems([])  // clear previous OCR results
    }
  }

  async function pickImage() {
    const result = await ImagePicker.launchImageLibraryAsync({
      mediaTypes: ['images'],
      quality: 0.85,
    })
    if (!result.canceled) {
      setImage(result.assets[0])
      setItems([])
    }
  }

  // ── OCR ──────────────────────────────────────────────────────

  async function runOcr() {
    if (!image || !userId) return
    setOcrLoading(true)
    setItems([])
    try {
      const formData = new FormData()
      formData.append('image', {
        uri: image.uri,
        type: 'image/jpeg',
        name: 'grocery.jpg',
      })
      const res = await fetch(`${BACKEND_URL}/api/ocr`, {
        method: 'POST',
        headers: { 'X-User-ID': userId },
        body: formData,
      })
      const data = await res.json()
      if (data.error && !data.items?.length) {
        Alert.alert('OCR error', data.error)
        return
      }
      const parsed = (data.items || []).map(name => ({
        name: name.replace(/\s+\d+$/, '').trim(),  // strip trailing numbers
        qty: '',
        count: '1',
      }))
      setItems(parsed.length ? parsed : [{ name: '', qty: '', count: '1' }])
    } catch (e) {
      Alert.alert('Error', 'Could not reach server.')
    } finally {
      setOcrLoading(false)
    }
  }

  // ── Items management ─────────────────────────────────────────

  function updateItem(i, field, value) {
    setItems(prev => prev.map((it, idx) => idx === i ? { ...it, [field]: value } : it))
  }

  function addItem() {
    setItems(prev => [...prev, { name: '', qty: '', count: '1' }])
  }

  function removeItem(i) {
    setItems(prev => prev.filter((_, idx) => idx !== i))
  }

  // ── Compare ──────────────────────────────────────────────────

  async function startCompare() {
    const validItems = items.filter(it => it.name.trim())
    if (!validItems.length) {
      Alert.alert('Add items', 'Enter at least one item to compare.')
      return
    }
    if (!connectedStores.length) {
      Alert.alert('No stores connected', 'Connect at least one store first.')
      navigation.navigate('Connect')
      return
    }

    setCompareLoading(true)
    setCompareStep(`Searching ${connectedStores.length} store(s)…`)

    try {
      const itemsPayload = validItems.map(it => ({
        name: it.name.trim(),
        qty: it.qty.trim(),
        count: parseInt(it.count) || 1,
      }))

      const data = await api(
        '/api/compare',
        {
          method: 'POST',
          body: JSON.stringify({ items: itemsPayload }),
        },
        userId
      )

      if (data.error) {
        Alert.alert('Compare failed', data.error)
        return
      }

      navigation.navigate('Results', { compareData: data, userId })
    } catch (e) {
      Alert.alert('Error', 'Could not reach server.')
    } finally {
      setCompareLoading(false)
      setCompareStep('')
    }
  }

  // ── Link code ────────────────────────────────────────────────

  async function generateLinkCode() {
    try {
      const data = await api('/api/link/generate', { method: 'POST' }, userId)
      if (data.code) {
        setLinkCode(data.code)
        setLinkModalVisible(true)
      }
    } catch {
      Alert.alert('Error', 'Could not generate link code.')
    }
  }

  // ── Render ───────────────────────────────────────────────────

  const storeLabels = { bigbasket: 'BB', zepto: 'Zepto', blinkit: 'Blinkit' }

  return (
    <View style={{ flex: 1 }}>
      <ScrollView style={styles.scroll} contentContainerStyle={styles.container} keyboardShouldPersistTaps="handled">

        {/* Header row */}
        <View style={styles.headerRow}>
          <Text style={styles.title}>scan2order</Text>
          <TouchableOpacity onPress={generateLinkCode} style={styles.codeBtn}>
            <Text style={styles.codeBtnText}>Share Code</Text>
          </TouchableOpacity>
        </View>

        {/* Connected stores badges */}
        {connectedStores.length > 0 && (
          <View style={styles.storeRow}>
            {connectedStores.map(s => (
              <View key={s} style={styles.storeBadge}>
                <Text style={styles.storeBadgeText}>{storeLabels[s] || s}</Text>
              </View>
            ))}
            <TouchableOpacity onPress={() => navigation.navigate('Connect')}>
              <Text style={styles.manageLink}>Manage stores</Text>
            </TouchableOpacity>
          </View>
        )}

        {/* Photo area */}
        <View style={styles.photoSection}>
          {image ? (
            <Image source={{ uri: image.uri }} style={styles.preview} />
          ) : (
            <View style={styles.photoPlaceholder}>
              <Text style={styles.photoPlaceholderText}>📷 No photo yet</Text>
            </View>
          )}
          <View style={styles.photoButtons}>
            <TouchableOpacity style={styles.photoBtn} onPress={takePhoto}>
              <Text style={styles.photoBtnText}>📷 Camera</Text>
            </TouchableOpacity>
            <TouchableOpacity style={[styles.photoBtn, styles.photoBtnSecondary]} onPress={pickImage}>
              <Text style={styles.photoBtnSecondaryText}>🖼 Library</Text>
            </TouchableOpacity>
          </View>

          {image && (
            <TouchableOpacity
              style={[styles.scanBtn, ocrLoading && styles.scanBtnDisabled]}
              onPress={runOcr}
              disabled={ocrLoading}
            >
              {ocrLoading
                ? <><ActivityIndicator color="#fff" size="small" /><Text style={styles.scanBtnText}> Reading list…</Text></>
                : <Text style={styles.scanBtnText}>🔍 Read list</Text>
              }
            </TouchableOpacity>
          )}
        </View>

        {/* Items list */}
        {(items.length > 0 || true) && (
          <View style={styles.itemsSection}>
            <View style={styles.itemsHeader}>
              <Text style={styles.itemsTitle}>Items to compare</Text>
              <TouchableOpacity onPress={addItem}>
                <Text style={styles.addItemLink}>+ Add item</Text>
              </TouchableOpacity>
            </View>

            {items.map((item, i) => (
              <View key={i} style={styles.itemRow}>
                <TextInput
                  style={styles.itemNameInput}
                  value={item.name}
                  onChangeText={v => updateItem(i, 'name', v)}
                  placeholder="Item name"
                  placeholderTextColor="#aaa"
                />
                <TextInput
                  style={styles.itemQtyInput}
                  value={item.qty}
                  onChangeText={v => updateItem(i, 'qty', v)}
                  placeholder="500g"
                  placeholderTextColor="#aaa"
                />
                <TextInput
                  style={styles.itemCountInput}
                  value={String(item.count)}
                  onChangeText={v => updateItem(i, 'count', v)}
                  keyboardType="numeric"
                  placeholder="1"
                  placeholderTextColor="#aaa"
                />
                <TouchableOpacity onPress={() => removeItem(i)} style={styles.removeBtn}>
                  <Text style={styles.removeBtnText}>✕</Text>
                </TouchableOpacity>
              </View>
            ))}

            {items.length === 0 && (
              <Text style={styles.emptyHint}>
                Take a photo and tap "Read list", or add items manually above.
              </Text>
            )}
          </View>
        )}

        {/* Compare button */}
        {items.some(it => it.name.trim()) && (
          <TouchableOpacity
            style={[styles.compareBtn, compareLoading && styles.compareBtnDisabled]}
            onPress={startCompare}
            disabled={compareLoading}
          >
            {compareLoading ? (
              <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8 }}>
                <ActivityIndicator color="#fff" size="small" />
                <Text style={styles.compareBtnText}>{compareStep || 'Comparing…'}</Text>
              </View>
            ) : (
              <Text style={styles.compareBtnText}>
                Compare prices across {connectedStores.length || '?'} store{connectedStores.length !== 1 ? 's' : ''}
              </Text>
            )}
          </TouchableOpacity>
        )}

      </ScrollView>

      {/* Link code modal */}
      <Modal
        visible={linkModalVisible}
        transparent
        animationType="fade"
        onRequestClose={() => setLinkModalVisible(false)}
      >
        <View style={styles.modalOverlay}>
          <View style={styles.modalCard}>
            <Text style={styles.modalTitle}>Web link code</Text>
            <Text style={styles.modalSubtitle}>
              Enter this code at{'\n'}scan2order-backend.onrender.com
            </Text>
            <Text style={styles.codeDisplay}>{linkCode}</Text>
            <Text style={styles.codeExpiry}>Expires in 15 minutes</Text>
            <TouchableOpacity
              style={styles.modalCloseBtn}
              onPress={() => setLinkModalVisible(false)}
            >
              <Text style={styles.modalCloseBtnText}>Done</Text>
            </TouchableOpacity>
          </View>
        </View>
      </Modal>
    </View>
  )
}

const styles = StyleSheet.create({
  scroll: { backgroundColor: '#fff' },
  container: { padding: 20, paddingBottom: 60 },
  headerRow: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12, marginTop: 8 },
  title: { fontSize: 28, fontWeight: '700' },
  codeBtn: { backgroundColor: '#1a1a1a', paddingHorizontal: 14, paddingVertical: 7, borderRadius: 8 },
  codeBtnText: { color: '#fff', fontSize: 13, fontWeight: '600' },
  storeRow: { flexDirection: 'row', alignItems: 'center', gap: 6, marginBottom: 18, flexWrap: 'wrap' },
  storeBadge: { backgroundColor: '#f1f5f9', paddingHorizontal: 10, paddingVertical: 4, borderRadius: 99 },
  storeBadgeText: { fontSize: 12, fontWeight: '600', color: '#334155' },
  manageLink: { fontSize: 12, color: '#2563eb', marginLeft: 4 },
  photoSection: { marginBottom: 20 },
  preview: { width: '100%', height: 180, borderRadius: 12, marginBottom: 10, resizeMode: 'cover' },
  photoPlaceholder: {
    width: '100%', height: 120, borderRadius: 12, marginBottom: 10,
    backgroundColor: '#f8fafc', borderWidth: 1.5, borderColor: '#e2e8f0',
    borderStyle: 'dashed', justifyContent: 'center', alignItems: 'center',
  },
  photoPlaceholderText: { color: '#aaa', fontSize: 15 },
  photoButtons: { flexDirection: 'row', gap: 10, marginBottom: 10 },
  photoBtn: { flex: 1, backgroundColor: '#2563eb', padding: 12, borderRadius: 10, alignItems: 'center' },
  photoBtnText: { color: '#fff', fontWeight: '600' },
  photoBtnSecondary: { backgroundColor: '#fff', borderWidth: 1, borderColor: '#e2e8f0' },
  photoBtnSecondaryText: { color: '#1a1a1a', fontWeight: '600' },
  scanBtn: { backgroundColor: '#7c3aed', padding: 13, borderRadius: 10, alignItems: 'center', flexDirection: 'row', justifyContent: 'center', gap: 6 },
  scanBtnDisabled: { opacity: 0.6 },
  scanBtnText: { color: '#fff', fontWeight: '600', fontSize: 15 },
  itemsSection: { marginBottom: 20 },
  itemsHeader: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 },
  itemsTitle: { fontSize: 16, fontWeight: '600' },
  addItemLink: { fontSize: 14, color: '#2563eb' },
  itemRow: { flexDirection: 'row', alignItems: 'center', gap: 6, marginBottom: 8 },
  itemNameInput: { flex: 3, borderWidth: 1, borderColor: '#e2e8f0', borderRadius: 8, padding: 9, fontSize: 14, color: '#1a1a1a' },
  itemQtyInput: { flex: 1.5, borderWidth: 1, borderColor: '#e2e8f0', borderRadius: 8, padding: 9, fontSize: 14, color: '#1a1a1a' },
  itemCountInput: { width: 44, borderWidth: 1, borderColor: '#e2e8f0', borderRadius: 8, padding: 9, fontSize: 14, color: '#1a1a1a', textAlign: 'center' },
  removeBtn: { padding: 6 },
  removeBtnText: { color: '#ef4444', fontSize: 16 },
  emptyHint: { fontSize: 13, color: '#aaa', textAlign: 'center', paddingVertical: 16 },
  compareBtn: { backgroundColor: '#16a34a', padding: 16, borderRadius: 12, alignItems: 'center', marginTop: 4 },
  compareBtnDisabled: { opacity: 0.6 },
  compareBtnText: { color: '#fff', fontWeight: '700', fontSize: 15 },
  // Modal
  modalOverlay: { flex: 1, backgroundColor: 'rgba(0,0,0,0.6)', justifyContent: 'center', alignItems: 'center' },
  modalCard: { backgroundColor: '#fff', borderRadius: 16, padding: 28, alignItems: 'center', width: 300 },
  modalTitle: { fontSize: 18, fontWeight: '700', marginBottom: 6 },
  modalSubtitle: { fontSize: 13, color: '#555', textAlign: 'center', marginBottom: 20, lineHeight: 19 },
  codeDisplay: { fontSize: 36, fontWeight: '900', letterSpacing: 8, color: '#1a1a1a', marginBottom: 8 },
  codeExpiry: { fontSize: 12, color: '#aaa', marginBottom: 20 },
  modalCloseBtn: { backgroundColor: '#1a1a1a', paddingHorizontal: 28, paddingVertical: 12, borderRadius: 10 },
  modalCloseBtnText: { color: '#fff', fontWeight: '600' },
})
