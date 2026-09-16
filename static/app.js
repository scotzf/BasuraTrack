/*
  Sukod logger - client side.

  How this works, before the API surface:

  Saving an entry NEVER touches the network. The Save button writes rows into
  IndexedDB (the browser's on-device database) and returns immediately. A
  separate, silent routine later drains that queue to the server. So the
  logger's experience is identical whether the phone has signal, has one bar,
  or is in a basement.

  The duplicate problem: if the queue is drained twice - because the phone
  reconnected mid-flight, or the response was lost after the server wrote the
  row - we must not count the sacks twice. Every entry gets a UUID at the
  moment of creation on the phone. The server uses that UUID as the primary
  key, so the second write collides and is skipped. The phone only deletes a
  queued row once the server confirms it HOLDS that UUID, which is true both
  for a fresh write and for a duplicate.
*/

(function () {
  "use strict";

  var DB_NAME = "sukod";
  var DB_VERSION = 1;
  var STORE = "queue";        // entries not yet confirmed by the server
  var LAST_AREA_KEY = "sukod.lastArea";

  var db = null;
  var lastFix = null;         // most recent GPS reading, may be null forever

  // ---------------------------------------------------------------------
  // IndexedDB - a thin promise wrapper, because the raw API is event-based
  // ---------------------------------------------------------------------

  function openDb() {
    return new Promise(function (resolve, reject) {
      var req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = function (e) {
        var d = e.target.result;
        if (!d.objectStoreNames.contains(STORE)) {
          // keyPath is the client UUID: the same identity the server uses.
          d.createObjectStore(STORE, { keyPath: "id_uuid" });
        }
      };
      req.onsuccess = function () { resolve(req.result); };
      req.onerror = function () { reject(req.error); };
    });
  }

  function tx(mode) {
    return db.transaction(STORE, mode).objectStore(STORE);
  }

  function putEntry(entry) {
    return new Promise(function (resolve, reject) {
      var r = tx("readwrite").put(entry);
      r.onsuccess = resolve;
      r.onerror = function () { reject(r.error); };
    });
  }

  function allEntries() {
    return new Promise(function (resolve, reject) {
      var r = tx("readonly").getAll();
      r.onsuccess = function () { resolve(r.result || []); };
      r.onerror = function () { reject(r.error); };
    });
  }

  function deleteEntries(ids) {
    var store = tx("readwrite");
    ids.forEach(function (id) { store.delete(id); });
  }

  // ---------------------------------------------------------------------
  // Identity
  // ---------------------------------------------------------------------

  function uuid() {
    // crypto.randomUUID is not on older Android WebViews, so fall back to a
    // v4 built from crypto.getRandomValues. Never Math.random: a collision
    // here would silently merge two real entries.
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    var b = new Uint8Array(16);
    crypto.getRandomValues(b);
    b[6] = (b[6] & 0x0f) | 0x40;      // version 4
    b[8] = (b[8] & 0x3f) | 0x80;      // variant 1
    var h = [].map.call(b, function (x) {
      return ("0" + x.toString(16)).slice(-2);
    }).join("");
    return h.slice(0, 8) + "-" + h.slice(8, 12) + "-" + h.slice(12, 16) + "-" +
           h.slice(16, 20) + "-" + h.slice(20);
  }

  function deviceToken() {
    return localStorage.getItem("sukod.device") || "";
  }

  // ---------------------------------------------------------------------
  // Location - background only, never blocking
  // ---------------------------------------------------------------------

  function watchLocation() {
    if (!navigator.geolocation) return;
    // We ask once and keep whatever we get. A fix that never arrives costs
    // nothing: the entry saves regardless and is simply tagged unverified.
    navigator.geolocation.watchPosition(
      function (p) { lastFix = { lat: p.coords.latitude, lng: p.coords.longitude }; },
      function () { /* denied or unavailable - silently continue */ },
      { enableHighAccuracy: false, maximumAge: 120000, timeout: 15000 }
    );
  }

  // ---------------------------------------------------------------------
  // Counters
  // ---------------------------------------------------------------------

  var fill = "full";

  function bindCounters() {
    document.querySelectorAll(".stream-row").forEach(function (row) {
      var valueEl = row.querySelector(".count-value");

      var minusBtn = row.querySelector(".minus");

      function set(n) {
        n = Math.max(0, n);
        valueEl.dataset.count = n;
        valueEl.textContent = n;
        valueEl.classList.toggle("zero", n === 0);
        // Light the whole tile once this stream has sacks on it, so glancing
        // down the screen shows what is about to be saved without reading
        // four separate numbers.
        row.classList.toggle("has-count", n > 0);
        // Nothing to take away at zero. Dimming it stops a mis-tap on the
        // wrong side of the pair from feeling like the screen ignored you.
        minusBtn.disabled = n === 0;
        refreshSaveButton();
      }

      set(0);   // establish the disabled minus and the resting styles

      row.querySelector(".plus").addEventListener("click", function () {
        set(parseInt(valueEl.dataset.count, 10) + 1);
      });
      row.querySelector(".minus").addEventListener("click", function () {
        set(parseInt(valueEl.dataset.count, 10) - 1);
      });
    });

    document.querySelectorAll(".fills button").forEach(function (b) {
      b.addEventListener("click", function () {
        document.querySelectorAll(".fills button").forEach(function (o) {
          o.setAttribute("aria-pressed", String(o === b));
        });
        fill = b.dataset.fill;
      });
    });
  }

  function currentCounts() {
    var out = [];
    document.querySelectorAll(".stream-row").forEach(function (row) {
      var n = parseInt(row.querySelector(".count-value").dataset.count, 10);
      if (n > 0) out.push({ stream: parseInt(row.dataset.stream, 10), count: n });
    });
    return out;
  }

  function resetCounts() {
    document.querySelectorAll(".count-value").forEach(function (el) {
      el.dataset.count = 0;
      el.textContent = "0";
      el.classList.add("zero");
    });
    document.querySelectorAll(".stream-row").forEach(function (row) {
      row.classList.remove("has-count");
      row.querySelector(".minus").disabled = true;
    });
    refreshSaveButton();
  }

  function refreshSaveButton() {
    var counts = currentCounts();
    // Disabled at zero: an empty save is always a mis-tap, never an intention.
    document.getElementById("save").disabled = counts.length === 0;
    updateTally(counts);
  }

  function updateTally(counts) {
    // A running total of what is about to be saved, so the person can check it
    // against the pile in front of them without re-reading four rows.
    var el = document.getElementById("tally");
    if (!el) return;                       // not the logger screen

    var sacks = counts.reduce(function (t, c) { return t + c.count; }, 0);
    if (!sacks) {
      el.textContent = "Nothing counted yet";
      el.classList.add("none");
      return;
    }
    el.classList.remove("none");
    el.innerHTML = "<strong>" + sacks + "</strong> " +
      (sacks === 1 ? "sack" : "sacks") + " in " + counts.length +
      (counts.length === 1 ? " stream" : " streams");
  }

  // ---------------------------------------------------------------------
  // Save - local first, always
  // ---------------------------------------------------------------------

  function save() {
    var areaSelect = document.getElementById("area");
    var areaId = parseInt(areaSelect.value, 10);
    var now = new Date().toISOString();

    // Remember the area so the screen opens where it was left. This is what
    // makes the second entry of the day faster than the first.
    localStorage.setItem(LAST_AREA_KEY, String(areaId));

    // One row per stream with a non-zero count. Each gets its own UUID.
    var entries = currentCounts().map(function (c) {
      return {
        id_uuid: uuid(),
        source_area: areaId,
        stream: c.stream,
        count: c.count,
        fill: fill,
        method: "C",             // counted and calibrated
        logged_at: now,
        lat: lastFix ? lastFix.lat : null,
        lng: lastFix ? lastFix.lng : null,
        note: ""
      };
    });

    // Write them all, then clear the screen. No dialog, no spinner: the entry
    // is already durable on the device the moment this resolves.
    Promise.all(entries.map(putEntry)).then(function () {
      resetCounts();
      note(entries.length + (entries.length === 1 ? " entry saved" : " entries saved"));
      renderToday();
      drainQueue();          // opportunistic; failure is invisible and fine
    }).catch(function () {
      // IndexedDB itself failed, which is rare and serious. This is the one
      // case the logger must be told about, because nothing was stored.
      note("Could not save on this device. Write it on paper.", true);
    });
  }

  function note(text, bad) {
    var el = document.getElementById("sync-line");
    el.textContent = text;
    el.style.color = bad ? "#b91c1c" : "";
  }

  // ---------------------------------------------------------------------
  // Sync - silent, idempotent
  // ---------------------------------------------------------------------

  function drainQueue() {
    if (!navigator.onLine) { showPending(); return; }

    allEntries().then(function (rows) {
      if (!rows.length) { showPending(); return; }

      return fetch("/api/sync/", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Sukod-Device": deviceToken()
        },
        body: JSON.stringify({ entries: rows })
      }).then(function (r) {
        if (!r.ok) throw new Error("sync rejected");
        return r.json();
      }).then(function (res) {
        // `held` covers both fresh writes and UUIDs the server already had.
        // Deleting on "already had" is exactly what prevents double-counting
        // on a retry: the row leaves the queue without being written twice.
        deleteEntries(res.held || []);
        showPending();
        renderToday();
      });
    }).catch(function () {
      // Offline, server down, captive portal - all the same to us. The queue
      // keeps the rows and we try again on the next save or reconnect.
      showPending();
    });
  }

  function showPending() {
    allEntries().then(function (rows) {
      note(rows.length ? rows.length + " entries waiting" : "All entries synced");
    });
  }

  // ---------------------------------------------------------------------
  // Today's list, with undo on the most recent
  // ---------------------------------------------------------------------

  function renderToday() {
    fetch("/api/today/").then(function (r) { return r.json(); }).then(function (data) {
      var host = document.getElementById("today");
      if (!data.entries.length) {
        host.innerHTML = '<div class="muted small">No entries yet today.</div>';
        return;
      }
      host.innerHTML = "";
      data.entries.forEach(function (e, i) {
        var row = document.createElement("div");
        row.className = "today-item";
        row.innerHTML = '<span>' + e.at + ' &middot; ' + escapeHtml(e.area) +
          ' &middot; <strong>' + e.count + '</strong> ' + escapeHtml(e.stream) +
          ' <span class="muted">(' + escapeHtml(e.fill) + ')</span></span>';
        // Undo only on the newest entry - a notebook lets you scratch out the
        // last line, not rewrite the page. No confirmation dialog.
        if (i === 0) {
          var b = document.createElement("button");
          b.className = "undo";
          b.textContent = "Undo";
          b.addEventListener("click", function () {
            fetch("/api/undo/" + e.id_uuid + "/", { method: "POST" })
              .then(renderToday);
          });
          row.appendChild(b);
        }
        host.appendChild(row);
      });
    }).catch(function () {
      // Offline: today's server-side list is simply unavailable. Not an error
      // worth showing - the entries are safe in the queue either way.
    });
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // ---------------------------------------------------------------------
  // Start-up
  // ---------------------------------------------------------------------

  function restoreArea() {
    var select = document.getElementById("area");
    if (!select) return;
    // A QR scan wins over the remembered area: the sign on the wall is the
    // most explicit statement of intent available.
    var wanted = window.SUKOD_AREA_PARAM || localStorage.getItem(LAST_AREA_KEY);
    if (wanted && select.querySelector('option[value="' + wanted + '"]')) {
      select.value = wanted;
    }
  }

  openDb().then(function (d) {
    db = d;
    if (!document.getElementById("save")) return;   // not the logger screen
    bindCounters();
    restoreArea();
    watchLocation();
    document.getElementById("save").addEventListener("click", save);
    renderToday();
    drainQueue();
    // Retry the moment the radio comes back, and periodically otherwise.
    window.addEventListener("online", drainQueue);
    setInterval(drainQueue, 60000);
  }).catch(function () {
    note("This browser cannot store entries offline.", true);
  });
})();
