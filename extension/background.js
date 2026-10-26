import {flowScope, operationErrorSource, sameScope, serverUrl, validRequest} from "./protocol.mjs";
import {flowOperation} from "./operations.mjs";

let socket = null;
let selected = null;
let epoch = 0;
let status = "disconnected";
let busy = false;
let derived = null;
let seen = new Set();
let heartbeat = null;
let lastPong = 0;
let connectionRevision = 0;
let enabled = false;
const heartbeatTimeout = 45000;

function verifyHeartbeat() {
  if (!socket || !["paired", "pairing", "awaiting_approval"].includes(status)) return;
  if (socket.readyState !== WebSocket.OPEN || Date.now() - lastPong > heartbeatTimeout) {
    const ws = socket;
    release(ws);
    status = "waiting";
    ws.close();
    void scheduleRetry();
  }
}
let connecting = false;
let pairingId = null;
let retryDelay = null;
const retryAlarm = "openflow-reconnect";
const bridgeUrl = "ws://127.0.0.1:9233";

async function scheduleRetry() {
  try {
    const config = await chrome.storage.local.get(["disabled", "retryAttempt"]);
    if (config.disabled || status !== "waiting") return;
    const attempt = Math.max(0, Math.min(Number(config.retryAttempt) || 0, 5));
    retryDelay = Math.min(0.5 * 2 ** attempt, 5);
    await chrome.storage.local.set({retryAttempt: Math.min(attempt + 1, 5)});
    if (status !== "waiting") return;
    await chrome.alarms.create(retryAlarm, {delayInMinutes: retryDelay});
    if (status === "disconnected" || status === "authorization_required") {
      await chrome.alarms.clear(retryAlarm);
    }
  } catch {
    if (status === "waiting") {
      retryDelay = null;
      status = "retry_unavailable";
    }
  }
}

function release(ws) {
  if (socket !== ws) return;
  clearInterval(heartbeat);
  heartbeat = null;
  socket = null;
  pairingId = null;
  retryDelay = null;
  invalidate(false);
}

async function stop(reason = "disconnected") {
  connectionRevision += 1;
  enabled = false;
  status = reason;
  const ws = socket;
  release(ws);
  ws?.close(1000, reason);
  await chrome.storage.local.set({disabled: true});
  await chrome.alarms.clear(retryAlarm);
}

async function discover() {
  if (busy || selected || status !== "paired") return;
  busy = true;
  try {
    const available = await tabs();
    if (available.length === 1) await select(available[0].tab_id, false);
  } catch { invalidate(); } finally { busy = false; }
}

function invalidate(notify = true) {
  selected = null;
  derived = null;
  epoch += 1;
  if (notify && socket?.readyState === WebSocket.OPEN && status === "paired") {
    socket.send(JSON.stringify({type: "invalidated"}));
  }
}

async function tabs() {
  return (await chrome.tabs.query({})).flatMap(tab => {
    const scope = flowScope(tab.url);
    return scope && Number.isInteger(tab.id) ? [{tab_id: tab.id, ...scope}] : [];
  }).slice(0, 100);
}

async function accountPanel(action) {
  const visible = node => node.getClientRects().length && getComputedStyle(node).visibility === "visible";
  const dialogs = () => [...document.querySelectorAll('[role="dialog"][aria-label], dialog[aria-label]')]
    .filter(node => /^(Configurações da conta|Account settings)$/i.test(node.getAttribute("aria-label") || "") && visible(node));
  if (action === "open" && dialogs().length === 1) return false;
  const pattern = action === "open" ? /^(Detalhes da conta|Account details)$/i : /^(Fechar painel da conta|Close account panel)$/i;
  const roots = action === "open" ? [document] : dialogs();
  const buttons = roots.flatMap(root => [...root.querySelectorAll('button, [role="button"]')])
    .filter(node => visible(node) && pattern.test((node.getAttribute("aria-label") || node.innerText || "").trim()));
  if (buttons.length !== 1) throw new Error("account_control_unavailable");
  buttons[0].click();
  if (action === "open") {
    for (let attempt = 0; attempt < 20; attempt++) {
      if (dialogs().length === 1) return true;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    throw new Error("account_panel_timeout");
  }
  return true;
}

function inspectPage() {
  const visible = node => {
    if (!node.getClientRects().length) return false;
    for (let current = node; current; current = current.parentElement) {
      const style = getComputedStyle(current);
      if (current.hidden || current.getAttribute("aria-hidden") === "true" ||
          style.display === "none" || ["hidden", "collapse"].includes(style.visibility) || style.opacity === "0") return false;
    }
    return true;
  };
  const addresses = text => text.match(/[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi) || [];
  const emails = new Set();
  let ambiguous = false;
  for (const node of document.querySelectorAll('button[aria-label], a[aria-label], [role="button"][aria-label]')) {
    const label = node.getAttribute("aria-label") || "";
    if (!/^(?:Google Account|Conta do Google):\s/i.test(label) || !visible(node)) continue;
    const matches = addresses(label);
    if (matches.length !== 1) ambiguous = true;
    else emails.add(matches[0].toLowerCase());
  }
  const email = !ambiguous && emails.size === 1 ? [...emails][0] : null;
  let credits = null;
  const dialogs = [...document.querySelectorAll('[role="dialog"][aria-label], dialog[aria-label]')]
    .filter(node => /^(?:Configurações da conta|Account settings)$/i.test(node.getAttribute("aria-label") || "") && visible(node));
  if (email && dialogs.length === 1) {
    const dialog = dialogs[0];
    const accounts = new Set(addresses(dialog.innerText || "").map(value => value.toLowerCase()));
    const candidates = [];
    for (const link of dialog.querySelectorAll('a, [role="link"]')) {
      if (!visible(link)) continue;
      const text = (link.getAttribute("aria-label") || link.innerText || "").trim();
      const match = text.match(/^(\d+|\d{1,3}(?:\.\d{3})+|\d{1,3}(?:[\u00a0\u202f ]\d{3})+) créditos do Google Flow$/i) ||
        text.match(/^(\d+|\d{1,3}(?:,\d{3})+|\d{1,3}(?:[\u00a0\u202f ]\d{3})+) Google Flow credits$/i);
      if (!match) continue;
      let valid = accounts.size === 1 && accounts.has(email);
      const href = link.getAttribute("href");
      if (href !== null) {
        try {
          const url = new URL(href);
          const values = url.searchParams.getAll("Email");
                     valid = (accounts.size === 0 || (accounts.size === 1 && accounts.has(email))) && url.protocol === "https:" && url.hostname === "accounts.google.com" &&
            !url.port && !url.username && !url.password && url.pathname === "/AccountChooser" &&
            values.length === 1 && values[0].toLowerCase() === email;
        } catch { valid = false; }
      }
      const value = Number(match[1].replace(/[.,\u00a0\u202f ]/g, ""));
      candidates.push(valid && Number.isSafeInteger(value) ? value : null);
    }
    if (candidates.length === 1) credits = candidates[0];
  }
  return {origin: location.origin, pathname: location.pathname, email, credits,
    credits_source: credits !== null ? "account_settings_dialog" : "unavailable",
    observed_at: email ? new Date().toISOString() : null};
}

async function inspect(tabId, pinned = null) {
  const tab = await chrome.tabs.get(tabId);
  if (tab.discarded || tab.status !== "complete" || !flowScope(tab.url)) {
    throw new Error("tab_unavailable");
  }
  const target = pinned ? {tabId, documentIds: [pinned.document_id]} : {tabId, frameIds: [0]};
  const results = await chrome.scripting.executeScript({target, world: "ISOLATED", func: inspectPage});
  if (results.length !== 1 || results[0].frameId !== 0 || !results[0].documentId) {
    throw new Error("inspection_failed");
  }
  const result = results[0];
  const page = flowScope(result.result.origin + result.result.pathname);
  const after = await chrome.tabs.get(tabId);
  const current = flowScope(after.url);
  if (!page || !current || after.status !== "complete" ||
      page.origin !== current.origin || page.project_id !== current.project_id) {
    throw new Error("scope_changed");
  }
  const scope = {tab_id: tabId, ...page, document_id: result.documentId};
  if (pinned && derived && sameScope(pinned, derived.scope)) {
    if (!sameScope(scope, derived.page) || result.result.email !== derived.email) throw new Error("scope_changed");
    return {...derived.scope};
  }
  if (pinned && !sameScope(scope, pinned)) throw new Error("scope_changed");
  return scope;
}

async function select(tabId, notify = true) {
  if (!Number.isInteger(tabId) || tabId < 0) throw new Error("invalid_request");
  invalidate(notify);
  const revision = epoch;
  const scope = await inspect(tabId);
  if (revision !== epoch) throw new Error("scope_changed");
  selected = scope;
  return scope;
}

async function createProject(scope, revision, diagnostics = false, title = "OpenFlow project") {
  if (scope.origin !== "https://flow.google.com" || scope.project_id !== null) throw new Error("invalid_request");
  const account = await chrome.scripting.executeScript({
    target: {tabId: scope.tab_id, documentIds: [scope.document_id]}, world: "ISOLATED", func: inspectPage
  });
  const email = account[0]?.result?.email;
  if (revision !== epoch || account.length !== 1 || account[0].frameId !== 0 || account[0].documentId !== scope.document_id) throw new Error("scope_changed");
  if (!email) return {state: "not_submitted", scope, error: "account_evidence_required"};
  const results = await chrome.scripting.executeScript({
    target: {tabId: scope.tab_id, documentIds: [scope.document_id]}, world: "MAIN",
    func: flowOperation, args: ["create_project", {scope, title, ...(diagnostics ? {diagnostics: true} : {})}]
  });
  if (revision !== epoch || results.length !== 1 || results[0].frameId !== 0 || results[0].documentId !== scope.document_id) throw new Error("scope_changed");
  await inspect(scope.tab_id, scope);
  const result = results[0].result;
  if (result.state === "completed") {
    if (!sameScope(result.scope, scope) || !sameScope({...result.selected, project_id: null}, scope) ||
        !/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(result.selected.project_id)) throw new Error("scope_changed");
    derived = {page: scope, scope: result.selected, email};
    selected = result.selected;
    await inspect(scope.tab_id, selected);
  }
  if (revision !== epoch) throw new Error("scope_changed");
  return result;
}

async function extensionFingerprint() {
  let hash = 2166136261;
  for (const asset of ["background.js", "operations.mjs", "protocol.mjs", "manifest.json", "hijack_bypass.js"]) {
    try {
      const text = await (await fetch(chrome.runtime.getURL(asset))).text();
      for (let index = 0; index < text.length; index++) {
        hash ^= text.charCodeAt(index);
        hash = Math.imul(hash, 16777619);
      }
    } catch { hash = 0; }
  }
  return (hash >>> 0).toString(16).padStart(8, "0");
}

async function dispatch(message) {
  if (message.operation === "capabilities") return {build: "operational-bench-2", source_digest: await extensionFingerprint(), operations: ["list_project_media", "find_project_media", "probe", "reload_extension", "reload_tab", "create_project", "upload", "generate_image", "generate_video", "poll", "upscale_image", "upscale_video"], model_availability: "configured_not_live_verified", upscale: "experimental", video_4k: "unsupported"};
  if (message.operation === "reload_extension") {
    setTimeout(() => chrome.runtime.reload(), 250);
    return {reloading: true};
  }
  if (message.operation === "reload_tab") {
    if (!selected) throw new Error("select_flow_tab_required");
    if (!sameScope(selected, message.params.scope)) throw new Error("scope_changed");
    const revision = epoch;
    const reloading = await inspect(selected.tab_id, message.params.scope);
    if (revision !== epoch || !sameScope(selected, reloading)) throw new Error("scope_changed");
    setTimeout(async () => {
      try {
        await inspect(reloading.tab_id, reloading);
        if (revision !== epoch || !sameScope(selected, reloading)) return;
        invalidate();
        await chrome.tabs.reload(reloading.tab_id, {bypassCache: true});
      } catch {}
    }, 0);
    return {reloading: reloading.tab_id, project_id: reloading.project_id,
      previous_document_id: reloading.document_id};
  }
  if (message.operation === "snapshot") {
    if (selected) {
      try { await inspect(selected.tab_id, selected); } catch { invalidate(); }
    }
    const available = await tabs();
    if (!selected && available.length === 1) await select(available[0].tab_id, false);
    return {tabs: available, selected};
  }
  if (message.operation === "select") return select(message.params.tab_id, false);
  if (!sameScope(selected, message.params.scope)) throw new Error("scope_changed");
  const revision = epoch;
  const scope = await inspect(selected.tab_id, selected);
  if (revision !== epoch) throw new Error("scope_changed");
  if (message.operation === "captcha_probe") {
    const results = await chrome.scripting.executeScript({
      target: {tabId: scope.tab_id, documentIds: [scope.document_id]}, world: "MAIN",
      func: () => {
        const gc = globalThis.grecaptcha;
        const ent = gc?.enterprise;
        const exec = ent?.execute;
        let executeSource = null;
        try { executeSource = typeof exec === "function" ? exec.toString().slice(0, 400) : null; } catch {}
        const restricted = typeof executeSource === "string" && executeSource.includes("extension_hijack_detected");
        const reasons = [];
        if (!gc) reasons.push("grecaptcha_absent");
        if (gc && !ent) reasons.push("enterprise_absent");
        if (ent && typeof exec !== "function") reasons.push("execute_not_assigned");
        if (restricted) reasons.push("native_recaptcha_automation_unsupported");
        return {
          native_restriction: restricted,
          grecaptcha_present: typeof gc !== "undefined",
          enterprise_present: !!ent,
          ready_function: typeof ent?.ready === "function",
          execute_function: typeof exec === "function",
          execute_trapped_wrapper: typeof executeSource === "string" &&
            executeSource.includes("extension_hijack_detected"),
          document_ready_state: document.readyState,
          reasons,
        };
      }
    });
    if (revision !== epoch || results.length !== 1 ||
        results[0].documentId !== scope.document_id || results[0].frameId !== 0) {
      throw new Error("scope_changed");
    }
    await inspect(scope.tab_id, scope);
    if (revision !== epoch) throw new Error("scope_changed");
    return {scope, captcha: results[0].result};
  }
  if (message.operation === "home_read") {
    const results = await chrome.scripting.executeScript({
      target: {tabId: scope.tab_id, documentIds: [scope.document_id]}, world: "ISOLATED",
      func: () => ({
        pathname: /^\/(?:u\/\d+\/)?(?:project\/[a-f0-9-]{36}\/?|home\/?|)?$/i.test(location.pathname) ? location.pathname : null,
        route_shape: location.pathname.split("/").slice(0, 8).map(part => /^(?:u|project|home|\d{1,3}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})$/i.test(part) ? part : part ? "[redacted]" : "").join("/"),
        links: [...document.querySelectorAll('a[href]')].flatMap(node => {
          const url = new URL(node.href, location.href);
          const match = url.pathname.match(/^\/(?:u\/\d+\/)?project\/([a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})\/?$/i);
          return url.origin === location.origin && url.protocol === "https:" && !url.username && !url.password && !url.search && !url.hash && match
            ? [{project_id: match[1], pathname: url.pathname, title: (node.innerText || '').slice(0, 200)}] : [];
        }).slice(0, 100),
        scripts: [...new Set([...document.scripts].map(node => node.src).concat(
          performance.getEntriesByType("resource").map(entry => entry.name)))].filter(value => {
          try { const url = new URL(value); return url.protocol === "https:" &&
            ["www.gstatic.com", "flow.google.com"].includes(url.hostname) && !url.search && !url.hash; }
          catch { return false; }
        }).slice(0, 50)
      })
    });
    if (revision !== epoch || results.length !== 1 || results[0].documentId !== scope.document_id || results[0].frameId !== 0) throw new Error("scope_changed");
    await inspect(scope.tab_id, scope);
    if (revision !== epoch) throw new Error("scope_changed");
    const native = await chrome.scripting.executeScript({
      target: {tabId: scope.tab_id, documentIds: [scope.document_id]}, world: "MAIN",
      func: () => ({grecaptcha: typeof globalThis.grecaptcha !== "undefined",
        enterprise: typeof globalThis.grecaptcha?.enterprise !== "undefined",
        ready: typeof globalThis.grecaptcha?.enterprise?.ready === "function",
        execute: typeof globalThis.grecaptcha?.enterprise?.execute === "function",
        native_restriction:
          (() => { try { return globalThis.grecaptcha?.enterprise?.execute.toString().includes("extension_hijack_detected") === true; } catch { return false; } })()})
    });
    if (revision !== epoch || native.length !== 1 || native[0].documentId !== scope.document_id || native[0].frameId !== 0) throw new Error("scope_changed");
    await inspect(scope.tab_id, scope);
    if (revision !== epoch) throw new Error("scope_changed");
    return {scope, ...results[0].result, captchaStatus: native[0].result, source: "home_dom_read_only"};
  }
  if (message.operation === "create_project") return createProject(scope, revision, message.params.diagnostics === true, message.params.title);
  if (["inspect", "refresh_account"].includes(message.operation)) {
    let opened = false;
    const panel = async action => {
      const result = await chrome.scripting.executeScript({
        target: {tabId: scope.tab_id, documentIds: [scope.document_id]}, world: "ISOLATED",
        func: accountPanel, args: [action]
      });
      if (revision !== epoch || result.length !== 1 || result[0].documentId !== scope.document_id || result[0].frameId !== 0) throw new Error("scope_changed");
      return result[0].result === true;
    };
    try {
      if (message.operation === "refresh_account") opened = await panel("open");
    const observations = await chrome.scripting.executeScript({
      target: {tabId: scope.tab_id, documentIds: [scope.document_id]}, world: "ISOLATED", func: inspectPage
    });
    if (revision !== epoch || observations.length !== 1 || observations[0].documentId !== scope.document_id || observations[0].frameId !== 0) throw new Error("scope_changed");
    const page = observations[0].result;
    const current = flowScope(page.origin + page.pathname);
    const pageScope = derived && sameScope(scope, derived.scope) ? derived.page : scope;
    if (!current || current.origin !== pageScope.origin || current.project_id !== pageScope.project_id ||
        (derived && page.email !== derived.email)) throw new Error("scope_changed");
    const email = typeof page.email === "string" ? page.email : null;
    return {scope, email, email_confirmed: !!email, credits: page.credits ?? null,
      source: email ? "google_account_aria" : "unavailable",
      credits_source: page.credits_source ?? "unavailable", observed_at: page.observed_at ?? null};
    } finally {
      if (opened && revision === epoch) await panel("close");
    }
  }
  const results = await chrome.scripting.executeScript({
    target: {tabId: scope.tab_id, documentIds: [scope.document_id]}, world: "MAIN",
    func: flowOperation, args: [message.operation, {...message.params, diagnostics: true,
      page_scope: derived && sameScope(scope, derived.scope) ? derived.page : scope},
      operationErrorSource]
  });
  if (revision !== epoch || results.length !== 1 || results[0].documentId !== scope.document_id || results[0].frameId !== 0) throw new Error("scope_changed");
  await inspect(scope.tab_id, scope);
  if (revision !== epoch) throw new Error("scope_changed");
  const result = results[0].result;
  // Chrome does NOT reject executeScript when the injected function throws: it swallows the
  // error and reports a null injection result. Returning that null would make the bridge reply
  // carry result:null with no error, which the Python side can only read as an opaque
  // invalid_response:python_parse. Surface it as a real, classified bridge error instead.
  if (result === null || typeof result !== "object") throw new Error("inspection_failed");
  return result;
}

async function connect() {
  if (connecting || socket) return;
  connecting = true;
  const revision = connectionRevision;
  let ws;
  try {
    const config = await chrome.storage.local.get(["secret", "installation_id", "disabled"]);
    if (config.disabled || revision !== connectionRevision) return;
    enabled = true;
    const url = serverUrl(bridgeUrl);
    if (!/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(config.installation_id || "")) {
      config.installation_id = crypto.randomUUID();
      await chrome.storage.local.set({installation_id: config.installation_id});
    }
    if (!enabled || revision !== connectionRevision) return;
    ws = new WebSocket(url);
    socket = ws;
    status = "waiting";
    retryDelay = null;
    seen = new Set();
    ws.onopen = () => {
      if (socket !== ws) return;
      status = "pairing";
      lastPong = Date.now();
      ws.send(JSON.stringify(config.secret
        ? {type: "hello", version: 1, secret: config.secret, installation_id: config.installation_id}
        : {type: "pair", version: 1, installation_id: config.installation_id}));
      heartbeat = setInterval(() => {
        verifyHeartbeat();
        if (socket === ws && ws.readyState === WebSocket.OPEN) ws.send('{"type":"ping"}');
      }, 20000);
    };
    ws.onmessage = event => handleMessage(event).catch(() => {
      if (socket === ws) ws.close();
    });
    const handleMessage = async event => {
      let message;
      try { message = JSON.parse(event.data); } catch { ws.close(); return; }
      if (socket !== ws) return;
      if (message.type === "pairing_pending" && status === "pairing" && !config.secret) {
        pairingId = message.pairing_id;
        status = "awaiting_approval";
        return;
      }
      if (message.type === "provisioned" && status === "awaiting_approval" &&
          /^[A-Za-z0-9_-]{64}$/.test(message.secret)) {
        await chrome.storage.local.set({secret: message.secret});
        if (socket !== ws) return;
        config.secret = message.secret;
        status = "pairing";
        ws.send(JSON.stringify({type: "hello", version: 1, secret: config.secret,
          installation_id: config.installation_id}));
        return;
      }
      if (message.type === "paired" && status === "pairing" && config.secret) {
        status = "paired";
        pairingId = null;
        await chrome.storage.local.set({retryAttempt: 0});
        await chrome.alarms.clear(retryAlarm);
        await discover();
        return;
      }
      if (message.type === "pong") { lastPong = Date.now(); return; }
      if (busy && validRequest(message)) {
        for (let attempt = 0; busy && attempt < 50; attempt++) await new Promise(resolve => setTimeout(resolve, 100));
        if (socket !== ws) return;
      }
      if (status !== "paired" || !validRequest(message) || busy || seen.has(message.id)) {
        ws.close(); return;
      }
      if (seen.size >= 10000) { ws.close(); return; }
      seen.add(message.id);
      busy = true;
      try {
        const result = await dispatch(message);
        if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type: "reply", id: message.id, result}));
      } catch (error) {
        invalidate();
        const code = ["scope_changed", "tab_unavailable", "invalid_request"].includes(error.message)
          ? error.message : "inspection_failed";
        if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type: "reply", id: message.id, error: code}));
      } finally { busy = false; }
    };
    ws.onclose = event => handleClose(event).catch(() => {
      if (status === "waiting") status = "retry_unavailable";
    });
    const handleClose = async event => {
      if (socket !== ws) return;
      release(ws);
      if (event?.reason === "revoked" || event?.code === 1008) {
        await stop("authorization_required");
        await chrome.storage.local.remove("secret");
      } else if (event?.reason === "disconnected") {
        await stop();
      } else {
        status = "waiting";
        await scheduleRetry();
      }
    };
    ws.onerror = () => {
      if (socket !== ws) return;
      release(ws);
      status = "waiting";
      ws.close();
      void scheduleRetry();
    };
    await chrome.alarms.create(retryAlarm, {delayInMinutes: 0.5});
  } catch {
    if (ws && socket !== ws) return;
    if (ws) {
      release(ws);
      ws.close();
    }
    if (revision !== connectionRevision) return;
    if (!ws || !["disconnected", "authorization_required"].includes(status)) {
      status = "waiting";
      await scheduleRetry();
    }
  } finally { connecting = false; }
}

chrome.tabs.onUpdated.addListener((tabId, change) => {
  if (selected?.tab_id === tabId && (change.url || change.status === "loading")) invalidate();
  if (change.status === "complete") void discover();
});
chrome.tabs.onRemoved.addListener(tabId => {
  if (selected?.tab_id === tabId) invalidate();
  void discover();
});
chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (sender.id !== chrome.runtime.id || sender.url !== chrome.runtime.getURL("popup.html")) return false;
  (async () => {
    await initialization;
    verifyHeartbeat();
    if (message.type === "status") {
      let account = null;
      if (!busy && selected && status === "paired") {
        busy = true;
        try { account = await dispatch({operation: "inspect", params: {scope: {...selected}}}); }
        catch { invalidate(); }
        finally { busy = false; }
      }
      const available = await tabs();
      if (!sameScope(account?.scope, selected)) account = null;
      verifyHeartbeat();
      if (status !== "paired") account = null;
      return {status, enabled, retry_delay_minutes: retryDelay, pairing_id: pairingId,
        tabs: available, selected, email: account?.email ?? null, credits: account?.credits ?? null,
        account_status: account?.email ? "observed" : "unavailable"};
    }
    if (message.type === "connect") {
      await chrome.storage.local.set({disabled: false, retryAttempt: 0});
      await connect();
      return {status};
    }
    if (message.type === "disconnect") { await stop(); return {status}; }
    if (message.type === "select") {
      if (busy) return {error: "busy"};
      busy = true;
      try { return {selected: await select(message.tab_id)}; } finally { busy = false; }
    }
    return {error: "invalid_request"};
  })().then(reply).catch(() => reply({error: "action_failed"}));
  return true;
});
async function resume() {
  await initialization;
  verifyHeartbeat();
  if (socket && ["connecting", "waiting"].includes(status)) {
    const ws = socket;
    release(ws);
    status = "waiting";
    ws.close();
  }
  await connect();
}
chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === retryAlarm) void resume();
});
chrome.runtime.onStartup.addListener(() => { void resume(); });
chrome.runtime.onInstalled.addListener(() => { void resume(); });
async function initialize() {
  try {
    await chrome.storage.local.setAccessLevel({accessLevel: "TRUSTED_CONTEXTS"});
    await connect();
  } catch {
    status = "initialization_failed";
  }
}

const initialization = initialize();
