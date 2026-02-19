const CACHE_NAME = 'mtb-meteo-v1';

// Risorse da cachare subito all'installazione (shell dell'app)
const STATIC_ASSETS = [
  '/dashboard-completa',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
];

// ── Installazione: pre-cacha le risorse statiche ──────────────────────────────
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

// ── Attivazione: rimuove cache vecchie ───────────────────────────────────────
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(
        keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))
      )
    )
  );
  self.clients.claim();
});

// ── Fetch: strategia "Network first, fallback cache" ─────────────────────────
// Per i dati meteo vogliamo sempre dati freschi dalla rete.
// Se la rete non è disponibile, serviamo la versione cachata.
self.addEventListener('fetch', event => {
  // Ignora richieste non-GET e richieste a domini esterni (CDN chart.js ecc.)
  if (event.request.method !== 'GET') return;
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return;

  event.respondWith(
    fetch(event.request)
      .then(response => {
        // Aggiorna la cache con la risposta fresca
        const clone = response.clone();
        caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
        return response;
      })
      .catch(() => {
        // Rete non disponibile → servi dalla cache
        return caches.match(event.request).then(cached => {
          if (cached) return cached;
          // Fallback generico se nemmeno la cache ha la risorsa
          return new Response(
            '<h2>📡 Nessuna connessione</h2><p>Apri l\'app quando sei online per aggiornare i dati meteo.</p>',
            { headers: { 'Content-Type': 'text/html; charset=utf-8' } }
          );
        });
      })
  );
});
