// Service Worker minimo da Athena PWA (habilita instalacao em tela cheia sem cache agressivo de /api/*)
self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  // Ignora requisicoes cross-origin, metodos nao-GET e rotas /api/*
  if (url.origin !== self.location.origin || event.request.method !== "GET" || url.pathname.startsWith("/api/")) {
    return;
  }
});
