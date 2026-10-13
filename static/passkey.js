// Passkeys (WebAuthn) for the login page and the settings dialog. Plain script, no build step.
// Options and credentials travel as JSON with base64url fields; converted by hand because
// PublicKeyCredential.parseCreationOptionsFromJSON / toJSON are missing in older browsers and some
// password-manager extensions break toJSON.
"use strict";

const passkey = (() => {
  const enc = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const dec = (s) => {
    const b = atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4));
    return Uint8Array.from(b, (c) => c.charCodeAt(0)).buffer;
  };
  const ids = (list) => (list || []).map((c) => ({ ...c, id: dec(c.id) }));
  const FLAG = "bard-passkey"; // this browser has a passkey here: offer it right away next time

  async function post(path, body) {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body ?? {}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw Object.assign(new Error(data.error || "Fehler bei der Anmeldung"), { status: res.status });
    return data;
  }

  function remember(on) {
    try {
      if (on) localStorage.setItem(FLAG, "1");
      else localStorage.removeItem(FLAG);
    } catch { /* private mode */ }
  }

  return {
    supported: () => !!window.PublicKeyCredential && window.isSecureContext && !/^[\d.]+$|^\[/.test(location.hostname),
    remembered() {
      try { return localStorage.getItem(FLAG) === "1"; } catch { return false; }
    },
    remember,

    // Create a passkey for the signed-in person; upgrades the session to 30 days.
    async register() {
      const o = await post("auth/passkey/register-options");
      const cred = await navigator.credentials.create({
        publicKey: {
          ...o,
          challenge: dec(o.challenge),
          user: { ...o.user, id: dec(o.user.id) },
          excludeCredentials: ids(o.excludeCredentials),
        },
      });
      const r = cred.response;
      await post("auth/passkey/register", {
        credential: {
          id: cred.id,
          rawId: enc(cred.rawId),
          type: cred.type,
          authenticatorAttachment: cred.authenticatorAttachment ?? undefined,
          response: {
            clientDataJSON: enc(r.clientDataJSON),
            attestationObject: enc(r.attestationObject),
            transports: r.getTransports?.() ?? [],
          },
          clientExtensionResults: cred.getClientExtensionResults?.() ?? {},
        },
      });
      remember(true);
    },

    // Sign in with any passkey this site knows (discoverable, no name needed).
    async login() {
      const { flow, options: o } = await post("auth/passkey/login-options");
      const cred = await navigator.credentials.get({
        publicKey: { ...o, challenge: dec(o.challenge), allowCredentials: ids(o.allowCredentials) },
      });
      const r = cred.response;
      try {
        await post("auth/passkey/login", {
          flow,
          credential: {
            id: cred.id,
            rawId: enc(cred.rawId),
            type: cred.type,
            authenticatorAttachment: cred.authenticatorAttachment ?? undefined,
            response: {
              clientDataJSON: enc(r.clientDataJSON),
              authenticatorData: enc(r.authenticatorData),
              signature: enc(r.signature),
              userHandle: r.userHandle ? enc(r.userHandle) : null,
            },
            clientExtensionResults: cred.getClientExtensionResults?.() ?? {},
          },
        });
      } catch (err) {
        if (err.status === 401) remember(false); // e.g. the passkey was deleted
        throw err;
      }
      remember(true);
    },
  };
})();
