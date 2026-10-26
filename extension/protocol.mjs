import {knownOperationErrors} from "./operations.mjs";

export const operationErrorPattern = knownOperationErrors;

// String form of the error classifier. chrome.scripting.executeScript serializes its
// args as JSON, so a RegExp crossed into the page MAIN world would arrive as {} and break
// the catch path inside flowOperation (see operations.mjs). The background worker passes
// this string instead; flowOperation rebuilds the RegExp on the other side.
export const operationErrorSource = knownOperationErrors.source;

export function serverUrl(value) {
  const url = new URL(value);
  if (url.protocol !== "ws:" || !["127.0.0.1", "localhost"].includes(url.hostname) ||
      !url.port || url.port === "9222" || url.username || url.password ||
      url.pathname !== "/" || url.search || url.hash) throw new Error("invalid_server");
  return url.href;
}

export function flowScope(value) {
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.port || url.username || url.password) return null;
    if (url.hostname !== "flow.google.com" &&
        !(url.hostname === "labs.google" && /^\/fx\/(?:[a-z-]+\/)?tools\/flow(?:\/|$)/.test(url.pathname))) return null;
    const match = url.pathname.match(/\/project\/([a-f0-9-]{36})(?:\/|$)/i);
    return {origin: url.origin, project_id: match ? match[1] : null};
  } catch {
    return null;
  }
}

const exact = (value, keys) => value && typeof value === "object" && !Array.isArray(value) &&
  Object.keys(value).every(key => keys.includes(key)) && keys.every(key => Object.hasOwn(value, key));
const optionalExact = (value, required, optional) => value && typeof value === "object" && !Array.isArray(value) &&
  Object.keys(value).every(key => required.includes(key) || optional.includes(key)) &&
  required.every(key => Object.hasOwn(value, key));
const title = value => typeof value === "string" && value.length >= 1 && value.length <= 200 && value.trim().length > 0;
const seed = value => value === null || (Number.isInteger(value) && value >= 1 && value <= 1e9);
const identifier = value => typeof value === "string" && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(value);
const nullableId = value => value === null || identifier(value);
const mediaIdentifier = value => typeof value === "string" && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}(?:_upsampled)?$/i.test(value);
const nullableMedia = value => value === null || mediaIdentifier(value);
const refs = (value, max) => Array.isArray(value) && value.length <= max && value.every(identifier) && new Set(value).size === value.length;
const prompt = value => typeof value === "string" && value.length > 0 && value.length <= 10000;

export function validScope(value) {
  return exact(value, ["tab_id", "origin", "project_id", "document_id"]) &&
    Number.isSafeInteger(value.tab_id) && value.tab_id >= 0 &&
    ["https://flow.google.com", "https://labs.google"].includes(value.origin) &&
    nullableId(value.project_id) && typeof value.document_id === "string" &&
    /^[a-zA-Z0-9-]{1,100}$/.test(value.document_id);
}

export function validInput(operation, p) {
  if (operation === "generate_image") return optionalExact(p, ["prompt", "model", "ratio", "references"], ["seed"]) &&
    (!Object.hasOwn(p, "seed") || seed(p.seed)) &&
    prompt(p.prompt) && ["nano_banana_pro", "nano_banana_2", "nano_banana_lite"].includes(p.model) &&
    ["1:1", "9:16", "16:9", "3:4", "4:3"].includes(p.ratio) && refs(p.references, 4);
  if (operation === "generate_video") return exact(p, ["prompt", "model", "ratio", "duration", "resolution", "start", "end", "references"]) &&
    prompt(p.prompt) && ["omni", "veo_lite"].includes(p.model) &&
    ["9:16", "16:9"].includes(p.ratio) && [4, 6, 8, 10].includes(p.duration) &&
    ["360p", "720p"].includes(p.resolution) && nullableId(p.start) && nullableId(p.end) &&
    refs(p.references, 3) && (!p.end || !!p.start) && !(p.references.length && (p.start || p.end)) &&
    (p.model === "omni" || (!!p.start && !p.end && !p.references.length && p.duration === 8 && p.resolution === "720p"));
  if (operation === "upscale_image") return exact(p, ["media_id", "resolution"]) &&
    identifier(p.media_id) && ["2K", "4K"].includes(p.resolution);
  if (operation === "upscale_video") return exact(p, ["media_id", "scene_id", "ratio", "resolution"]) &&
    identifier(p.media_id) && identifier(p.scene_id) && ["9:16", "16:9"].includes(p.ratio) && p.resolution === "1080p";
  if (operation === "upload") return exact(p, ["data", "mime", "name"]) &&
    typeof p.data === "string" && p.data.length > 0 && p.data.length <= 11184812 &&
    p.data.length % 4 === 0 && /^[A-Za-z0-9+/]+={0,2}$/.test(p.data) &&
    ["image/png", "image/jpeg", "image/webp"].includes(p.mime) &&
    typeof p.name === "string" && /^[a-zA-Z0-9_. -]{1,120}$/.test(p.name);
  if (operation === "poll") return exact(p, ["operation_id", "media_id", "kind"]) &&
    nullableId(p.operation_id) && nullableMedia(p.media_id) &&
    !!((p.operation_id || p.media_id) && ["image", "video"].includes(p.kind));
  return false;
}

export function validRequest(message) {
  if (!exact(message, ["type", "id", "operation", "params"]) || message.type !== "request" || !identifier(message.id)) return false;
  const p = message.params;
  if (["snapshot", "capabilities"].includes(message.operation)) return exact(p, []);
  if (message.operation === "reload_extension") return exact(p, ["marker"]) && p.marker === "openflow-dev-reload";
  if (message.operation === "reload_tab") return exact(p, ["scope"]) && validScope(p.scope);
  if (message.operation === "list_project_media") return exact(p, ["scope", "input"]) &&
    validScope(p.scope) && p.scope.origin === "https://flow.google.com" && identifier(p.scope.project_id) &&
    exact(p.input, ["limit"]) && Number.isSafeInteger(p.input.limit) && p.input.limit >= 1 && p.input.limit <= 100;
  if (message.operation === "find_project_media") return exact(p, ["scope", "input"]) &&
    validScope(p.scope) && p.scope.origin === "https://flow.google.com" && identifier(p.scope.project_id) &&
    exact(p.input, ["media_id"]) && mediaIdentifier(p.input.media_id);
  if (message.operation === "probe") return exact(p, ["scope", "input", "diagnostics"]) &&
    validScope(p.scope) && p.scope.origin === "https://flow.google.com" && p.diagnostics === true &&
    exact(p.input, ["project_id"]) && identifier(p.input.project_id);
  if (["home_read", "list_projects"].includes(message.operation)) return exact(p, ["scope"]) && validScope(p.scope) &&
    p.scope.origin === "https://flow.google.com" && p.scope.project_id === null;
  if (message.operation === "select") return exact(p, ["tab_id"]) && Number.isSafeInteger(p.tab_id) && p.tab_id >= 0;
  if (["inspect", "refresh_account", "captcha_probe"].includes(message.operation)) return exact(p, ["scope"]) && validScope(p.scope);
  if (message.operation === "create_project") return optionalExact(p, ["scope"], ["title", "diagnostics"]) &&
    (!Object.hasOwn(p, "title") || title(p.title)) &&
    (!Object.hasOwn(p, "diagnostics") || typeof p.diagnostics === "boolean") && validScope(p.scope) &&
    p.scope.origin === "https://flow.google.com" && p.scope.project_id === null;
  return (exact(p, ["scope", "input"]) ||
    (exact(p, ["scope", "input", "diagnostics"]) && typeof p.diagnostics === "boolean")) &&
    validScope(p.scope) &&
    p.scope.origin === "https://flow.google.com" && identifier(p.scope.project_id) && validInput(message.operation, p.input);
}

export function sameScope(a, b) {
  return !!a && !!b && ["tab_id", "origin", "project_id", "document_id"].every(key => a[key] === b[key]);
}
