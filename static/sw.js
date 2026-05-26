const CACHE = 'equb-v3.1';
const STATIC = [
  '/static/css/tailwind.css',
  '/static/manifest.json',
  '/static/icon.svg',
];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(STATIC)));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;
  const url = new URL(e.request.url);

  // API calls: always network (no cache)
  if (url.pathname.startsWith('/api/')) return;

  // Static assets: cache-first
  if (url.pathname.startsWith('/static/')) {
    e.respondWith(
      caches.match(e.request).then(cached =>
        cached || fetch(e.request).then(res => {
          const clone = res.clone();
          caches.open(CACHE).then(c => c.put(e.request, clone));
          return res;
        })
      )
    );
    return;
  }

  // HTML pages: network-first, cache fallback
  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request))
  );
});
