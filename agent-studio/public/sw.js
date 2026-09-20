// Service worker minimal : les données doivent rester fraîches, seuls les fichiers
// de l'interface sont mis en cache.
const CACHE = "agent-studio-v2";

self.addEventListener("install", (e) => {
  self.skipWaiting();
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(["/", "/manifest.webmanifest"])));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))),
    ).then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== self.location.origin) return;

  // L'API n'est jamais servie depuis le cache : un cycle en cours doit être exact.
  if (url.pathname.startsWith("/api/")) return;

  // Navigation et index.html : réseau d'abord, pour ne jamais servir indéfiniment
  // une page obsolète qui référencerait des fichiers déjà supprimés d'un déploiement
  // ultérieur. Le cache ne sert qu'en secours hors-ligne.
  if (e.request.mode === "navigate" || url.pathname === "/index.html") {
    e.respondWith(
      fetch(e.request)
        .then((res) => {
          if (res.ok) {
            const copy = res.clone();
            caches.open(CACHE).then((c) => c.put(e.request, copy));
          }
          return res;
        })
        .catch(() => caches.match(e.request).then((hit) => hit || caches.match("/"))),
    );
    return;
  }

  // Fichiers statiques (JS/CSS content-hashés, immuables par construction) :
  // cache d'abord, sans risque puisqu'un contenu différent produit une URL différente.
  e.respondWith(
    caches.match(e.request).then((hit) =>
      hit ||
      fetch(e.request)
        .then((res) => {
          if (res.ok && res.type === "basic") {
            const copy = res.clone();
            caches.open(CACHE).then((c) => c.put(e.request, copy));
          }
          return res;
        })
        .catch(() => caches.match("/")),
    ),
  );
});
