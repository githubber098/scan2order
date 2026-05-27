/**
 * ConnectScreen.js - Store connection hub.
 *
 * Shows all three stores with connect/disconnect buttons.
 * Navigates to individual WebView login screens per store.
 * Also handles Blinkit location (GPS, no login required for search).
 *
 * Initial route if nothing is connected; also reachable from HomeScreen
 * via "Manage Stores".
 */

import {
  View, Text, StyleSheet, TouchableOpacity, ScrollView,
  ActivityIndicator, Alert,
} from 'react-native'
import { useState, useEffect, useCallback } from 'react'
import AsyncStorage from '@react-native-async-storage/async-storage'
import { useFocusEffect } from '@react-navigation/native'
import { api, BACKEND_URL } from '../config'

const STORES = [
  {
    key: 'bigbasket',
    label: 'BigBasket',
    emoji: '🛒',
    color: '#16a34a',
    bg: '#f0fdf4',
    description: 'Groceries, fresh produce & more',
  },
  {
    key: 'zepto',
    label: 'Zepto',
    emoji: '⚡',
    color: '#2563eb',
    bg: '#eff6ff',
    description: '10-minute delivery',
  },
  {
    key: 'blinkit',
    label: 'Blinkit',
    emoji: '🟡',
    color: '#d97706',
    bg: '#fffbeb',
    description: 'Fast delivery — needs your location',
  },
]

export default function ConnectScreen({ navigation }) {
  const [userId, setUserId] = useState(null)
  const [storeStatus, setStoreStatus] = useState({})
  const [loading, setLoading] = useState(true)

  // Generate or load a stable user_id on first run.
  useEffect(() => {
    async function initUser() {
      let id = await AsyncStorage.getItem('scan2order_user_id')
      if (!id) {
        id = 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
          const r = (Math.random() * 16) | 0
          return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16)
        })
        await AsyncStorage.setItem('scan2order_user_id', id)
      }
      setUserId(id)
    }
    initUser()
  }, [])

  // Reload status whenever this screen gains focus (after returning from a
  // connect screen).
  useFocusEffect(
    useCallback(() => {
      if (userId) loadStoreStatus(userId)
    }, [userId])
  )

  async function loadStoreStatus(uid) {
    setLoading(true)
    try {
      const data = await api('/api/stores', {}, uid)
      setStoreStatus(data.stores || {})
    } catch {
      // Offline or backend not ready - show disconnected for all.
    } finally {
      setLoading(false)
    }
  }

  async function handleDisconnect(store) {
    Alert.alert(
      `Disconnect ${store}?`,
      'You can reconnect at any time.',
      [
        { text: 'Cancel', style: 'cancel' },
        {
          text: 'Disconnect',
          style: 'destructive',
          onPress: async () => {
            await api(`/api/disconnect/${store}`, { method: 'POST' }, userId)
            loadStoreStatus(userId)
          },
        },
      ]
    )
  }

  function navigateTo(store) {
    switch (store) {
      case 'bigbasket': navigation.navigate('ConnectBigBasket', { userId }); break
      case 'zepto':     navigation.navigate('ConnectZepto',     { userId }); break
      case 'blinkit':   navigation.navigate('ConnectBlinkit',   { userId }); break
    }
  }

  const anyConnected = Object.values(storeStatus).some(s => s?.connected)

  return (
    <ScrollView style={styles.scroll} contentContainerStyle={styles.container}>
      <Text style={styles.title}>scan2order</Text>
      <Text style={styles.subtitle}>Connect your grocery apps to get started</Text>

      {loading && <ActivityIndicator color="#2563eb" style={{ marginBottom: 16 }} />}

      {STORES.map(store => {
        const info = storeStatus[store.key] || {}
        const connected = info.connected
        const needsLocation = store.key === 'blinkit' && connected && !info.location_set

        return (
          <View key={store.key} style={[styles.card, { borderColor: connected ? store.color : '#e2e8f0' }]}>
            <View style={styles.cardLeft}>
              <Text style={styles.cardEmoji}>{store.emoji}</Text>
              <View>
                <Text style={styles.cardLabel}>{store.label}</Text>
                <Text style={styles.cardDesc}>{store.description}</Text>
                {needsLocation && (
                  <Text style={styles.locationHint}>⚠️ Tap to set location</Text>
                )}
              </View>
            </View>

            <View style={styles.cardRight}>
              {connected ? (
                <>
                  <View style={[styles.pill, { backgroundColor: store.bg }]}>
                    <Text style={[styles.pillText, { color: store.color }]}>Connected</Text>
                  </View>
                  <TouchableOpacity
                    style={styles.manageBtn}
                    onPress={() => needsLocation ? navigateTo(store.key) : handleDisconnect(store.key)}
                  >
                    <Text style={styles.manageBtnText}>{needsLocation ? 'Set location' : 'Disconnect'}</Text>
                  </TouchableOpacity>
                </>
              ) : (
                <TouchableOpacity
                  style={[styles.connectBtn, { backgroundColor: store.color }]}
                  onPress={() => navigateTo(store.key)}
                >
                  <Text style={styles.connectBtnText}>Connect</Text>
                </TouchableOpacity>
              )}
            </View>
          </View>
        )
      })}

      {anyConnected && (
        <TouchableOpacity
          style={styles.doneBtn}
          onPress={() => navigation.replace('Home')}
        >
          <Text style={styles.doneBtnText}>Continue →</Text>
        </TouchableOpacity>
      )}

      <Text style={styles.hint}>
        Connect at least one store. You can add more later from the home screen.
      </Text>
    </ScrollView>
  )
}

const styles = StyleSheet.create({
  scroll: { backgroundColor: '#fff' },
  container: { padding: 24, paddingBottom: 48 },
  title: { fontSize: 30, fontWeight: '700', marginBottom: 6, textAlign: 'center' },
  subtitle: { fontSize: 15, color: '#555', textAlign: 'center', marginBottom: 28 },
  card: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    borderWidth: 1.5,
    borderRadius: 14,
    padding: 16,
    marginBottom: 14,
    backgroundColor: '#fff',
  },
  cardLeft: { flexDirection: 'row', alignItems: 'center', gap: 12, flex: 1 },
  cardEmoji: { fontSize: 28 },
  cardLabel: { fontSize: 16, fontWeight: '600', color: '#1a1a1a' },
  cardDesc: { fontSize: 13, color: '#888', marginTop: 2 },
  locationHint: { fontSize: 12, color: '#d97706', marginTop: 4 },
  cardRight: { alignItems: 'flex-end', gap: 6 },
  pill: {
    paddingHorizontal: 10,
    paddingVertical: 3,
    borderRadius: 99,
  },
  pillText: { fontSize: 12, fontWeight: '600' },
  connectBtn: {
    paddingHorizontal: 16,
    paddingVertical: 8,
    borderRadius: 8,
  },
  connectBtnText: { color: '#fff', fontWeight: '600', fontSize: 14 },
  manageBtn: { paddingHorizontal: 8, paddingVertical: 4 },
  manageBtnText: { fontSize: 12, color: '#888' },
  doneBtn: {
    backgroundColor: '#1a1a1a',
    paddingVertical: 14,
    borderRadius: 12,
    alignItems: 'center',
    marginTop: 8,
    marginBottom: 16,
  },
  doneBtnText: { color: '#fff', fontSize: 16, fontWeight: '600' },
  hint: { fontSize: 13, color: '#aaa', textAlign: 'center' },
})
