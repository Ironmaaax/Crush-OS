/* Service worker — rend la PWA installable et l'ouverture instantanée.
 *
 * Stratégie volontairement étroite : on ne met en cache QUE la coquille
 * (HTML, CSS, JS, icônes). Jamais les réponses de l'API ni le WebSocket —
 * mettre en cache une conversation ou un état d'authentification produirait
 * des réponses périmées et masquerait une session expirée.
 */

const CACHE = "assistant-coquille-v6";
const COQUILLE = [
  "/mobile",
  "/mobile/style.css",
  "/mobile/app.js",
  "/mobile/manifest.json",
  "/mobile/icon-192.png",
  "/mobile/icon-512.png",
];

self.addEventListener("install", (evt) => {
  evt.waitUntil(
    caches.open(CACHE)
      // addAll échoue en bloc si une seule ressource manque ; on tolère les
      // absences pour ne pas empêcher l'installation entière.
      .then((c) => Promise.allSettled(COQUILLE.map((u) => c.add(u))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (evt) => {
  evt.waitUntil(
    caches.keys()
      .then((noms) => Promise.all(noms.filter((n) => n !== CACHE).map((n) => caches.delete(n))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (evt) => {
  const url = new URL(evt.request.url);

  // Tout ce qui n'est pas la coquille passe directement au réseau : API,
  // WebSocket, authentification. Une réponse d'API servie depuis le cache
  // afficherait des données périmées, voire une session déjà expirée.
  // `/three.min.js` et `/orb.js` vivent hors de /mobile/ (partages avec
  // l'interface bureau) mais font partie de la coquille : sans eux, l'orbe ne
  // s'affiche pas hors ligne.
  // Les scripts de l'orbe ne sont PLUS mis en cache : ils sont versionnes par
  // `?v=<mtime>` dans le HTML, et une copie figee ici a deja fait disparaitre
  // l'orbe apres une mise a jour. Ils passent donc toujours par le reseau.
  const HORS_PREFIXE = [];
  const estCoquille =
    evt.request.method === "GET" &&
    url.origin === location.origin &&
    (url.pathname === "/mobile" ||
      url.pathname.startsWith("/mobile/") ||
      HORS_PREFIXE.includes(url.pathname));

  if (!estCoquille) return;

  // Réseau d'abord, cache en secours : on reste à jour quand le Pi répond, et
  // l'application s'ouvre quand même s'il est éteint.
  evt.respondWith(
    fetch(evt.request)
      .then((rep) => {
        const copie = rep.clone();
        caches.open(CACHE).then((c) => c.put(evt.request, copie)).catch(() => {});
        return rep;
      })
      .catch(() => caches.match(evt.request).then((r) => r || Response.error()))
  );
});
