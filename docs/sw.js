'use strict';

// Journey Talk service worker: keeps the player usable offline.
// - App shell and every visited data file (episode list, scripts, transcripts): network first, cache fallback.
// - Saved episodes (offline/*.mp3, stored by the page in AUDIO_CACHE): served from cache with Range
//   support so seeking, tap-to-line and line repeat keep working without a connection.
// - Full-quality audio on GitHub Releases is cross-origin and simply streams from the network.
const SHELL_CACHE = 'jt-shell-v1';
const DATA_CACHE = 'jt-data';
const AUDIO_CACHE = 'jt-audio';
const SHELL = [
  './', 'index.html', 'assets/app.css', 'assets/app.js', 'manifest.webmanifest',
  'icons/icon-192.png', 'icons/icon-512.png', 'icons/apple-touch-icon.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    await (await caches.open(SHELL_CACHE)).addAll(SHELL);
    // The first page load fetched the episode list before this worker existed; keep a copy now.
    await (await caches.open(DATA_CACHE)).add(new URL('episodes.json', self.registration.scope).href).catch(() => {});
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names.filter((n) => n.startsWith('jt-shell-') && n !== SHELL_CACHE).map((n) => caches.delete(n)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.includes('/offline/')) {
    event.respondWith(savedAudio(request));
    return;
  }
  event.respondWith(networkFirst(request));
});

async function networkFirst(request) {
  try {
    const response = await fetch(request);
    if (response.ok && response.type === 'basic') {
      const copy = response.clone();
      caches.open(DATA_CACHE).then((cache) => cache.put(request.url.split('#')[0], copy)).catch(() => {});
    }
    return response;
  } catch (err) {
    const cached = await caches.match(request.url.split('#')[0], { ignoreSearch: true });
    if (cached) return cached;
    if (request.mode === 'navigate') {
      const shell = await caches.match('index.html');
      if (shell) return shell;
    }
    throw err;
  }
}

async function savedAudio(request) {
  const cache = await caches.open(AUDIO_CACHE);
  const cached = await cache.match(request.url);
  if (!cached) return fetch(request);
  const blob = await cached.blob();
  const size = blob.size;
  const headers = { 'Content-Type': 'audio/mpeg', 'Accept-Ranges': 'bytes' };
  const range = /bytes=(\d*)-(\d*)/.exec(request.headers.get('range') || '');
  if (!range) return new Response(blob, { status: 200, headers: { ...headers, 'Content-Length': String(size) } });
  let start;
  let end;
  if (range[1] === '' && range[2] !== '') { // suffix range: the last N bytes
    start = Math.max(0, size - Number(range[2]));
    end = size - 1;
  } else {
    start = Number(range[1] || 0);
    end = range[2] === '' ? size - 1 : Math.min(Number(range[2]), size - 1);
  }
  if (start >= size || start > end) {
    return new Response(null, { status: 416, headers: { 'Content-Range': `bytes */${size}` } });
  }
  return new Response(blob.slice(start, end + 1), {
    status: 206,
    headers: { ...headers, 'Content-Range': `bytes ${start}-${end}/${size}`, 'Content-Length': String(end - start + 1) },
  });
}
