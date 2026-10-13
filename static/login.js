// Login page: passkey first (automatically when this browser has one), otherwise a code that the
// person sends to the bot in the chat (like the OAuth device flow). See app/auth.py.
"use strict";

const $ = (s) => document.querySelector(s);
const show = (id) => {
  for (const s of ["#step-start", "#step-code", "#step-passkey"]) $(s).hidden = s !== id;
};
const goIn = () => location.replace("./");

let status = {};
let poll;

function error(message) {
  $("#error").textContent = message || "";
  $("#error").hidden = !message;
}

async function post(path, body) {
  const res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body ?? {}) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "Der Bot ist gerade nicht erreichbar");
  return data;
}

// A cancelled or unavailable passkey prompt is not an error worth shouting about.
const quiet = (err) => ["NotAllowedError", "AbortError"].includes(err?.name);

async function withPasskey() {
  error("");
  $("#passkey-btn").disabled = true;
  try {
    await passkey.login();
    goIn();
  } catch (err) {
    if (!quiet(err)) error(err.message);
  } finally {
    $("#passkey-btn").disabled = false;
  }
}

async function withCode() {
  error("");
  clearInterval(poll);
  let started;
  try {
    started = await post("auth/code");
  } catch (err) {
    error(err.message);
    return;
  }
  const command = `${status.prefix || "/"}login ${started.code}`;
  $("#code-command").textContent = command;
  show("#step-code");
  const until = Date.now() + started.expires_in * 1000;
  const tick = async () => {
    const left = Math.max(0, Math.round((until - Date.now()) / 1000));
    $("#code-wait").textContent = `Warte auf den Bot … noch ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
    let result;
    try {
      result = await post("auth/code/poll", { flow: started.flow });
    } catch {
      return; // bot restarting: keep trying until the code expires
    }
    if (result.state === "ok") {
      clearInterval(poll);
      afterCode();
    } else if (result.state === "expired" || left === 0) {
      clearInterval(poll);
      show("#step-start");
      error("Der Code ist abgelaufen. Hol dir einfach einen neuen.");
    }
  };
  poll = setInterval(tick, 2000);
  tick();
}

async function afterCode() {
  status = await fetch("auth/status", { cache: "no-store" }).then((r) => r.json());
  if (!passkey.supported()) return goIn();
  $("#hello").textContent = `Hallo ${status.user}!`;
  show("#step-passkey");
  $("#create-btn").focus();
}

$("#passkey-btn").addEventListener("click", withPasskey);
$("#code-btn").addEventListener("click", withCode);
$("#code-back").addEventListener("click", () => {
  clearInterval(poll);
  show("#step-start");
});
$("#copy-btn").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText($("#code-command").textContent);
    $("#copy-btn").title = "Kopiert";
  } catch { /* select it by hand */ }
});
$("#create-btn").addEventListener("click", async () => {
  error("");
  $("#create-btn").disabled = true;
  try {
    await passkey.register();
    goIn();
  } catch (err) {
    if (!quiet(err)) error(err.message);
  } finally {
    $("#create-btn").disabled = false;
  }
});
$("#skip-btn").addEventListener("click", goIn);

(async () => {
  try {
    status = await fetch("auth/status", { cache: "no-store" }).then((r) => r.json());
  } catch {
    error("Der Bot ist gerade nicht erreichbar.");
    return;
  }
  if (!status.enabled || status.user) return goIn();
  const mumble = status.platform !== "Discord";
  document.title = `Anmelden · Soundboard · ${status.platform}`;
  $("#platform").textContent = status.platform;
  $("#code-btn").textContent = `Mit ${status.platform} anmelden`;
  const bot = status.bot ? `„${status.bot}“` : "dem Bot";
  $("#code-how").innerHTML = "";
  $("#code-how").append(
    mumble
      ? `Schick ${bot} in Mumble eine Privatnachricht (Rechtsklick auf den Bot, „Nachricht senden“) oder schreib in seinen Kanal:`
      : `Schick ${bot} eine Direktnachricht oder nutz den Slash-Befehl /login auf dem Server:`
  );
  if (!passkey.supported()) {
    $("#passkey-btn").disabled = true;
    $("#passkey-note").hidden = false;
    $("#passkey-note").textContent = /^[\d.]+$/.test(location.hostname)
      ? `Passkeys gehen nur mit einem Namen in der Adresse, z. B. http://localhost:${location.port || 80}.`
      : "Dieser Browser kann keine Passkeys.";
  }
  show("#step-start");
  // This browser has a passkey here: just use it. Not right after logging out on purpose.
  let loggedOut = false;
  try {
    loggedOut = sessionStorage.getItem("bard-logged-out") === "1";
    sessionStorage.removeItem("bard-logged-out");
  } catch { /* private mode */ }
  if (passkey.supported() && passkey.remembered() && !loggedOut) withPasskey();
})();
