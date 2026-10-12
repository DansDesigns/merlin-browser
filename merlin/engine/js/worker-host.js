// A page's Web Worker, in Merlin Engine: a Node worker thread of the page's
// own JavaScript process (host.js), under the same permission model.
//
// What it fetches (its script, its modules, fetch(), importScripts) is asked
// of the page's host, which asks Merlin: with the page's cookies, through the
// content blocker, as Chrome connects. Messages to and from the page are
// structured clones, transferables (ArrayBuffers, MessagePorts) carried as in
// a browser. Nothing of Node is left for the worker's code (as in host.js).
import vm from "node:vm";
import { parentPort, workerData, receiveMessageOnPort, MessageChannel as NodeChannel } from "node:worker_threads";
import { registerHooks } from "node:module";

let locked = false;
registerHooks({
  resolve(specifier, context, next) {
    if (locked) throw Object.assign(new TypeError(`Failed to resolve module specifier "${specifier}"`), { code: "ERR_MERLIN_BLOCKED" });
    return next(specifier, context);
  },
});

const proc = globalThis.process;
const g = globalThis;
const { url: scriptUrl, type, name, userAgent, language, source: givenSource } = workerData;
const decoder = new TextDecoder();
const NativeResponse = globalThis.Response, NativeHeaders = globalThis.Headers, NativeRequest = globalThis.Request;

// ------------------------------------------------------------ to the page's host
let nextId = 1;
const waiting = new Map();
parentPort.on("message", (message) => {
  if (message && message.__merlin === "answer") {
    const resolve = waiting.get(message.id); waiting.delete(message.id); resolve?.(message);
    return;
  }
  if (message && message.__merlin === "message") { deliver(message.data, message.ports || []); return; }
});
function ask(request) {
  return new Promise((resolve) => { const id = nextId++; waiting.set(id, resolve); parentPort.postMessage({ __merlin: "ask", id, ...request }); });
}
// what importScripts needs, at once: this thread waits while the page's host
// fetches, then takes the answer from a port of its own
function askNow(request) {
  const flag = new Int32Array(new SharedArrayBuffer(4));
  const { port1, port2 } = new NodeChannel();
  parentPort.postMessage({ __merlin: "ask_now", flag, port: port2, ...request }, [port2]);
  Atomics.wait(flag, 0, 0, 60000);
  const got = receiveMessageOnPort(port1);
  port1.close();
  return got ? got.message : { error: "no answer" };
}
function report(error) {
  try { parentPort.postMessage({ __merlin: "console", level: "error", text: "worker: " + String(error && error.stack || error) }); } catch (_) {}
}
proc.on("uncaughtException", (error) => { tell("error", { error, message: String(error && error.message || error) }); report(error); });
proc.on("unhandledRejection", (reason) => report(reason));

// ------------------------------------------------------------ the worker's global
const events = new EventTarget();
const listeners = new WeakMap();
function tell(type, fields) {
  try {
    const event = new Event(type, { cancelable: true });
    for (const [key, value] of Object.entries(fields)) Object.defineProperty(event, key, { value, configurable: true });
    events.dispatchEvent(event);
  } catch (_) {}
}
g.self = g;
g.addEventListener = (kind, listener, options) => {
  if (!listener) return;
  let wrapped = listeners.get(listener);
  if (!wrapped) {
    wrapped = typeof listener === "function" ? function (event) { return listener.call(g, event); }
      : { handleEvent: (event) => listener.handleEvent(event) };
    listeners.set(listener, wrapped);
  }
  events.addEventListener(kind, wrapped, options);
};
g.removeEventListener = (kind, listener, options) => {
  const wrapped = listener && listeners.get(listener);
  if (wrapped) events.removeEventListener(kind, wrapped, options);
};
g.dispatchEvent = (event) => events.dispatchEvent(event);
for (const kind of ["message", "messageerror", "error"]) {
  let handler = null;
  Object.defineProperty(g, "on" + kind, { configurable: true,
    get: () => handler,
    set(fn) {
      if (handler) g.removeEventListener(kind, handler);
      handler = typeof fn === "function" ? fn : null;
      if (handler) g.addEventListener(kind, handler);
    } });
}
g.postMessage = (data, transfer) => {
  if (transfer && !Array.isArray(transfer) && transfer.transfer) transfer = transfer.transfer;
  parentPort.postMessage({ __merlin: "message", data }, transfer || []);
};
g.close = () => { parentPort.postMessage({ __merlin: "closed" }); setTimeout(() => proc.exit(0), 10); };
g.name = name || "";
const here = new URL(scriptUrl);
g.location = { href: here.href, origin: here.origin, protocol: here.protocol, host: here.host, hostname: here.hostname,
               port: here.port, pathname: here.pathname, search: here.search, hash: here.hash, toString() { return here.href; } };
try {
  Object.defineProperty(g, "navigator", { configurable: true, value: {
    userAgent, language: language || "en-GB", languages: [language || "en-GB"], onLine: true,
    hardwareConcurrency: 4, platform: /Windows/.test(userAgent) ? "Win32" : /Mac/.test(userAgent) ? "MacIntel" : "Linux x86_64" } });
} catch (_) {}
// what the page sends before the worker's script has run waits for it, as in
// a browser
let started = false;
const early = [];
function deliver(data, ports) {
  if (!started) { early.push([data, ports]); return; }
  const event = new MessageEvent("message", { data, ports: ports || [] });
  events.dispatchEvent(event);
}
function begin() {
  started = true;
  for (const [data, ports] of early.splice(0)) deliver(data, ports);
}

async function workerFetch(input, init = {}) {
  const request = input instanceof NativeRequest ? input : null;
  const url = new URL(request ? request.url : String(input), scriptUrl).href;
  const method = (init.method || (request && request.method) || "GET").toUpperCase();
  const headers = {};
  new NativeHeaders(init.headers || (request && request.headers) || {}).forEach((v, k) => { headers[k] = v; });
  let body = init.body ?? null;
  if (body === null && request && method !== "GET" && method !== "HEAD") body = await request.arrayBuffer();
  let bytes = null;
  if (body !== null && body !== undefined) {
    const made = new NativeRequest("http://localhost/", { method: "POST", body });
    bytes = new Uint8Array(await made.arrayBuffer());
    const kind = made.headers.get("content-type");
    if (kind && !Object.keys(headers).some((k) => k.toLowerCase() === "content-type")) headers["content-type"] = kind;
  }
  const answer = await ask({ kind: "fetch", url, method, headers, body: bytes });
  if (answer.error) throw new TypeError(`Failed to fetch: ${answer.error}`);
  const response = new NativeResponse([101, 204, 205, 304].includes(answer.status) ? null : answer.body,
    { status: answer.status, statusText: answer.statusText || "", headers: answer.headers || {} });
  Object.defineProperty(response, "url", { value: answer.url || url });
  return response;
}
g.fetch = workerFetch;
g.importScripts = (...urls) => {
  for (const each of urls) {
    const url = new URL(String(each), scriptUrl).href;
    const answer = askNow({ kind: "fetch", url, method: "GET", headers: { "sec-fetch-dest": "script" }, body: null });
    if (answer.error || answer.status >= 400) throw new DOMException(`Failed to execute 'importScripts': The script at '${url}' failed to load.`, "NetworkError");
    new vm.Script(decoder.decode(answer.body), { filename: url }).runInThisContext();
  }
};

// ------------------------------------------------------------ its modules
const modules = new Map();
function makeModule(text, url) {
  return new vm.SourceTextModule(text, {
    identifier: url,
    initializeImportMeta(meta) { meta.url = url; meta.resolve = (s) => new URL(s, url).href; },
    importModuleDynamically: (specifier) => evaluated(new URL(specifier, url).href),
  });
}
function linked(url) {
  if (modules.has(url)) return modules.get(url);
  const made = (async () => {
    const response = await workerFetch(url, { headers: { "sec-fetch-dest": "script" } });
    if (!response.ok) throw new TypeError(`Failed to fetch module ${url} (${response.status})`);
    const module = makeModule(await response.text(), url);
    await module.link((specifier, referrer) => linked(new URL(specifier, referrer.identifier).href));
    return module;
  })();
  modules.set(url, made);
  return made;
}
async function evaluated(url) {
  const module = await linked(url);
  if (module.status === "linked") await module.evaluate();
  if (module.status === "errored") throw module.error;
  return module;
}

// ------------------------------------------------------------ nothing of Node left
locked = true;
for (const each of ["process", "global", "require", "module", "setImmediate", "clearImmediate", "WebSocket", "EventSource"]) {
  try { delete g[each]; } catch (_) {}
}

(async () => {
  try {
    if (type === "module") {
      if (givenSource !== undefined) {
        const module = makeModule(givenSource, scriptUrl);
        await module.link((specifier, referrer) => linked(new URL(specifier, referrer.identifier).href));
        await module.evaluate();
      } else {
        await evaluated(scriptUrl);
      }
    } else {
      let text = givenSource;
      if (text === undefined) {
        const response = await workerFetch(scriptUrl, { headers: { "sec-fetch-dest": "script" } });
        if (!response.ok) throw new TypeError(`Failed to load worker script ${scriptUrl} (${response.status})`);
        text = await response.text();
      }
      new vm.Script(text, { filename: scriptUrl,
        importModuleDynamically: (specifier) => evaluated(new URL(specifier, scriptUrl).href) }).runInThisContext();
    }
    parentPort.postMessage({ __merlin: "started" });
    begin();
  } catch (error) {
    report(error);
    parentPort.postMessage({ __merlin: "failed", message: String(error && error.message || error) });
  }
})();
