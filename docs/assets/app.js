'use strict';

const FLAGS = { de: '🇩🇪', es: '🇪🇸', ru: '🇷🇺', zh: '🇨🇳', ko: '🇰🇷', en: '🇺🇸' };
const RATES = [0.75, 0.9, 1, 1.25];
const REVIEW_DAYS = [0, 1, 3, 7, 14, 30]; // Leitner box -> days until the next review
const BUCKET_SECONDS = 10; // listening coverage is tracked in 10 s buckets
const DONE_RATIO = 0.85;
const STORE_KEY = 'journeyTalk.v1';

const SpeechRecognitionImpl = window.SpeechRecognition || window.webkitSpeechRecognition || null;
// Languages written without spaces between words are compared character by character.
const CHAR_LANGS = /^(zh|ja|ko)/;

const app = document.getElementById('app');
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const httpUrl = (value) => (/^https?:\/\//i.test(String(value || '')) ? String(value) : '#');
const relUrl = (value) => (/^[\w./-]+$/.test(String(value || '')) && !String(value).includes('..') ? String(value) : '');
const fmt = (sec) => {
  const s = Math.max(0, Math.floor(sec || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
};
const localDay = (d = new Date()) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
const addDays = (n) => { const d = new Date(); d.setDate(d.getDate() + n); return localDay(d); };
// Weekend review editions use slugs like "es-weekly"; the language is the part before the dash.
const baseSlug = (slug) => String(slug || '').split('-')[0];
const langLabel = (ep) => `${FLAGS[baseSlug(ep.slug)] || '🌐'} ${esc(ep.japanese_name)}`;

/* ---------- persistent learner state (per browser) ---------- */
const store = (() => {
  const blank = () => ({ lang: 'all', progress: {}, days: [], words: {}, prefs: { translate: true, blind: false, follow: true, rate: 1 } });
  let state = blank();
  try {
    const saved = JSON.parse(localStorage.getItem(STORE_KEY) || 'null');
    if (saved && typeof saved === 'object') {
      state = { ...blank(), ...saved };
      state.prefs = { ...blank().prefs, ...(saved.prefs || {}) };
    }
  } catch (_) { /* storage unavailable: run with in-memory state */ }
  return {
    get state() { return state; },
    save() { try { localStorage.setItem(STORE_KEY, JSON.stringify(state)); } catch (_) { /* ignore */ } },
  };
})();

function progressOf(key) {
  const p = store.state.progress;
  if (!p[key]) p[key] = { buckets: [], done: false, pos: 0, quiz: null };
  return p[key];
}
function markActiveToday() {
  const today = localDay();
  if (!store.state.days.includes(today)) store.state.days = [...store.state.days, today].slice(-400);
  store.save();
}
function streak() {
  const days = new Set(store.state.days);
  const d = new Date();
  if (!days.has(localDay(d))) d.setDate(d.getDate() - 1);
  let n = 0;
  while (days.has(localDay(d))) { n += 1; d.setDate(d.getDate() - 1); }
  return n;
}
function minutesListened() {
  const buckets = Object.values(store.state.progress).reduce((sum, p) => sum + (p.buckets ? p.buckets.length : 0), 0);
  return Math.round((buckets * BUCKET_SECONDS) / 60);
}
const dueWords = () => Object.values(store.state.words).filter((w) => w.due <= localDay());

let toastTimer = 0;
function toast(message) {
  const el = document.getElementById('toast');
  el.textContent = message;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 2600);
}

/* ---------- data ---------- */
let historyCache = null;
const detailCache = new Map();
async function loadHistory() {
  if (!historyCache) {
    const res = await fetch('episodes.json', { cache: 'no-store' });
    if (!res.ok) throw new Error(`episodes.json HTTP ${res.status}`);
    historyCache = await res.json();
  }
  return historyCache;
}
async function loadDetail(url) {
  if (!detailCache.has(url)) {
    const res = await fetch(url, { cache: 'no-cache' });
    if (!res.ok) throw new Error(`${url} HTTP ${res.status}`);
    detailCache.set(url, await res.json());
  }
  return detailCache.get(url);
}

/* ---------- offline saving (service worker + Cache Storage) ---------- */
const AUDIO_CACHE = 'jt-audio';
const offlineSupported = 'caches' in window && 'serviceWorker' in navigator;
const absUrl = (rel) => new URL(rel, location.href.split('#')[0]).href;
const mb = (bytes) => (bytes ? `（${(bytes / 1048576).toFixed(1)}MB）` : '');

async function savedAudioUrls() {
  if (!offlineSupported) return new Set();
  try {
    const cache = await caches.open(AUDIO_CACHE);
    return new Set((await cache.keys()).map((r) => r.url));
  } catch (_) { return new Set(); }
}
async function isSaved(rel) {
  return Boolean(rel) && (await savedAudioUrls()).has(absUrl(rel));
}
async function saveOffline(rel, onProgress, dataUrls = []) {
  // Keep the script, vocabulary and episode list with the audio so the whole page works offline.
  try { await (await caches.open('jt-data')).addAll(dataUrls.map(absUrl)); } catch (_) { /* best effort */ }
  const res = await fetch(rel, { cache: 'no-store' });
  if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
  const total = Number(res.headers.get('content-length')) || 0;
  const reader = res.body.getReader();
  const chunks = [];
  let received = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    received += value.length;
    if (total) onProgress(Math.round((received / total) * 100));
  }
  const blob = new Blob(chunks, { type: 'audio/mpeg' });
  const cache = await caches.open(AUDIO_CACHE);
  await cache.put(absUrl(rel), new Response(blob, { headers: { 'Content-Type': 'audio/mpeg', 'Content-Length': String(blob.size) } }));
  try { if (navigator.storage && navigator.storage.persist) await navigator.storage.persist(); } catch (_) { /* optional */ }
}
async function removeOffline(rel) {
  const cache = await caches.open(AUDIO_CACHE);
  await cache.delete(absUrl(rel));
}

let installPrompt = null;
window.addEventListener('beforeinstallprompt', (event) => {
  event.preventDefault();
  installPrompt = event;
  const slot = document.getElementById('install-slot');
  if (slot) slot.hidden = false;
});
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => navigator.serviceWorker.register('sw.js').catch(() => { /* offline mode unavailable */ }));
}

/* ---------- shared chrome ---------- */
function topbar(current) {
  const due = dueWords().length;
  return `
    <div class="topbar">
      <a class="brand" href="#/">🎧 Journey Talk</a>
      <nav class="nav">
        <a href="#/" ${current === 'home' ? 'aria-current="page"' : ''}>エピソード</a>
        <a href="#/words" ${current === 'words' ? 'aria-current="page"' : ''}>単語帳${due ? ` (${due})` : ''}</a>
      </nav>
    </div>`;
}
function statsRow() {
  return `
    <div class="stats" aria-label="学習記録">
      <div class="stat"><b>🔥 ${streak()}</b><span>連続学習日数</span></div>
      <div class="stat"><b>${minutesListened()}</b><span>聴いた分数</span></div>
      <div class="stat"><b>${Object.keys(store.state.words).length}</b><span>単語帳の語数</span></div>
    </div>`;
}

/* ---------- home ---------- */
async function renderHome() {
  const days = await loadHistory();
  const langs = new Map();
  days.forEach((day) => day.episodes.forEach((ep) => { if (!langs.has(baseSlug(ep.slug))) langs.set(baseSlug(ep.slug), ep); }));
  const selected = langs.has(store.state.lang) ? store.state.lang : 'all';
  const chips = [['all', 'すべて'], ...[...langs.entries()].map(([slug, ep]) => [slug, langLabel(ep)])]
    .map(([slug, label]) => `<button class="chip" data-lang="${esc(slug)}" aria-pressed="${slug === selected}">${label}</button>`)
    .join('');
  const feed = selected === 'all'
    ? '<a href="feed.xml">全言語のRSS</a>'
    : `<a href="feeds/${esc(selected)}.xml">この言語だけをPodcastアプリで購読（RSS）</a>`;

  const sections = days.map((day) => {
    const eps = day.episodes.filter((ep) => selected === 'all' || baseSlug(ep.slug) === selected);
    if (!eps.length) return '';
    return `<section class="day"><h2>${esc(day.date)}</h2><div class="cards">${eps.map((ep) => episodeCard(day, ep)).join('')}</div></section>`;
  }).join('');

  app.innerHTML = `
    ${topbar('home')}
    <h1>ニュースで毎日、ことばの旅へ</h1>
    <p class="sub">最新ニュースを題材にした10〜15分の語学ラジオ。スクリプト同期・1文リピート・単語帳・クイズつき。</p>
    ${navigator.onLine ? '' : '<div class="empty offline-banner">📴 オフラインです。保存済みのエピソード、単語帳、クイズが使えます。</div>'}
    ${statsRow()}
    <div id="install-slot" ${installPrompt ? '' : 'hidden'}><button class="btn" id="install">📲 ホーム画面に追加（アプリとして使う）</button></div>
    <details class="howto">
      <summary>おすすめの学び方（1日15分）</summary>
      <ol>
        <li><b>ブラインド</b>をONにして、まずは字幕なしで聴く</li>
        <li>スクリプトと日本語訳で、聞き取れなかった所を確認</li>
        <li>難しい文は <b>🔁 1文リピート</b> で、声に出してシャドーイング</li>
        <li>気になる表現を<b>単語帳</b>へ。最後に<b>クイズ</b>で理解度チェック</li>
      </ol>
      <p class="muted" style="font-size:.85rem;margin:6px 0 0">通勤前に「⬇ オフライン保存」しておくと、電波がなくてもスクリプト同期つきで聴けます。iPhoneは共有ボタン →「ホーム画面に追加」でアプリのように使えます。</p>
    </details>
    <div class="chips" role="group" aria-label="学習する言語">${chips}</div>
    <p class="feedlink">${feed}</p>
    ${sections || '<div class="empty">まだ公開済みエピソードはありません。毎朝7時ごろ新しい回が追加されます。</div>'}`;

  app.querySelectorAll('.chip').forEach((chip) => chip.addEventListener('click', () => {
    store.state.lang = chip.dataset.lang;
    store.save();
    renderHome();
  }));
  const install = document.getElementById('install');
  install.addEventListener('click', async () => {
    if (!installPrompt) return;
    installPrompt.prompt();
    await installPrompt.userChoice.catch(() => null);
    installPrompt = null;
    document.getElementById('install-slot').hidden = true;
  });
  const saved = await savedAudioUrls();
  app.querySelectorAll('.card[data-offline]').forEach((card) => {
    const isHere = saved.has(absUrl(card.dataset.offline));
    if (isHere) card.querySelector('.badges').insertAdjacentHTML('afterbegin', '<span class="tag badge-done">📥 保存済み</span>');
    if (!navigator.onLine && !isHere) card.classList.add('unavailable');
  });
}

function episodeCard(day, ep) {
  const key = `${day.date}/${ep.slug}`;
  const p = store.state.progress[key];
  const badges = [];
  if (ep.kind === 'weekly') badges.push('<span class="tag badge-weekly">🗓 週末まとめ</span>');
  if (p && p.done) badges.push('<span class="tag badge-done">✓ 聴了</span>');
  if (p && p.quiz) badges.push(`<span class="tag">クイズ ${p.quiz.best}/${p.quiz.total}</span>`);
  (ep.highlights || []).slice(0, 3).forEach((h) => badges.push(`<span class="tag">${esc(String(h).split(' — ')[0])}</span>`));
  const top = `<div class="card-top"><span>${langLabel(ep)}</span><span>${Math.round(ep.duration_seconds / 60)}分</span></div>`;
  if (relUrl(ep.detail_url)) {
    return `
      <a class="card" href="#/${esc(day.date)}/${esc(ep.slug)}" ${relUrl(ep.offline_url) ? `data-offline="${esc(ep.offline_url)}"` : ''}>
        ${top}
        <h3>${esc(ep.title)}</h3>
        ${ep.summary_ja ? `<p class="summary">${esc(ep.summary_ja)}</p>` : ''}
        <div class="badges">${badges.join('')}</div>
      </a>`;
  }
  // Episodes published before the study player existed: plain audio + script download.
  return `
    <article class="card">
      ${top}
      <h3>${esc(ep.title)}</h3>
      <audio controls preload="none" src="${esc(httpUrl(ep.audio_url))}"></audio>
      <div class="badges"><a href="${esc(httpUrl(ep.audio_url))}" download>MP3</a> · <a href="${esc(httpUrl(ep.script_url))}">台本</a></div>
    </article>`;
}

/* ---------- episode study view ---------- */
let teardown = null;

async function renderEpisode(date, slug) {
  const days = await loadHistory();
  const day = days.find((d) => d.date === date);
  const entry = day && day.episodes.find((ep) => ep.slug === slug);
  if (!entry || !relUrl(entry.detail_url)) {
    app.innerHTML = `${topbar('')}<div class="empty">エピソードが見つかりませんでした。<a href="#/">一覧へ戻る</a></div>`;
    return;
  }
  const ep = await loadDetail(relUrl(entry.detail_url));
  const key = `${date}/${slug}`;
  const progress = progressOf(key);
  const prefs = store.state.prefs;
  const lines = Array.isArray(ep.lines) ? ep.lines : [];
  const timed = lines.length > 0 && lines.every((l) => typeof l.start === 'number');
  const hosts = ep.hosts || {};
  const vocab = ep.vocabulary || [];
  const quiz = ep.quiz || [];

  app.innerHTML = `
    ${topbar('')}
    <a class="back" href="#/">← エピソード一覧</a>
    <header class="ep-head">
      <div class="muted">${langLabel(ep)} · ${esc(ep.date)} · ${Math.round(ep.duration_seconds / 60)}分</div>
      <h1>${esc(ep.title)}</h1>
      ${ep.summary_ja ? `<p>${esc(ep.summary_ja)}</p>` : ''}
      <div class="ep-actions">
        ${progress.pos > 15 && !progress.done ? `<button class="btn resume" id="resume">▶ 続きから再生（${fmt(progress.pos)}）</button>` : ''}
        <span id="offline-slot"></span>
      </div>
    </header>
    <div class="tabs" role="tablist">
      <button role="tab" data-tab="script" aria-selected="true">スクリプト</button>
      <button role="tab" data-tab="vocab" aria-selected="false">単語 ${vocab.length}</button>
      <button role="tab" data-tab="quiz" aria-selected="false">クイズ ${quiz.length}</button>
      <button role="tab" data-tab="news" aria-selected="false">ニュース</button>
    </div>
    <section data-panel="script">
      <div class="toolbar">
        <button class="btn" data-pref="translate" aria-pressed="${prefs.translate}">日本語訳</button>
        <button class="btn" data-pref="blind" aria-pressed="${prefs.blind}">ブラインド</button>
        ${timed ? `<button class="btn" data-pref="follow" aria-pressed="${prefs.follow}">自動スクロール</button>` : ''}
      </div>
      <ol class="script ${prefs.translate ? '' : 'hide-ja'} ${prefs.blind ? 'blind' : ''}" id="script">
        ${lines.map((line, i) => scriptLine(line, i, ep.language, hosts)).join('')}
      </ol>
      <p class="muted" style="font-size:.8rem;margin-top:14px">ショートカット: <kbd>Space</kbd> 再生/停止 · <kbd>←</kbd><kbd>→</kbd> 前後の文 · <kbd>R</kbd> 1文リピート</p>
    </section>
    <section data-panel="vocab" hidden>${vocabPanel(ep, vocab)}</section>
    <section data-panel="quiz" hidden><div class="quiz" id="quiz"></div></section>
    <section data-panel="news" hidden>
      <ul class="stories">${(ep.stories || []).map((s) => `<li><span class="tag">${esc(s.source)}</span> ${httpUrl(s.url) === '#' ? esc(s.title) : `<a href="${esc(httpUrl(s.url))}" target="_blank" rel="noopener noreferrer">${esc(s.title)}</a>`}</li>`).join('')}</ul>
      <p class="muted" style="font-size:.85rem">番組はこれらの見出しと要約をもとに会話しています。原文を読むと、さらに語彙が広がります。</p>
    </section>
    <div class="player" role="region" aria-label="プレーヤー">
      <div class="player-inner">
        <input class="seek" type="range" min="0" max="1000" value="0" step="1" aria-label="再生位置">
        <div class="controls">
          <span class="time" id="time">0:00 / ${fmt(ep.duration_seconds)}</span>
          <div class="group">
            <button data-act="prev" aria-label="${timed ? '前の文' : '10秒戻る'}">${timed ? '⏮' : '↺10'}</button>
            <button data-act="play" class="play" aria-label="再生">▶</button>
            <button data-act="next" aria-label="${timed ? '次の文' : '10秒進む'}">${timed ? '⏭' : '10↻'}</button>
          </div>
          <div class="group">
            <button data-act="rate" aria-label="再生速度">${prefs.rate}×</button>
            ${timed ? '<button data-act="loop" aria-pressed="false" aria-label="1文リピート">🔁<span class="label"> 1文</span></button>' : ''}
          </div>
        </div>
      </div>
    </div>`;
  document.body.classList.add('has-player');

  setupTabs();
  setupPrefs();
  setupVocab(ep, vocab);
  renderQuiz(quiz, progress);
  const offlineRel = relUrl(ep.offline_url);
  const savedHere = offlineSupported && offlineRel ? await isSaved(offlineRel) : false;
  teardown = setupPlayer(ep, lines, timed, key, progress, savedHere ? offlineRel : httpUrl(ep.audio_url));
  setupOffline(offlineRel, entry, teardown);
}

// Wrap each spoken word in a span so it can light up while it is being said (karaoke view).
function karaoke(line) {
  const ranges = Array.isArray(line.w) ? line.w : [];
  if (!ranges.length) return esc(line.text);
  let html = '';
  let cursor = 0;
  ranges.forEach(([, , from, to], k) => {
    if (from < cursor || to > line.text.length) return;
    html += esc(line.text.slice(cursor, from)) + `<span class="w" data-k="${k}">${esc(line.text.slice(from, to))}</span>`;
    cursor = to;
  });
  return html + esc(line.text.slice(cursor));
}

function scriptLine(line, i, targetLanguage, hosts) {
  const target = line.language === targetLanguage;
  const who = line.speaker === 'MC_F' ? 'f' : 'm';
  return `
    <li class="line ${target ? 't' : 'n'}" data-i="${i}">
      <button class="line-main" type="button">
        <span class="who ${who}">${esc(hosts[line.speaker] || line.speaker)}</span>
        <span class="txt" lang="${esc(line.language)}">${karaoke(line)}${line.slow ? '<span class="slow">🐢 ゆっくり</span>' : ''}</span>
        ${line.ja ? `<span class="ja" lang="ja">${esc(line.ja)}</span>` : ''}
      </button>
      ${target ? `<span class="line-actions">
        <button class="reveal" type="button" aria-label="この文を表示">👁</button>
        ${SpeechRecognitionImpl ? '<button class="mic" type="button" aria-label="発音チェック">🎤</button>' : ''}
      </span>` : ''}
    </li>`;
}

function setupTabs() {
  const tabs = app.querySelectorAll('[role="tab"]');
  tabs.forEach((tab) => tab.addEventListener('click', () => {
    tabs.forEach((t) => t.setAttribute('aria-selected', String(t === tab)));
    app.querySelectorAll('[data-panel]').forEach((panel) => { panel.hidden = panel.dataset.panel !== tab.dataset.tab; });
  }));
}

function setupPrefs() {
  const script = document.getElementById('script');
  app.querySelectorAll('[data-pref]').forEach((btn) => btn.addEventListener('click', () => {
    const name = btn.dataset.pref;
    store.state.prefs[name] = !store.state.prefs[name];
    store.save();
    btn.setAttribute('aria-pressed', String(store.state.prefs[name]));
    script.classList.toggle('hide-ja', !store.state.prefs.translate);
    script.classList.toggle('blind', store.state.prefs.blind);
  }));
  script.addEventListener('click', (event) => {
    const reveal = event.target.closest('.reveal');
    if (reveal) reveal.closest('.line').classList.add('revealed');
  });
}

/* ---------- vocabulary ---------- */
const wordId = (ep, item) => `${baseSlug(ep.slug)}|${item.term}`;

function vocabPanel(ep, vocab) {
  if (!vocab.length) return '<div class="empty">この回の単語リストはありません。</div>';
  return `
    <div class="toolbar"><button class="btn" id="add-all">すべて単語帳に追加</button></div>
    <div class="vocab">${vocab.map((item, i) => `
      <article class="vcard">
        <div class="row">
          <div><span class="term" lang="${esc(ep.language)}">${esc(item.term)}</span>${item.reading ? `<span class="reading">${esc(item.reading)}</span>` : ''}</div>
          <button class="btn" data-word="${i}" aria-pressed="${Boolean(store.state.words[wordId(ep, item)])}">${store.state.words[wordId(ep, item)] ? '✓ 追加済み' : '＋ 単語帳'}</button>
        </div>
        <div class="meaning">${esc(item.meaning_ja)}</div>
        ${item.example ? `<div class="ex"><div lang="${esc(ep.language)}">${esc(item.example)}</div><div>${esc(item.example_ja)}</div></div>` : ''}
      </article>`).join('')}
    </div>`;
}

function addWord(ep, item) {
  const id = wordId(ep, item);
  if (store.state.words[id]) return false;
  store.state.words[id] = {
    term: item.term, reading: item.reading || '', meaning_ja: item.meaning_ja,
    example: item.example || '', example_ja: item.example_ja || '',
    slug: baseSlug(ep.slug), language: ep.language, japanese_name: ep.japanese_name,
    source: `${ep.date}/${ep.slug}`, box: 0, due: localDay(),
  };
  return true;
}

function setupVocab(ep, vocab) {
  const refresh = (btn, on) => { btn.setAttribute('aria-pressed', String(on)); btn.textContent = on ? '✓ 追加済み' : '＋ 単語帳'; };
  app.querySelectorAll('[data-word]').forEach((btn) => btn.addEventListener('click', () => {
    const item = vocab[Number(btn.dataset.word)];
    const id = wordId(ep, item);
    if (store.state.words[id]) delete store.state.words[id]; else addWord(ep, item);
    store.save();
    refresh(btn, Boolean(store.state.words[id]));
  }));
  const all = document.getElementById('add-all');
  if (all) all.addEventListener('click', () => {
    const added = vocab.filter((item) => addWord(ep, item)).length;
    store.save();
    app.querySelectorAll('[data-word]').forEach((btn) => refresh(btn, true));
    toast(added ? `${added}語を単語帳に追加しました` : 'すべて追加済みです');
  });
}

/* ---------- quiz ---------- */
function renderQuiz(quiz, progress) {
  const root = document.getElementById('quiz');
  if (!quiz.length) { root.innerHTML = '<div class="empty">この回のクイズはありません。</div>'; return; }
  const answers = new Array(quiz.length).fill(null);
  const draw = () => {
    const answered = answers.filter((a) => a !== null).length;
    const correct = answers.filter((a, i) => a === quiz[i].answer).length;
    root.innerHTML = quiz.map((q, i) => `
      <div class="q">
        <p>Q${i + 1}. ${esc(q.question_ja)}</p>
        <div class="choices">${q.choices.map((c, j) => {
          let cls = 'choice';
          if (answers[i] !== null && j === q.answer) cls += ' correct';
          else if (answers[i] === j) cls += ' wrong';
          return `<button class="${cls}" data-q="${i}" data-c="${j}" ${answers[i] !== null ? 'disabled' : ''}>${esc(c)}</button>`;
        }).join('')}</div>
        ${answers[i] !== null && q.explanation_ja ? `<div class="explain">${answers[i] === q.answer ? '⭕' : '❌'} ${esc(q.explanation_ja)}</div>` : ''}
      </div>`).join('') + (answered === quiz.length
      ? `<div class="q"><span class="score">${correct} / ${quiz.length} 正解</span> <button class="btn" id="quiz-retry">もう一度</button></div>`
      : '<p class="muted" style="font-size:.85rem">ヒント: 先に音声を聴いてから挑戦すると効果的です。</p>');
    root.querySelectorAll('[data-q]').forEach((btn) => btn.addEventListener('click', () => {
      answers[Number(btn.dataset.q)] = Number(btn.dataset.c);
      if (answers.every((a) => a !== null)) {
        const score = answers.filter((a, i) => a === quiz[i].answer).length;
        progress.quiz = { best: Math.max(score, progress.quiz ? progress.quiz.best : 0), total: quiz.length };
        markActiveToday();
      }
      draw();
    }));
    const retry = document.getElementById('quiz-retry');
    if (retry) retry.addEventListener('click', () => { answers.fill(null); draw(); });
  };
  draw();
}

/* ---------- audio player with transcript sync ---------- */
function setupOffline(rel, entry, player) {
  const slot = document.getElementById('offline-slot');
  if (!slot || !offlineSupported || !rel) return;
  const draw = async () => {
    if (await isSaved(rel)) {
      slot.innerHTML = '<span class="tag badge-done">📥 オフライン保存済み</span> <button class="btn" id="off-del">削除</button>';
      document.getElementById('off-del').addEventListener('click', async () => { await removeOffline(rel); player.swapSource(httpUrl(entry.audio_url)); draw(); });
      return;
    }
    if (!navigator.onLine) { slot.innerHTML = '<span class="muted">📴 この回は未保存のため、オンライン時に再生できます</span>'; return; }
    let available = false;
    try { available = (await fetch(rel, { method: 'HEAD', cache: 'no-store' })).ok; } catch (_) { /* treat as unavailable */ }
    if (!available) { slot.innerHTML = '<span class="muted" style="font-size:.82rem">オフライン保存は公開から3日以内の回でできます</span>'; return; }
    slot.innerHTML = `<button class="btn" id="off-save">⬇ オフライン保存${mb(entry.offline_bytes)}</button>`;
    const button = document.getElementById('off-save');
    button.addEventListener('click', async () => {
      button.disabled = true;
      button.textContent = '保存中…';
      try {
        await saveOffline(rel, (pct) => { button.textContent = `保存中… ${pct}%`; }, ['episodes.json', entry.detail_url]);
        player.swapSource(rel);
        toast('📥 保存しました。電波がなくても聴けます');
      } catch (err) {
        toast(`保存できませんでした: ${err.message || err}`);
      }
      draw();
    });
  };
  draw();
}

function setupPlayer(ep, lines, timed, key, progress, source) {
  const audio = new Audio();
  audio.preload = 'metadata';
  audio.src = source;
  audio.playbackRate = store.state.prefs.rate;
  const seek = app.querySelector('.seek');
  const timeEl = document.getElementById('time');
  const playBtn = app.querySelector('[data-act="play"]');
  const rateBtn = app.querySelector('[data-act="rate"]');
  const loopBtn = app.querySelector('[data-act="loop"]');
  const script = document.getElementById('script');
  const rows = [...script.querySelectorAll('.line')];
  const buckets = new Set(progress.buckets);
  let active = -1;
  let loopIndex = -1;
  let raf = 0;
  let lastSave = 0;
  let seeking = false;
  const duration = () => (Number.isFinite(audio.duration) && audio.duration > 0 ? audio.duration : ep.duration_seconds);

  const lineAt = (t) => {
    let lo = 0; let hi = lines.length - 1; let found = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (lines[mid].start <= t + 0.05) { found = mid; lo = mid + 1; } else hi = mid - 1;
    }
    return found;
  };
  const play = () => audio.play().catch(() => toast('再生できませんでした。通信状況を確認してください。'));
  let stopAt = null;
  const playLineOnce = (i) => {
    if (!timed) return;
    setLoop(-1);
    audio.currentTime = lines[i].start;
    stopAt = lines[i].end + 0.1;
    play();
  };
  const jumpToLine = (i) => {
    if (!timed || i < 0 || i >= lines.length) return;
    audio.currentTime = lines[i].start;
    stopAt = null;
    if (loopIndex >= 0) setLoop(i);
    if (audio.paused) play();
  };
  const setLoop = (i) => {
    if (loopIndex >= 0 && rows[loopIndex]) rows[loopIndex].classList.remove('looping');
    loopIndex = i;
    if (loopIndex >= 0 && rows[loopIndex]) rows[loopIndex].classList.add('looping');
    if (loopBtn) loopBtn.setAttribute('aria-pressed', String(loopIndex >= 0));
  };
  const save = (force) => {
    const now = Date.now();
    if (!force && now - lastSave < 5000) return;
    lastSave = now;
    progress.buckets = [...buckets];
    progress.pos = audio.currentTime || progress.pos;
    store.save();
  };
  const setActive = (i) => {
    if (i === active) return;
    if (active >= 0 && rows[active]) {
      rows[active].classList.remove('active');
      rows[active].classList.add('revealed'); // blind mode: show the line once it has been heard
    }
    active = i;
    if (active >= 0 && rows[active]) {
      rows[active].classList.add('active');
      if (store.state.prefs.follow && !script.closest('[hidden]')) {
        const box = rows[active].getBoundingClientRect();
        if (box.top < 80 || box.bottom > window.innerHeight - 150) rows[active].scrollIntoView({ block: 'center', behavior: 'smooth' });
      }
    }
  };
  let litWord = null;
  const setWord = (t) => {
    let next = null;
    const ranges = active >= 0 && Array.isArray(lines[active].w) ? lines[active].w : [];
    for (let k = 0; k < ranges.length; k += 1) {
      if (ranges[k][0] > t) break;
      if (t < ranges[k][1] + 0.08) { next = rows[active].querySelector(`.w[data-k="${k}"]`); break; }
    }
    if (next === litWord) return;
    if (litWord) litWord.classList.remove('on');
    if (next) next.classList.add('on');
    litWord = next;
  };
  const tick = () => {
    const t = audio.currentTime;
    if (!seeking) seek.value = String(Math.round((t / duration()) * 1000));
    timeEl.textContent = `${fmt(t)} / ${fmt(duration())}`;
    if (timed) {
      if (stopAt !== null && t >= stopAt) { stopAt = null; audio.pause(); }
      if (loopIndex >= 0 && t >= lines[loopIndex].end + 0.15) audio.currentTime = lines[loopIndex].start;
      setActive(lineAt(audio.currentTime));
      setWord(audio.currentTime);
    }
    if (!audio.paused) {
      buckets.add(Math.floor(t / BUCKET_SECONDS));
      if (!progress.done && buckets.size >= Math.ceil(duration() / BUCKET_SECONDS) * DONE_RATIO) {
        progress.done = true;
        markActiveToday();
        toast('🎉 聴了しました！クイズで理解度をチェックしよう');
      }
      save(false);
    }
  };
  const frame = () => {
    tick();
    if (!audio.paused) raf = requestAnimationFrame(frame);
  };

  audio.addEventListener('play', () => { playBtn.textContent = '❚❚'; playBtn.setAttribute('aria-label', '一時停止'); cancelAnimationFrame(raf); raf = requestAnimationFrame(frame); });
  audio.addEventListener('pause', () => { playBtn.textContent = '▶'; playBtn.setAttribute('aria-label', '再生'); save(true); });
  // Animation frames stop while the screen is locked; timeupdate keeps progress and line repeat working.
  audio.addEventListener('timeupdate', () => { if (audio.paused || document.hidden) tick(); });
  audio.addEventListener('loadedmetadata', () => tick());
  audio.addEventListener('ended', () => { setLoop(-1); save(true); });

  playBtn.addEventListener('click', () => (audio.paused ? play() : audio.pause()));
  app.querySelector('[data-act="prev"]').addEventListener('click', () => {
    if (!timed) { audio.currentTime = Math.max(0, audio.currentTime - 10); return; }
    const cur = lineAt(audio.currentTime);
    // Like a music player: first press restarts the current line, a quick second press goes back one.
    jumpToLine(cur > 0 && audio.currentTime - lines[cur].start < 1 ? cur - 1 : Math.max(0, cur));
  });
  app.querySelector('[data-act="next"]').addEventListener('click', () => {
    if (!timed) { audio.currentTime = Math.min(duration(), audio.currentTime + 10); return; }
    jumpToLine(Math.min(lines.length - 1, lineAt(audio.currentTime) + 1));
  });
  rateBtn.addEventListener('click', () => {
    const next = RATES[(RATES.indexOf(store.state.prefs.rate) + 1) % RATES.length];
    store.state.prefs.rate = next;
    store.save();
    audio.playbackRate = next;
    rateBtn.textContent = `${next}×`;
  });
  if (loopBtn) loopBtn.addEventListener('click', () => {
    if (loopIndex >= 0) { setLoop(-1); return; }
    setLoop(Math.max(0, lineAt(audio.currentTime)));
    toast('🔁 この文をくり返します。声に出してまねしてみよう');
  });
  seek.addEventListener('input', () => { seeking = true; timeEl.textContent = `${fmt((seek.value / 1000) * duration())} / ${fmt(duration())}`; });
  seek.addEventListener('change', () => { setLoop(-1); stopAt = null; audio.currentTime = (seek.value / 1000) * duration(); seeking = false; tick(); });
  script.addEventListener('click', (event) => {
    const main = event.target.closest('.line-main');
    if (!main || !timed) return;
    jumpToLine(Number(main.closest('.line').dataset.i));
  });
  const resume = document.getElementById('resume');
  if (resume) resume.addEventListener('click', () => { audio.currentTime = progress.pos; play(); resume.remove(); });

  const onKey = (event) => {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.target.closest && event.target.closest('input, textarea, select')) return;
    // Space always means play/pause here, even when a transcript line or control button has focus.
    if (event.code === 'Space') { event.preventDefault(); playBtn.click(); }
    else if (event.key === 'ArrowLeft') { event.preventDefault(); app.querySelector('[data-act="prev"]').click(); }
    else if (event.key === 'ArrowRight') { event.preventDefault(); app.querySelector('[data-act="next"]').click(); }
    else if ((event.key === 'r' || event.key === 'R') && loopBtn) loopBtn.click();
  };
  document.addEventListener('keydown', onKey);
  const onHide = () => { if (document.visibilityState === 'hidden') save(true); };
  document.addEventListener('visibilitychange', onHide);

  if ('mediaSession' in navigator) {
    try {
      navigator.mediaSession.metadata = new MediaMetadata({ title: ep.title, artist: 'Journey Talk', album: `${ep.japanese_name} · ${ep.date}` });
      navigator.mediaSession.setActionHandler('previoustrack', () => app.querySelector('[data-act="prev"]').click());
      navigator.mediaSession.setActionHandler('nexttrack', () => app.querySelector('[data-act="next"]').click());
    } catch (_) { /* optional */ }
  }

  const stopCoach = timed ? createSpeechCoach({ lines, rows, progress, pause: () => audio.pause(), playLineOnce }) : () => {};

  const close = () => {
    stopCoach();
    save(true);
    audio.pause();
    cancelAnimationFrame(raf);
    audio.removeAttribute('src');
    audio.load();
    document.removeEventListener('keydown', onKey);
    document.removeEventListener('visibilitychange', onHide);
    document.body.classList.remove('has-player');
  };
  // Switch between the streamed and the saved copy without losing the listening position.
  close.swapSource = (url) => {
    const at = audio.currentTime;
    const wasPlaying = !audio.paused;
    audio.src = url;
    audio.addEventListener('loadedmetadata', () => { audio.currentTime = at; if (wasPlaying) play(); }, { once: true });
  };
  return close;
}

/* ---------- shadowing coach: speech recognition + comparison ---------- */
const normalizeSpeech = (text) => String(text || '').normalize('NFKC').toLowerCase().replace(/[\p{P}\p{S}]+/gu, ' ').trim();

// Returns the words (or characters) to compare, plus how to show each one to the learner.
function speechTokens(text, language) {
  const clean = normalizeSpeech(text);
  if (CHAR_LANGS.test(language)) {
    const chars = [...clean.replace(/\s+/g, '')];
    return { keys: chars, shown: chars };
  }
  const shown = clean.split(/\s+/).filter(Boolean);
  // Accents do not change how a word sounds to the recognizer's ear (sí / si, ё / е), so ignore them.
  return { keys: shown.map((w) => w.normalize('NFD').replace(/\p{M}+/gu, '').normalize('NFC')), shown };
}

// Longest common subsequence: which target tokens were heard, in order.
function compareSpeech(target, heard, language) {
  const { keys: want, shown } = speechTokens(target, language);
  const got = speechTokens(heard, language).keys;
  const table = Array.from({ length: want.length + 1 }, () => new Array(got.length + 1).fill(0));
  for (let i = want.length - 1; i >= 0; i -= 1) {
    for (let j = got.length - 1; j >= 0; j -= 1) {
      table[i][j] = want[i] === got[j] ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
    }
  }
  const matched = new Array(want.length).fill(false);
  let i = 0; let j = 0;
  while (i < want.length && j < got.length) {
    if (want[i] === got[j]) { matched[i] = true; i += 1; j += 1; }
    else if (table[i + 1][j] >= table[i][j + 1]) i += 1;
    else j += 1;
  }
  const hits = matched.filter(Boolean).length;
  return { tokens: shown, matched, score: want.length ? Math.round((hits / want.length) * 100) : 0, joiner: CHAR_LANGS.test(language) ? '' : ' ' };
}

function speechVerdict(score) {
  if (score >= 90) return 'すばらしい！ネイティブの耳にも通じます 🎉';
  if (score >= 70) return 'いい感じ！赤い所だけもう一度';
  if (score >= 40) return 'もう少し！お手本を聞いてから再挑戦';
  return 'ゆっくりでOK。1語ずつ区切って言ってみよう';
}

function createSpeechCoach({ lines, rows, progress, pause, playLineOnce }) {
  if (!SpeechRecognitionImpl) return () => {};
  let recognizer = null;
  const stop = () => { if (recognizer) { try { recognizer.abort(); } catch (_) { /* ignore */ } recognizer = null; } };
  const panelFor = (i) => {
    let panel = rows[i].querySelector('.coach');
    if (!panel) {
      panel = document.createElement('div');
      panel.className = 'coach';
      rows[i].appendChild(panel);
    }
    return panel;
  };
  const listen = (i) => {
    stop();
    pause();
    const line = lines[i];
    const panel = panelFor(i);
    rows[i].classList.add('revealed');
    panel.innerHTML = '<div class="coach-status">🎙 聞き取り中… 文を声に出して読んでください</div><div class="coach-heard muted"></div>';
    const heardEl = panel.querySelector('.coach-heard');
    const rec = new SpeechRecognitionImpl();
    recognizer = rec;
    rec.lang = line.language;
    rec.interimResults = true;
    rec.maxAlternatives = 3;
    let finalText = '';
    let best = null;
    rec.onresult = (event) => {
      let interim = '';
      for (let k = event.resultIndex; k < event.results.length; k += 1) {
        const result = event.results[k];
        if (result.isFinal) {
          // Recognizers return a few guesses; keep the one closest to the target sentence.
          for (let a = 0; a < result.length; a += 1) {
            const candidate = (finalText + ' ' + result[a].transcript).trim();
            const graded = compareSpeech(line.text, candidate, line.language);
            if (!best || graded.score > best.graded.score) best = { text: candidate, graded };
          }
          finalText = best.text;
        } else {
          interim += result[0].transcript;
        }
      }
      heardEl.textContent = (finalText + ' ' + interim).trim();
    };
    rec.onerror = (event) => {
      const reason = event.error === 'not-allowed' || event.error === 'service-not-allowed'
        ? 'マイクの使用が許可されていません。ブラウザの設定で許可してください。'
        : event.error === 'no-speech' ? '声が聞き取れませんでした。もう一度どうぞ。' : `音声認識エラー: ${event.error}`;
      panel.innerHTML = `<div class="coach-status">${esc(reason)}</div>`;
      recognizer = null;
    };
    rec.onend = () => {
      if (recognizer !== rec) return;
      recognizer = null;
      if (!best) { panel.innerHTML = '<div class="coach-status">声が聞き取れませんでした。もう一度どうぞ。</div>'; return; }
      const { graded } = best;
      const previous = (progress.speak || {})[i] || 0;
      progress.speak = { ...(progress.speak || {}), [i]: Math.max(previous, graded.score) };
      markActiveToday();
      const diff = graded.tokens.map((token, k) => `<span class="${graded.matched[k] ? 'hit' : 'miss'}">${esc(token)}</span>`).join(graded.joiner);
      panel.innerHTML = `
        <div class="coach-score"><b>${graded.score}</b><span>点</span> ${esc(speechVerdict(graded.score))}${previous ? ` <span class="muted">（ベスト ${Math.max(previous, graded.score)}点）</span>` : ''}</div>
        <div class="coach-diff" lang="${esc(line.language)}">${diff}</div>
        <div class="coach-heard muted">聞き取り: ${esc(best.text)}</div>
        <div class="coach-actions">
          <button class="btn" data-coach="model">🔊 お手本</button>
          <button class="btn primary" data-coach="again">🎤 もう一度</button>
        </div>`;
    };
    try { rec.start(); } catch (err) { panel.innerHTML = `<div class="coach-status">${esc(err.message || err)}</div>`; }
  };
  const onClick = (event) => {
    const mic = event.target.closest('.mic');
    const action = event.target.closest('[data-coach]');
    const row = event.target.closest('.line');
    if (!row) return;
    const i = Number(row.dataset.i);
    if (mic) listen(i);
    else if (action && action.dataset.coach === 'again') listen(i);
    else if (action && action.dataset.coach === 'model') { stop(); playLineOnce(i); }
  };
  rows[0]?.parentElement.addEventListener('click', onClick);
  return stop;
}

/* ---------- word bank with spaced review ---------- */
function renderWords() {
  const words = Object.entries(store.state.words);
  const due = dueWords();
  const langs = new Map(words.map(([, w]) => [w.slug, w.japanese_name]));
  app.innerHTML = `
    ${topbar('words')}
    <h1>単語帳</h1>
    <p class="sub">エピソードで追加した表現を、間隔をあけてくり返し復習します（1→3→7→14→30日）。</p>
    ${statsRow()}
    ${words.length ? `
      <div class="toolbar">
        <button class="btn primary" id="start" ${due.length ? '' : 'disabled'}>復習する（${due.length}語）</button>
        <button class="btn" id="export">Anki用に書き出し（TSV）</button>
      </div>
      <div id="review"></div>
      <ul class="wordlist">${words.map(([id, w]) => `
        <li>
          <span><span class="tag">${FLAGS[w.slug] || '🌐'}</span> <b lang="${esc(w.language)}">${esc(w.term)}</b> <span class="muted">${esc(w.meaning_ja)}</span></span>
          <span class="muted" style="font-size:.78rem;white-space:nowrap">${w.due <= localDay() ? '今日' : esc(w.due.slice(5))}
            <button class="btn" data-del="${esc(id)}" aria-label="削除">✕</button></span>
        </li>`).join('')}
      </ul>` : '<div class="empty">まだ単語がありません。エピソードの「単語」タブから追加しましょう。</div>'}
    ${langs.size > 1 ? `<p class="muted" style="font-size:.8rem">収録言語: ${[...langs.values()].map(esc).join('・')}</p>` : ''}`;

  app.querySelectorAll('[data-del]').forEach((btn) => btn.addEventListener('click', () => {
    delete store.state.words[btn.dataset.del];
    store.save();
    renderWords();
  }));
  const start = document.getElementById('start');
  if (start) start.addEventListener('click', () => review(due));
  const exp = document.getElementById('export');
  if (exp) exp.addEventListener('click', exportTsv);
}

function review(queue) {
  const root = document.getElementById('review');
  let i = 0;
  const next = () => {
    if (i >= queue.length) {
      markActiveToday();
      root.innerHTML = '<div class="flash"><div class="term">🎉</div><p>今日の復習は完了です！</p><button class="btn" id="done">単語帳へ戻る</button></div>';
      document.getElementById('done').addEventListener('click', renderWords);
      return;
    }
    const w = queue[i];
    root.innerHTML = `
      <div class="flash">
        <div class="muted" style="font-size:.8rem">${i + 1} / ${queue.length} · ${esc(w.japanese_name)}</div>
        <div class="term" lang="${esc(w.language)}">${esc(w.term)}</div>
        <div class="back-side" hidden>
          ${w.reading ? `<div class="muted">${esc(w.reading)}</div>` : ''}
          <div style="font-size:1.1rem;margin:6px 0">${esc(w.meaning_ja)}</div>
          ${w.example ? `<div class="muted" lang="${esc(w.language)}">${esc(w.example)}</div><div class="muted">${esc(w.example_ja)}</div>` : ''}
        </div>
        <div class="actions">
          <button class="btn primary" id="flip">答えを見る</button>
        </div>
      </div>`;
    document.getElementById('flip').addEventListener('click', () => {
      root.querySelector('.back-side').hidden = false;
      root.querySelector('.actions').innerHTML = '<button class="btn" id="again">もう一度</button><button class="btn primary" id="good">覚えた</button>';
      document.getElementById('again').addEventListener('click', () => grade(false));
      document.getElementById('good').addEventListener('click', () => grade(true));
    });
  };
  const grade = (good) => {
    const w = queue[i];
    w.box = good ? Math.min(REVIEW_DAYS.length - 1, (w.box || 0) + 1) : 0;
    w.due = addDays(good ? REVIEW_DAYS[w.box] : 0);
    if (!good) queue.push(w);
    store.save();
    i += 1;
    next();
  };
  next();
  root.scrollIntoView({ block: 'start', behavior: 'smooth' });
}

function exportTsv() {
  const clean = (v) => String(v || '').replace(/[\t\r\n]+/g, ' ');
  const rows = Object.values(store.state.words).map((w) => [w.term, w.reading, w.meaning_ja, w.example, w.example_ja, w.japanese_name].map(clean).join('\t'));
  const blob = new Blob([rows.join('\n') + '\n'], { type: 'text/tab-separated-values' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `journey-talk-words-${localDay()}.tsv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

/* ---------- router ---------- */
async function route() {
  if (teardown) { teardown(); teardown = null; }
  const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean).map(decodeURIComponent);
  try {
    if (parts[0] === 'words') renderWords();
    else if (parts.length === 2) await renderEpisode(parts[0], parts[1]);
    else await renderHome();
  } catch (err) {
    const message = navigator.onLine
      ? `読み込みに失敗しました: ${esc(err.message || err)}`
      : '📴 オフラインのため開けません。この回は、オンライン時に一度開くか「⬇ オフライン保存」しておくとオフラインでも使えます。';
    app.innerHTML = `${topbar('')}<div class="empty">${message} <a href="#/">一覧へ戻る</a></div>`;
  }
  window.scrollTo(0, 0);
}
window.addEventListener('hashchange', route);
route();
