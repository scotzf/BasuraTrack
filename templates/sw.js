/*
  Sukod service worker.

  Its only job is the SHELL: keep the logger screen, its stylesheet and its
  script available when the network is not. The entries themselves are handled
  by IndexedDB in app.js, not here - a service worker cache is the wrong place
  for data you cannot afford to lose.

  Strategy:
    * navigations   -> network first, fall back to the cached shell, then to
                       the offline page. A stale notebook beats a dinosaur.
    * static assets -> cache first. They change only on deploy.
    * /api/         -> never cached. A cached sync response would be a lie.
*/
var CACHE = 'sukod-shell-v1';
var SHELL = ['/', '/offline/', '/static/style.css', '/static/app.js',
             '/static/manifest.json'];

self.addEventListener('install', function (e) {
  // Take over as soon as the new worker is ready rather than waiting for
  // every tab to close - a field device may never close its tab.
  self.skipWaiting();
  e.waitUntil(caches.open(CACHE).then(function (c) {
    // addAll fails the whole install if one URL 404s, so tolerate misses.
    return Promise.all(SHELL.map(function (u) {
      return c.add(u).catch(function () {});
    }));
  }));
});

self.addEventListener('activate', function (e) {
  e.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (k) { return k !== CACHE; })
                           .map(function (k) { return caches.delete(k); }));
  }).then(function () { return self.clients.claim(); }));
});

self.addEventListener('fetch', function (e) {
  var req = e.request;
  if (req.method !== 'GET') return;                    // sync POSTs pass through
  var url = new URL(req.url);
  if (url.origin !== location.origin) return;
  if (url.pathname.indexOf('/api/') === 0) return;     // never cache API calls

  if (req.mode === 'navigate') {
    e.respondWith(
      fetch(req).then(function (res) {
        var copy = res.clone();
        caches.open(CACHE).then(function (c) { c.put(req, copy); });
        return res;
      }).catch(function () {
        return caches.match(req)
          .then(function (hit) { return hit || caches.match('/offline/'); });
      })
    );
    return;
  }

  e.respondWith(
    caches.match(req).then(function (hit) {
      return hit || fetch(req).then(function (res) {
        var copy = res.clone();
        caches.open(CACHE).then(function (c) { c.put(req, copy); });
        return res;
      });
    })
  );
});
