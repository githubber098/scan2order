/**
 * ConnectZeptoScreen.js
 *
 * Opens Zepto in a WebView. After the user logs in (enters phone + OTP),
 * we detect the accessToken cookie, then extract:
 *   1. All cookies via CookieManager
 *   2. All localStorage keys via injected JS + postMessage
 *
 * Both are POSTed to /api/connect/zepto.
 *
 * Login detection: `accessToken` cookie appears after OTP entry. It's
 * a long JWT (>100 chars). We also need localStorage because Zepto stores
 * device_id and session_id there (or in cookies with camelCase names).
 *
 * localStorage extraction: WebView doesn't expose localStorage directly.
 * We inject JS that reads all entries and posts them back via window.ReactNativeWebView.postMessage().
 */

import {
  View, Text, StyleSheet, TouchableOpacity, ActivityIndicator,
} from 'react-native'
import { useState, useRef } from 'react'
import { WebView } from 'react-native-webview'
import CookieManager from '@react-native-cookies/cookies'
import { api } from '../config'

const ZEPTO_URL = 'https://www.zeptonow.com/'

// Injected into the page after every load to extract localStorage.
// Posts a JSON string: {"type":"localStorage","data":{"key":"value",...}}
const EXTRACT_LS_JS = `
(function() {
  try {
    var data = {};
    for (var i = 0; i < localStorage.length; i++) {
      var k = localStorage.key(i);
      data[k] = localStorage.getItem(k) || '';
    }
    window.ReactNativeWebView.postMessage(
      JSON.stringify({ type: 'localStorage', data: data })
    );
  } catch(e) {}
})();
true; // required for Android
`

export default function ConnectZeptoScreen({ route, navigation }) {
  const { userId } = route.params
  const [status, setStatus] = useState('')
  const [connecting, setConnecting] = useState(false)
  const [done, setDone] = useState(false)
  const localStorageRef = useRef({})
  const attemptedRef = useRef(false)

  // Receive localStorage data sent by injected JS.
  function handleMessage(event) {
    try {
      const msg = JSON.parse(event.nativeEvent.data)
      if (msg.type === 'localStorage') {
        localStorageRef.current = msg.data || {}
      }
    } catch {}
  }

  async function handleLoadEnd() {
    if (attemptedRef.current) return

    try {
      const allCookies = await CookieManager.getAll(true)
      const accessToken = allCookies['accessToken'] || allCookies['accesstoken']

      // Require a real JWT (>100 chars), not an empty/placeholder value.
      if (!accessToken?.value || accessToken.value.length < 50) return

      attemptedRef.current = true
      setConnecting(true)
      setStatus('Logged in! Saving session…')

      const cookieDict = {}
      Object.keys(allCookies).forEach(k => {
        if (allCookies[k]?.value) cookieDict[k] = allCookies[k].value
      })

      const data = await api(
        '/api/connect/zepto',
        {
          method: 'POST',
          body: JSON.stringify({
            cookies: cookieDict,
            local_storage: localStorageRef.current,
          }),
        },
        userId
      )

      if (data.success) {
        setStatus('Zepto connected! ✓')
        setDone(true)
        setTimeout(() => navigation.goBack(), 1000)
      } else {
        setStatus('Connection failed — try again')
        setConnecting(false)
        attemptedRef.current = false
      }
    } catch (e) {
      console.log('[Zepto connect] error:', e.message)
      setConnecting(false)
      attemptedRef.current = false
    }
  }

  return (
    <View style={{ flex: 1 }}>
      {(connecting || done) && (
        <View style={[styles.banner, done ? styles.bannerSuccess : styles.bannerInfo]}>
          {connecting && !done && (
            <ActivityIndicator color="#fff" size="small" style={{ marginRight: 8 }} />
          )}
          <Text style={styles.bannerText}>{status}</Text>
        </View>
      )}

      {!done && (
        <>
          <View style={styles.instructionBar}>
            <Text style={styles.instructionText}>
              Log in to Zepto — enter your phone number and OTP
            </Text>
          </View>

          <WebView
            source={{ uri: ZEPTO_URL }}
            onLoadEnd={handleLoadEnd}
            onMessage={handleMessage}
            injectedJavaScript={EXTRACT_LS_JS}
            style={{ flex: 1 }}
            sharedCookiesEnabled={true}
            thirdPartyCookiesEnabled={true}
          />

          <TouchableOpacity
            style={styles.cancelBtn}
            onPress={() => navigation.goBack()}
          >
            <Text style={styles.cancelText}>Cancel</Text>
          </TouchableOpacity>
        </>
      )}
    </View>
  )
}

const styles = StyleSheet.create({
  banner: {
    flexDirection: 'row',
    alignItems: 'center',
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
  instructionText: {
    fontSize: 13,
    color: '#555',
    textAlign: 'center',
  },
  cancelBtn: {
    padding: 16,
    alignItems: 'center',
    backgroundColor: '#f8fafc',
    borderTopWidth: 1,
    borderTopColor: '#e2e8f0',
  },
  cancelText: { color: '#555', fontSize: 15 },
})
