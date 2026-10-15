// Digidaw on the phone: notifications while the HUD page is closed (Web Push), and the page itself when the PC
// can't be reached (it then shows "tidak terhubung" instead of the browser's error page).
const CACHE = "digidaw-v2";
const SHELL = ["/", "/icons/icon-192.png", "/icons/badge-96.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== CACHE && k !== INBOX).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

// Android's "Bagikan → Digidaw": keep what was shared on the phone, then open the HUD, which asks what to do
// with it (open / play / copy / save on the PC). Nothing reaches the PC before the user picks.
const INBOX = "digidaw-share";
async function takeShare(request) {
  const form = await request.formData();
  const box = await caches.open(INBOX);
  for (const key of await box.keys()) await box.delete(key);
  const meta = { title: form.get("title") || "", text: form.get("text") || "", url: form.get("url") || "", files: [], at: Date.now() };
  let i = 0;
  for (const f of form.getAll("files")) {
    if (!(f instanceof File) || !f.size) continue;
    const key = `/share-inbox/${i++}`;
    await box.put(key, new Response(f, { headers: { "Content-Type": f.type || "application/octet-stream" } }));
    meta.files.push({ key, name: f.name || `file-${i}`, size: f.size, type: f.type || "" });
  }
  await box.put("/share-inbox/meta", new Response(JSON.stringify(meta), { headers: { "Content-Type": "application/json" } }));
  return Response.redirect("/?share=1", 303);
}

// the page: always fresh from the PC; the saved copy only when the PC is off or out of reach
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (url.origin === location.origin && url.pathname === "/share-target" && e.request.method === "POST") {
    e.respondWith(takeShare(e.request).catch(() => Response.redirect("/?share=fail", 303)));
    return;
  }
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (e.request.mode === "navigate") {
    e.respondWith(fetch(e.request).then((r) => {
      if (r.ok) { const copy = r.clone(); caches.open(CACHE).then((c) => c.put("/", copy)); }
      return r;
    }).catch(() => caches.match("/")));
  } else if (url.pathname.startsWith("/icons/")) {
    e.respondWith(caches.match(e.request).then((hit) => hit || fetch(e.request)));
  }
});

async function pageVisible() {
  const pages = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  return pages.some((p) => p.visibilityState === "visible");
}

self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) { d = { text: e.data ? e.data.text() : "" }; }
  e.waitUntil((async () => {
    if (d.kind === "alarm_end") {                       // stopped on the PC (or elsewhere): take it away
      (await self.registration.getNotifications({ tag: d.tag })).forEach((n) => n.close());
      return;
    }
    if (d.tag !== "test" && await pageVisible()) return; // the open page shows its own banner and speaks
    const alarm = d.kind === "alarm";
    await self.registration.showNotification(d.title || "Digidaw", {
      body: d.text || "", tag: d.tag || "digidaw", renotify: true, lang: "id",
      icon: "/icons/icon-192.png", badge: "/icons/badge-96.png",
      vibrate: alarm ? [500, 250, 500, 250, 500, 250, 500] : [200, 100, 200],
      requireInteraction: alarm, data: { kind: d.kind },
      actions: alarm ? [{ action: "stop", title: "Stop" }, { action: "snooze", title: "Tunda 5 menit" }] : [],
    });
  })());
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  e.waitUntil((async () => {
    if (e.action === "stop" || e.action === "snooze") {   // straight from the notification, page not needed
      try {
        await fetch("/api/alarm", { method: "POST", credentials: "same-origin",
          headers: { "X-Digidaw": "1", "Content-Type": "application/json" }, body: JSON.stringify({ action: e.action }) });
        return;
      } catch (err) { /* PC out of reach: open the page instead */ }
    }
    const pages = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    if (pages.length) return pages[0].focus();
    return self.clients.openWindow("/");
  })());
});
