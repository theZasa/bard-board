"use strict";

// ---------------------------------------------------------------- utilities

const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, attrs = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === false || v == null) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children) if (c != null) node.append(c);
  return node;
};
const icon = (name, cls = "icon") => {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", cls);
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
};
const fold = (s) =>
  s.toLocaleLowerCase("de").replace(/ß/g, "ss").normalize("NFKD").replace(/[̀-ͯ]/g, "").replace(/[^\p{L}\p{N}]+/gu, " ").trim();
const fmtSeconds = (s) => (s == null ? "" : s < 10 ? `${s.toLocaleString("de", { maximumFractionDigits: 1 })} s` : s < 60 ? `${Math.round(s)} s` : `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`);
const fmtTime = (ts) => new Date(ts * 1000).toLocaleTimeString("de", { hour: "2-digit", minute: "2-digit" });
const storage = {
  get(key, fallback) { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private mode */ } },
};

function toast(message, kind = "") {
  const node = el("div", { class: `toast ${kind}`, text: message });
  $("#toasts").append(node);
  setTimeout(() => node.remove(), kind === "err" ? 6000 : 3000);
}

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) {
    opts.body = JSON.stringify(body);
    opts.headers["Content-Type"] = "application/json";
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch {
    toast("Server nicht erreichbar", "err");
    throw new Error("network");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    if (data.error) toast(data.error, "err");
    const err = new Error(data.error || res.statusText);
    err.data = data;
    throw err;
  }
  return data;
}

// -------------------------------------------------------------------- state

const state = {
  sounds: new Map(),
  categories: [],
  playing: [],
  playingReceived: 0,
  status: null,
  volume: 70,
  activity: [],
  prefix: "/",
  maxUploadMb: 50,
  music: { state: "idle", current: null, queue: [], position: 0, volume: 50 },
  musicReceived: 0,
  maxClipSeconds: 60,
  extras: { joinsounds: [], aliases: [], radio: [], pull: null, tts: { available: false, reason: "" } },
  clipCategory: "YouTube Clips",
  collections: [],
};
const view = {
  query: "",
  chip: storage.get("chip", "all"),
  sort: storage.get("sort", "name"),
  menuSound: null,
  mode: storage.get("mode", "board"), // "board" or the id of a collection
};
// Search state of the collection view (results are paged from the server, never all at once)
const coll = { q: "", group: storage.get("collGroup", ""), mood: storage.get("collMood", ""), items: [], total: 0, seq: 0, loading: false, groupFilter: "" };

// ------------------------------------------------------------------ theming

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $("#theme-toggle use").setAttribute("href", theme === "dark" ? "#i-sun" : "#i-moon");
}
applyTheme(storage.get("theme", matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"));
$("#theme-toggle").addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  storage.set("theme", next);
  applyTheme(next);
});

const app = $(".app");
if (storage.get("sidebarHidden", false)) app.classList.add("no-sidebar");
$("#panel-toggle").addEventListener("click", () => {
  app.classList.toggle("no-sidebar");
  storage.set("sidebarHidden", app.classList.contains("no-sidebar"));
});

// --------------------------------------------------------------- rendering

// Categories are folder paths. The first segment is a top-level chip, deeper folders become a second
// row of chips under their top-level folder. Chip ids: "all", "fav", "top:<A>" (A and everything below),
// "dir:<A>" (only directly in A), "sub:<A/B>" (exactly that folder).
const topOf = (cat) => cat.split("/")[0];

function chipMatches(chip, s) {
  if (chip === "all") return true;
  if (chip === "fav") return s.favorite;
  const kind = chip.slice(0, 3);
  const path = chip.slice(4);
  if (kind === "top") return s.category === path || s.category.startsWith(`${path}/`);
  if (kind === "dir" || kind === "sub") return s.category === path;
  return false;
}

function searchMatches(list) {
  if (!view.query) return list;
  const words = fold(view.query).split(" ").filter(Boolean);
  return list.filter((s) => words.every((w) => s._folded.includes(w)));
}

function visibleSounds() {
  let list = searchMatches([...state.sounds.values()]).filter((s) => chipMatches(view.chip, s));
  if (view.query) {
    // Prefix matches first, so Enter plays the most likely sound.
    const q = fold(view.query);
    list.sort((a, b) => Number(!a._foldedName.startsWith(q)) - Number(!b._foldedName.startsWith(q)));
  }
  const byName = (a, b) => a.name.localeCompare(b.name, "de", { sensitivity: "base", numeric: true });
  const sorters = {
    name: byName,
    plays: (a, b) => b.plays - a.plays || byName(a, b),
    recent: (a, b) => (b.last_played || 0) - (a.last_played || 0) || byName(a, b),
    duration: (a, b) => (a.duration ?? 1e9) - (b.duration ?? 1e9) || byName(a, b),
  };
  if (!view.query) list.sort(sorters[view.sort] || byName);
  else if (view.sort !== "name") list.sort(sorters[view.sort]);
  return list;
}

function selectChip(id) {
  view.chip = id;
  storage.set("chip", id);
  renderChips();
  renderGrid();
}

function chipButton(item, active, small = false) {
  return el(
    "button",
    {
      class: `chip${small ? " chip-sm" : ""}${active ? " active" : ""}`,
      type: "button",
      "aria-pressed": String(active),
      title: item.title || null,
      onclick: () => selectChip(item.id),
    },
    item.icon ? icon(item.icon) : null,
    item.label,
    el("span", { class: "count", text: String(item.count) }),
    item.nested ? icon("chevron", "icon chev") : null
  );
}

function renderChips() {
  const all = [...state.sounds.values()];
  const pool = searchMatches(all); // while searching, counts are hits and empty folders disappear
  const searching = Boolean(view.query);
  const count = (pred) => pool.reduce((n, s) => n + (pred(s) ? 1 : 0), 0);
  const tops = [...new Set(state.categories.filter(Boolean).map(topOf))].sort((a, b) => a.localeCompare(b, "de", { sensitivity: "base", numeric: true }));
  const subsOf = (top) => state.categories.filter((c) => c.startsWith(`${top}/`));
  const valid = new Set(["all", "fav", ...tops.map((t) => `top:${t}`), ...tops.map((t) => `dir:${t}`), ...state.categories.filter((c) => c.includes("/")).map((c) => `sub:${c}`)]);
  if (!valid.has(view.chip) || (view.chip === "fav" && !all.some((s) => s.favorite))) view.chip = "all";
  const activeTop = view.chip.startsWith("top:") || view.chip.startsWith("dir:") || view.chip.startsWith("sub:") ? topOf(view.chip.slice(4)) : null;
  const visible = (item) => !searching || item.count > 0 || item.id === view.chip || item.id === `top:${activeTop}`;

  const items = [{ id: "all", label: "Alle", count: pool.length }];
  const favCount = count((s) => s.favorite);
  if (all.some((s) => s.favorite)) items.push({ id: "fav", label: "Favoriten", count: favCount, icon: "star" });
  for (const t of tops) {
    items.push({ id: `top:${t}`, label: t, count: count((s) => chipMatches(`top:${t}`, s)), icon: "folder", nested: subsOf(t).length > 0 });
  }
  const shown = items.filter((item) => item.id === "all" || visible(item));
  const chips = $("#chips");
  chips.replaceChildren(...shown.map((item) => chipButton(item, item.id === view.chip || item.id === `top:${activeTop}`)));
  chips.hidden = items.length <= 1;

  // Second row: the folders below the selected top-level folder
  const sub = $("#subchips");
  const subs = activeTop ? subsOf(activeTop) : [];
  sub.hidden = subs.length === 0;
  if (!subs.length) return sub.replaceChildren();
  const subItems = [{ id: `top:${activeTop}`, label: `Alle in ${activeTop}`, count: count((s) => chipMatches(`top:${activeTop}`, s)) }];
  if (state.categories.includes(activeTop)) subItems.push({ id: `dir:${activeTop}`, label: "Ohne Unterordner", count: count((s) => s.category === activeTop) });
  for (const c of subs) subItems.push({ id: `sub:${c}`, label: c.slice(activeTop.length + 1), title: c, count: count((s) => s.category === c) });
  sub.replaceChildren(...subItems.filter((item, i) => i === 0 || visible(item)).map((item) => chipButton(item, item.id === view.chip, true)));
}

function tileMeta(s) {
  const parts = [];
  // Lines from a collection show who speaks; other tiles their folder unless the filter makes it obvious
  const exactFolder = view.chip.startsWith("sub:") || view.chip.startsWith("dir:");
  const folder = view.chip.startsWith("top:") ? s.category.slice(view.chip.length - 3) : s.category;
  if (s.speaker) parts.push(el("span", { class: "cat", text: s.speaker, title: `${s.category} · ${s.speaker}` }));
  else if (folder && !exactFolder) parts.push(el("span", { class: "cat", text: folder, title: s.category }));
  if (s.duration != null) parts.push(el("span", { text: fmtSeconds(s.duration) }));
  if (s.plays) parts.push(el("span", { text: `${s.plays}×`, title: `${s.plays}× gespielt` }));
  if (s.volume !== 100) parts.push(el("span", { class: "vol", text: `${s.volume} %`, title: "Eigene Lautstärke" }));
  const out = [];
  parts.forEach((p, i) => {
    if (i) out.push(el("span", { class: "sep", text: "·" }));
    out.push(p);
  });
  return out;
}

function buildTile(s) {
  const fav = el(
    "button",
    {
      class: `tile-btn${s.favorite ? " fav" : ""}`,
      type: "button",
      title: s.favorite ? "Favorit entfernen" : "Als Favorit markieren",
      "aria-label": "Favorit",
      onclick: (e) => {
        e.stopPropagation();
        api("PATCH", `/api/sounds/${s.id}`, { favorite: !s.favorite }).catch(() => {});
      },
    },
    icon("star")
  );
  const more = el(
    "button",
    {
      class: "tile-btn",
      type: "button",
      title: "Optionen",
      "aria-label": "Optionen",
      onclick: (e) => {
        e.stopPropagation();
        const r = e.currentTarget.getBoundingClientRect();
        openMenu(s, r.right, r.bottom + 4, true);
      },
    },
    icon("more")
  );
  const tile = el(
    "div",
    {
      class: "tile",
      role: "button",
      tabindex: "0",
      "data-id": s.id,
      title: [s.text || s.name, s.speaker, s.category].filter(Boolean).join(" · "),
      onclick: () => play(s.id, tile),
      onkeydown: (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          play(s.id, tile);
        }
      },
      oncontextmenu: (e) => {
        e.preventDefault();
        openMenu(s, e.clientX, e.clientY);
      },
    },
    el("div", { class: "tile-name", text: s.name }),
    el("div", { class: "tile-meta" }, ...tileMeta(s)),
    el("div", { class: "tile-actions" }, fav, more),
    el("div", { class: "progress" })
  );
  return tile;
}

function renderGrid() {
  const grid = $("#grid");
  const list = visibleSounds();
  grid.replaceChildren(...list.map(buildTile));
  const empty = $("#empty");
  if (!list.length) {
    empty.replaceChildren();
    if (!state.sounds.size) {
      empty.append(el("div", {}, el("strong", { text: "Noch keine Sounds" }), "Zieh Audiodateien hierher oder nutze „Hochladen“."));
    } else {
      empty.append(el("div", {}, el("strong", { text: "Nichts gefunden" }), view.query ? `Kein Sound passt zu „${view.query}“.` : "Diese Auswahl ist leer."));
    }
  }
  empty.hidden = list.length > 0;
  grid.hidden = list.length === 0;
  markPlaying();
}

function updateTile(s) {
  const old = $(`.tile[data-id="${s.id}"]`);
  if (old) old.replaceWith(buildTile(s));
  markPlaying();
}

function markPlaying() {
  const active = new Set(state.playing.map((v) => v.sound_id));
  for (const tile of document.querySelectorAll(".tile")) {
    const on = active.has(tile.dataset.id);
    tile.classList.toggle("playing", on);
    if (!on) tile.querySelector(".progress").style.transform = "scaleX(0)";
  }
  for (const line of document.querySelectorAll(".line")) line.classList.toggle("playing", active.has(line.dataset.id));
}

function renderVoices() {
  const list = $("#voices");
  list.replaceChildren(
    ...state.playing.map((v) =>
      el(
        "li",
        { class: "voice", "data-voice": v.id },
        el("div", { class: "voice-name", text: v.name, title: v.name }),
        el("div", { class: "voice-meta", text: `${v.source === "web" ? "Web" : v.requested_by} · ${fmtSeconds(v.duration)}` }),
        el(
          "button",
          { class: "btn btn-icon", type: "button", title: "Stoppen", "aria-label": `${v.name} stoppen`, onclick: () => api("POST", "/api/stop", { voice_id: v.id }).catch(() => {}) },
          icon("stop")
        ),
        el("div", { class: "voice-bar" }, el("span"))
      )
    )
  );
  $("#voices-empty").hidden = state.playing.length > 0;
  markPlaying();
  if (state.playing.length) startProgressLoop();
}

let rafId = 0;
function startProgressLoop() {
  if (rafId) return;
  const tick = () => {
    const now = performance.now();
    const passed = (now - state.playingReceived) / 1000;
    const perSound = new Map();
    for (const v of state.playing) {
      const p = Math.min(1, (v.elapsed + passed) / Math.max(v.duration, 0.01));
      perSound.set(v.sound_id, Math.max(perSound.get(v.sound_id) || 0, p));
      const bar = document.querySelector(`.voice[data-voice="${v.id}"] .voice-bar span`);
      if (bar) bar.style.transform = `scaleX(${p})`;
    }
    for (const [id, p] of perSound) {
      const bar = document.querySelector(`.tile[data-id="${id}"] .progress`);
      if (bar) bar.style.transform = `scaleX(${p})`;
    }
    rafId = state.playing.length ? requestAnimationFrame(tick) : 0;
  };
  rafId = requestAnimationFrame(tick);
}

function renderStatus() {
  const st = state.status;
  const node = $("#status");
  const text = $(".status-text", node);
  node.classList.remove("ok", "err");
  if (!st) {
    text.textContent = "Verbinde ...";
  } else if (st.connected) {
    node.classList.add("ok");
    text.textContent = `${st.username} · ${st.channel || (st.platform === "discord" ? "kein Sprachkanal" : "Root")}`;
    node.title = [`Verbunden mit ${st.server}`, st.server_release, st.bitrate ? `${Math.round(st.bitrate / 1000)} kbit/s` : null].filter(Boolean).join(" · ");
  } else {
    node.classList.add("err");
    text.textContent = st.invite_needed ? "Wartet auf Einladung"
      : st.last_error ? `Getrennt: ${st.last_error}` : `Verbinde mit ${st.server} ...`;
    node.title = st.last_error || "";
  }

  renderPlatforms();
  renderNotice();
  const users = $("#users");
  users.replaceChildren();
  if (st && st.connected) {
    users.append(el("li", {}, icon("user"), el("span", { class: "bot", text: `${st.username} (Bot)` })));
    for (const u of st.users) {
      users.append(el("li", {}, icon("user"), el("span", { text: u.name }), u.muted || u.deaf ? icon("mic-off", "icon off") : null));
    }
  }
  const follow = $("#follow");
  if (document.activeElement !== follow) follow.value = st?.follow_user || "";
  $("#follow-btn").textContent = st?.follow_user ? "Entfolgen" : "Folgen";
  loadChannels();
}

let channelsKey = "";
async function loadChannels() {
  const select = $("#channel");
  const st = state.status;
  if (!st || !st.connected) {
    select.replaceChildren(el("option", { text: "Nicht verbunden" }));
    select.disabled = true;
    channelsKey = "";
    return;
  }
  let channels;
  try {
    channels = await api("GET", "/api/channels");
  } catch {
    return;
  }
  const key = JSON.stringify([channels, st.channel_id]);
  if (key === channelsKey) return;
  channelsKey = key;
  select.disabled = false;
  select.replaceChildren(
    ...channels.map((c) =>
      el("option", { value: c.id, selected: c.id === st.channel_id, text: `${c.path}${c.users ? `  (${c.users})` : ""}` })
    )
  );
}

function renderVolume() {
  const input = $("#volume");
  if (document.activeElement !== input) input.value = state.volume;
  input.style.setProperty("--fill", `${input.value}%`);
  $("#volume-value").textContent = `${input.value} %`;
}

function activityItem(a) {
  return el(
    "li",
    { class: `src-${a.source}` },
    el("time", { text: fmtTime(a.ts), datetime: new Date(a.ts * 1000).toISOString() }),
    el("span", {}, el("span", { class: "who", text: a.user }), ` ${a.text}`)
  );
}

function renderActivity() {
  $("#activity").replaceChildren(...state.activity.slice(-40).reverse().map(activityItem));
}

const PLATFORM_ORDER = ["mumble", "discord"];
// The other bot's web interface: a configured URL (server) or the same host with another port (PC).
const peerUrl = (peer) => peer.url || `${location.protocol}//${location.hostname}:${peer.port}/`;

// "Mumble | Discord": the current platform and, when configured, a link to the other web interface.
function renderPlatforms() {
  const st = state.status;
  if (!st?.platform_name) return;
  document.title = `Soundboard · ${st.platform_name}`;
  const items = [{ platform: st.platform, name: st.platform_name, current: true }];
  if (state.peer) items.push({ ...state.peer, current: false });
  items.sort((a, b) => PLATFORM_ORDER.indexOf(a.platform) - PLATFORM_ORDER.indexOf(b.platform));
  const nav = $("#platforms");
  nav.replaceChildren(...items.map((it) => it.current
    ? el("span", { class: "seg", "data-platform": it.platform, "aria-current": "page", text: it.name })
    : el("a", {
        class: `seg${state.peerOnline === false ? " offline" : ""}`,
        "data-platform": it.platform,
        href: peerUrl(it),
        title: state.peerOnline === false
          ? `Das ${it.name}-Soundboard läuft gerade nicht`
          : `Zum ${it.name}-Soundboard wechseln`,
        text: it.name,
      })));
  nav.classList.toggle("single", items.length === 1);
  nav.hidden = false;
}

async function checkPeer() {
  if (!state.peer) return;
  let online = true;
  try {
    // no-cors: the answer itself is unreadable, but a refused connection still fails.
    await fetch(new URL("healthz", peerUrl(state.peer)), { mode: "no-cors", cache: "no-store" });
  } catch {
    online = false;
  }
  if (online !== state.peerOnline) {
    state.peerOnline = online;
    renderPlatforms();
  }
}

function renderNotice() {
  const st = state.status;
  const text = st?.notice || (st?.invite_needed ? "Der Bot ist noch auf keinem Discord-Server." : "");
  $("#notice").hidden = !text;
  $("#notice-text").textContent = text;
  const invite = $("#notice-invite");
  invite.hidden = !(st?.invite_url && st.invite_needed);
  if (st?.invite_url) invite.href = st.invite_url;
}

function renderHelp() {
  const p = state.prefix;
  const items = [
    [`${p}sb name`, "abspielen"],
    [`${p}name`, "Kurzform"],
    [`${p}random`, "Zufall"],
    [`${p}stop`, "stoppen"],
    [`${p}list`, "alle Sounds"],
    [`${p}play link`, "Musik"],
    [`${p}skip`, "nächster Song"],
    [`${p}queue`, "Warteschlange"],
    [`${p}clip link 1:23 1:27`, "Clip"],
    [`${p}radio name`, "Sender"],
    [`${p}tts text`, "vorlesen"],
    [`${p}pull 10`, "Pull-Timer"],
    [`${p}joinsound name`, "Join-Sound"],
    [`${p}alias lr leeroy`, "Kürzel"],
    ...state.collections.filter((c) => c.commands.length).map((c) => [`${p}${c.commands[0]} text`, `Zeile aus ${c.name}`]),
    [`${p}help`, "alle Befehle"],
    ...(state.status?.platform === "discord" ? [["/sb", "auch als Slash-Befehl"]] : []),
  ];
  $("#help").replaceChildren(...items.flatMap(([c, t], i) => [i ? " · " : "", el("code", { text: c }), ` ${t}`]));
  $("#version").textContent = state.version ?? "";
}

// ------------------------------------------------------------------ actions

async function play(id, tile) {
  tile?.classList.remove("flash");
  void tile?.offsetWidth;
  tile?.classList.add("flash");
  try {
    await api("POST", `/api/sounds/${id}/play`);
  } catch {
    /* toast already shown */
  }
}

function stopAll() {
  api("POST", "/api/stop", {}).catch(() => {});
}

let volumeTimer = 0;
$("#volume").addEventListener("input", (e) => {
  renderVolume();
  clearTimeout(volumeTimer);
  volumeTimer = setTimeout(() => api("POST", "/api/volume", { volume: Number(e.target.value) }).catch(() => {}), 120);
});

$("#stop-all").addEventListener("click", stopAll);
$("#random").addEventListener("click", () => {
  const list = visibleSounds();
  if (!list.length) return toast("Keine Sounds in der Auswahl");
  const s = list[Math.floor(Math.random() * list.length)];
  play(s.id, $(`.tile[data-id="${s.id}"]`));
});

const search = $("#search");
function setQuery(value) {
  search.value = value;
  view.query = value.trim();
  $("#search-clear").hidden = !value;
  $("#search-kbd").hidden = Boolean(value);
  renderChips();
  renderGrid();
}
search.addEventListener("input", () => setQuery(search.value));
$("#search-clear").addEventListener("click", () => {
  setQuery("");
  search.focus();
});
search.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    const first = visibleSounds()[0];
    if (first) play(first.id, $(`.tile[data-id="${first.id}"]`));
  } else if (e.key === "Escape" && search.value) {
    e.stopPropagation();
    setQuery("");
  }
});

const sort = $("#sort");
sort.value = view.sort;
sort.addEventListener("change", () => {
  view.sort = sort.value;
  storage.set("sort", view.sort);
  renderGrid();
});

$("#channel").addEventListener("change", (e) => {
  api("POST", "/api/join", { channel_id: e.target.value }).catch(() => {});
});
$("#follow-btn").addEventListener("click", () => {
  const following = state.status?.follow_user;
  const user = following ? "" : $("#follow").value.trim();
  if (!following && !user) {
    toast("Namen eingeben, dem der Bot folgen soll");
    $("#follow").focus();
    return;
  }
  api("POST", "/api/follow", { user }).catch(() => {});
});
$("#follow").addEventListener("keydown", (e) => {
  if (e.key === "Enter") api("POST", "/api/follow", { user: e.target.value.trim() }).catch(() => {});
});

document.addEventListener("keydown", (e) => {
  const typing = ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName);
  if (e.key === "Escape") {
    if (!$("#menu").hidden) return closeMenu();
    if ($("#dialog").open || $("#settings").open) return;
    if (document.activeElement === search) search.blur();
    stopAll();
  } else if (!typing && (e.key === "/" || (e.key === "k" && (e.ctrlKey || e.metaKey)))) {
    e.preventDefault();
    const field = currentMode() === "board" ? search : $("#coll-search");
    field.focus();
    field.select();
  }
});

// --------------------------------------------------------------------- menu

const preview = $("#preview");
let previewId = null;
preview.addEventListener("ended", () => (previewId = null));

function closeMenu() {
  $("#menu").hidden = true;
  view.menuSound = null;
}

function openMenu(s, x, y, alignRight = false) {
  const menu = $("#menu");
  view.menuSound = s;
  const volOut = el("output", { text: `${s.volume} %` });
  const vol = el("input", { type: "range", min: "0", max: "200", step: "5", value: String(s.volume), "aria-label": "Lautstärke dieses Sounds" });
  vol.style.setProperty("--fill", `${(s.volume / 200) * 100}%`);
  vol.addEventListener("input", () => {
    volOut.textContent = `${vol.value} %`;
    vol.style.setProperty("--fill", `${(vol.value / 200) * 100}%`);
  });
  vol.addEventListener("change", () => api("PATCH", `/api/sounds/${s.id}`, { volume: Number(vol.value) }).catch(() => {}));

  const item = (iconName, label, action, cls = "") =>
    el("button", { class: `menu-item ${cls}`, type: "button", role: "menuitem", onclick: () => { closeMenu(); action(); } }, icon(iconName), label);

  const previewing = previewId === s.id && !preview.paused;
  menu.replaceChildren(
    el("div", { class: "menu-title", text: s.name, title: s.file }),
    item("play", "Im Kanal abspielen", () => play(s.id, $(`.tile[data-id="${s.id}"]`))),
    item("headphones", previewing ? "Vorhören stoppen" : "Lokal vorhören", () => {
      if (previewing) {
        preview.pause();
        previewId = null;
      } else {
        preview.src = `/api/sounds/${s.id}/audio`;
        preview.play().catch(() => toast("Vorhören nicht möglich", "err"));
        previewId = s.id;
      }
    }),
    el("div", { class: "menu-volume" }, icon("volume"), vol, volOut),
    el("div", { class: "menu-sep" }),
    item("star", s.favorite ? "Favorit entfernen" : "Als Favorit", () => api("PATCH", `/api/sounds/${s.id}`, { favorite: !s.favorite }).catch(() => {})),
    item("user-plus", "Als Join-Sound ...", () => joinSoundDialog(s)),
    item("edit", "Umbenennen", () => renameDialog(s)),
    item("folder", "Kategorie ändern", () => categoryDialog(s)),
    el("div", { class: "menu-sep" }),
    item("trash", "In den Papierkorb", () => deleteDialog(s), "danger")
  );
  menu.hidden = false;
  const r = menu.getBoundingClientRect();
  let left = alignRight ? x - r.width : x;
  let top = y;
  left = Math.max(8, Math.min(left, innerWidth - r.width - 8));
  if (top + r.height > innerHeight - 8) top = Math.max(8, y - r.height - (alignRight ? 36 : 0));
  menu.style.left = `${left}px`;
  menu.style.top = `${top}px`;
  menu.querySelector(".menu-item")?.focus();
}

document.addEventListener("pointerdown", (e) => {
  if (!$("#menu").hidden && !$("#menu").contains(e.target)) closeMenu();
});
addEventListener("resize", closeMenu);
$("#grid").addEventListener("scroll", closeMenu, { passive: true });

// ------------------------------------------------------------------ dialogs

function dialog(title, fields, okLabel = "Speichern", danger = false) {
  return new Promise((resolve) => {
    const dlg = $("#dialog");
    $("#dialog-title").textContent = title;
    $("#dialog-body").replaceChildren(...fields);
    const ok = $("#dialog-ok");
    ok.textContent = okLabel;
    ok.classList.toggle("btn-danger", danger);
    ok.classList.toggle("btn-primary", !danger);
    dlg.returnValue = "";
    dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true });
    dlg.showModal();
    dlg.querySelector("input")?.select();
  });
}

async function renameDialog(s) {
  const input = el("input", { type: "text", value: s.name, required: true, maxlength: "120", id: "f-name" });
  if (await dialog("Sound umbenennen", [el("label", { for: "f-name", text: "Name" }), input])) {
    const name = input.value.trim();
    if (name && name !== s.name) api("PATCH", `/api/sounds/${s.id}`, { name }).then(() => toast("Umbenannt", "ok")).catch(() => {});
  }
}

async function categoryDialog(s) {
  const list = el("datalist", { id: "cats" }, ...state.categories.filter(Boolean).map((c) => el("option", { value: c })));
  const input = el("input", { type: "text", value: s.category, list: "cats", id: "f-cat", placeholder: "Ohne Kategorie" });
  const hint = el("p", { class: "muted small", text: "Kategorien sind Unterordner im Sounds-Ordner. Leer lassen für keine Kategorie." });
  if (await dialog("Kategorie ändern", [el("label", { for: "f-cat", text: "Kategorie" }), input, list, hint])) {
    const category = input.value.trim();
    if (category !== s.category) api("PATCH", `/api/sounds/${s.id}`, { category }).then(() => toast("Verschoben", "ok")).catch(() => {});
  }
}

async function deleteDialog(s) {
  const text = el("p", { text: `„${s.name}“ wird in den Papierkorb (data/papierkorb) verschoben und kann dort wiederhergestellt werden.` });
  if (await dialog("In den Papierkorb?", [text], "Verschieben", true)) {
    api("DELETE", `/api/sounds/${s.id}`).then(() => toast("In den Papierkorb verschoben", "ok")).catch(() => {});
  }
}

// ------------------------------------------------------------------- upload

async function uploadFiles(files) {
  files = [...files];
  if (!files.length) return;
  const tooBig = files.filter((f) => f.size > state.maxUploadMb * 1024 * 1024);
  if (tooBig.length) toast(`${tooBig.map((f) => f.name).join(", ")}: größer als ${state.maxUploadMb} MB`, "err");
  files = files.filter((f) => !tooBig.includes(f));
  if (!files.length) return;
  const form = new FormData();
  for (const f of files) form.append("files", f, f.name);
  if (/^(top|dir|sub):/.test(view.chip)) form.append("category", view.chip.slice(4));
  toast(`Lade ${files.length} Datei(en) hoch ...`);
  try {
    const res = await api("POST", "/api/upload", form);
    toast(`${res.added.length} Sound(s) hinzugefügt`, "ok");
    for (const e of res.errors) toast(e, "err");
  } catch (err) {
    for (const e of err.data?.errors || []) toast(e, "err");
  }
}

$("#upload-btn").addEventListener("click", () => $("#file-input").click());
$("#file-input").addEventListener("change", (e) => {
  uploadFiles(e.target.files);
  e.target.value = "";
});

let dragDepth = 0;
const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
addEventListener("dragenter", (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault();
  dragDepth++;
  $("#drop-overlay").hidden = false;
});
addEventListener("dragover", (e) => {
  if (hasFiles(e)) e.preventDefault();
});
addEventListener("dragleave", (e) => {
  if (!hasFiles(e)) return;
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) $("#drop-overlay").hidden = true;
});
addEventListener("drop", (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault();
  dragDepth = 0;
  $("#drop-overlay").hidden = true;
  uploadFiles(e.dataTransfer.files);
});

// -------------------------------------------------------------------- music

const fmtClock = (s) => {
  if (s == null || !isFinite(s)) return "–:––";
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
};

function musicPosition() {
  const m = state.music;
  if (!m.current) return 0;
  let pos = m.position || 0;
  if (m.state === "playing") pos += (performance.now() - state.musicReceived) / 1000;
  return m.current.duration ? Math.min(pos, m.current.duration) : pos;
}

function renderMusic() {
  const m = state.music;
  const player = $("#player");
  player.dataset.state = m.state;
  const cur = m.current;
  const title = $("#player-title");
  const art = $("#player-art");
  if (cur) {
    title.textContent = cur.title;
    title.title = cur.title;
    if (/^https?:\/\//.test(cur.webpage || "")) title.href = cur.webpage;
    else title.removeAttribute("href");
    const status = { loading: "Lädt ...", paused: "Pausiert", playing: "" }[m.state] || "";
    const station = cur.is_live ? (state.extras.radio || []).find((r) => r.url === cur.webpage) : null;
    const parts = [status, station ? `Sender ${station.name}` : null, cur.uploader, `von ${cur.requested_by}`].filter(Boolean);
    $("#player-sub").textContent = parts.join(" · ");
    art.style.backgroundImage = cur.thumbnail ? `url("${cur.thumbnail.replace(/"/g, "%22")}")` : "";
    art.classList.toggle("has-image", Boolean(cur.thumbnail));
  } else {
    title.removeAttribute("href");
    art.style.backgroundImage = "";
    art.classList.remove("has-image");
    if (m.queue.length && m.state === "paused") {
      title.textContent = "Pausiert";
      $("#player-sub").textContent = `${m.queue.length} Titel in der Warteschlange`;
    } else {
      title.textContent = "Keine Musik";
      $("#player-sub").textContent = "YouTube-Link, Playlist oder Suchbegriff eingeben";
    }
    title.title = "";
  }
  const toggle = $("#music-toggle");
  const canPlay = Boolean(cur || m.queue.length);
  const playing = m.state === "playing" || m.state === "loading";
  toggle.disabled = !canPlay;
  toggle.querySelector("use").setAttribute("href", playing ? "#i-pause" : "#i-play");
  toggle.title = playing ? "Pause" : "Abspielen";
  toggle.setAttribute("aria-label", toggle.title);
  $("#music-skip").disabled = !cur;
  const live = Boolean(cur?.is_live);
  player.classList.toggle("live", live);
  $("#music-duration").textContent = live ? "LIVE" : cur ? fmtClock(cur.duration) : "0:00";
  $("#music-duration").title = live ? "Livestream: läuft, bis jemand überspringt" : "";
  const vol = $("#music-volume");
  if (document.activeElement !== vol) vol.value = m.volume;
  vol.style.setProperty("--fill", `${vol.value}%`);
  $("#music-volume-value").textContent = `${vol.value} %`;
  renderMusicProgress();
  if (m.state === "playing") startMusicLoop();
}

function renderMusicProgress() {
  const cur = state.music.current;
  const pos = musicPosition();
  $("#music-time").textContent = cur ? fmtClock(pos) : "0:00";
  const p = cur?.is_live ? 1 : cur && cur.duration ? Math.min(1, pos / cur.duration) : 0;
  $("#music-bar span").style.transform = `scaleX(${p})`;
}

let musicTimer = 0;
function startMusicLoop() {
  if (musicTimer) return;
  musicTimer = setInterval(() => {
    if (state.music.state !== "playing") {
      clearInterval(musicTimer);
      musicTimer = 0;
    }
    renderMusicProgress();
  }, 250);
}

function renderQueue() {
  const q = state.music.queue;
  const list = $("#queue");
  list.replaceChildren(
    ...q.map((t, i) =>
      el(
        "li",
        { draggable: "true", "data-id": t.id },
        el("span", { class: "grip", title: "Ziehen zum Umsortieren" }, icon("grip")),
        el(
          "div",
          { class: "q-main" },
          el("span", { class: "q-title", text: `${i + 1}. ${t.title}`, title: t.title }),
          el("span", { class: "q-meta" }, t.is_live ? el("span", { class: "live-badge", text: "LIVE" }) : null, [t.is_live ? null : t.duration ? fmtClock(t.duration) : null, t.requested_by].filter(Boolean).join(" · "))
        ),
        el(
          "button",
          { class: "btn btn-icon btn-sm", type: "button", title: "Entfernen", "aria-label": `${t.title} entfernen`, onclick: () => api("DELETE", `/api/music/queue/${t.id}`).catch(() => {}) },
          icon("close")
        )
      )
    )
  );
  $("#queue-empty").hidden = q.length > 0;
  $("#queue-count").textContent = q.length ? `(${q.length})` : "";
  $("#queue-shuffle").disabled = q.length < 2;
  $("#queue-clear").disabled = q.length === 0;
}

function setMusic(m) {
  state.music = m;
  state.musicReceived = performance.now();
  renderMusic();
  renderQueue();
  renderStations();
}

// Saved livestreams: one click starts a station (switching from another station right away)
function renderStations() {
  const list = state.extras.radio || [];
  const cur = state.music.current;
  const box = $("#stations");
  box.replaceChildren(
    ...list.map((r) => {
      const onAir = Boolean(cur && cur.webpage === r.url);
      return el(
        "button",
        {
          class: `chip chip-sm station${onAir ? " active" : ""}`,
          type: "button",
          title: `${r.title}${onAir ? " (läuft)" : ""}`,
          "aria-pressed": String(onAir),
          onclick: async () => {
            try {
              const res = await api("POST", `/api/radio/${encodeURIComponent(r.name)}/play`);
              toast(res.already ? `${r.name} läuft schon` : `Sender ${r.name}: ${r.title}`, "ok");
            } catch {
              /* toast shown by api() */
            }
          },
        },
        icon("radio"),
        r.name
      );
    }),
    el("button", { class: "chip chip-sm station-add", type: "button", title: "Sender speichern", "aria-label": "Sender speichern", onclick: stationDialog }, icon("plus"), list.length ? null : "Sender")
  );
}

async function stationDialog() {
  const cur = state.music.current;
  const field = (id, label, attrs) => [el("label", { for: id, text: label }), el("input", { id, type: "text", ...attrs })];
  const fields = [
    ...field("f-radio-name", "Name", { required: true, maxlength: "24", placeholder: "z. B. lofi" }),
    ...field("f-radio-url", "Link zum Livestream", { required: true, value: cur?.is_live ? cur.webpage : "", placeholder: "https://www.youtube.com/watch?v=..." }),
    el("p", { class: "muted small", text: "Danach startet ihn ein Klick hier oder /radio name im Chat." }),
  ];
  if (!(await dialog("Sender speichern", fields, "Speichern"))) return;
  saveStation($("#f-radio-name").value, $("#f-radio-url").value);
}

async function saveStation(name, url) {
  name = name.trim().toLowerCase();
  toast("Prüfe den Link ...");
  try {
    await api("PUT", "/api/radio", { name, url: url.trim() });
    toast(`Sender ${name} gespeichert`, "ok");
    return true;
  } catch {
    return false; /* toast shown by api() */
  }
}

// Drag & drop reordering of the queue
let dragId = null;
$("#queue").addEventListener("dragstart", (e) => {
  const li = e.target.closest("li");
  if (!li) return;
  dragId = Number(li.dataset.id);
  li.classList.add("dragging");
  e.dataTransfer.effectAllowed = "move";
  e.dataTransfer.setData("text/plain", String(dragId));
});
$("#queue").addEventListener("dragend", () => {
  dragId = null;
  document.querySelectorAll(".queue li").forEach((li) => li.classList.remove("dragging", "drop-before", "drop-after"));
});
$("#queue").addEventListener("dragover", (e) => {
  if (dragId == null) return;
  const li = e.target.closest("li");
  if (!li) return;
  e.preventDefault();
  e.stopPropagation();
  const r = li.getBoundingClientRect();
  const after = e.clientY > r.top + r.height / 2;
  document.querySelectorAll(".queue li").forEach((x) => x.classList.remove("drop-before", "drop-after"));
  li.classList.add(after ? "drop-after" : "drop-before");
});
$("#queue").addEventListener("drop", (e) => {
  if (dragId == null) return;
  const li = e.target.closest("li");
  if (!li) return;
  e.preventDefault();
  e.stopPropagation();
  const ids = state.music.queue.map((t) => t.id);
  const from = ids.indexOf(dragId);
  let to = ids.indexOf(Number(li.dataset.id));
  if (li.classList.contains("drop-after")) to += 1;
  if (from < to) to -= 1;
  if (from !== to && from >= 0) api("POST", "/api/music/move", { id: dragId, index: to }).catch(() => {});
});

async function addMusic(next = false) {
  const input = $("#music-query");
  const query = input.value.trim();
  if (!query) {
    input.focus();
    return toast("Link oder Suchbegriff eingeben");
  }
  const form = $("#music-add");
  form.classList.add("busy");
  input.disabled = true;
  try {
    const res = await api("POST", "/api/music/add", { query, next });
    input.value = "";
    if (res.added.length > 1 || res.playlist) {
      toast(`Playlist${res.playlist ? ` „${res.playlist}“` : ""}: ${res.added.length} Titel hinzugefügt${res.skipped ? `, ${res.skipped} übersprungen` : ""}`, "ok");
    } else {
      const t = res.added[0];
      toast(res.position === 0 ? `▶ ${t.title}` : `${t.title} – Platz ${res.position}`, "ok");
    }
  } catch {
    /* toast shown by api() */
  } finally {
    form.classList.remove("busy");
    input.disabled = false;
    input.focus();
  }
}

$("#music-add").addEventListener("submit", (e) => {
  e.preventDefault();
  addMusic(false);
});
$("#music-next").addEventListener("click", () => addMusic(true));
$("#music-toggle").addEventListener("click", () => {
  const playing = state.music.state === "playing" || state.music.state === "loading";
  api("POST", `/api/music/${playing ? "pause" : "resume"}`).catch(() => {});
});
$("#music-skip").addEventListener("click", () => api("POST", "/api/music/skip").catch(() => {}));
$("#queue-shuffle").addEventListener("click", () => api("POST", "/api/music/shuffle").catch(() => {}));
$("#queue-clear").addEventListener("click", () => api("POST", "/api/music/clear").catch(() => {}));
let musicVolumeTimer = 0;
$("#music-volume").addEventListener("input", (e) => {
  e.target.style.setProperty("--fill", `${e.target.value}%`);
  $("#music-volume-value").textContent = `${e.target.value} %`;
  clearTimeout(musicVolumeTimer);
  musicVolumeTimer = setTimeout(() => api("POST", "/api/music/volume", { volume: Number(e.target.value) }).catch(() => {}), 120);
});

async function clipDialog() {
  const typed = $("#music-query").value.trim();
  const url = /^https?:\/\//.test(typed) ? typed : state.music.current?.webpage || "";
  const field = (id, label, attrs) => [el("label", { for: id, text: label }), el("input", { id, type: "text", ...attrs })];
  const fields = [
    ...field("f-url", "YouTube-Link", { value: url, required: true, placeholder: "https://www.youtube.com/watch?v=..." }),
    el(
      "div",
      { class: "field-pair" },
      el("div", {}, ...field("f-start", "Start", { value: "0:00", required: true, placeholder: "1:23" })),
      el("div", {}, ...field("f-end", "Ende", { value: "0:05", required: true, placeholder: "1:27" }))
    ),
    ...field("f-clip-name", "Name (optional)", { placeholder: "Videotitel + Startzeit" }),
    ...field("f-clip-cat", "Kategorie", { value: state.clipCategory, list: "clip-cats" }),
    el("datalist", { id: "clip-cats" }, ...state.categories.filter(Boolean).map((c) => el("option", { value: c }))),
    el("p", { class: "muted small", text: `Höchstens ${state.maxClipSeconds} Sekunden. Der Clip landet als neuer Sound im Soundboard.` }),
  ];
  if (!(await dialog("Ausschnitt als Sound speichern", fields, "Clip speichern"))) return;
  const body = {
    url: $("#f-url").value.trim(),
    start: $("#f-start").value.trim(),
    end: $("#f-end").value.trim(),
    name: $("#f-clip-name").value.trim(),
    category: $("#f-clip-cat").value.trim(),
  };
  toast("Schneide Clip ...");
  try {
    const sound = await api("POST", "/api/clip", body);
    toast(`Clip „${sound.name}“ gespeichert`, "ok");
  } catch {
    /* toast shown by api() */
  }
}
$("#clip-btn").addEventListener("click", clipDialog);

// ------------------------------------------------- durchsage, pull, settings

function renderExtras() {
  const x = state.extras;
  // Text-to-speech
  const ttsOn = Boolean(x.tts?.available);
  $("#speak-text").disabled = !ttsOn;
  $("#speak-btn").disabled = !ttsOn;
  $("#tts-off").hidden = ttsOn;
  $("#tts-off").textContent = ttsOn ? "" : `Sprachausgabe nicht verfügbar: ${x.tts?.reason || "unbekannt"}`;
  // Pull timer
  $("#pull-idle").hidden = Boolean(x.pull);
  $("#pull-active").hidden = !x.pull;
  if (x.pull) {
    $("#pull-by").textContent = `von ${x.pull.by}`;
    startPullLoop();
  }
  // Settings lists
  const joins = $("#join-list");
  joins.replaceChildren(
    ...(x.joinsounds.length
      ? x.joinsounds.map((j) =>
          el(
            "li",
            {},
            el("span", { class: "k", text: j.user, title: j.user }),
            el("span", { class: "arrow", text: "→" }),
            el("span", { class: `v${j.missing ? " missing" : ""}`, text: j.missing ? `${j.sound} (fehlt)` : j.sound, title: j.sound }),
            el(
              "button",
              { class: "btn btn-icon btn-sm", type: "button", title: "Entfernen", "aria-label": `Join-Sound von ${j.user} entfernen`, onclick: () => api("DELETE", `/api/joinsounds/${encodeURIComponent(j.user)}`).catch(() => {}) },
              icon("close")
            )
          )
        )
      : [el("li", {}, el("span", { class: "empty", text: "Noch keine Join-Sounds." }))])
  );
  const stations = $("#radio-list");
  stations.replaceChildren(
    ...(x.radio?.length
      ? x.radio.map((r) =>
          el(
            "li",
            {},
            el("span", { class: "k", text: r.name }),
            el("span", { class: "arrow", text: "→" }),
            el("span", { class: "v", title: `${r.url} (von ${r.created_by})` }, r.title, el("span", { class: "by", text: `  ${r.created_by}` })),
            el(
              "button",
              { class: "btn btn-icon btn-sm", type: "button", title: "Löschen", "aria-label": `Sender ${r.name} löschen`, onclick: () => api("DELETE", `/api/radio/${encodeURIComponent(r.name)}`).catch(() => {}) },
              icon("close")
            )
          )
        )
      : [el("li", {}, el("span", { class: "empty", text: "Noch keine Sender." }))])
  );
  renderStations();
  renderMusic(); // the player names the station that is on air
  const aliases = $("#alias-list");
  aliases.replaceChildren(
    ...(x.aliases.length
      ? x.aliases.map((a) =>
          el(
            "li",
            {},
            el("span", { class: "k", text: `${state.prefix}${a.name}` }),
            el("span", { class: "arrow", text: "→" }),
            el("span", { class: "v", title: `${a.target} (von ${a.created_by})` }, a.target, el("span", { class: "by", text: `  ${a.created_by}` })),
            el(
              "button",
              { class: "btn btn-icon btn-sm", type: "button", title: "Löschen", "aria-label": `Kürzel ${a.name} löschen`, onclick: () => api("DELETE", `/api/aliases/${encodeURIComponent(a.name)}`).catch(() => {}) },
              icon("close")
            )
          )
        )
      : [el("li", {}, el("span", { class: "empty", text: "Noch keine Kürzel." }))])
  );
  renderDatalists();
}

function renderDatalists() {
  const users = new Set((state.status?.users || []).map((u) => u.name));
  for (const j of state.extras.joinsounds) users.add(j.user);
  $("#known-users").replaceChildren(...[...users].sort((a, b) => a.localeCompare(b, "de")).map((u) => el("option", { value: u })));
  $("#sound-names").replaceChildren(...[...state.sounds.values()].map((s) => el("option", { value: s.name })));
}

let pullTimer = 0;
function startPullLoop() {
  const tick = () => {
    const p = state.extras.pull;
    if (!p) {
      clearInterval(pullTimer);
      pullTimer = 0;
      return;
    }
    const left = Math.max(0, Math.ceil(p.ends_at - Date.now() / 1000));
    $("#pull-count").textContent = left > 0 ? `Pull in ${left}` : "Pull!";
  };
  tick();
  if (!pullTimer) pullTimer = setInterval(tick, 200);
}

$("#speak-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("#speak-text");
  const text = input.value.trim();
  if (!text) return input.focus();
  input.disabled = true;
  try {
    await api("POST", "/api/tts", { text });
    input.value = "";
  } catch {
    /* toast shown by api() */
  } finally {
    input.disabled = !state.extras.tts?.available;
    input.focus();
  }
});
document.querySelectorAll("[data-pull]").forEach((b) =>
  b.addEventListener("click", () => api("POST", "/api/pull", { seconds: Number(b.dataset.pull) }).catch(() => {}))
);
$("#pull-cancel").addEventListener("click", () => api("POST", "/api/pull/cancel").catch(() => {}));

const findSoundByName = (name) => {
  const f = fold(name);
  return [...state.sounds.values()].find((s) => s._foldedName === f || `${fold(s.category)} ${s._foldedName}`.trim() === f);
};

$("#settings-btn").addEventListener("click", () => {
  renderExtras();
  $("#settings").showModal();
});
$("#settings-close").addEventListener("click", () => $("#settings").close());
$("#settings").addEventListener("click", (e) => {
  if (e.target === $("#settings")) $("#settings").close(); // click on the backdrop
});
$("#join-add").addEventListener("submit", async (e) => {
  e.preventDefault();
  const user = $("#join-user").value.trim();
  const sound = findSoundByName($("#join-sound").value);
  if (!sound) return toast("Diesen Sound gibt es nicht, bitte aus der Liste wählen", "err");
  try {
    await api("PUT", "/api/joinsounds", { user, sound_id: sound.id });
    $("#join-user").value = "";
    $("#join-sound").value = "";
    toast(`Join-Sound für ${user}: ${sound.name}`, "ok");
  } catch {
    /* toast shown by api() */
  }
});
$("#radio-add").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (await saveStation($("#radio-name").value, $("#radio-url").value)) {
    $("#radio-name").value = "";
    $("#radio-url").value = "";
  }
});
$("#alias-add").addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = $("#alias-name").value.trim().replace(/^[\/!]+/, "");
  const target = $("#alias-target").value.trim();
  try {
    await api("PUT", "/api/aliases", { name, target });
    $("#alias-name").value = "";
    $("#alias-target").value = "";
    toast(`Kürzel ${state.prefix}${name.toLowerCase()} angelegt`, "ok");
  } catch {
    /* toast shown by api() */
  }
});

async function joinSoundDialog(s) {
  const input = el("input", { type: "text", id: "f-join-user", list: "known-users", required: true, placeholder: "Name" });
  renderDatalists();
  const hint = el("p", { class: "muted small", text: `„${s.name}“ spielt dann, wenn diese Person den Kanal betritt.` });
  if (!(await dialog("Als Join-Sound festlegen", [el("label", { for: "f-join-user", text: "Für wen?" }), input, hint], "Festlegen"))) return;
  const user = input.value.trim();
  if (!user) return;
  api("PUT", "/api/joinsounds", { user, sound_id: s.id }).then(() => toast(`Join-Sound für ${user}: ${s.name}`, "ok")).catch(() => {});
}

// -------------------------------------------------------------- collections

const fmtCount = (n) => n.toLocaleString("de");
const activeCollection = () => state.collections.find((c) => c.id === view.mode) || null;
// The stored view stays remembered even while the collections are not known yet
const currentMode = () => (activeCollection() ? view.mode : "board");

function renderViews() {
  const views = $("#views");
  const mode = currentMode();
  const items = [{ id: "board", label: "Soundboard", icon: "grid", count: state.sounds.size }];
  for (const c of state.collections) items.push({ id: c.id, label: c.name, icon: "quote", count: c.loaded ? c.count : null });
  views.hidden = state.collections.length === 0;
  views.replaceChildren(
    ...items.map((item) =>
      el(
        "button",
        {
          class: `view-btn${item.id === mode ? " active" : ""}`,
          type: "button",
          role: "tab",
          "aria-selected": String(item.id === mode),
          onclick: () => setMode(item.id),
        },
        icon(item.icon),
        item.label,
        el("span", { class: "count", text: item.count == null ? "…" : fmtCount(item.count) })
      )
    )
  );
  const board = mode === "board";
  for (const id of ["board-toolbar", "chips", "subchips", "grid", "empty"]) $(`#${id}`).classList.toggle("off", !board);
  $("#coll").hidden = board;
}

function setMode(mode) {
  if (mode === currentMode()) return;
  view.mode = mode;
  storage.set("mode", mode);
  closeMenu();
  renderViews();
  if (mode !== "board") {
    const c = activeCollection();
    if (coll.group && !c.groups.some((g) => g.name === coll.group)) coll.group = "";
    renderGroups();
    loadLines(true);
  }
}

function renderGroups() {
  const c = activeCollection();
  if (!c) return;
  const f = fold(coll.groupFilter);
  const groups = c.groups.filter((g) => !f || fold(g.name).includes(f));
  const button = (name, label, count) =>
    el(
      "li",
      {},
      el(
        "button",
        {
          class: `group-btn${coll.group === name ? " active" : ""}`,
          type: "button",
          "aria-pressed": String(coll.group === name),
          title: label,
          onclick: () => setGroup(name),
        },
        el("span", { class: "name", text: label }),
        el("span", { class: "count", text: fmtCount(count) })
      )
    );
  const list = [];
  if (!f) list.push(button("", "Alle", c.count), el("li", { class: "group-sep", "aria-hidden": "true" }));
  list.push(...groups.map((g) => button(g.name, g.name, g.count)));
  if (f && !groups.length) list.push(el("li", { class: "none", text: "Niemand gefunden" }));
  $("#coll-group-list").replaceChildren(...list);
  const select = $("#coll-group-select");
  select.replaceChildren(
    el("option", { value: "", text: `Alle Sprecher (${fmtCount(c.count)})` }),
    ...c.groups.map((g) => el("option", { value: g.name, text: `${g.name} (${fmtCount(g.count)})` }))
  );
  select.value = coll.group;
  const mood = $("#coll-mood");
  mood.hidden = !c.moods.length;
  mood.value = coll.mood;
}

function setGroup(name) {
  coll.group = name;
  storage.set("collGroup", name);
  renderGroups();
  loadLines(true);
}

const PAGE = 100;
async function loadLines(reset) {
  const c = activeCollection();
  if (!c) return;
  if (!reset && (coll.loading || coll.items.length >= coll.total)) return;
  const seq = ++coll.seq;
  coll.loading = true;
  const offset = reset ? 0 : coll.items.length;
  const params = new URLSearchParams({ q: coll.q, group: coll.group, mood: coll.mood, offset: String(offset), limit: String(PAGE) });
  let res;
  try {
    res = await api("GET", `/api/collections/${encodeURIComponent(c.id)}/items?${params}`);
  } catch {
    if (seq === coll.seq) coll.loading = false;
    return;
  }
  if (seq !== coll.seq) return; // a newer search is already running
  coll.loading = false;
  coll.total = res.total;
  coll.items = reset ? res.items : coll.items.concat(res.items);
  const list = $("#coll-lines");
  if (reset) {
    list.replaceChildren(...res.items.map(buildLine));
    $("#coll-results").scrollTop = 0;
  } else {
    list.append(...res.items.map(buildLine));
  }
  renderLinesFooter(c, res.loaded);
  markPlaying();
}

function renderLinesFooter(c, loaded) {
  const where = coll.group ? ` von ${coll.group}` : "";
  const filtered = coll.q || coll.mood;
  $("#coll-summary").textContent = !loaded
    ? `${c.name} wird eingelesen ...`
    : coll.total
      ? `${fmtCount(coll.total)} ${coll.total === 1 ? "Zeile" : "Zeilen"}${where}${filtered ? " gefunden" : ""}${coll.items.length < coll.total ? ` · ${fmtCount(coll.items.length)} angezeigt` : ""}`
      : "";
  $("#coll-more").hidden = coll.items.length >= coll.total;
  const empty = $("#coll-empty");
  empty.hidden = coll.total > 0 || !loaded;
  if (!empty.hidden) {
    empty.replaceChildren(el("div", {}, el("strong", { text: "Nichts gefunden" }), coll.q ? `Keine Zeile${where} passt zu „${coll.q}“.` : "Diese Auswahl ist leer."));
  }
}

function lineMeta(item) {
  const parts = [];
  if (item.number) parts.push(el("span", { class: "num", text: `#${item.number}` }));
  if (!coll.group) parts.push(el("span", { text: item.group }));
  if (item.speaker && item.speaker.toLowerCase() !== item.group.toLowerCase()) parts.push(el("span", { text: item.speaker }));
  if (item.mood) parts.push(el("span", { class: `mood ${item.mood}`, text: item.mood === "positiv" ? "positiv" : "negativ" }));
  if (item.situation) parts.push(el("span", { class: "situation", text: item.situation, title: item.situation }));
  const out = [];
  parts.forEach((p, i) => {
    if (i && !p.classList.contains("mood") && !parts[i - 1].classList.contains("mood")) out.push(el("span", { class: "sep", text: "·" }));
    out.push(p);
  });
  return out;
}

function buildLine(item) {
  const c = activeCollection();
  const base = `/api/collections/${encodeURIComponent(c.id)}/items/${item.id}`;
  const label = item.text || "Ohne Text";
  const previewBtn = el(
    "button",
    {
      class: "tile-btn",
      type: "button",
      title: "Lokal vorhören",
      "aria-label": `${label} lokal vorhören`,
      onclick: (e) => {
        e.stopPropagation();
        if (previewId === item.id && !preview.paused) {
          preview.pause();
          previewId = null;
          return;
        }
        preview.src = `${base}/audio`;
        preview.play().catch(() => toast("Vorhören nicht möglich", "err"));
        previewId = item.id;
      },
    },
    icon("headphones")
  );
  const boardBtn = el(
    "button",
    {
      class: "tile-btn",
      type: "button",
      title: "Aufs Soundboard legen",
      "aria-label": `${label} aufs Soundboard legen`,
      onclick: async (e) => {
        e.stopPropagation();
        const btn = e.currentTarget;
        try {
          const res = await api("POST", `${base}/board`);
          btn.classList.add("done");
          btn.querySelector("use").setAttribute("href", "#i-check");
          btn.title = "Liegt auf dem Soundboard";
          toast(res.created ? `Aufs Board gelegt: ${res.sound.category}` : "Liegt schon auf dem Board", "ok");
        } catch {
          /* toast shown by api() */
        }
      },
    },
    icon("pin")
  );
  const line = el(
    "li",
    {
      class: "line",
      tabindex: "0",
      "data-id": item.id,
      title: item.text_en ? `EN: ${item.text_en}` : "",
      onclick: () => playLine(item, line),
      onkeydown: (e) => {
        if (e.target === line && (e.key === "Enter" || e.key === " ")) {
          e.preventDefault();
          playLine(item, line);
        }
      },
    },
    el("span", { class: "line-play", "aria-hidden": "true" }, icon("play")),
    el(
      "div",
      {},
      el("div", { class: `line-text${item.text ? "" : " none"}`, text: label }),
      // Lines in other languages (Dota chat wheel): the English translation right below
      item.text_en && activeCollection()?.translations ? el("div", { class: "line-translation", lang: "en", text: item.text_en }) : null,
      el("div", { class: "line-meta" }, ...lineMeta(item))
    ),
    el("div", { class: "line-actions" }, previewBtn, boardBtn)
  );
  return line;
}

async function playLine(item, line) {
  const c = activeCollection();
  line?.classList.remove("flash");
  void line?.offsetWidth;
  line?.classList.add("flash");
  try {
    await api("POST", `/api/collections/${encodeURIComponent(c.id)}/items/${item.id}/play`);
  } catch {
    /* toast already shown */
  }
}

let collSearchTimer = 0;
function setCollQuery(value) {
  $("#coll-search").value = value;
  coll.q = value.trim();
  $("#coll-search-clear").hidden = !value;
  clearTimeout(collSearchTimer);
  collSearchTimer = setTimeout(() => loadLines(true), 160);
}
$("#coll-search").addEventListener("input", (e) => setCollQuery(e.target.value));
$("#coll-search-clear").addEventListener("click", () => {
  setCollQuery("");
  $("#coll-search").focus();
});
$("#coll-search").addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    const first = coll.items[0];
    if (first) playLine(first, $(`.line[data-id="${first.id}"]`));
  } else if (e.key === "Escape" && e.target.value) {
    e.stopPropagation();
    setCollQuery("");
  }
});
$("#coll-group-filter").addEventListener("input", (e) => {
  coll.groupFilter = e.target.value;
  renderGroups();
});
$("#coll-group-filter").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("#coll-group-list .group-btn:not(.active)")?.click();
  else if (e.key === "Escape" && e.target.value) {
    e.stopPropagation();
    e.target.value = "";
    coll.groupFilter = "";
    renderGroups();
  }
});
$("#coll-group-select").addEventListener("change", (e) => setGroup(e.target.value));
$("#coll-mood").addEventListener("change", (e) => {
  coll.mood = e.target.value;
  storage.set("collMood", coll.mood);
  loadLines(true);
});
$("#coll-more-btn").addEventListener("click", () => loadLines(false));
$("#coll-reload").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  if (btn.classList.contains("busy")) return;
  btn.classList.add("busy");
  const before = activeCollection()?.count ?? 0;
  try {
    const list = await api("POST", "/api/collections/reload");
    setCollections(list);
    const c = activeCollection();
    if (c) {
      const diff = c.count - before;
      toast(`${c.name}: ${fmtCount(c.count)} Zeilen${diff > 0 ? `, ${fmtCount(diff)} neu` : diff < 0 ? `, ${fmtCount(-diff)} weniger` : ", nichts Neues"}`, "ok");
      loadLines(true);
    }
  } catch {
    /* toast shown by api() */
  } finally {
    btn.classList.remove("busy");
  }
});
$("#coll-random").addEventListener("click", async () => {
  const c = activeCollection();
  if (!c) return;
  try {
    const res = await api("POST", `/api/collections/${encodeURIComponent(c.id)}/random`, { q: coll.q, group: coll.group, mood: coll.mood });
    toast(`${res.item.group}: „${res.item.text || "Ohne Text"}“`, "ok");
  } catch {
    /* toast shown by api() */
  }
});
// Load the next page shortly before the end of the list becomes visible
new IntersectionObserver((entries) => entries.some((e) => e.isIntersecting) && loadLines(false), { rootMargin: "400px" }).observe($("#coll-more"));

function setCollections(list) {
  const before = activeCollection();
  state.collections = list;
  renderViews();
  renderHelp();
  const now = activeCollection();
  if (now) {
    renderGroups();
    if (!before || before.loaded !== now.loaded || before.count !== now.count) loadLines(true);
  }
}

// ---------------------------------------------------------------- live data

function setLibrary(lib) {
  state.sounds = new Map(
    lib.sounds.map((s) => {
      s._foldedName = fold(s.name);
      s._folded = `${s._foldedName} ${fold(s.category)} ${fold(s.speaker || "")} ${fold(s.text || "")}`;
      return [s.id, s];
    })
  );
  state.categories = lib.categories;
  renderChips();
  renderGrid();
  renderViews();
}

function setPlaying(list) {
  state.playing = list;
  state.playingReceived = performance.now();
  renderVoices();
}

const handlers = {
  state(data) {
    state.status = data.status;
    state.volume = data.volume;
    state.activity = data.activity;
    state.prefix = data.prefix;
    state.version = data.version;
    const peerChanged = JSON.stringify(data.peer ?? null) !== JSON.stringify(state.peer ?? null);
    state.peer = data.peer ?? null;
    if (peerChanged) {
      state.peerOnline = undefined;
      checkPeer();
    }
    state.maxUploadMb = data.max_upload_mb;
    setLibrary(data.library);
    setPlaying(data.playing);
    renderStatus();
    renderVolume();
    renderActivity();
    renderHelp();
    state.maxClipSeconds = data.max_clip_seconds ?? state.maxClipSeconds;
    state.clipCategory = data.clip_category ?? state.clipCategory;
    if (data.music) setMusic(data.music);
    if (data.extras) {
      state.extras = data.extras;
      renderExtras();
    }
    setCollections(data.collections || []);
  },
  collections: setCollections,
  extras(x) {
    state.extras = x;
    renderExtras();
  },
  music: setMusic,
  library: setLibrary,
  playing: setPlaying,
  status(data) {
    state.status = data;
    renderStatus();
  },
  volume(v) {
    state.volume = v;
    renderVolume();
  },
  activity(a) {
    state.activity.push(a);
    if (state.activity.length > 100) state.activity.shift();
    $("#activity").prepend(activityItem(a));
    while ($("#activity").children.length > 40) $("#activity").lastChild.remove();
  },
  sound(s) {
    const old = state.sounds.get(s.id);
    if (!old) return;
    Object.assign(old, s);
    updateTile(old);
  },
};

let ws;
let retry = 0;
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => {
    retry = 0;
  };
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    handlers[msg.type]?.(msg.data);
  };
  ws.onclose = () => {
    state.status = state.status ? { ...state.status, connected: false, last_error: "Webinterface hat die Verbindung zum Bot verloren" } : null;
    renderStatus();
    setPlaying([]);
    setTimeout(connect, Math.min(10000, 500 * 2 ** retry++));
  };
}
setInterval(() => ws?.readyState === WebSocket.OPEN && ws.send("ping"), 25000);
setInterval(checkPeer, 30000);
connect();
