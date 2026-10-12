// What a page and its workers share in Merlin Engine: Web Workers and
// SharedWorkers (as Node worker threads of the page's process, running
// worker-host.js), XMLHttpRequest, and the Cache API (caches).
//
// Everything is given an "env": how to fetch from where it is (the page asks
// Merlin; a worker asks the thread that made it, which asks on up to the page),
// the address things are relative to, and the classes to use. So a worker can
// start workers of its own, with the same code as the page.
import { Worker as NodeWorker, MessageChannel as NodeChannel } from "node:worker_threads";
import { resolveObjectURL } from "node:buffer";

export const WORKER_HOST = new URL("./worker-host.js", import.meta.url);
const decoder = new TextDecoder();
const NodeEventTarget = globalThis.EventTarget;
const NodeEvent = globalThis.Event;
const NodeMessageEvent = globalThis.MessageEvent;
const NativeResponse = globalThis.Response, NativeHeaders = globalThis.Headers, NativeRequest = globalThis.Request;

function fromBase64(text) {
  const raw = atob(text || "");
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  return bytes;
}

// A worker's script given as a Blob or data: address is read here, where it
// was made (a Blob's address means nothing in another thread)
async function scriptOf(url, base) {
  if (url.startsWith("blob:")) {
    const blob = resolveObjectURL(url);
    if (!blob) throw new Error(`the worker's script (${url}) is gone`);
    return { source: await blob.text(), base };
  }
  if (url.startsWith("data:")) {
    const comma = url.indexOf(",");
    const head = url.slice(5, comma), body = url.slice(comma + 1);
    return { source: head.endsWith(";base64") ? decoder.decode(fromBase64(body)) : decodeURIComponent(body), base };
  }
  return { source: undefined, base: url };
}

/**
 * The clocks of a global (performance.now, Date) telling time in steps of
 * stepMs, for Settings > Shields > Blur timing; gives back the rounding.
 */
export function coarsenClocks(g, stepMs) {
  const step = (t) => Math.floor(t / stepMs) * stepMs;
  const perf = g.performance;
  if (perf && typeof perf.now === "function") {
    const realNow = perf.now.bind(perf);
    try { Object.defineProperty(perf, "now", { configurable: true, writable: true, value: function now() { return step(realNow()); } }); } catch (_) {}
  }
  const RealDate = g.Date, realDateNow = RealDate.now;
  function Date(...args) {
    if (!new.target) return RealDate();
    return Reflect.construct(RealDate, args.length ? args : [step(realDateNow())], new.target);
  }
  Date.prototype = RealDate.prototype;
  Object.setPrototypeOf(Date, RealDate);
  Date.now = function now() { return step(realDateNow()); };
  Date.UTC = RealDate.UTC; Date.parse = RealDate.parse;
  try { Object.defineProperty(RealDate.prototype, "constructor", { value: Date, configurable: true, writable: true }); } catch (_) {}
  g.Date = Date;
  return step;
}

// the on<event> properties a worker object has, as a browser's
function handlerProperties(target, kinds) {
  for (const kind of kinds) {
    let handler = null;
    Object.defineProperty(target, "on" + kind, { configurable: true, enumerable: true,
      get: () => handler,
      set: (fn) => {
        if (handler) target.removeEventListener(kind, handler);
        handler = typeof fn === "function" ? fn : null;
        if (handler) target.addEventListener(kind, handler);
      } });
  }
}

/**
 * env: {
 *   base(): the address a worker's script is relative to,
 *   fetch(request): Promise of { status, statusText, headers, url, body: Uint8Array } or { error },
 *   userAgent(), language(), console(level, text)
 * }
 */
export function makeWorkerClasses(env) {
  const shared = new Map();         // "address|name" -> the SharedWorker's thread

  // answers a thread's requests: fetches (made here or asked on up) and the
  // waiting ones importScripts and a synchronous XMLHttpRequest make
  async function serve(thread, message) {
    let answer;
    try {
      answer = await env.fetch({ url: message.url, method: message.method || "GET", headers: message.headers || {},
                                 body: message.body || null });
    } catch (error) { answer = { error: String(error && error.message || error) }; }
    // the answer asked on up carries that thread's own __merlin and id: this
    // thread's are put after it, or a nested worker's answer goes astray
    if (answer && typeof answer === "object") { delete answer.__merlin; delete answer.id; }
    if (message.__merlin === "ask") {
      try { thread.postMessage({ ...answer, __merlin: "answer", id: message.id }); } catch (_) {}
    } else {
      try { message.port.postMessage(answer); } catch (_) {}
      Atomics.store(message.flag, 0, 1);
      Atomics.notify(message.flag, 0);
    }
  }

  function start(url, type, name, isShared) {
    return scriptOf(url, env.base()).then(({ source, base }) => {
      const thread = new NodeWorker(WORKER_HOST, {
        workerData: { url: source === undefined ? url : base, type, name, source, shared: isShared,
                      userAgent: env.userAgent(), language: env.language(),
                      blurTiming: env.blurTiming ? env.blurTiming() : false },
        resourceLimits: { maxOldGenerationSizeMb: 1024 },
      });
      thread.on("message", (message) => {
        if (message && (message.__merlin === "ask" || message.__merlin === "ask_now")) serve(thread, message);
        else if (message && message.__merlin === "console") env.console(message.level || "log", message.text || "");
      });
      return thread;
    });
  }

  class Worker extends NodeEventTarget {
    constructor(scriptURL, options = {}) {
      super();
      const url = new URL(String(scriptURL), env.base()).href;
      this._queue = [];
      this._thread = null;
      this._ended = false;
      handlerProperties(this, ["message", "messageerror", "error"]);
      const type = options && options.type === "module" ? "module" : "classic";
      start(url, type, options && options.name ? String(options.name) : "", false).then((thread) => {
        if (this._ended) { thread.terminate().catch(() => {}); return; }
        this._thread = thread;
        thread.on("message", (message) => this._fromThread(message));
        thread.on("error", (error) => this._failed(String(error && error.message || error)));
        for (const [data, transfer] of this._queue.splice(0)) this._send(data, transfer);
      }).catch((error) => this._failed(String(error && error.message || error)));
    }
    _send(data, transfer) {
      const list = Array.isArray(transfer) ? transfer : (transfer && transfer.transfer) || [];
      const ports = list.filter((x) => x instanceof MessagePort);
      try { this._thread.postMessage({ __merlin: "message", data, ports }, list); }
      catch (error) { throw new DOMException(String(error && error.message || error), "DataCloneError"); }
    }
    postMessage(data, transfer) {
      if (this._ended) return;
      if (!this._thread) {
        // cloned now, as a browser does, though sent once the thread is up
        this._queue.push([transfer ? data : structuredClone(data), transfer]);
        return;
      }
      this._send(data, transfer);
    }
    terminate() {
      this._ended = true;
      if (this._thread) this._thread.terminate().catch(() => {});
    }
    _failed(message) {
      const event = new NodeEvent("error", { cancelable: true });
      try { Object.defineProperty(event, "message", { value: message, configurable: true }); } catch (_) {}
      env.console("warn", `worker: ${message}`);
      this.dispatchEvent(event);
    }
    _fromThread(message) {
      if (!message || typeof message !== "object") return;
      const kind = message.__merlin;
      if (kind === "message") {
        this.dispatchEvent(new NodeMessageEvent("message", { data: message.data, ports: message.ports || [] }));
      } else if (kind === "failed") {
        this._failed(message.message || "the worker's script failed");
      } else if (kind === "closed") {
        this.terminate();
      }
    }
  }

  // A SharedWorker: one thread for each address and name in this page (and
  // the frames and workers of its process), each new SharedWorker a port to
  // it. Tabs are processes of their own, so one is not shared between tabs.
  class SharedWorker extends NodeEventTarget {
    constructor(scriptURL, options = {}) {
      super();
      if (typeof options === "string") options = { name: options };
      const url = new URL(String(scriptURL), env.base()).href;
      const name = options && options.name ? String(options.name) : "";
      const type = options && options.type === "module" ? "module" : "classic";
      handlerProperties(this, ["error"]);
      const { port1, port2 } = new NodeChannel();
      this.port = port1;
      const key = `${url}|${name}`;
      let entry = shared.get(key);
      if (!entry) {
        entry = { thread: start(url, type, name, true), owners: new Set() };
        shared.set(key, entry);
        entry.thread.then((thread) => {
          thread.on("message", (message) => {
            if (message && message.__merlin === "failed") {
              for (const owner of entry.owners) owner._failed(message.message || "the worker's script failed");
            }
          });
          thread.on("exit", () => shared.delete(key));
        }).catch((error) => { shared.delete(key); for (const owner of entry.owners) owner._failed(String(error && error.message || error)); });
      }
      entry.owners.add(this);
      entry.thread.then((thread) => thread.postMessage({ __merlin: "connect", port: port2 }, [port2])).catch(() => {});
    }
    _failed(message) {
      const event = new NodeEvent("error", { cancelable: true });
      try { Object.defineProperty(event, "message", { value: message, configurable: true }); } catch (_) {}
      env.console("warn", `shared worker: ${message}`);
      this.dispatchEvent(event);
    }
  }

  return { Worker, SharedWorker };
}

/**
 * XMLHttpRequest over a fetch of Merlin's. env: {
 *   base(), fetch(request) (as above), fetchNow(request) (a worker's waiting
 *   fetch, for open(..., false); none on the page's own thread), reportError
 * }
 */
export function makeXHR(env) {
  const toBytes = async (body) => {
    if (body === null || body === undefined) return { bytes: null, kind: null };
    if (typeof body === "string") return { bytes: new TextEncoder().encode(body), kind: "text/plain;charset=UTF-8" };
    const made = new NativeRequest("http://localhost/", { method: "POST", body });
    return { bytes: new Uint8Array(await made.arrayBuffer()), kind: made.headers.get("content-type") };
  };
  return class XMLHttpRequest {
    static UNSENT = 0; static OPENED = 1; static HEADERS_RECEIVED = 2; static LOADING = 3; static DONE = 4;
    constructor() {
      this.readyState = 0; this.status = 0; this.statusText = ""; this.response = null; this.responseText = "";
      this.responseType = ""; this.responseURL = ""; this.withCredentials = false; this.timeout = 0; this._headers = {};
      this._listeners = {}; this.upload = { addEventListener() {}, removeEventListener() {} };
      this._responseHeaders = new NativeHeaders(); this._async = true;
    }
    open(method, url, async = true) {
      this._method = String(method || "GET").toUpperCase(); this._url = new URL(String(url), env.base()).href;
      this._async = async !== false; this.readyState = 1; this._emit("readystatechange");
    }
    setRequestHeader(k, v) { this._headers[k] = v; }
    getResponseHeader(k) { return this._responseHeaders.get(k); }
    getAllResponseHeaders() { let text = ""; this._responseHeaders.forEach((v, k) => { text += `${k}: ${v}\r\n`; }); return text; }
    overrideMimeType(kind) { this._mime = kind; }
    addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); }
    removeEventListener(type, fn) { this._listeners[type] = (this._listeners[type] || []).filter((f) => f !== fn); }
    _emit(type) {
      const event = { type, target: this, currentTarget: this, lengthComputable: false, loaded: 0, total: 0 };
      try { this["on" + type]?.(event); } catch (e) { env.reportError(e); }
      for (const fn of this._listeners[type] || []) { try { fn.call(this, event); } catch (e) { env.reportError(e); } }
    }
    abort() { this._aborted = true; }
    _finish(answer) {
      if (answer.error) {
        this.readyState = 4; this._emit("readystatechange"); this._emit("error"); this._emit("loadend");
        return;
      }
      this.status = answer.status; this.statusText = answer.statusText || ""; this.responseURL = answer.url || this._url;
      this._responseHeaders = new NativeHeaders(answer.headers || {});
      this.readyState = 2; this._emit("readystatechange");
      const bytes = answer.body instanceof Uint8Array ? answer.body : new Uint8Array(answer.body || []);
      const text = decoder.decode(bytes);
      this.responseText = text;
      const buffer = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
      this.response = this.responseType === "json" ? (() => { try { return JSON.parse(text); } catch (_) { return null; } })()
        : this.responseType === "arraybuffer" ? buffer
        : this.responseType === "blob" ? new Blob([buffer], { type: this._responseHeaders.get("content-type") || "" })
        : text;
      this.readyState = 4; this._emit("readystatechange"); this._emit("load"); this._emit("loadend");
    }
    send(body = null) {
      const headers = { ...this._headers };
      if (!this._async) {
        // a worker may wait for its answer (Emscripten's file loading does)
        if (!env.fetchNow) throw new DOMException("Synchronous requests are not available here.", "InvalidAccessError");
        if (body !== null && body !== undefined && typeof body !== "string") {
          throw new DOMException("A synchronous request can carry only text here.", "InvalidAccessError");
        }
        const bytes = typeof body === "string" ? new TextEncoder().encode(body) : null;
        this._finish(env.fetchNow({ url: this._url, method: this._method, headers, body: bytes }));
        return;
      }
      (async () => {
        try {
          const { bytes, kind } = await toBytes(body);
          if (kind && !Object.keys(headers).some((k) => k.toLowerCase() === "content-type")) headers["content-type"] = kind;
          const answer = await env.fetch({ url: this._url, method: this._method, headers, body: bytes });
          if (this._aborted) return;
          this._finish(answer);
        } catch (e) { this._finish({ error: String(e && e.message || e) }); }
      })();
    }
  };
}

/**
 * The Cache API (caches.open, cache.put, match...), kept in memory for the
 * page's process: sites keep what they fetched there for later, and a site
 * that finds no caches at all may stop. env: { fetch(input, init) } (the
 * global fetch of where it is).
 */
export function makeCacheStorage(env) {
  const stores = new Map();
  const keyOf = (request) => new URL(typeof request === "string" ? request : request.url, env.base()).href.split("#")[0];
  const ignoringSearch = (url) => url.split("?")[0];
  class Cache {
    constructor() { this._entries = new Map(); }
    async match(request, options = {}) { return (await this.matchAll(request, options))[0]; }
    async matchAll(request, options = {}) {
      const found = [];
      for (const [url, entry] of this._entries) {
        if (request !== undefined) {
          const wanted = keyOf(request);
          if (options.ignoreSearch ? ignoringSearch(url) !== ignoringSearch(wanted) : url !== wanted) continue;
        }
        found.push(new NativeResponse(entry.body.slice(), { status: entry.status, statusText: entry.statusText, headers: entry.headers }));
      }
      return found;
    }
    async add(request) { return this.addAll([request]); }
    async addAll(requests) {
      for (const request of requests) {
        const response = await env.fetch(request);
        if (!response.ok) throw new TypeError(`Cache.addAll: ${keyOf(request)} answered ${response.status}`);
        await this.put(request, response);
      }
    }
    async put(request, response) {
      const body = new Uint8Array(await response.arrayBuffer());
      const headers = {};
      response.headers.forEach((v, k) => { headers[k] = v; });
      this._entries.set(keyOf(request), { body, status: response.status, statusText: response.statusText, headers });
    }
    async delete(request) { return this._entries.delete(keyOf(request)); }
    async keys() { return [...this._entries.keys()].map((url) => new NativeRequest(url)); }
  }
  return {
    async open(name) { if (!stores.has(name)) stores.set(name, new Cache()); return stores.get(name); },
    async has(name) { return stores.has(name); },
    async delete(name) { return stores.delete(name); },
    async keys() { return [...stores.keys()]; },
    async match(request, options = {}) {
      for (const cache of stores.values()) { const found = await cache.match(request, options); if (found) return found; }
      return undefined;
    },
  };
}
