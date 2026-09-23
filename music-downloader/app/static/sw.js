const CACHE_NAME = "xrob-music-shell-v372";
const APP_SHELL = [
  "/",
  "/static/index.html",
  "/static/style.css?v=321",
  "/static/app.js?v=313",
  "/static/icon-180.png",
  "/static/icon-192.png",
  "/static/icon-512.png",
  "/static/icon.png",
  "/static/logo.png",
  "/static/manifest.webmanifest"
];
self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(APP_SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", event => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/rest/")) return;
  if (url.pathname.startsWith("/api/library/stream/")) return;
  event.respondWith(
    caches.match(request).then(cached => {
      const network = fetch(request).then(response => {
        if (response && response.ok && response.type === "basic") {
          const copy = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(request, copy)).catch(() => {});
        }
        return response;
      }).catch(() => cached || caches.match("/"));
      return cached || network;
    })
  );
});
