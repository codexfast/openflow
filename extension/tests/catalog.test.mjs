import assert from "node:assert/strict";
import test from "node:test";
import vm from "node:vm";
import {flowOperation} from "../operations.mjs";
import {validRequest} from "../protocol.mjs";

const id = n => `10000000-0000-0000-0000-${String(n).padStart(12, "0")}`;
const project = id(1);
const scope = {tab_id: 1, document_id: "doc", origin: "https://flow.google.com", project_id: project};
// Synthetic fixture matches captured root8 / scene5 / detail7 / media7-or-8.
function fixture(count = 7) {
  const scenes = [], media = [];
  for (let i = 0; i < count; i++) {
    scenes.push([id(100 + i), null, null, ["private title", [1, 2], null, null, id(200 + i), id(300 + i), [1, 3]], project]);
    media.push([id(200 + i), project, id(400 + i), ["CAE", "CAI", null][i % 3], null, null, ["https://private.invalid/?Signature=secret"]]);
  }
  return [null, scenes, media, [], null, null, null, []];
}
async function run(payload, limit = 100, options = {}) {
  const operation = options.operation || "list_project_media";
  const input = operation === "find_project_media" ? {media_id: options.media_id} : {limit};
  const calls = [];
  const context = vm.createContext({
    URL, URLSearchParams, TextDecoder, AbortSignal,
    location: {origin: scope.origin, pathname: `/project/${project}`},
    document: {documentElement: {lang: "en"}},
    WIZ_global_data: {SNlM0e: "private-at"},
    fetch: async (url, spec) => {
      calls.push({rpc: new URL(url, scope.origin).searchParams.get("rpcids"), inner: JSON.parse(JSON.parse(spec.body.get("f.req"))[0][0][1])});
      if (options.navigate) context.location.pathname = `/project/${id(2)}`;
      return new Response(")]}'\n" + JSON.stringify([["wrb.fr", "Zzl0ze", JSON.stringify(payload)]]));
    }
  });
  const result = await vm.runInContext(`(${flowOperation.toString()})`, context)(operation, {scope, input, diagnostics: true});
  return {result: JSON.parse(JSON.stringify(result)), calls};
}

test("catalog uses one scoped RPC; mixed markers remain unknown and private fields omitted", async () => {
  const {result, calls} = await run(fixture());
  assert.deepEqual(calls, [{rpc: "Zzl0ze", inner: [`projects/${project}`, null, null, null, [1]]}]);
  assert.equal(result.state, "completed");
  assert.equal(result.items.length, 7);
  assert.equal(result.source, "remote_project_scenes");
  assert.equal(result.completeness, "unknown");
  assert.equal(result.truncated, false);
  for (const item of result.items) assert.deepEqual(Object.keys(item).sort(), ["kind", "media_id", "scene_id", "thumbnail_url"]);
  assert.ok(result.items.every(item => item.kind === "unknown"));
  assert.ok(!JSON.stringify(result).includes("private"));
  assert.equal(result.diagnostics, undefined);
});

test("empty, limit, shared media, unlinked media, and local truncation", async () => {
  assert.deepEqual((await run(fixture(0))).result.items, []);
  const limited = (await run(fixture(7), 2)).result;
  assert.equal(limited.items.length, 2);
  assert.equal(limited.truncated, true);
  const shared = fixture(2);
  shared[1][1][3][4] = shared[1][0][3][4];
  assert.equal((await run(shared)).result.items.length, 2);
  assert.equal((await run(fixture(1000))).result.items.length, 100);
});

const mutations = {
  root: p => p.pop(),
  nestedFallback: p => { p[1] = {nested: p[1]}; },
  foreignScene: p => { p[1][6][4] = id(2); },
  foreignMedia: p => { p[2][6][1] = id(2); },
  invalidScene: p => { p[1][6][0] = "bad"; },
  invalidMedia: p => { p[2][6][0] = "bad"; },
  absentLink: p => { p[1][6][3][4] = id(999); },
  duplicateMedia: p => { p[2].push(p[2][0]); },
  duplicateScene: p => { p[1].push(p[1][0]); },
  malformedDetail: p => { p[1][6][3] = null; },
  malformedMedia: p => { p[2][6] = [id(206)]; },
  tooManyScenes: p => { p[1] = Array(1001).fill(p[1][0]); },
  tooManyMedia: p => { p[2] = Array(1001).fill(p[2][0]); },
};
for (const [name, mutate] of Object.entries(mutations)) test(`catalog fails closed: ${name}`, async () => {
  const payload = fixture(); mutate(payload);
  const {result} = await run(payload, 1);
  assert.equal(result.error, "invalid_catalog_response");
  assert.equal(result.items, undefined);
});

test("invalid limits and scope changes fail closed", async () => {
  for (const limit of [0, 101, -1, 1.5, "1", true]) {
    const {result, calls} = await run(fixture(), limit);
    assert.equal(result.error, "invalid_catalog_input");
    assert.equal(calls.length, 0);
  }
  assert.equal((await run(fixture(), 1, {navigate: true})).result.error, "scope_changed");
});

test("protocol allows only exact catalog input and scoped project", () => {
  const request = {type: "request", id: id(5), operation: "list_project_media", params: {scope, input: {limit: 100}}};
  assert.equal(validRequest(request), true);
  for (const limit of [0, 101, true, "1"]) assert.equal(validRequest({...request, params: {scope, input: {limit}}}), false);
  assert.equal(validRequest({...request, params: {scope, input: {limit: 1, cursor: "x"}}}), false);
  assert.equal(validRequest({...request, params: {scope: {...scope, project_id: null}, input: {limit: 1}}}), false);
  const find = {type: "request", id: id(5), operation: "find_project_media", params: {scope, input: {media_id: id(200)}}};
  assert.equal(validRequest(find), true);
  assert.equal(validRequest({...find, params: {scope, input: {media_id: id(200) + "_upsampled"}}}), true);
  assert.equal(validRequest({...find, params: {scope, input: {media_id: "external-id"}}}), false);
  assert.equal(validRequest({...find, params: {scope, input: {media_id: id(200), limit: 1}}}), false);
});

test("directed lookup validates all rows and finds media beyond list limit", async () => {
  const data = fixture(101);
  const target = data[1][100][3][4];
  const {result, calls} = await run(data, 100, {operation: "find_project_media", media_id: target});
  assert.deepEqual(calls, [{rpc: "Zzl0ze", inner: [`projects/${project}`, null, null, null, [1]]}]);
  assert.equal(result.observed, true);
  assert.equal(result.item.media_id, target);
  assert.equal(result.completeness, "unknown");
  assert.equal(result.reason, undefined);
});

test("directed lookup absence stays partial and validates foreign rows", async () => {
  const data = fixture(101);
  const {result} = await run(data, 100, {operation: "find_project_media", media_id: id(999)});
  assert.deepEqual(result, {scope, state: "completed", observed: false,
    reason: "not_found_in_observed_catalog", source: "remote_project_scenes", completeness: "unknown"});
  data[1][100][4] = id(2);
  const failed = await run(data, 100, {operation: "find_project_media", media_id: id(999)});
  assert.equal(failed.result.error, "invalid_catalog_response");
});

test("directed lookup accepts upsampled ID and refuses duplicate catalog media", async () => {
  const data = fixture(2);
  data[1][1][3][4] += "_upsampled";
  data[2][1][0] += "_upsampled";
  const found = await run(data, 100, {operation: "find_project_media", media_id: data[2][1][0]});
  assert.equal(found.result.observed, true);
  assert.equal(found.result.item.media_id, data[2][1][0]);
  data[2].push(structuredClone(data[2][0]));
  const failed = await run(data, 100, {operation: "find_project_media", media_id: data[2][0][0]});
  assert.equal(failed.result.error, "invalid_catalog_response");
});


test("upsampled media links accepted; suffix never applies to scenes or projects", async () => {
  const payload = fixture(2);
  payload[1][0][3][4] += "_upsampled";
  payload[2][0][0] += "_upsampled";
  const {result} = await run(payload);
  assert.equal(result.state, "completed");
  assert.equal(result.items[0].media_id, id(200) + "_upsampled");
  for (const mutate of [
    p => { p[1][0][0] += "_upsampled"; },
    p => { p[1][0][4] += "_upsampled"; },
    p => { p[2][0][0] += "_other"; },
    p => { p[1][0][3][4] += "_other"; },
  ]) {
    const invalid = structuredClone(payload); mutate(invalid);
    const failure = (await run(invalid)).result;
    assert.equal(failure.error, "invalid_catalog_response");
    assert.equal(failure.state, "not_submitted");
    assert.equal(failure.items, undefined);
  }
});


test("catalog classifies observed detail slots independently of markers and JPEG thumbs", async () => {
  const data = fixture(4);
  const thumb = "https://lh3.googleusercontent.com/synthetic-thumb.jpg=w320";
  for (const row of data[2]) row[5] = [null, null, null, null, null, thumb];
  data[2][0][6] = [[], null, [1376, 768], null, null, []];
  data[2][1][6] = null;
  data[2][1][7] = [[], null, [], null, null, []];
  data[2][2][6] = [[], null, null, null, null, []];
  data[2][2][7] = [[], null, null, null, null, []];
  data[2][3][6] = [];
  const {result, calls} = await run(data);
  assert.equal(calls.length, 1);
  assert.deepEqual(result.items.map(item => item.kind), ["image", "video", "unknown", "unknown"]);
  assert.ok(result.items.every(item => item.thumbnail_url === thumb));
});

for (const thumb of [
  "http://lh3.googleusercontent.com/thumb",
  "https://lh3.googleusercontent.com.evil.invalid/thumb",
  "https://user@lh3.googleusercontent.com/thumb",
  "https://lh3.googleusercontent.com:443/thumb",
  "https://lh3.googleusercontent.com/thumb#fragment",
  "https://lh3.googleusercontent.com/",
  "https://lh3.googleusercontent.com/thumb\n",
  "https://lh3.googleusercontent.com/thumb with space",
  "https://lh3.googleusercontent.com/" + "x".repeat(8192),
  null,
]) {
  test(`catalog omits unsafe or absent thumbnail ${String(thumb).slice(0, 64)}`, async () => {
    const data = fixture(1);
    data[2][0][5] = [null, null, null, null, null, thumb];
    const {result} = await run(data);
    assert.equal(result.state, "completed");
    assert.equal(result.items[0].thumbnail_url, null);
  });
}
