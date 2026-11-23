// Service Worker minimo da Athena PWA (habilita instalacao em tela cheia sem cache agressivo de /api/*)
self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  // Nunca faz cache de rotas /api/* ou requisicoes que nao sejam GET
  if (event.request.method !== "GET" || url.pathname.startsWith("/api/")) {
    return;
  }
  event.respondWith(fetch(event.request));
});
