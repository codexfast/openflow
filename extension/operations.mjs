export const knownOperationErrors = /^(scope_changed|page_auth_unavailable|native_recaptcha_[a-z_]+|rpc_[a-z_]+|invalid_[a-z_]+|response_too_large|operation_scope_mismatch|image_result_unrecognized|captcha_[a-z_]+)$/;

// Pure normalizer for the error classifier, kept at module scope so it can be unit tested.
// It is ALSO inlined verbatim inside flowOperation: that function is injected into the page
// MAIN world through chrome.scripting.executeScript, which recompiles only the function's own
// source with Function.prototype.toString. It cannot see module-scope bindings, so it must not
// call out to this helper. Keep the two bodies identical.
export function operationErrorMatcher(value) {
  const fallback = /^(scope_changed|page_auth_unavailable|native_recaptcha_[a-z_]+|rpc_[a-z_]+|invalid_[a-z_]+|response_too_large|operation_scope_mismatch|image_result_unrecognized|captcha_[a-z_]+)$/;
  if (value instanceof RegExp) return value;
  if (typeof value === "string" && value.length > 0 && value.length <= 512) {
    try { return new RegExp(value); } catch { return fallback; }
  }
  if (value && typeof value === "object" && typeof value.source === "string") {
    try { return new RegExp(value.source, typeof value.flags === "string" ? value.flags : ""); }
    catch { return fallback; }
  }
  return fallback;
}

export async function flowOperation(operation, params, operationErrors) {
  // Injected into the page MAIN world; self-contained by necessity. The third argument crosses
  // chrome.scripting.executeScript, whose args are serialized as JSON: a RegExp does NOT survive
  // and lands as a plain {} object (verified live in Chrome 153). A {} is truthy, so the old
  // `operationErrors ?? default` kept it and the catch block then called {}.test(...) -> TypeError,
  // which Chrome silently turns into a null injection result and drops the whole reply. Accept the
  // classifier as a RegExp (same-realm call) or a STRING source (survives the boundary) and rebuild.
  const fallbackErrors = /^(scope_changed|page_auth_unavailable|native_recaptcha_[a-z_]+|rpc_[a-z_]+|invalid_[a-z_]+|response_too_large|operation_scope_mismatch|image_result_unrecognized|captcha_[a-z_]+)$/;
  const isKnownError = operationErrors instanceof RegExp ? operationErrors
    : (typeof operationErrors === "string" && operationErrors.length > 0 && operationErrors.length <= 512
      ? (() => { try { return new RegExp(operationErrors); } catch { return fallbackErrors; } })()
      : fallbackErrors);
  const scope = {...params.scope, project_id: params.scope.project_id ?? null};
  let sent = false;
  let phase = "preflight";
  const diagnostics = {};
  const initialPath = location.pathname;
  const shape = (value, depth = 0) => value === null ? "null" : Array.isArray(value) ?
    {type: "array", length: value.length, items: depth < 2 ? value.slice(0, 8).map(item => shape(item, depth + 1)) : []} : typeof value;
  const finish = result => params.diagnostics === true ? {...result, diagnostics} : result;
  const check = () => {
    if (operation === "delete_project" && location.pathname !== initialPath) throw new Error("scope_changed");
    const pageScope = params.page_scope || scope;
    const project = location.pathname.match(/\/project\/([a-f0-9-]{36})(?:\/|$)/i)?.[1] ?? null;
    const pageProject = pageScope.project_id ?? null;
    if (location.origin !== pageScope.origin || project !== pageProject || location.origin !== "https://flow.google.com") throw new Error("scope_changed");
  };
  const uuid = () => crypto.randomUUID().toUpperCase();
  const idPattern = /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i;
  const validId = value => typeof value === "string" && idPattern.test(value);
  const validMedia = value => typeof value === "string" && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}(?:_upsampled)?$/i.test(value);
  const walk = function* (node) {
    if (Array.isArray(node)) { yield node; for (const item of node) yield* walk(item); }
  };
  const urls = (payload, kind, expected = null) => {
    const found = new Map();
    for (const node of walk(payload)) for (const value of node) {
      if (typeof value !== "string" || !value.startsWith("https://")) continue;
      try {
        const u = new URL(value);
        const match = u.pathname.match(/^\/(image|video)\/([a-f0-9-]{36}(?:_upsampled)?)$/i);
        if (u.hostname !== "flow-content.google" || u.port || u.username || u.password || u.hash || !(u.searchParams.get("Signature") || u.searchParams.get("X-Goog-Signature")) || !match || match[1] !== kind || !validMedia(match[2]) || (expected && match[2] !== expected)) continue;
        found.set(match[2], {media_id: match[2], kind, url: value});
      } catch {}
    }
    return [...found.values()];
  };
  const parse = (text, rpc) => {
    let envelopes = 0;
    let chunks = 0;
    let start = -1, depth = 0, quoted = false, escaped = false;
    for (let index = text.startsWith(")]}'") ? text.indexOf("\n") + 1 : 0; index < text.length; index++) {
      const char = text[index];
      if (start < 0) {
        if (char !== "[") continue;
        start = index;
        depth = 0;
      }
      if (quoted) {
        if (escaped) escaped = false;
        else if (char === "\\") escaped = true;
        else if (char === '"') quoted = false;
        continue;
      }
      if (char === '"') { quoted = true; continue; }
      if (char === "[") depth++;
      if (char !== "]" || --depth !== 0) continue;
      let chunk;
      try { chunk = JSON.parse(text.slice(start, index + 1)); }
      catch { start = -1; continue; }
      start = -1;
      chunks++;
      diagnostics.chunks = chunks;
      for (const entry of chunk) {
        if (!Array.isArray(entry) || entry[0] !== "wrb.fr") continue;
        envelopes++;
        diagnostics.envelopes = envelopes;
        if (entry[1] !== rpc) continue;
        diagnostics.envelope_shape = shape(entry);
        if (entry[2] == null) {
          const code = entry[5]?.[0];
          if (Number.isSafeInteger(code)) diagnostics.rpc_code = code;
          diagnostics.rpc_error_shape = shape(entry[5]);
          throw new Error("rpc_rejected");
        }
        let payload = entry[2];
        if (typeof payload === "string") {
          try { payload = JSON.parse(payload); }
          catch { throw new Error("invalid_rpc_payload"); }
        }
        diagnostics.payload_shape = shape(payload);
        return payload;
      }
    }
    throw new Error(envelopes ? "rpc_envelope_missing" : "invalid_batch_response");
  };
  const rpc = async (name, inner) => {
    const allowed = {get_credits: ["nzlxg"], list_projects: ["UpteDb"], list_project_media: ["Zzl0ze"], find_project_media: ["Zzl0ze"], probe: ["Zzl0ze"], create_project: ["jHPbke"], delete_project: ["UpteDb", "QI2zvc"], poll: ["jwpduf", "Zzl0ze", "as29s"],
      upscale_image: ["SPrCad"], upscale_video: ["p0UkFb"],
      upload: ["maseQ"], generate_image: ["ogiZ0b"], generate_video: ["MZZa6b", "nprQif", "eb1hJf", "YhhmEf"]};
    if (!allowed[operation]?.includes(name)) throw new Error("invalid_rpc");
    check();
    const wiz = globalThis.WIZ_global_data;
    if (typeof wiz?.SNlM0e !== "string" || !wiz.SNlM0e) throw new Error("page_auth_unavailable");
    const query = new URLSearchParams({rpcids: name, "source-path": location.pathname,
      bl: wiz.cfb2h || "", "f.sid": wiz.FdrFJe || "", hl: document.documentElement.lang?.split("-")[0] || "en", _reqid: String(Math.floor(Math.random() * 900000) + 100000), rt: "c"});
    const body = new URLSearchParams({"f.req": JSON.stringify([[[name, JSON.stringify(inner), null, "generic"]]]), at: wiz.SNlM0e});
    diagnostics.rpc = name;
    const base = typeof wiz.eptZe === "string" ? wiz.eptZe : "/_/AiSandboxAngularFrontend/";
    const endpoint = new URL(base + "data/batchexecute", location.origin);
    if (operation === "list_projects") diagnostics.endpoint_path = endpoint.pathname.replace(/[^a-zA-Z0-9_/-]/g, "").slice(0, 200);
    if (endpoint.origin !== location.origin || !/^\/(?:u\/\d+\/)?_\/AiSandboxAngularFrontend\/data\/batchexecute$/.test(endpoint.pathname) || endpoint.search || endpoint.hash) throw new Error("invalid_rpc");
    phase = "transport";
    if (operation !== "delete_project" || name === "QI2zvc") sent = true;
    const response = await fetch(endpoint.pathname + "?" + query, {
      method: "POST", credentials: "include", redirect: "error", signal: AbortSignal.timeout(operation === "get_credits" ? 10000 : 180000),
      headers: {"content-type": "application/x-www-form-urlencoded;charset=UTF-8", "x-same-domain": "1"}, body
    });
    diagnostics.http_status = response.status;
    if (!response.ok) throw new Error("rpc_http_error");
    phase = "body";
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let text = "", size = 0;
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > 24000000) { await reader.cancel(); throw new Error("response_too_large"); }
      text += decoder.decode(value, {stream: true});
    }
    text += decoder.decode();
    check();
    phase = "parse";
    const payload = parse(text, name);
    phase = "validate";
    return payload;
  };
  try {
    check();
    if (operation === "get_credits") {
      const result = await rpc("nzlxg", []);
      if (!Array.isArray(result) || !Number.isSafeInteger(result[0]) || result[0] < 0) throw new Error("invalid_credits_response");
      return finish({scope, state: "completed", credits: result[0]});
    }
    if (operation === "list_projects") {
      if (scope.project_id !== null) throw new Error("invalid_project_scope");
      const payload = await rpc("UpteDb", ["projects/*", 100]);
      if (!Array.isArray(payload) || (payload[0] != null && !Array.isArray(payload[0]))) throw new Error("invalid_project_response");
      const projects = (payload[0] || []).map(record => {
        if (!validId(record?.[0]) || typeof record?.[1]?.[0] !== "string") throw new Error("invalid_project_response");
        return {project_id: record[0], title: record[1][0].slice(0, 200)};
      });
      return finish({scope, state: "completed", projects, has_more: !!payload[1], source: "FlowService.GetProjects:UpteDb"});
    }
    if (operation === "list_project_media" || operation === "find_project_media") {
      const limit = params.input?.limit;
      const requestedMedia = params.input?.media_id;
      if (!validId(scope.project_id) ||
          (operation === "list_project_media" && (!Number.isSafeInteger(limit) || limit < 1 || limit > 100)) ||
          (operation === "find_project_media" && !validMedia(requestedMedia))) throw new Error("invalid_catalog_input");
      const payload = await rpc("Zzl0ze", [`projects/${scope.project_id}`, null, null, null, [1]]);
      // Only captured root/row paths count. Never recursively discover IDs or URLs.
      if (!Array.isArray(payload) || payload.length !== 8 || !Array.isArray(payload[1]) || !Array.isArray(payload[2]) || payload[1].length > 1000 || payload[2].length > 1000) throw new Error("invalid_catalog_response");
      const sameProject = value => validId(value) && value.toLowerCase() === scope.project_id.toLowerCase();
      const thumbnail = value => {
        if (typeof value !== "string" || value.length > 8192 || /[\x00-\x20\x7f\\\\#]/.test(value) || !value.startsWith("https://lh3.googleusercontent.com/")) return null;
        try {
          const url = new URL(value);
          if (url.origin !== "https://lh3.googleusercontent.com" || url.username || url.password || url.port || url.pathname === "/") return null;
          return value;
        } catch { return null; }
      };
      const media = new Map();
      for (const row of payload[2]) {
        if (!Array.isArray(row) || ![7, 8].includes(row.length) || !validMedia(row[0]) || !sameProject(row[1]) || media.has(row[0].toLowerCase())) throw new Error("invalid_catalog_response");
        // Observed image/video detail slots, not CAE/CAI or thumbnail file format.
        const detail = value => Array.isArray(value) && value.length === 6 && Array.isArray(value[0]);
        const image = detail(row[6]) && row[7] == null;
        const video = row[6] == null && detail(row[7]);
        media.set(row[0].toLowerCase(), {
          kind: image ? "image" : video ? "video" : "unknown",
          thumbnail_url: thumbnail(Array.isArray(row[5]) ? row[5][5] : null)
        });
      }
      const scenes = new Set();
      const items = [];
      // Validate every row before applying limit or matching, including foreign scenes beyond limit.
      for (const row of payload[1]) {
        if (!Array.isArray(row) || row.length !== 5 || !validId(row[0]) || !sameProject(row[4]) || !Array.isArray(row[3]) || row[3].length !== 7 || !validMedia(row[3][4]) || scenes.has(row[0].toLowerCase()) || !media.has(row[3][4].toLowerCase())) throw new Error("invalid_catalog_response");
        scenes.add(row[0].toLowerCase());
        // Metadata comes only from the validated, project-bound media record.
        items.push({media_id: row[3][4], scene_id: row[0], ...media.get(row[3][4].toLowerCase())});
      }
      if (operation === "find_project_media") {
        const item = items.find(value => value.media_id.toLowerCase() === requestedMedia.toLowerCase());
        return item ? {scope, state: "completed", observed: true, item,
          source: "remote_project_scenes", completeness: "unknown"} :
          {scope, state: "completed", observed: false, reason: "not_found_in_observed_catalog",
            source: "remote_project_scenes", completeness: "unknown"};
      }
      return {scope, state: "completed", items: items.slice(0, limit),
        source: "remote_project_scenes", completeness: "unknown", truncated: items.length > limit};
    }
    if (operation === "delete_project") {
      const p = params.input;
      if (scope.project_id !== null || params.page_scope ||
          !/^\/(?:u\/\d+\/?)?$/.test(location.pathname) ||
          !p || Object.keys(p).sort().join(",") !== "account_sha256,expected_title,project_id" ||
          !validId(p.project_id) || typeof p.expected_title !== "string" ||
          !p.expected_title.length || p.expected_title.length > 10000 ||
          typeof p.account_sha256 !== "string" || !/^[a-f0-9]{64}$/.test(p.account_sha256)) {
        throw new Error("invalid_delete_input");
      }
      // Identity must remain visible and unambiguous; never open account panels.
      const verifyAccount = async () => {
        const emails = new Set(); let ambiguous = false;
        for (const node of document.querySelectorAll(
            'button[aria-label],a[aria-label],[role="button"][aria-label]')) {
          const label = node.getAttribute("aria-label") || "";
          if (!node.getClientRects().length || !/^(?:Google Account|Conta do Google):\s/i.test(label)) continue;
          let visible = true;
          for (let n = node; n; n = n.parentElement) {
            const style = getComputedStyle(n);
            if (n.hidden || n.getAttribute("aria-hidden") === "true" || style.display === "none" ||
                ["hidden", "collapse"].includes(style.visibility) || style.opacity === "0") visible = false;
          }
          if (!visible) continue;
          const found = label.match(/[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi) || [];
          if (found.length !== 1) ambiguous = true;
          else emails.add(found[0].toLowerCase());
        }
        if (ambiguous || emails.size !== 1) throw new Error("invalid_account_evidence");
        const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode([...emails][0]));
        const hash = [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, "0")).join("");
        if (hash !== p.account_sha256) throw new Error("invalid_account_binding");
        check();
      };
      await verifyAccount();
      const listing = await rpc("UpteDb", ["projects/*", 100]);
      if (!Array.isArray(listing) || !Array.isArray(listing[0]) || listing[0].length > 100) {
        throw new Error("invalid_project_response");
      }
      const matches = listing[0].filter(record => record?.[0] === p.project_id);
      if (matches.length !== 1 || matches[0]?.[1]?.[0] !== p.expected_title) {
        throw new Error("invalid_delete_target_or_title");
      }
      await verifyAccount();
      const result = await rpc("QI2zvc", [`projects/${p.project_id}`]);
      if (!Array.isArray(result) || result.length !== 0) throw new Error("invalid_delete_response");
      return finish({scope, state: "completed", deleted_project_id: p.project_id});
    }
    if (operation === "probe") {
      diagnostics.auth_present = typeof globalThis.WIZ_global_data?.SNlM0e === "string" && !!globalThis.WIZ_global_data.SNlM0e;
    diagnostics.native_restriction =
      (() => { try { return globalThis.grecaptcha?.enterprise?.execute?.toString().includes("extension_hijack_detected") === true; } catch { return false; } })();
    diagnostics.captcha_pristine_available = typeof globalThis.__fk_hijack?.pristine === "function";
    diagnostics.captcha_source = globalThis.__fk_hijack?.source ?? null;
    if (!validId(params.input?.project_id)) throw new Error("invalid_project_scope");
      const payload = await rpc("Zzl0ze", [`projects/${params.input.project_id}`, null, null, null, [1]]);
      if (!Array.isArray(payload)) throw new Error("invalid_project_response");
      diagnostics.phase = "validated_envelope";
      return finish({scope, state: "completed", rpc_verified: true});
    }
    if (!["create_project", "poll", "upload", "generate_image", "generate_video", "upscale_image", "upscale_video"].includes(operation)) throw new Error("invalid_operation");
    if (operation === "create_project") {
      if (scope.project_id !== null) throw new Error("invalid_project_scope");
      const title = Object.hasOwn(params, "title") ? params.title : "OpenFlow project";
      if (typeof title !== "string" || title.length < 1 || title.length > 200 || !title.trim()) throw new Error("invalid_project_title");
      const result = await rpc("jHPbke", ["projects/*", [null, [title]], [null, 22]]);
      if (!validId(result?.[0])) throw new Error("invalid_project_response");
      return finish({scope, state: "completed", selected: {...scope, project_id: result[0]}});
    }
    const p = params.input;
    if (operation === "generate_image" && Object.hasOwn(p, "seed") && p.seed !== null &&
        (!Number.isInteger(p.seed) || p.seed < 1 || p.seed > 1e9)) throw new Error("invalid_image_seed");
    if (operation === "poll") {
      let media = p.media_id;
      if (!media && p.operation_id) {
        const sameId = (a, b) => typeof a === "string" && typeof b === "string" && a.toLowerCase() === b.toLowerCase();
        const result = await rpc("jwpduf", [null, null, [[p.operation_id]]]);
        const record = result?.[2]?.[0];
        if (!record || !sameId(record[0], p.operation_id) || (typeof record[1] === "string" && !sameId(record[1], scope.project_id))) throw new Error("operation_scope_mismatch");
        const done = record[3] === "CAE";
        const listing = await rpc("Zzl0ze", [`projects/${scope.project_id}`, null, null, null, [1]]);
        for (const node of walk(listing)) {
          if (!Array.isArray(node) || node.length < 4 || !sameId(node[0], p.operation_id)) continue;
          const detail = node[3];
          const proj = node.length > 4 ? node[4] : scope.project_id;
          if (typeof proj === "string" && !sameId(proj, scope.project_id)) continue;
          if (Array.isArray(detail) && detail.length > 4 && validMedia(detail[4])) media = detail[4];
        }
        if (!media && !done) return {scope, state: "pending", operation_id: p.operation_id};
        if (!media) {
          for (const node of walk(listing)) {
            if (node[0] === p.operation_id && validMedia(node[3]?.[4])) media = node[3][4];
          }
        }
      }
      if (!media) return {scope, state: "pending", operation_id: p.operation_id};
      const result = await rpc("as29s", [media]);
      const assets = urls(result, p.kind, media);
      return {scope, state: assets.length ? "completed" : "pending", media_id: media, assets};
    }
    const hijack = globalThis.__fk_hijack;
    const enterprise = globalThis.grecaptcha?.enterprise;
    if (!enterprise?.ready || !enterprise?.execute) throw new Error("native_recaptcha_enterprise_unavailable");
    const keys = new Set();
    for (const script of document.scripts) {
      try {
        const u = new URL(script.src);
        if (!["www.google.com", "www.recaptcha.net"].includes(u.hostname) || u.pathname !== "/recaptcha/enterprise.js") continue;
        const key = u.searchParams.get("render");
        if (key && /^[A-Za-z0-9_-]{20,100}$/.test(key)) keys.add(key);
      } catch {}
    }
    try {
      const clients = globalThis.___grecaptcha_cfg?.clients || {};
      for (const k of Object.keys(clients)) {
        const sitekey = clients[k]?.sitekey;
        if (typeof sitekey === "string" && /^[A-Za-z0-9_-]{20,100}$/.test(sitekey)) keys.add(sitekey);
      }
    } catch {}
    if (keys.size !== 1) throw new Error("native_recaptcha_sitekey_unavailable_or_ambiguous");
    let execute = null;
    if (typeof hijack?.pristine === "function") {
      const source = Function.prototype.toString.call(hijack.pristine);
      if (!source.includes("extension_hijack_detected")) {
        execute = hijack.pristine;
      }
    }
    if (execute === null) {
      const source = Function.prototype.toString.call(enterprise.execute);
      if (!source.includes("extension_hijack_detected")) {
        execute = enterprise.execute;
      }
    }
    if (typeof execute !== "function") {
      throw new Error("native_recaptcha_automation_unsupported");
    }
    diagnostics.captcha_source = hijack?.source ?? "fallback";
    diagnostics.captcha_pristine_available = typeof hijack?.pristine === "function";
    const action = ["generate_video", "upscale_video"].includes(operation) ? "VIDEO_GENERATION" : "IMAGE_GENERATION";
    const token = await Promise.race([
      new Promise((resolve, reject) => enterprise.ready(() => Promise.resolve(execute.call(enterprise, [...keys][0], {action})).then(resolve, reject))),
      new Promise((_, reject) => setTimeout(() => reject(new Error("native_recaptcha_timeout")), 20000))
    ]);
    if (typeof token !== "string" || !token) throw new Error("native_recaptcha_empty_token");
    check();
    const context = () => [null, 22, null, null, null, scope.project_id, null, null, null, null, [token, 1]];
    const projectOk = (value) => typeof value !== "string" || value.toLowerCase() === scope.project_id.toLowerCase();
    if (operation === "upscale_image") {
      const ctx = context();
      ctx[5] = null;
      const result = await rpc("SPrCad", [p.media_id, p.resolution === "2K" ? 1 : 2, ctx]);
      const encoded = result?.[1];
      if (typeof encoded !== "string" || encoded.length < 100 || encoded.length > 22000000 || !/^[A-Za-z0-9+/]+={0,2}$/.test(encoded)) throw new Error("invalid_upscale_response");
      return {scope, state: "completed", encoded_image: encoded};
    }
    if (operation === "upscale_video") {
      const item = Array(32).fill(null);
      item[0] = [null, p.media_id];
      item[2] = p.ratio === "9:16" ? 1 : 2;
      item[4] = [null, p.scene_id, null, null, uuid()];
      item[6] = 2;
      item[31] = "veo_3_1_upsampler_1080p";
      const result = await rpc("p0UkFb", [[item], context(), [uuid()]]);
      const candidates = [result?.[2]?.[0], result?.[3]?.[0]];
      const fresh = candidates.find((r) => validId(r?.[0]) && r[0] !== p.media_id && r[0] !== p.scene_id);
      const record = fresh ?? result?.[2]?.[0];
      diagnostics.record_len = Array.isArray(record) ? record.length : null;
      diagnostics.record_id_valid = validId(record?.[0]);
      diagnostics.record_id = validId(record?.[0]) ? record[0] : null;
      if (!validId(record?.[0])) throw new Error("invalid_upscale_response");
      return {scope, state: "pending", operation_id: record[0]};
    }
    if (operation === "upload") {
      const result = await rpc("maseQ", [context(), p.data, p.mime, 1, null, null, null, null, p.name, null, uuid(), uuid()]);
      const record = result?.[0];
      if (!validId(record?.[0])) throw new Error("invalid_upload_response");
      return {scope, state: "completed", assets: [{media_id: record[0], kind: "image"}]};
    }
    if (operation === "generate_image") {
      const model = {nano_banana_pro: "GEM_PIX_2", nano_banana_2: "BELUGA", nano_banana_lite: "HARBOR_SEAL"}[p.model];
      const ratio = {"1:1": 1, "9:16": 2, "16:9": 3, "3:4": 4, "4:3": 5}[p.ratio];
      const refs = p.references.map(id => [id, null, null, null, 1]);
      const item = [null, null, refs.length ? refs : null, p.seed ?? (Math.floor(Math.random() * 1e9) + 1), ratio, model, null, context(), [[[p.prompt]]], null, null, null, uuid(), uuid()];
      const result = await rpc("ogiZ0b", [null, [item], 1, context(), [uuid()]]);
      const assets = urls(result, "image");
      if (!assets.length) throw new Error("image_result_unrecognized");
      return {scope, state: "completed", assets};
    }
    if (operation !== "generate_video") throw new Error("invalid_operation");
    const ratio = p.ratio === "9:16" ? 1 : 2;
    const frame = id => [null, id, null, null, null, [null, null, 1, 1]];
    const descriptor = () => [null, null, null, null, uuid(), uuid()];
    const low = p.resolution === "360p";
    const suffix = low ? "_360p" : "";
    const text = [null, null, [[[p.prompt]]]];
    let name, item;
    if (p.references.length) {
      name = "MZZa6b";
      item = [text, p.references.map(id => [null, id]), `abra_r2v_${p.duration}s${suffix}`, ratio, null, descriptor()];
      if (low) item.push(null, null, null, null, null, [4]);
    } else if (p.end) {
      name = "nprQif";
      item = [text, `omni_flash_i2v_${p.duration}s_first_last${suffix}`, ratio, null, frame(p.start), frame(p.end), descriptor()];
    } else if (p.start) {
      name = "eb1hJf";
      const model = p.model === "omni" ? `abra_i2v_${p.duration}s${suffix}` : "veo_3_1_i2v_lite";
      item = [text, model, ratio, null, frame(p.start), descriptor()];
      if (low) item.push(null, null, null, [4]);
    } else {
      name = "YhhmEf";
      item = [text, `abra_t2v_${p.duration}s${suffix}`, ratio, null, descriptor()];
      if (low) item.push(null, null, [4]);
    }
    const result = await rpc(name, [[item], context(), [uuid(), 2]]);
    if (name === "YhhmEf") {
      const record = result?.[3]?.[0];
      diagnostics.record_len = Array.isArray(record) ? record.length : null;
      diagnostics.record_id_valid = validId(record?.[0]);
      diagnostics.project_match = projectOk(record?.[1]);
      if (!validId(record?.[0])) throw new Error("invalid_video_response");
      return {scope, state: "pending", media_id: record[0]};
    }
    const record = result?.[2]?.[0];
    diagnostics.record_len = Array.isArray(record) ? record.length : null;
    diagnostics.record_id_valid = validId(record?.[0]);
    diagnostics.project_match = projectOk(record?.[1]);
    if (!validId(record?.[0])) throw new Error("invalid_video_response");
    return {scope, state: "pending", operation_id: record[0]};
  } catch (error) {
    const fallback = phase === "transport" ? (error?.name === "TimeoutError" ? "rpc_timeout" : "rpc_transport_error") : phase === "body" ? "rpc_body_read_error" : "page_operation_failed";
    diagnostics.phase = phase;
    return finish({scope, state: sent && !["list_project_media", "find_project_media"].includes(operation) ? "submitted_unverified" : "not_submitted", ...(operation === "probe" ? {rpc_verified: false} : {}), error: isKnownError.test(error?.message) ? error.message : fallback});
  }
}
