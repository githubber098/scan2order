/**
 * ConnectBlinkitScreen.js
 *
 * Two-step connection for Blinkit:
 *
 * Step 1 - Location (required for search):
 *   Request device GPS permission via expo-location.
 *   POST lat/lon to /api/location.
 *   This is all Blinkit needs for unauthenticated search.
 *
 * Step 2 - Login (optional, for cart when endpoint is confirmed):
 *   Show Blinkit in a WebView.
 *   Detect gr_1_deviceId cookie (set after any page load — not just login).
 *   Extract all cookies → POST to /api/connect/blinkit.
 *
 * The user can skip Step 2 and still use Blinkit for price comparison.
 * Cart add will fail gracefully until the cart endpoint is verified.
 */

import {
  View, Text, StyleSheet, TouchableOpacity, ActivityIndicator, Alert,
} from 'react-native'
import { useState, useRef } from 'react'
import { WebView } from 'react-native-webview'
import CookieManager from '@react-native-cookies/cookies'
import * as Location from 'expo-location'
import { api } from '../config'

const BLINKIT_URL = 'https://blinkit.com/'

export default function ConnectBlinkitScreen({ route, navigation }) {
  const { userId } = route.params
  const [step, setStep] = useState('location') // 'location' | 'webview' | 'done'
  const [status, setStatus] = useState('')
  const [locConnected, setLocConnected] = useState(false)
  const [cookiesConnected, setCookiesConnected] = useState(false)
  const connectingRef = useRef(false)

  // ── Step 1: Get GPS location ─────────────────────────────────

  async function requestLocation() {
    setStatus('Requesting location permission…')
    const { status: permStatus } = await Location.requestForegroundPermissionsAsync()
    if (permStatus !== 'granted') {
      Alert.alert(
        'Location needed',
        'Blinkit requires your location for delivery availability. Please grant location access.',
        [{ text: 'OK' }]
      )
      setStatus('')
      return
    }

    setStatus('Getting your location…')
    try {
      const loc = await Location.getCurrentPositionAsync({
        accuracy: Location.Accuracy.Balanced,
      })
      const { latitude, longitude } = loc.coords
      setStatus('Saving location…')

      const data = await api(
        '/api/location',
        {
          method: 'POST',
          body: JSON.stringify({
            lat: String(latitude),
            lon: String(longitude),
          }),
        },
        userId
      )

      if (data.success) {
        setLocConnected(true)
        setStatus(`Location set (${latitude.toFixed(4)}, ${longitude.toFixed(4)})`)
      } else {
        setStatus('Location save failed — try again')
      }
    } catch (e) {
      console.log('[Blinkit location] error:', e.message)
      setStatus('Could not get location — check GPS and try again')
    }
  }

  // ── Step 2: WebView cookie extraction ────────────────────────

  async function handleLoadEnd() {
    if (connectingRef.current) return

    try {
      const allCookies = await CookieManager.getAll(true)
      // gr_1_deviceId is set on first page load (even without login).
      // We also need city for Blinkit's location-aware search.
      const deviceCookie = allCookies['gr_1_deviceId']
      if (!deviceCookie?.value) return

      connectingRef.current = true
      setStatus('Saving Blinkit session…')

      const cookieDict = {}
      Object.keys(allCookies).forEach(k => {
        if (allCookies[k]?.value) cookieDict[k] = allCookies[k].value
      })

      const data = await api(
        '/api/connect/blinkit',
        {
          method: 'POST',
          body: JSON.stringify({ cookies: cookieDict }),
        },
        userId
      )

      if (data.success) {
        setCookiesConnected(true)
        setStatus('Blinkit session saved! ✓')
      } else {
        setStatus('Session save failed')
        connectingRef.current = false
      }
    } catch (e) {
      console.log('[Blinkit WebView] error:', e.message)
      connectingRef.current = false
    }
  }

  // ── Render ───────────────────────────────────────────────────

  if (step === 'webview') {
    return (
      <View style={{ flex: 1 }}>
        {status ? (
          <View style={[styles.banner, cookiesConnected ? styles.bannerSuccess : styles.bannerInfo]}>
            <Text style={styles.bannerText}>{status}</Text>
          </View>
        ) : null}
        <View style={styles.instructionBar}>
          <Text style={styles.instructionText}>
            Browse Blinkit normally — we'll save your session
          </Text>
        </View>
        <WebView
          source={{ uri: BLINKIT_URL }}
          onLoadEnd={handleLoadEnd}
          style={{ flex: 1 }}
          sharedCookiesEnabled={true}
          thirdPartyCookiesEnabled={true}
        />
        <View style={styles.bottomRow}>
          <TouchableOpacity
            style={styles.doneWebViewBtn}
            onPress={() => {
              setStep('done')
              navigation.goBack()
            }}
          >
            <Text style={styles.doneWebViewText}>Done</Text>
          </TouchableOpacity>
        </View>
      </View>
    )
  }

  return (
    <View style={styles.container}>
      <Text style={styles.heading}>Connect Blinkit</Text>
      <Text style={styles.subheading}>
        Blinkit needs your location for delivery availability.{'\n'}
        Login is optional (needed for adding to cart).
      </Text>

      {/* Step 1: Location */}
      <View style={[styles.stepCard, locConnected && styles.stepCardDone]}>
        <View style={styles.stepHeader}>
          <Text style={styles.stepNum}>{locConnected ? '✓' : '1'}</Text>
          <Text style={styles.stepTitle}>Set your location</Text>
          {locConnected && (
            <View style={styles.pill}>
              <Text style={styles.pillText}>Done</Text>
            </View>
          )}
        </View>
        <Text style={styles.stepDesc}>
          We'll use your phone's GPS to find products available near you.
        </Text>
        {!locConnected ? (
          <TouchableOpacity style={styles.actionBtn} onPress={requestLocation}>
            <Text style={styles.actionBtnText}>📍 Use my location</Text>
          </TouchableOpacity>
        ) : (
          <Text style={styles.successText}>{status}</Text>
        )}
      </View>

      {/* Step 2: Login (optional) */}
      <View style={[styles.stepCard, cookiesConnected && styles.stepCardDone]}>
        <View style={styles.stepHeader}>
          <Text style={styles.stepNum}>{cookiesConnected ? '✓' : '2'}</Text>
          <Text style={styles.stepTitle}>Log in to Blinkit</Text>
          <View style={styles.pillOptional}>
            <Text style={styles.pillOptionalText}>Optional</Text>
          </View>
        </View>
        <Text style={styles.stepDesc}>
          Log in to enable adding items to your cart directly.{'\n'}
          Skip this to use Blinkit for price comparison only.
        </Text>
        {!cookiesConnected && (
          <TouchableOpacity
            style={[styles.actionBtn, styles.actionBtnSecondary]}
            onPress={() => setStep('webview')}
          >
            <Text style={styles.actionBtnSecondaryText}>Open Blinkit login →</Text>
          </TouchableOpacity>
        )}
        {cookiesConnected && (
          <Text style={styles.successText}>Session saved ✓</Text>
        )}
      </View>

      {locConnected && (
        <TouchableOpacity
          style={styles.continueBtn}
          onPress={() => navigation.goBack()}
        >
          <Text style={styles.continueBtnText}>
            {cookiesConnected ? 'All done →' : 'Continue with location only →'}
          </Text>
        </TouchableOpacity>
      )}
    </View>
  )
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    padding: 24,
    backgroundColor: '#fff',
  },
  heading: { fontSize: 26, fontWeight: '700', marginBottom: 8, marginTop: 8 },
  subheading: { fontSize: 14, color: '#666', marginBottom: 24, lineHeight: 20 },
  stepCard: {
    borderWidth: 1.5,
    borderColor: '#e2e8f0',
    borderRadius: 12,
    padding: 16,
    marginBottom: 16,
  },
  stepCardDone: { borderColor: '#16a34a', backgroundColor: '#f0fdf4' },
  stepHeader: { flexDirection: 'row', alignItems: 'center', gap: 10, marginBottom: 8 },
  stepNum: {
    width: 26, height: 26,
    borderRadius: 13,
    backgroundColor: '#e2e8f0',
    textAlign: 'center',
    lineHeight: 26,
    fontWeight: '700',
    fontSize: 13,
  },
  stepTitle: { fontSize: 15, fontWeight: '600', flex: 1 },
  pill: {
    backgroundColor: '#d1fae5',
    paddingHorizontal: 8,
    paddingVertical: 2,
    borderRadius: 99,
  },
  pillText: { color: '#065f46', fontSize: 11, fontWeight: '600' },
  pillOptional: {
    backgroundColor: '#f1f5f9',
    paddingHorizontal: 8,
    paddingVertical: 2,
    borderRadius: 99,
  },
  pillOptionalText: { color: '#64748b', fontSize: 11, fontWeight: '600' },
  stepDesc: { fontSize: 13, color: '#555', marginBottom: 12, lineHeight: 19 },
  actionBtn: {
    backgroundColor: '#d97706',
    paddingVertical: 10,
    paddingHorizontal: 16,
    borderRadius: 8,
    alignSelf: 'flex-start',
  },
  actionBtnText: { color: '#fff', fontWeight: '600', fontSize: 14 },
  actionBtnSecondary: {
    backgroundColor: '#fff',
    borderWidth: 1,
    borderColor: '#d97706',
  },
  actionBtnSecondaryText: { color: '#d97706', fontWeight: '600', fontSize: 14 },
  successText: { fontSize: 13, color: '#16a34a', fontWeight: '500' },
  continueBtn: {
    backgroundColor: '#1a1a1a',
    paddingVertical: 14,
    borderRadius: 12,
    alignItems: 'center',
    marginTop: 8,
  },
  continueBtnText: { color: '#fff', fontSize: 15, fontWeight: '600' },
  // WebView screen styles
  banner: {
    padding: 12,
    paddingHorizontal: 16,
  },
  bannerInfo: { backgroundColor: '#2563eb' },
  bannerSuccess: { backgroundColor: '#16a34a' },
  bannerText: { color: '#fff', fontWeight: '600', fontSize: 14 },
  instructionBar: {
    backgroundColor: '#f8fafc',
    padding: 10,
    borderBottomWidth: 1,
    borderBottomColor: '#e2e8f0',
  },
  instructionText: { fontSize: 13, color: '#555', textAlign: 'center' },
  bottomRow: {
    borderTopWidth: 1,
    borderTopColor: '#e2e8f0',
    backgroundColor: '#f8fafc',
  },
  doneWebViewBtn: { padding: 16, alignItems: 'center' },
  doneWebViewText: { fontSize: 15, color: '#555' },
})
