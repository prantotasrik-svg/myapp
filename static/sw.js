// --- PWA Caching Logic (keeps your app fast and offline-capable) ---
const CACHE_NAME = 'chat-app-cache-v6'; // Increment version to force update
const urlsToCache = [
  '/',
  '/download',
  '/static/style.css',
  '/static/manifest.json',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png'
];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(urlsToCache)));
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(cacheNames => {
      return Promise.all(
        cacheNames.filter(cacheName => cacheName !== CACHE_NAME).map(cacheName => caches.delete(cacheName))
      );
    })
  );
});

self.addEventListener('fetch', event => {
  event.respondWith(
    caches.match(event.request).then(response => response || fetch(event.request))
  );
});


// --- Firebase Push Notification Logic ---
importScripts('https://www.gstatic.com/firebasejs/9.15.0/firebase-app-compat.js');
importScripts('https://www.gstatic.com/firebasejs/9.15.0/firebase-messaging-compat.js');

const firebaseConfig = {
  apiKey: "AIzaSyDJb0OzEmgxIGkqFsPnhRv2Tq33sNzHuRQ",
  authDomain: "tasrikchat.firebaseapp.com",
  projectId: "tasrikchat",
  storageBucket: "tasrikchat.firebasestorage.app",
  messagingSenderId: "425161922639",
  appId: "1:425161922639:web:f362581de0231ae345723a",
  measurementId: "G-6W5Y1SHNTQ"
};

firebase.initializeApp(firebaseConfig);
const messaging = firebase.messaging();

// This is the main handler for background notifications.
messaging.onBackgroundMessage(function(payload) {
  console.log('[sw.js] Received background message ', payload);

  const notificationTitle = payload.notification.title;
  const notificationOptions = {
    body: payload.notification.body,
    icon: '/static/icons/icon-192.png',
    tag: payload.notification.tag, // Groups notifications by room
    data: {
        url: payload.fcmOptions.link // The URL to open on click
    }
  };

  // This command actually shows the notification to the user.
  return self.registration.showNotification(notificationTitle, notificationOptions);
});

// This handler runs when a user clicks the notification.
self.addEventListener('notificationclick', event => {
  const notification = event.notification;
  const urlToOpen = notification.data.url || '/';
  notification.close();

  // This focuses the chat window if it's already open, or opens a new one.
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clientList => {
      for (let client of clientList) {
        if (client.url.endsWith(urlToOpen) && 'focus' in client) {
          return client.focus();
        }
      }
      if (clients.openWindow) {
        return clients.openWindow(urlToOpen);
      }
    })
  );
});