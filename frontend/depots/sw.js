// Service worker minimal : met en cache la coquille de l'app pour un démarrage rapide.
// Les appels API (/cash/...) ne sont jamais mis en cache.
const CACHE = 'depots-shell-v1';
const SHELL = ['/depots/', '/depots/index.html', '/depots/manifest.webmanifest', '/depots/icon-192.png', '/depots/icon-512.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.pathname.startsWith('/cash/')) return;
  if (!url.pathname.startsWith('/depots/')) return;
  e.respondWith(
    fetch(e.request).then((res) => { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); return res; })
      .catch(() => caches.match(e.request).then((r) => r || caches.match('/depots/index.html')))
  );
});
