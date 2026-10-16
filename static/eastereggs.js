// Easter eggs for people who know the games (loaded after app.js, uses its helpers).
//  - AoE2 taunt numbers: a bare number in the search + Enter plays that numbered collection line
//  - Dota chat wheel: hold Y, point in a direction, let go
//  - Konami code: Leeroy mode
//  - Wololo: click the logo seven times, the interface changes to the next AoE2 player color
"use strict";

const calm = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
const typingIn = (t) => ["INPUT", "SELECT", "TEXTAREA"].includes(t?.tagName) || t?.isContentEditable;

// ------------------------------------------------------------------ AoE2 taunt numbers

// The line with this number in any collection that numbers its lines (AoE2: "11" is the laugh).
async function findTaunt(number) {
  const wanted = String(Number(number));
  const hits = await Promise.all(
    state.collections.map(async (c) => {
      try {
        const res = await fetch(`/api/collections/${encodeURIComponent(c.id)}/items?q=${wanted}&limit=10`);
        const data = res.ok ? await res.json() : null;
        const item = data?.items?.find((i) => i.number && String(Number(i.number)) === wanted);
        return item ? { collection: c, item } : null;
      } catch {
        return null;
      }
    })
  );
  return hits.find(Boolean) || null;
}

async function playTaunt(number) {
  const hit = await findTaunt(number);
  if (!hit) return false;
  try {
    await api("POST", `/api/collections/${encodeURIComponent(hit.collection.id)}/items/${hit.item.id}/play`);
    toast(`${hit.collection.name} ${number}: ${hit.item.text}`);
  } catch { /* the reason was shown as a toast */ }
  return true;
}

// ------------------------------------------------------------------ Dota chat wheel

const wheel = {
  node: null,
  items: [],
  active: -1,
  x: innerWidth / 2,
  y: innerHeight / 2,
  open: false,
};
document.addEventListener("pointermove", (e) => {
  wheel.x = e.clientX;
  wheel.y = e.clientY;
  if (wheel.open) pickWheelItem(e.clientX, e.clientY);
}, { passive: true });

// Favorites first, then the most played sounds.
function wheelSounds() {
  const all = [...state.sounds.values()];
  const favorites = all.filter((s) => s.favorite).sort((a, b) => a.name.localeCompare(b.name, "de"));
  const rest = all.filter((s) => !s.favorite).sort((a, b) => b.plays - a.plays || a.name.localeCompare(b.name, "de"));
  return [...favorites, ...rest].slice(0, 8);
}

const WHEEL_RADIUS = 118;
const WHEEL_SIZE = 320;

function openWheel() {
  const sounds = wheelSounds();
  if (!sounds.length) return;
  wheel.node?.remove();
  const x = Math.min(Math.max(wheel.x, WHEEL_SIZE / 2 + 8), innerWidth - WHEEL_SIZE / 2 - 8);
  const y = Math.min(Math.max(wheel.y, WHEEL_SIZE / 2 + 8), innerHeight - WHEEL_SIZE / 2 - 8);
  wheel.center = { x, y };
  wheel.items = sounds.map((s, i) => {
    const angle = (i / 8) * 2 * Math.PI - Math.PI / 2; // first one at the top, then clockwise
    return el("button", {
      class: "wheel-item",
      type: "button",
      text: s.name,
      title: s.name,
      style: `left: calc(50% + ${Math.round(Math.cos(angle) * WHEEL_RADIUS)}px); top: calc(50% + ${Math.round(Math.sin(angle) * WHEEL_RADIUS)}px)`,
      onpointerup: () => {
        wheel.active = i;
        closeWheel(true);
      },
      "data-id": s.id,
    });
  });
  wheel.node = el("div", { class: "wheel", role: "menu", "aria-label": "Chatwheel", style: `left: ${x}px; top: ${y}px` },
    el("span", { class: "wheel-hub", text: "Y" }), ...wheel.items);
  document.body.append(wheel.node);
  wheel.active = -1;
  wheel.open = true;
}

function pickWheelItem(px, py) {
  const dx = px - wheel.center.x;
  const dy = py - wheel.center.y;
  let index = -1;
  if (Math.hypot(dx, dy) > 36) {
    const angle = (Math.atan2(dy, dx) + Math.PI / 2 + 2 * Math.PI) % (2 * Math.PI); // 0 = top, clockwise
    index = Math.round(angle / (Math.PI / 4)) % 8;
    if (index >= wheel.items.length) index = -1;
  }
  wheel.active = index;
  wheel.items.forEach((item, i) => item.classList.toggle("active", i === index));
}

function closeWheel(playIt) {
  if (!wheel.open) return;
  wheel.open = false;
  const chosen = wheel.items[wheel.active];
  wheel.node?.remove();
  wheel.node = null;
  if (playIt && chosen) play(chosen.dataset.id, $(`.tile[data-id="${chosen.dataset.id}"]`));
}

document.addEventListener("keydown", (e) => {
  if (e.key.toLowerCase() !== "y" || e.repeat || e.ctrlKey || e.metaKey || e.altKey || typingIn(e.target)) return;
  if ($("#dialog").open || $("#settings").open) return;
  openWheel();
});
document.addEventListener("keyup", (e) => {
  if (e.key.toLowerCase() === "y") closeWheel(true);
});
// Escape only closes an open wheel (otherwise it stops all sounds, see app.js).
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && wheel.open) {
    e.stopImmediatePropagation();
    closeWheel(false);
  }
}, true);
addEventListener("blur", () => closeWheel(false));

// ------------------------------------------------------------------ Konami code: Leeroy mode

const KONAMI = ["arrowup", "arrowup", "arrowdown", "arrowdown", "arrowleft", "arrowright", "arrowleft", "arrowright", "b", "a"];
let konami = 0;
document.addEventListener("keydown", (e) => {
  if (typingIn(e.target)) return;
  const key = e.key.toLowerCase();
  konami = key === KONAMI[konami] ? konami + 1 : key === KONAMI[0] ? 1 : 0;
  if (konami < KONAMI.length) return;
  konami = 0;
  const leeroy = [...state.sounds.values()].find((s) => s._foldedName.includes("leeroy"));
  if (leeroy) play(leeroy.id, $(`.tile[data-id="${leeroy.id}"]`));
  else api("POST", "/api/random", {}).catch(() => {});
  toast("LEEEEROY JENKINS!", "ok");
  if (!calm()) {
    const grid = $("#grid");
    grid.classList.remove("leeroy");
    void grid.offsetWidth;
    grid.classList.add("leeroy");
    setTimeout(() => grid.classList.remove("leeroy"), 1100);
  }
});

// ------------------------------------------------------------------ Wololo: AoE2 player colors

const PLAYER_COLORS = [
  ["Blau", "#2f6bff"],
  ["Rot", "#e5383b"],
  ["Grün", "#2ea043"],
  ["Gelb", "#b8860b"],
  ["Türkis", "#0fa3b1"],
  ["Lila", "#9b4dca"],
  ["Grau", "#7d8590"],
  ["Orange", "#f07d1a"],
];

function applyPlayerColor(index) {
  const root = document.documentElement.style;
  const color = PLAYER_COLORS[index]?.[1];
  for (const [name, value] of [
    ["--accent", color],
    ["--accent-soft", color && `color-mix(in srgb, ${color} 16%, transparent)`],
    ["--accent-text", color && `color-mix(in srgb, ${color} 72%, var(--text))`],
  ]) {
    if (value) root.setProperty(name, value);
    else root.removeProperty(name);
  }
}
applyPlayerColor(storage.get("wololo", -1));

let logoClicks = [];
$(".brand img")?.addEventListener("click", () => {
  const now = Date.now();
  logoClicks = [...logoClicks.filter((t) => now - t < 3000), now];
  if (logoClicks.length < 7) return;
  logoClicks = [];
  const next = storage.get("wololo", -1) + 1;
  const index = next < PLAYER_COLORS.length ? next : -1;
  storage.set("wololo", index);
  applyPlayerColor(index);
  toast(index < 0 ? "Wololo! Zurück zur eigenen Farbe." : `Wololo! Du spielst jetzt für ${PLAYER_COLORS[index][0]}.`, "ok");
  const sound = [...state.sounds.values()].find((s) => s._foldedName.includes("wololo"));
  if (sound) play(sound.id, $(`.tile[data-id="${sound.id}"]`));
  else playTaunt("30"); // AoE2 taunt 30 is WOLOLO
});
