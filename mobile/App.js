/**
 * App.js - Root navigation.
 *
 * Initial route: ConnectScreen if no stores are connected,
 * HomeScreen if at least one store is already connected.
 *
 * Screens:
 *   Connect             - Multi-store hub (connect / manage stores)
 *   ConnectBigBasket    - WebView login for BigBasket
 *   ConnectZepto        - WebView login for Zepto
 *   ConnectBlinkit      - GPS location + optional WebView for Blinkit
 *   Home                - Scan photo, edit items, compare
 *   Results             - Multi-store comparison + add-to-cart
 */

import { NavigationContainer } from '@react-navigation/native'
import { createNativeStackNavigator } from '@react-navigation/native-stack'
import { useEffect, useState } from 'react'
import { View, ActivityIndicator } from 'react-native'
import AsyncStorage from '@react-native-async-storage/async-storage'
import { api } from './config'

import ConnectScreen          from './screens/ConnectScreen'
import ConnectBigBasketScreen from './screens/ConnectBigBasketScreen'
import ConnectZeptoScreen     from './screens/ConnectZeptoScreen'
import ConnectBlinkitScreen   from './screens/ConnectBlinkitScreen'
import HomeScreen             from './screens/HomeScreen'
import ResultsScreen          from './screens/ResultsScreen'

const Stack = createNativeStackNavigator()

export default function App() {
  const [initialRoute, setInitialRoute] = useState(null)

  useEffect(() => {
    async function checkConnection() {
      try {
        const userId = await AsyncStorage.getItem('scan2order_user_id')
        if (!userId) {
          setInitialRoute('Connect')
          return
        }
        // Check Redis for any connected store.
        const data = await api('/api/stores', {}, userId)
        const stores = data.stores || {}
        const anyConnected = Object.values(stores).some(s => s?.connected)
        setInitialRoute(anyConnected ? 'Home' : 'Connect')
      } catch {
        // Offline or first launch - go to Connect.
        setInitialRoute('Connect')
      }
    }
    checkConnection()
  }, [])

  if (!initialRoute) {
    return (
      <View style={{ flex: 1, justifyContent: 'center', alignItems: 'center', backgroundColor: '#fff' }}>
        <ActivityIndicator size="large" color="#16a34a" />
      </View>
    )
  }

  return (
    <NavigationContainer>
      <Stack.Navigator initialRouteName={initialRoute}>
        <Stack.Screen
          name="Connect"
          component={ConnectScreen}
          options={{ title: 'Connect stores', headerBackVisible: false }}
        />
        <Stack.Screen
          name="ConnectBigBasket"
          component={ConnectBigBasketScreen}
          options={{ title: 'BigBasket login', headerBackTitle: 'Back' }}
        />
        <Stack.Screen
          name="ConnectZepto"
          component={ConnectZeptoScreen}
          options={{ title: 'Zepto login', headerBackTitle: 'Back' }}
        />
        <Stack.Screen
          name="ConnectBlinkit"
          component={ConnectBlinkitScreen}
          options={{ title: 'Blinkit setup', headerBackTitle: 'Back' }}
        />
        <Stack.Screen
          name="Home"
          component={HomeScreen}
          options={{ title: 'scan2order', headerBackVisible: false }}
        />
        <Stack.Screen
          name="Results"
          component={ResultsScreen}
          options={{ title: 'Price comparison', headerBackTitle: 'Back' }}
        />
      </Stack.Navigator>
    </NavigationContainer>
  )
}
