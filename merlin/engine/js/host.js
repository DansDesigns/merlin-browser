// Merlin Engine's JavaScript host: one per tab, run by Node.js (V8, C++: no
// Rust, as Deno, its runtime before, is) under Node's permission model: no
// files but its own, no processes, no add-ons.
//
// It holds the page's DOM (linkedom), runs the page's scripts against it, and
// talks to Merlin one JSON message per line on stdin and stdout. Merlin does
// everything that reaches outside: fetching (with the page's cookies, through
// the content blocker), storage, navigation. After scripts change the page,
// the new page is sent back for Merlin Engine to style and lay out.
import { parseHTML } from "./linkedom.bundle.js";
import vm from "node:vm";
import fs from "node:fs";
import { registerHooks } from "node:module";
import { makeWorkerClasses, makeXHR, makeCacheStorage, coarsenClocks } from "./workers.js";

// Node's own process, kept here before the page's globals are made: the page
// never sees it (see lockDown)
const proc = globalThis.process;
// Every module this host needs is above. After the page is in, nothing more
// is loaded the way Node loads modules: a page's code (an import() made from
// a Function, say) could otherwise reach Node's own (files, the network).
// Pages' modules are fetched by Merlin and made here (see importModule).
let locked = false;
registerHooks({
  resolve(specifier, context, next) {
    if (locked) throw Object.assign(new TypeError(`Failed to resolve module specifier "${specifier}"`), { code: "ERR_MERLIN_BLOCKED" });
    return next(specifier, context);
  },
});

// Node's own event classes, kept before the DOM's take their names. Node's
// global object is no event target (Deno's and a browser's are): the window's
// events go to one of its own, standing in for the window.
const NodeEvent = globalThis.Event;
const NodeEventTarget = globalThis.EventTarget;
const NodeMessageEvent = globalThis.MessageEvent;
const windowEvents = new EventTarget();
const windowListeners = new WeakMap();      // a page's listener -> what Node is given
function windowDispatch(event) {
  for (const name of ["target", "currentTarget", "srcElement"]) {
    try { Object.defineProperty(event, name, { value: globalThis, configurable: true }); } catch (_) {}
  }
  return windowEvents.dispatchEvent(event);
}
function windowListen(type, listener, options) {
  if (!listener) return;
  let wrapped = windowListeners.get(listener);
  if (!wrapped) {
    // called with the window as this, as a browser calls them
    wrapped = typeof listener === "function" ? function (event) { return listener.call(globalThis, event); }
      : { handleEvent: (event) => listener.handleEvent(event) };
    windowListeners.set(listener, wrapped);
  }
  windowEvents.addEventListener(type, wrapped, options);
}
function windowUnlisten(type, listener, options) {
  const wrapped = listener && windowListeners.get(listener);
  if (wrapped) windowEvents.removeEventListener(type, wrapped, options);
}
// navigator.userAgentData, as Chrome gives it, from the user agent the
// connections send: a Chrome user agent with none (as Merlin Engine's had) is
// a contradiction sites that look for robots look for. Other browsers: none.
function chromeHints(agent, sent) {
  const version = /Chrome\/(\d+)/.exec(String(agent || ""));
  if (!version) return undefined;
  const major = version[1];
  const platform = /Windows/.test(agent) ? "Windows" : /Mac OS X|Macintosh/.test(agent) ? "macOS"
    : /Android/.test(agent) ? "Android" : /Linux/.test(agent) ? "Linux" : "";
  const mobile = /Mobile|Android/.test(agent);
  // the brands the connections' sec-ch-ua header sends, in its order: a
  // header saying one set and navigator another is a robot's mark
  const brands = Array.isArray(sent) && sent.length ? sent.map((b) => ({ brand: String(b.brand), version: String(b.version) }))
    : [{ brand: "Chromium", version: major }, { brand: "Google Chrome", version: major },
       { brand: "Not_A Brand", version: "24" }];
  const full = (/Chrome\/([\d.]+)/.exec(agent) || [, major + ".0.0.0"])[1];
  const hints = {
    architecture: /arm|aarch64/i.test(agent) ? "arm" : "x86", bitness: "64", model: "", wow64: false,
    platformVersion: platform === "Windows" ? "15.0.0" : platform === "macOS" ? "15.5.0" : "6.8.0",
    uaFullVersion: full, fullVersionList: brands.map((b) => ({ brand: b.brand, version: b.version === major ? full : b.version + ".0.0.0" })),
    formFactors: [mobile ? "Mobile" : "Desktop"],
  };
  return {
    brands, mobile, platform,
    getHighEntropyValues(asked) {
      const out = { brands, mobile, platform };
      for (const name of asked || []) if (name in hints) out[name] = hints[name];
      return Promise.resolve(out);
    },
    toJSON() { return { brands, mobile, platform }; },
  };
}
const encoder = new TextEncoder();
const decoder = new TextDecoder();
function send(message) {
  const line = encoder.encode(JSON.stringify(message) + "\n");
  let written = 0;
  while (written < line.length) {
    try { written += fs.writeSync(1, line, written); }
    catch (e) { if (e.code !== "EAGAIN") throw e; }
  }
}

// until the page's own is made: an error is never lost for want of one
if (typeof globalThis.reportError !== "function") {
  globalThis.reportError = (error) => { try { send({ type: "console", level: "error", text: "Uncaught " + String(error && error.stack || error) }); } catch (_) {} };
}

// console goes to Merlin as messages: anything printed would break the channel
for (const level of ["log", "info", "warn", "error", "debug"]) {
  console[level] = (...args) => {
    if (level === "debug") return;
    try {
      send({ type: "console", level, text: args.map(a => typeof a === "string" ? a : safeString(a)).join(" ") });
      // an Error a page logs itself (its own error catcher) comes with where
      // it was thrown, as an uncaught one does: Hugging Face's had none
      if (level === "error" || level === "warn") {
        const error = args.find((a) => a instanceof Error);
        if (error && error.stack) {
          send({ type: "console", level, text: String(error.stack).split("\n").slice(1, 4).join("\n") });
          if (typeof showWhere === "function") showWhere(error).catch(() => {});
        }
      }
    } catch (_) { /* nothing to do */ }
  };
}
function safeString(value) {
  try { return value instanceof Error ? `${value.name}: ${value.message}` : JSON.stringify(value) ?? String(value); }
  catch (_) { return String(value); }
}

// ------------------------------------------------------------------ requests
let nextId = 1;
const waiting = new Map();
function ask(message) {
  const id = nextId++;
  return new Promise((resolve) => { waiting.set(id, resolve); send({ ...message, id }); });
}

function toBase64(bytes) {
  let text = "";
  for (let i = 0; i < bytes.length; i += 0x8000) text += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(text);
}
function fromBase64(text) {
  const raw = atob(text || "");
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  return bytes;
}

const NativeResponse = globalThis.Response, NativeHeaders = globalThis.Headers, NativeRequest = globalThis.Request;
async function merlinFetch(input, init = {}) {
  const request = input instanceof NativeRequest ? input : null;
  const url = new URL(request ? request.url : String(input), location.href).href;
  const method = (init.method || (request && request.method) || "GET").toUpperCase();
  const headers = {};
  new NativeHeaders(init.headers || (request && request.headers) || {}).forEach((v, k) => { headers[k] = v; });
  let body = init.body ?? null;
  if (body === null && request && method !== "GET" && method !== "HEAD") body = await request.arrayBuffer();
  let bodyB64 = null;
  if (body !== null && body !== undefined) {
    if (typeof body === "string") bodyB64 = toBase64(encoder.encode(body));
    else if (body instanceof URLSearchParams) { bodyB64 = toBase64(encoder.encode(body.toString())); headers["content-type"] ??= "application/x-www-form-urlencoded;charset=UTF-8"; }
    else if (body instanceof ArrayBuffer) bodyB64 = toBase64(new Uint8Array(body));
    else if (ArrayBuffer.isView(body)) bodyB64 = toBase64(new Uint8Array(body.buffer, body.byteOffset, body.byteLength));
    else if ((typeof FormData !== "undefined" && body instanceof FormData) || (typeof Blob !== "undefined" && body instanceof Blob)) {
      // as a browser sends them: FormData as multipart/form-data, its files
      // in it (it had gone as a plain form, its files lost: Ponder's
      // background uploads), and a Blob as its bytes, with its type
      const made = new NativeRequest("http://localhost/", { method: "POST", body });
      bodyB64 = toBase64(new Uint8Array(await made.arrayBuffer()));
      const kind = made.headers.get("content-type");
      if (kind) { for (const key of Object.keys(headers)) if (key.toLowerCase() === "content-type") delete headers[key]; headers["content-type"] = kind; }
    } else bodyB64 = toBase64(encoder.encode(String(body)));
  }
  const answer = await ask({ type: "fetch", url, method, headers, body: bodyB64, credentials: init.credentials || "same-origin" });
  if (answer.error) throw new TypeError(`Failed to fetch: ${answer.error}`);
  const response = new NativeResponse([101, 204, 205, 304].includes(answer.status) ? null : fromBase64(answer.body),
    { status: answer.status, statusText: answer.statusText || "", headers: answer.headers || {} });
  Object.defineProperty(response, "url", { value: answer.url || url });
  return response;
}

// A fetch for the page's workers and XMLHttpRequest: the answer as it came,
// its body as bytes
async function pageFetch(request) {
  const body = request.body ? toBase64(request.body instanceof Uint8Array ? request.body : new Uint8Array(request.body)) : null;
  const reply = await ask({ type: "fetch", url: request.url, method: request.method || "GET",
                            headers: request.headers || {}, body, credentials: "same-origin" });
  if (reply.error) return { error: reply.error };
  return { status: reply.status, statusText: reply.statusText, headers: reply.headers, url: reply.url, body: fromBase64(reply.body) };
}

// ------------------------------------------------------------------ the page
let window_, document_;
const state = { url: "about:blank", width: 1280, height: 800, scrollX: 0, scrollY: 0, cookie: "", scheme: "light",
                userAgent: "Mozilla/5.0", language: "en-GB", storage: {}, loaded: false };
const geometry = new Map();      // element id -> [x, y, width, height, fixed] in page terms
const observers = new Set();     // the live IntersectionObservers
const panels = new Map();        // a scrolling panel's id -> [top, shown height, whole height]
const resizers = new Set();      // the live ResizeObservers
const ids = new WeakMap();
const byId = new Map();
let lastId = 0;
function idOf(node) {
  let id = ids.get(node);
  if (!id) { id = ++lastId; ids.set(node, id); byId.set(id, new WeakRef(node)); }
  return id;
}
function nodeOf(id) { const ref = byId.get(id); return ref ? ref.deref() : null; }

class MerlinStorage {
  constructor(initial, persist) { this._items = new Map(Object.entries(initial || {})); this._persist = persist; }
  get length() { return this._items.size; }
  key(n) { return [...this._items.keys()][n] ?? null; }
  getItem(k) { return this._items.has(String(k)) ? this._items.get(String(k)) : null; }
  setItem(k, v) { this._items.set(String(k), String(v)); this._save(); }
  removeItem(k) { this._items.delete(String(k)); this._save(); }
  clear() { this._items.clear(); this._save(); }
  _save() { if (this._persist) send({ type: "storage", items: Object.fromEntries(this._items) }); }
}

function makeLocation() {
  const current = () => new URL(state.url);
  const location = {
    get href() { return state.url; }, set href(v) { navigate(v); },
    get origin() { return current().origin; }, get protocol() { return current().protocol; },
    get host() { return current().host; }, get hostname() { return current().hostname; },
    get port() { return current().port; }, get pathname() { return current().pathname; },
    get search() { return current().search; }, get hash() { return current().hash; },
    set hash(v) { const u = current(); u.hash = v; state.url = u.href; send({ type: "url", url: state.url }); },
    assign(v) { navigate(v); }, replace(v) { navigate(v, true); }, reload() { send({ type: "reload" }); },
    toString() { return state.url; },
  };
  return location;
}
function navigate(target, replace = false) {
  send({ type: "navigate", url: new URL(String(target), state.url).href, replace });
}

function evaluateMedia(query) {
  // enough of @media for scripts that ask: widths, heights, colour scheme, motion
  const q = String(query).toLowerCase();
  const test = (part) => {
    part = part.trim().replace(/^\(|\)$/g, "");
    const m = part.match(/^(min|max)-(width|height)\s*:\s*([\d.]+)(px|em|rem)?$/);
    if (m) {
      const value = parseFloat(m[3]) * (m[4] === "em" || m[4] === "rem" ? 16 : 1);
      const actual = m[2] === "width" ? state.width : state.height;
      return m[1] === "min" ? actual >= value : actual <= value;
    }
    if (part.startsWith("prefers-color-scheme")) return part.includes(state.scheme);
    if (part.startsWith("prefers-reduced-motion")) return part.includes("no-preference");
    if (part.startsWith("hover") || part.startsWith("any-hover")) return part.includes("hover") && !part.includes("none");
    if (part.startsWith("pointer") || part.startsWith("any-pointer")) return part.includes("fine");
    if (part === "screen" || part === "all" || part === "only screen") return true;
    if (part === "print") return false;
    return false;
  };
  return q.split(",").some(alt => alt.split(/\band\b/).every(test));
}

// The runtime has its own navigator, fetch and more, some read-only: assigned,
// the page's were silently ignored and it got the runtime's. Defined instead.
function define(name, value) {
  try { Object.defineProperty(globalThis, name, { value, writable: true, configurable: true, enumerable: true }); }
  catch (_) { try { globalThis[name] = value; } catch (__) {} }
}

const INTERFACES = [
  "Node", "Element", "HTMLElement", "Document", "HTMLDocument", "DocumentType", "DocumentFragment", "Attr",
  "CharacterData", "Text", "Comment", "ShadowRoot", "SVGElement", "SVGSVGElement", "SVGGraphicsElement",
  "HTMLAnchorElement", "HTMLAreaElement", "HTMLAudioElement", "HTMLBRElement", "HTMLBaseElement",
  "HTMLBodyElement", "HTMLButtonElement", "HTMLCanvasElement", "HTMLDListElement", "HTMLDataElement",
  "HTMLDataListElement", "HTMLDetailsElement", "HTMLDialogElement", "HTMLDivElement", "HTMLEmbedElement",
  "HTMLFieldSetElement", "HTMLFormElement", "HTMLHRElement", "HTMLHeadElement", "HTMLHeadingElement",
  "HTMLHtmlElement", "HTMLIFrameElement", "HTMLImageElement", "HTMLInputElement", "HTMLLIElement",
  "HTMLLabelElement", "HTMLLegendElement", "HTMLLinkElement", "HTMLMapElement", "HTMLMediaElement",
  "HTMLMenuElement", "HTMLMetaElement", "HTMLMeterElement", "HTMLModElement", "HTMLOListElement",
  "HTMLObjectElement", "HTMLOptGroupElement", "HTMLOptionElement", "HTMLOutputElement",
  "HTMLParagraphElement", "HTMLParamElement", "HTMLPictureElement", "HTMLPreElement", "HTMLProgressElement",
  "HTMLQuoteElement", "HTMLScriptElement", "HTMLSelectElement", "HTMLSlotElement", "HTMLSourceElement",
  "HTMLSpanElement", "HTMLStyleElement", "HTMLTableCaptionElement", "HTMLTableCellElement",
  "HTMLTableColElement", "HTMLTableElement", "HTMLTableRowElement", "HTMLTableSectionElement",
  "HTMLTemplateElement", "HTMLTextAreaElement", "HTMLTimeElement", "HTMLTitleElement", "HTMLTrackElement",
  "HTMLUListElement", "HTMLUnknownElement", "HTMLVideoElement", "HTMLFrameElement", "HTMLFrameSetElement",
  "Range", "NodeFilter", "NodeList", "HTMLCollection", "NamedNodeMap", "DOMTokenList", "CSSStyleDeclaration",
  "MutationObserver", "MutationRecord", "TreeWalker", "InputEvent", "KeyboardEvent", "MouseEvent",
  "FocusEvent", "PointerEvent", "UIEvent", "WheelEvent", "TouchEvent", "AnimationEvent", "TransitionEvent",
  "PopStateEvent", "HashChangeEvent", "SubmitEvent", "StorageEvent", "PageTransitionEvent", "BeforeUnloadEvent",
  "ClipboardEvent", "DragEvent", "CompositionEvent", "ToggleEvent", "SecurityPolicyViolationEvent",
];

function install(html) {
  const made = parseHTML(html);
  window_ = made.window; document_ = made.document;
  const g = globalThis;
  // The runtime's own versions of these go first (they are configurable), so the
  // page's below take their place rather than being silently ignored
  for (const name of ["location", "navigator", "localStorage", "sessionStorage", "history", "screen",
                      "alert", "confirm", "prompt", "open", "close", "WebSocket", "EventSource", "Worker",
                      "fetch", "XMLHttpRequest", "matchMedia", "PerformanceObserver"]) {
    try { delete g[name]; } catch (_) {}
  }
  // the DOM's classes and helpers become globals, as a browser's are
  for (const key of Object.getOwnPropertyNames(made)) {
    if (key in g && !["Event", "CustomEvent", "EventTarget"].includes(key)) continue;
    try { g[key] = made[key]; } catch (_) { /* read-only */ }
  }
  for (const key of ["Node", "Element", "HTMLElement", "Text", "Comment", "DocumentFragment", "Event", "CustomEvent",
                     "EventTarget", "MutationObserver", "customElements", "HTMLTemplateElement", "SVGElement",
                     "ShadowRoot", "Range", "NodeFilter", "DOMParser", "HTMLCollection", "NodeList"]) {
    if (made[key] !== undefined) g[key] = made[key];
  }
  // every element interface a browser has, as a global: React asks whether
  // things are an HTMLIFrameElement; one linkedom lacks is a class of its own,
  // so instanceof answers no rather than throwing
  const Base = made.window.HTMLElement || made.HTMLElement;
  for (const name of INTERFACES) {
    let found;
    try { found = made.window[name] ?? made[name]; } catch (_) { found = undefined; }
    // an event interface linkedom lacks is an Event, not an element
    // an event class the DOM lacks is an Event carrying what it is made with
    // (a PopStateEvent's state, a KeyboardEvent's key), as the real ones do
    if (typeof found !== "function") found = name.endsWith("Event")
      ? class extends (made.Event || g.Event) {
          constructor(type, init = {}) {
            super(type, init);
            for (const [key, value] of Object.entries(init || {})) {
              if (["bubbles", "cancelable", "composed"].includes(key)) continue;
              try { Object.defineProperty(this, key, { value, configurable: true, enumerable: true }); } catch (_) {}
            }
          }
        }
      : class extends Base {};
    try { Object.defineProperty(found, "name", { value: name }); } catch (_) {}
    if (!(name in g) || typeof g[name] !== "function") g[name] = found;
  }
  // what Object.prototype.toString says of the DOM's objects, as in browsers:
  // "[object HTMLButtonElement]". Libraries tell an element from a selector
  // so (Tippy, Convert to it!'s help button): linkedom's said "[object
  // Object]", and the button was taken for a selector and the page stopped
  for (const name of [...INTERFACES, "Node", "Element", "HTMLElement", "SVGElement", "Text", "Comment",
                      "DocumentFragment", "Document", "NodeList", "HTMLCollection", "Event", "CustomEvent"]) {
    const cls = g[name];
    const proto = typeof cls === "function" ? cls.prototype : null;
    if (proto && !Object.prototype.hasOwnProperty.call(proto, Symbol.toStringTag)) {
      try { Object.defineProperty(proto, Symbol.toStringTag, { value: name, configurable: true }); } catch (_) {}
    }
  }
  if (g.HTMLImageElement?.prototype) {
    const imageProto = g.HTMLImageElement.prototype;
    const srcOwner = (() => { let p = imageProto; while (p && !Object.getOwnPropertyDescriptor(p, "src")) p = Object.getPrototypeOf(p); return p; })();
    const srcDescriptor = srcOwner && Object.getOwnPropertyDescriptor(srcOwner, "src");
    Object.defineProperty(imageProto, "src", { configurable: true,
      get() { return srcDescriptor?.get ? srcDescriptor.get.call(this) : (this.getAttribute("src") || ""); },
      set(value) {
        if (srcDescriptor?.set) srcDescriptor.set.call(this, value); else this.setAttribute("src", String(value));
        loadsFired.delete(this);
        arrived(this);
      } });
    for (const [name, value] of [["complete", true], ["naturalWidth", 1], ["naturalHeight", 1]]) {
      if (!(name in imageProto)) Object.defineProperty(imageProto, name, { configurable: true, get() { return value; } });
    }
    if (typeof imageProto.decode !== "function") imageProto.decode = function () { return Promise.resolve(); };
  }
  g.Image = function Image(width, height) { const img = document_.createElement("img"); if (width) img.width = width; if (height) img.height = height; return img; };
  g.Option = function Option(text = "", value) { const o = document_.createElement("option"); o.textContent = text; if (value !== undefined) o.value = value; return o; };
  g.window = g; g.self = g; g.globalThis = g; g.document = document_; g.top = g; g.parent = g; g.frames = g;
  g.location = makeLocation();
  document_.location = g.location;
  try { Object.defineProperty(document_, "defaultView", { get: () => g, configurable: true }); } catch (_) {}
  try { Object.defineProperty(document_, "URL", { get: () => state.url, configurable: true }); } catch (_) {}
  try { Object.defineProperty(document_, "baseURI", { get: () => state.url, configurable: true }); } catch (_) {}
  try { Object.defineProperty(document_, "referrer", { get: () => "", configurable: true }); } catch (_) {}
  let readyState = "loading";
  try { Object.defineProperty(document_, "readyState", { get: () => readyState, configurable: true }); } catch (_) {}
  g.__setReadyState = (value) => { readyState = value; document_.dispatchEvent(new g.Event("readystatechange")); };
  try {
    Object.defineProperty(document_, "cookie", {
      get: () => state.cookie,
      set: (value) => {
        const [pair] = String(value).split(";");
        const [name] = pair.split("=");
        const kept = state.cookie ? state.cookie.split("; ").filter(c => !c.startsWith(name.trim() + "=")) : [];
        if (!/;\s*(expires=Thu, 01 Jan 1970|max-age=0)/i.test(value)) kept.push(pair.trim());
        state.cookie = kept.join("; ");
        send({ type: "cookie", value: String(value) });
      }, configurable: true,
    });
  } catch (_) {}
  try { Object.defineProperty(document_, "visibilityState", { get: () => "visible", configurable: true }); } catch (_) {}
  // what a browser's document has and linkedom's did not: GitHub's scripts
  // stopped on "Unable to get document domain"
  const documentProperties = {
    domain: () => new URL(state.url).hostname, characterSet: () => "UTF-8", charset: () => "UTF-8",
    inputEncoding: () => "UTF-8", compatMode: () => (state.quirks ? "BackCompat" : "CSS1Compat"), contentType: () => "text/html",
    designMode: () => "off", lastModified: () => new Date().toLocaleString("en-US"),
    scrollingElement: () => document_.documentElement, fullscreenElement: () => null,
    pointerLockElement: () => null, pictureInPictureElement: () => null,
  };
  for (const [name, getter] of Object.entries(documentProperties)) {
    let present;
    try { present = document_[name]; } catch (_) { present = undefined; }
    if (present === undefined) { try { Object.defineProperty(document_, name, { get: getter, configurable: true }); } catch (_) {} }
  }
  let activeElement;
  try { activeElement = document_.activeElement; } catch (_) {}
  if (activeElement === undefined) { try { Object.defineProperty(document_, "activeElement", { get: () => document_.body, configurable: true }); } catch (_) {} }
  if (!document_.fonts) {
    const fonts = { ready: Promise.resolve(), status: "loaded", size: 0, check: () => true, load: async () => [],
                    add() {}, delete() {}, clear() {}, forEach() {}, has: () => false, values: () => [][Symbol.iterator](),
                    addEventListener() {}, removeEventListener() {} };
    fonts.ready = Promise.resolve(fonts);
    try { Object.defineProperty(document_, "fonts", { value: fonts, configurable: true }); } catch (_) {}
  }
  try { Object.defineProperty(document_, "hidden", { get: () => false, configurable: true }); } catch (_) {}
  document_.hasFocus = () => true;
  g.history = {
    length: 1, state: null, scrollRestoration: "auto",
    pushState(data, _title, url) { this.state = data; if (url !== undefined && url !== null) { state.url = new URL(String(url), state.url).href; send({ type: "url", url: state.url }); } },
    replaceState(data, _title, url) { this.state = data; if (url !== undefined && url !== null) { state.url = new URL(String(url), state.url).href; send({ type: "url", url: state.url, replace: true }); } },
    back() { send({ type: "history", step: -1 }); }, forward() { send({ type: "history", step: 1 }); },
    go(n) { send({ type: "history", step: n || 0 }); },
  };
  g.navigator = {
    userAgent: state.userAgent, appVersion: state.userAgent.replace(/^Mozilla\//, ""), appName: "Netscape",
    platform: state.userAgent.includes("Windows") ? "Win32" : state.userAgent.includes("Macintosh") ? "MacIntel"
      : "Linux x86_64", vendor: "Google Inc.",
    language: state.language, languages: [state.language, state.language.split("-")[0]], onLine: true,
    cookieEnabled: true, hardwareConcurrency: 4, maxTouchPoints: 0, doNotTrack: null, webdriver: false,
    userAgentData: chromeHints(state.userAgent, state.brands), clipboard: { writeText: async () => {}, readText: async () => "" },
    sendBeacon(url, data) { merlinFetch(url, { method: "POST", body: data ?? null }).catch(() => {}); return true; },
    permissions: { query: async () => ({ state: "prompt", addEventListener() {} }) },
    mediaDevices: undefined, geolocation: undefined,
    // no serviceWorker at all: a site then does without, as in a browser with
    // them off; one there, but never taking requests, left sites waiting
  };
  g.screen = { width: state.width, height: state.height, availWidth: state.width, availHeight: state.height,
               colorDepth: 24, pixelDepth: 24, orientation: { type: "landscape-primary", angle: 0, addEventListener() {} } };
  for (const [name, getter] of Object.entries({
    innerWidth: () => state.width, innerHeight: () => state.height, outerWidth: () => state.width,
    outerHeight: () => state.height, scrollX: () => state.scrollX, scrollY: () => state.scrollY,
    pageXOffset: () => state.scrollX, pageYOffset: () => state.scrollY, devicePixelRatio: () => 1,
  })) Object.defineProperty(g, name, { get: getter, configurable: true });
  g.scrollTo = g.scroll = (x, y) => {
    if (typeof x === "object" && x) { y = x.top; x = x.left; }
    send({ type: "scroll", x: x ?? state.scrollX, y: y ?? state.scrollY });
  };
  g.scrollBy = (x, y) => { if (typeof x === "object" && x) { y = x.top; x = x.left; } g.scrollTo(state.scrollX + (x || 0), state.scrollY + (y || 0)); };
  g.localStorage = new MerlinStorage(state.storage, true);
  g.sessionStorage = new MerlinStorage({}, false);
  g.fetch = merlinFetch;
  g.matchMedia = (query) => ({
    media: String(query), get matches() { return evaluateMedia(query); }, onchange: null,
    addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {}, dispatchEvent() { return false; },
  });
  const frame = (cb) => setTimeout(() => { try { cb(performance.now()); } catch (e) { reportError(e); } }, 16);
  g.requestAnimationFrame = frame; g.cancelAnimationFrame = (id) => clearTimeout(id);
  g.requestIdleCallback = (cb) => setTimeout(() => { try { cb({ didTimeout: false, timeRemaining: () => 10 }); } catch (e) { reportError(e); } }, 1);
  g.cancelIdleCallback = (id) => clearTimeout(id);
  g.getComputedStyle = (element) => {
    const inline = element && element.style ? element.style : {};
    return new Proxy({}, { get(_t, name) {
      if (name === "getPropertyValue") return (p) => (inline.getPropertyValue ? inline.getPropertyValue(p) : "") || "";
      if (typeof name !== "string") return undefined;
      return inline[name] || (name === "display" ? "block" : name === "visibility" ? "visible" : "");
    } });
  };
  g.IntersectionObserver = class {
    // Worked out from Merlin Engine's layout and the scroll: an entry each
    // time a target comes into or goes out of view, past a threshold, as in a
    // browser. Until the page has been laid out, nothing is reported.
    constructor(callback, options = {}) {
      this._cb = callback; this._targets = new Map(); this.root = options.root ?? null;
      this.rootMargin = options.rootMargin ?? "0px";
      this.thresholds = [].concat(options.threshold ?? 0).map(Number).sort((a, b) => a - b);
      observers.add(this);
    }
    observe(target) { if (!this._targets.has(target)) { this._targets.set(target, null); queueMicrotask(() => this._check()); } }
    unobserve(target) { this._targets.delete(target); }
    disconnect() { this._targets.clear(); observers.delete(this); }
    takeRecords() { return []; }
    _margins() {
      const parts = String(this.rootMargin).trim().split(/\s+/).map((p) => p.endsWith("%") ? parseFloat(p) / 100 * state.height : parseFloat(p) || 0);
      const [top, right = top, bottom = top, left = right] = parts;
      return { top, right, bottom, left };
    }
    _check() {
      if (!geometry.size) return;
      const m = this._margins();
      const view = { top: -m.top, left: -m.left, bottom: state.height + m.bottom, right: state.width + m.right };
      const entries = [];
      for (const [target, last] of this._targets) {
        const rect = target.getBoundingClientRect();
        const w = Math.max(0, Math.min(rect.right, view.right) - Math.max(rect.left, view.left));
        const h = Math.max(0, Math.min(rect.bottom, view.bottom) - Math.max(rect.top, view.top));
        const area = rect.width * rect.height;
        const ratio = area > 0 ? (w * h) / area : (w > 0 || h > 0 ? 1 : 0);
        // in view if any of it is; a box with no size, as browsers have it, if
        // where it is lies in view (an empty placeholder waiting to be filled
        // when it is seen had been never in view)
        const inside = rect.bottom >= view.top && rect.top <= view.bottom && rect.right >= view.left && rect.left <= view.right;
        const visible = area > 0 ? w * h > 0 : inside && geometry.has(ids.get(target));
        // which threshold band the ratio is in: an entry when that changes
        const band = this.thresholds.filter((t) => ratio >= t && (t > 0 || visible)).length;
        if (last === null || last !== band) {
          this._targets.set(target, band);
          entries.push({ target, isIntersecting: visible, intersectionRatio: visible ? ratio : 0, time: performance.now(),
            boundingClientRect: rect, rootBounds: { top: view.top, left: view.left, bottom: view.bottom, right: view.right,
              width: view.right - view.left, height: view.bottom - view.top },
            intersectionRect: { top: Math.max(rect.top, view.top), left: Math.max(rect.left, view.left), width: w, height: h } });
        }
      }
      if (entries.length) { try { this._cb(entries, this); } catch (e) { reportError(e); } }
    }
  };
  // Worked out from Merlin Engine's layout: each target's size once laid out,
  // and again whenever a layout changes it. It had always said 0 high: Square
  // sizes AlterniTech's logo from it, and drew it 0 pixels tall.
  g.ResizeObserver = class {
    constructor(callback) { this._cb = callback; this._targets = new Map(); resizers.add(this); }
    observe(target) { if (!this._targets.has(target)) { this._targets.set(target, null); queueMicrotask(() => this._check()); } }
    unobserve(target) { this._targets.delete(target); }
    disconnect() { this._targets.clear(); resizers.delete(this); }
    _check() {
      if (!geometry.size) return;
      const entries = [];
      for (const [target, last] of this._targets) {
        const box = geometry.get(ids.get(target));
        const width = box ? box[2] : 0, height = box ? box[3] : 0;
        const key = width + "x" + height;
        if (key === last) continue;
        this._targets.set(target, key);
        const size = [{ inlineSize: width, blockSize: height }];
        entries.push({ target, contentRect: { x: 0, y: 0, top: 0, left: 0, width, height, right: width, bottom: height },
                       contentBoxSize: size, borderBoxSize: size, devicePixelContentBoxSize: size });
      }
      if (entries.length) { try { this._cb(entries, this); } catch (e) { reportError(e); } }
    }
  };
  g.PerformanceObserver = class { constructor() {} observe() {} disconnect() {} takeRecords() { return []; } static get supportedEntryTypes() { return []; } };
  g.alert = (text) => send({ type: "alert", text: String(text) });
  g.confirm = () => false; g.prompt = () => null; g.print = () => {};
  g.open = (url) => { if (url) send({ type: "open", url: new URL(String(url), state.url).href }); return null; };
  g.close = () => {}; g.focus = () => {}; g.blur = () => {};
  g.getSelection = () => ({ rangeCount: 0, toString: () => "", removeAllRanges() {}, addRange() {} });
  g.WebSocket = class { constructor() { throw new Error("WebSocket is not available in Merlin Engine yet"); } };
  g.EventSource = class { constructor() { this.readyState = 2; } close() {} addEventListener() {} };
  // Web Workers, SharedWorkers, XMLHttpRequest and the Cache API (workers.js):
  // what they fetch is fetched by Merlin, as everything the page fetches
  const env = {
    blurTiming: () => !!state.blurTiming,
    base: () => state.url,
    fetch: pageFetch,
    userAgent: () => state.userAgent, language: () => state.language,
    console: (level, text) => send({ type: "console", level, text }),
    reportError: (e) => reportError(e),
  };
  const workers = makeWorkerClasses(env);
  g.Worker = workers.Worker;
  g.SharedWorker = workers.SharedWorker;
  g.XMLHttpRequest = makeXHR(env);
  g.caches = makeCacheStorage({ base: () => state.url, fetch: (input, init) => merlinFetch(input, init) });
  // a form field's type as browsers give it: linkedom gave null for an input
  // with none, and React, seeing no text field, ignored typing in it
  const INPUT_TYPES = new Set(["text", "search", "tel", "url", "email", "password", "date", "month",
    "week", "time", "datetime-local", "number", "range", "color", "checkbox", "radio", "file",
    "submit", "image", "reset", "button", "hidden"]);
  const typed = (cls, get) => {
    if (typeof cls !== "function" || !cls.prototype) return;
    try {
      Object.defineProperty(cls.prototype, "type", { configurable: true, get,
        set(value) { this.setAttribute("type", String(value)); } });
    } catch (_) {}
  };
  typed(g.HTMLInputElement, function () {
    const t = (this.getAttribute("type") || "").toLowerCase(); return INPUT_TYPES.has(t) ? t : "text"; });
  typed(g.HTMLTextAreaElement, function () { return "textarea"; });
  typed(g.HTMLSelectElement, function () { return this.hasAttribute("multiple") ? "select-multiple" : "select-one"; });
  typed(g.HTMLButtonElement, function () {
    const t = (this.getAttribute("type") || "").toLowerCase(); return ["submit", "reset", "button"].includes(t) ? t : "submit"; });
  installHandlers(g, made);
  // Node.prototype's own getters (firstChild, childNodes...) are the DOM's
  // generic ones, null for every node; the real ones are its subclasses'.
  // Svelte takes firstChild's getter from Node.prototype and calls it on
  // every node, as is right in a browser: Hugging Face's app got null, and
  // stopped. Each now hands on to the node's own class.
  {
    const NodeClass = g.Node || made.Node;
    const base = NodeClass && NodeClass.prototype;
    const names = ["firstChild", "lastChild", "childNodes", "nextSibling", "previousSibling", "parentNode",
                   "parentElement", "textContent", "nodeValue", "ownerDocument", "isConnected",
                   "firstElementChild", "lastElementChild", "nextElementSibling", "previousElementSibling", "children"];
    const own = (node, name) => {
      let proto = Object.getPrototypeOf(node);
      while (proto && proto !== base) {
        const found = Object.getOwnPropertyDescriptor(proto, name);
        if (found) return found;
        proto = Object.getPrototypeOf(proto);
      }
      return null;
    };
    for (const name of names) {
      if (!base) break;
      const generic = Object.getOwnPropertyDescriptor(base, name);
      // only the DOM's generic getters: a plain value (ownerDocument, which
      // each node is given) or a name not there must be left to be assigned
      if (!generic || typeof generic.get !== "function") continue;
      try {
        Object.defineProperty(base, name, {
          configurable: true, enumerable: true,
          get() {
            const found = own(this, name);
            if (found && found.get) return found.get.call(this);
            return generic && generic.get ? generic.get.call(this) : (generic ? generic.value : undefined);
          },
          set(value) {
            const found = own(this, name);
            if (found && found.set) return found.set.call(this, value);
            if (generic && generic.set) return generic.set.call(this, value);
          },
        });
      } catch (_) {}
    }
  }
  // The window's event target takes only Node's own events: an
  // event made from the DOM's classes (PopStateEvent, or the load and scroll
  // this host sends) failed on it, reading 'target', so no page ever heard a
  // window's load, scroll or popstate. It is passed on as a native event with
  // the same details, and a cancelled one is cancelled for the page too.
  const NativeEvent = NodeEvent;
  g.addEventListener = windowListen;
  g.removeEventListener = windowUnlisten;
  g.dispatchEvent = function (event) {
    if (event instanceof NativeEvent) return windowDispatch(event);
    const native = new NativeEvent(event.type, { bubbles: !!event.bubbles, cancelable: event.cancelable !== false,
                                                 composed: !!event.composed });
    for (const key of Object.keys(event)) {
      if (key in native) continue;
      try { Object.defineProperty(native, key, { value: event[key], configurable: true, enumerable: true }); } catch (_) {}
    }
    for (const key of ["state", "detail", "key", "code", "clientX", "clientY", "button", "data", "newURL", "oldURL", "persisted"]) {
      if (key in event && !(key in native)) { try { Object.defineProperty(native, key, { value: event[key], configurable: true }); } catch (_) {} }
    }
    const result = windowDispatch(native);
    if (native.defaultPrevented) { try { event.preventDefault(); } catch (_) {} }
    return result;
  };
  // window.postMessage: a message event on the window, after the current
  // task, as in a browser. Missing, reCAPTCHA's script stopped on it.
  g.postMessage = function (data, targetOrigin) {
    const origin = (() => { try { return new URL(state.url).origin; } catch (_) { return "null"; } })();
    setTimeout(() => {
      try { g.dispatchEvent(messageEvent(data, origin, g, [])); } catch (e) { reportError(e); }
    }, 0);
  };
  installFrames(g, made);
  installWebExtras(g);
  installMedia(g);
  installWindowClass(g);
  installCSSOM(g);
  installDialogs(g, made);
  installSelects();
  // form.submit() and requestSubmit(), which the DOM had as nothing at all:
  // search boxes that send their form from a script (Google's, Ponder's) did
  // nothing on Enter. requestSubmit runs the page's submit handlers first.
  // A file input clicked, or its picker asked for, by the page's own button
  // (Ponder's Browse): Merlin opens the file dialog, and what is chosen comes
  // back as Files in input.files, with input and change, as in a browser.
  {
    const elementProto = (g.HTMLElement || made.HTMLElement).prototype;
    const isFile = (el) => el.tagName === "INPUT" && (el.getAttribute("type") || "").toLowerCase() === "file";
    // what the dialog is for: an input never put in the page is known only by this
    const pickFile = (el) => ({ type: "pick_file", target: idOf(el), accept: el.getAttribute("accept") || "",
                                multiple: el.hasAttribute("multiple") });
    const originalClick = elementProto.click;
    elementProto.click = function () {
      if (isFile(this)) { if (!this.hasAttribute("disabled")) send(pickFile(this)); return; }
      if (typeof originalClick === "function") return originalClick.call(this);
      this.dispatchEvent(new g.MouseEvent("click", { bubbles: true, cancelable: true }));
    };
    elementProto.showPicker = function () { if (isFile(this)) send(pickFile(this)); };
  }
  // on the element class itself: the DOM does not make <form> an
  // HTMLFormElement, and methods put there never reached a form
  const formProto = (g.HTMLElement || made.HTMLElement).prototype;
  {
    formProto.submit = function () { if (this.tagName === "FORM") send({ type: "form", target: idOf(this) }); };
    formProto.requestSubmit = function (submitter) {
      if (this.tagName !== "FORM") return;
      const event = new g.Event("submit", { bubbles: true, cancelable: true });
      try { event.submitter = submitter || null; } catch (_) {}
      this.dispatchEvent(event);
      if (!event.defaultPrevented) send({ type: "form", target: idOf(this), submitter: submitter ? idOf(submitter) : null });
    };
  }
  // performance.timing and .navigation, the older page timings YouTube reads
  // as it starts; and document.implementation, for a separate inert document
  // (YouTube's web components library makes one)
  if (!performance.timing) {
    const start = Date.now() - Math.round(performance.now());
    const timing = {};
    for (const [name, at] of Object.entries({ navigationStart: 0, fetchStart: 1, domainLookupStart: 1,
      domainLookupEnd: 2, connectStart: 2, connectEnd: 5, secureConnectionStart: 3, requestStart: 6,
      responseStart: 40, responseEnd: 60, domLoading: 61, domInteractive: 200, domContentLoadedEventStart: 210,
      domContentLoadedEventEnd: 215, domComplete: 400, loadEventStart: 401, loadEventEnd: 402,
      redirectStart: 0, redirectEnd: 0, unloadEventStart: 0, unloadEventEnd: 0 })) timing[name] = at ? start + at : (name === "navigationStart" ? start : 0);
    timing.toJSON = () => ({ ...timing });
    try { Object.defineProperty(performance, "timing", { value: timing, configurable: true }); } catch (_) {}
    try { Object.defineProperty(performance, "navigation", { value: { type: 0, redirectCount: 0 }, configurable: true }); } catch (_) {}
  }
  if (!document_.implementation || typeof document_.implementation.createHTMLDocument !== "function") {
    const implementation = {
      createHTMLDocument(title = "") {
        return parseHTML(`<!DOCTYPE html><html><head><title>${String(title)}</title></head><body></body></html>`).document;
      },
      createDocument() { return parseHTML("<html></html>").document; },
      createDocumentType(name) { return { nodeType: 10, name }; },
      hasFeature() { return true; },
    };
    try { Object.defineProperty(document_, "implementation", { value: implementation, configurable: true }); } catch (_) {}
  }
  // A whole TreeWalker, as the DOM specification gives it: the DOM's had only
  // nextNode, and GitHub's code (firstChild) and YouTube's failed on it
  const SHOW = (node, mask) => (mask >>> 0) & (1 << ((node.nodeType || 1) - 1));
  class MerlinTreeWalker {
    constructor(root, whatToShow = 0xFFFFFFFF, filter = null) {
      this.root = root; this.whatToShow = whatToShow >>> 0; this.filter = filter; this.currentNode = root;
    }
    _accept(node) {
      if (!SHOW(node, this.whatToShow)) return 3;                    // skip
      const f = this.filter;
      if (!f) return 1;
      const answer = typeof f === "function" ? f(node) : f.acceptNode(node);
      return answer === 2 ? 2 : answer === 3 ? 3 : 1;                // reject, skip, accept
    }
    parentNode() {
      let node = this.currentNode;
      while (node && node !== this.root) {
        node = node.parentNode;
        if (node && this._accept(node) === 1) { this.currentNode = node; return node; }
      }
      return null;
    }
    _child(first) {
      let node = first ? this.currentNode.firstChild : this.currentNode.lastChild;
      while (node) {
        const answer = this._accept(node);
        if (answer === 1) { this.currentNode = node; return node; }
        if (answer === 3) {
          const inner = first ? node.firstChild : node.lastChild;
          if (inner) { node = inner; continue; }
        }
        while (node) {
          const sibling = first ? node.nextSibling : node.previousSibling;
          if (sibling) { node = sibling; break; }
          const up = node.parentNode;
          if (!up || up === this.root || up === this.currentNode) return null;
          node = up;
        }
      }
      return null;
    }
    firstChild() { return this._child(true); }
    lastChild() { return this._child(false); }
    _sibling(next) {
      let node = this.currentNode;
      if (node === this.root) return null;
      for (;;) {
        let sibling = next ? node.nextSibling : node.previousSibling;
        while (sibling) {
          node = sibling;
          const answer = this._accept(node);
          if (answer === 1) { this.currentNode = node; return node; }
          sibling = next ? node.firstChild : node.lastChild;
          if (answer === 2 || !sibling) sibling = next ? node.nextSibling : node.previousSibling;
        }
        node = node.parentNode;
        if (!node || node === this.root) return null;
        if (this._accept(node) === 1) return null;
      }
    }
    nextSibling() { return this._sibling(true); }
    previousSibling() { return this._sibling(false); }
    previousNode() {
      let node = this.currentNode;
      while (node !== this.root) {
        let sibling = node.previousSibling;
        while (sibling) {
          node = sibling;
          let answer = this._accept(node);
          while (answer !== 2 && node.lastChild) { node = node.lastChild; answer = this._accept(node); }
          if (answer === 1) { this.currentNode = node; return node; }
          sibling = node.previousSibling;
        }
        if (node === this.root || !node.parentNode) return null;
        node = node.parentNode;
        if (this._accept(node) === 1) { this.currentNode = node; return node; }
      }
      return null;
    }
    nextNode() {
      let node = this.currentNode, answer = 1;
      for (;;) {
        while (answer !== 2 && node.firstChild) {
          node = node.firstChild;
          answer = this._accept(node);
          if (answer === 1) { this.currentNode = node; return node; }
        }
        let sibling = null, temporary = node;
        while (temporary) {
          if (temporary === this.root) return null;
          sibling = temporary.nextSibling;
          if (sibling) break;
          temporary = temporary.parentNode;
        }
        if (!sibling) return null;
        node = sibling;
        answer = this._accept(node);
        if (answer === 1) { this.currentNode = node; return node; }
      }
    }
  }
  const walkerOwner = Object.getPrototypeOf(document_);
  try {
    Object.defineProperty(walkerOwner, "createTreeWalker", { configurable: true, writable: true,
      value(root, whatToShow, filter) { return new MerlinTreeWalker(root, whatToShow ?? 0xFFFFFFFF, filter ?? null); } });
  } catch (_) {}
  g.TreeWalker = MerlinTreeWalker;
  // NodeFilter's constants, which the DOM's lacked: SHOW_ELEMENT undefined,
  // a walker for elements walked everything
  const filters = { FILTER_ACCEPT: 1, FILTER_REJECT: 2, FILTER_SKIP: 3, SHOW_ALL: 0xFFFFFFFF, SHOW_ELEMENT: 0x1,
    SHOW_ATTRIBUTE: 0x2, SHOW_TEXT: 0x4, SHOW_CDATA_SECTION: 0x8, SHOW_PROCESSING_INSTRUCTION: 0x40,
    SHOW_COMMENT: 0x80, SHOW_DOCUMENT: 0x100, SHOW_DOCUMENT_TYPE: 0x200, SHOW_DOCUMENT_FRAGMENT: 0x400 };
  const nodeFilter = (typeof g.NodeFilter === "function" || typeof g.NodeFilter === "object") && g.NodeFilter ? g.NodeFilter : {};
  for (const [name, value] of Object.entries(filters)) {
    if (nodeFilter[name] === undefined) { try { Object.defineProperty(nodeFilter, name, { value, enumerable: true }); } catch (_) {} }
  }
  g.NodeFilter = nodeFilter;
  // document.styleSheets: the page's stylesheets, a live list, as Google's
  // CSS loader reads its length. A rule inserted into a <style> sheet is added
  // to its text, so Merlin Engine styles the page with it.
  const sheets = new WeakMap();
  const sheetOf = (node) => {
    let sheet = sheets.get(node);
    if (sheet) return sheet;
    sheet = new g.CSSStyleSheet({ media: node.getAttribute("media") || "" }, node);
    sheets.set(node, sheet);
    return sheet;
  };
  try {
    Object.defineProperty(document_, "styleSheets", { configurable: true, get() {
      return g.StyleSheetList.of([...document_.querySelectorAll("style, link[rel~=stylesheet]")].map(sheetOf));
    } });
  } catch (_) {}
  for (const cls of [g.HTMLStyleElement, g.HTMLLinkElement]) {
    if (typeof cls !== "function") continue;
    try { Object.defineProperty(cls.prototype, "sheet", { configurable: true, get() { return sheetOf(this); } }); } catch (_) {}
  }
  // document.getElementsByName, which GitHub's behaviours script uses
  for (const proto of [Object.getPrototypeOf(document_), g.Document?.prototype].filter(Boolean)) {
    if (typeof proto.getElementsByName !== "function") {
      try {
        Object.defineProperty(proto, "getElementsByName", { configurable: true, writable: true,
          value(name) { return this.querySelectorAll(`[name="${String(name).replace(/["\\]/g, "\\$&")}"]`); } });
      } catch (_) {}
    }
  }
  // A second definition of an element is refused as browsers refuse it, with
  // a NotSupportedError: GitHub defines some twice and expects that refusal,
  // which linkedom gave as a plain Error that GitHub's code did not recognise.
  if (g.customElements && typeof g.customElements.define === "function") {
    const registry = g.customElements;
    const define = registry.define.bind(registry);
    // the registry's own get, kept: Polymer's ES5 adapter (YouTube's)
    // replaces customElements.get with its own list, and adds the name to it
    // before defining, which read as a second definition, and YouTube stopped
    const known = registry.get.bind(registry);
    registry.define = (name, constructor, options) => {
      if (known(name)) {
        throw new DOMException(`Failed to execute 'define' on 'CustomElementRegistry': the name "${name}" has already been used with this registry`, "NotSupportedError");
      }
      return define(name, constructor, options);
    };
  }
  // A link's address and its parts, as every browser's <a> and <area> have
  // them: Square's sites read link.pathname to take addresses apart, and,
  // with it undefined, stopped before building the site.
  for (const cls of [g.HTMLAnchorElement, g.HTMLAreaElement]) {
    if (typeof cls !== "function") continue;
    const whole = function () {
      const raw = this.getAttribute("href");
      if (raw === null) return null;
      try { return new URL(raw, state.url); } catch (_) { return null; }
    };
    const parts = ["protocol", "host", "hostname", "port", "pathname", "search", "hash", "username", "password"];
    try {
      Object.defineProperty(cls.prototype, "href", { configurable: true,
        get() { const u = whole.call(this); return u ? u.href : (this.getAttribute("href") ?? ""); },
        set(value) { this.setAttribute("href", String(value)); } });
      Object.defineProperty(cls.prototype, "origin", { configurable: true,
        get() { const u = whole.call(this); return u ? u.origin : ""; } });
    } catch (_) {}
    for (const part of parts) {
      try {
        Object.defineProperty(cls.prototype, part, { configurable: true,
          get() { const u = whole.call(this); return u ? u[part] : ""; },
          set(value) {
            const u = whole.call(this);
            if (!u) return;
            u[part] = value;
            this.setAttribute("href", u.href);
          } });
      } catch (_) {}
    }
  }
  // Node's globals, which no browser has, go: GitHub's code saw
  // process, took itself to be in Node, and failed reading process.env
  // An error in a timer or an unhandled promise, through Node's timers (as
  // scripts in node:vm use them), ended the whole process: reported instead,
  // and the page goes on, as in a browser. Then Node's globals go.
  try {
    // the page hears them as a browser's window does (window.onerror and
    // listeners for "error" and "unhandledrejection"), and they are logged
    const tell = (type, fields) => {
      try {
        const event = new NodeEvent(type, { cancelable: true });
        for (const [key, value] of Object.entries(fields)) Object.defineProperty(event, key, { value, configurable: true });
        windowDispatch(event);
      } catch (_) {}
    };
    proc.on("uncaughtException", (error) => {
      tell("error", { error, message: String(error && error.message || error), filename: "", lineno: 0, colno: 0 });
      reportError(error);
    });
    proc.on("unhandledRejection", (reason, promise) => {
      tell("unhandledrejection", { reason, promise });
      reportError(reason);
    });
  } catch (_) {}
  for (const name of ["setTimeout", "setInterval"]) {
    const original = g[name];
    if (typeof original !== "function" || original.__guarded) continue;
    // a number, as a browser's timers give (Node's are objects; clearTimeout
    // takes the number back once it has been asked for)
    const numbered = (timer) => (timer && typeof timer[Symbol.toPrimitive] === "function" ? timer[Symbol.toPrimitive]() : timer);
    const guarded = function (callback, delay, ...rest) {
      if (typeof callback !== "function") return numbered(original.call(this, () => {}, delay));
      return numbered(original.call(this, function () { try { return callback.apply(this, arguments); } catch (e) { reportError(e); } }, delay, ...rest));
    };
    guarded.__guarded = true;
    g[name] = guarded;
  }
  for (const name of ["process", "global", "setImmediate", "clearImmediate"]) {
    try { delete g[name]; } catch (_) {}
  }
  // Buffer, as Node's own fetch classes see it (Response.text() needs it),
  // and as the page sees it: not there, unless the page puts one there itself
  // (some bundles bring their own), as in a browser
  {
    const NodeBuffer = g.Buffer;
    let pageBuffer;
    try {
      Object.defineProperty(g, "Buffer", { enumerable: false, configurable: true,
        get() {
          const limit = Error.stackTraceLimit, prepare = Error.prepareStackTrace;
          Error.stackTraceLimit = 4; Error.prepareStackTrace = undefined;
          const holder = {};
          try { Error.captureStackTrace(holder); } finally { Error.stackTraceLimit = limit; Error.prepareStackTrace = prepare; }
          const caller = String(holder.stack || "").split("\n")[2] || "";
          return caller.includes("node:") ? NodeBuffer : pageBuffer;
        },
        set(value) { pageBuffer = value; } });
    } catch (_) {}
  }
  g.CSS = {
    escape(value) {
      // as the CSSOM specification gives it
      const text = String(value); let out = "";
      for (let i = 0; i < text.length; i++) {
        const c = text.charCodeAt(i), ch = text[i];
        if (c === 0) out += "\uFFFD";
        else if ((c >= 1 && c <= 31) || c === 127 || (i === 0 && c >= 48 && c <= 57)
                 || (i === 1 && c >= 48 && c <= 57 && text.charCodeAt(0) === 45)) out += "\\" + c.toString(16) + " ";
        else if (i === 0 && text.length === 1 && c === 45) out += "\\" + ch;
        else if (c >= 128 || c === 45 || c === 95 || (c >= 48 && c <= 57) || (c >= 65 && c <= 90) || (c >= 97 && c <= 122)) out += ch;
        else out += "\\" + ch;
      }
      return out;
    },
    supports(property, value) {
      if (value === undefined) {
        const text = String(property).trim();
        const selector = text.match(/^selector\((.*)\)$/s);
        if (selector) { try { document_.querySelector(selector[1]); return true; } catch (_) { return false; } }
        return /[a-z-]+\s*:/.test(text);
      }
      return /^-{0,2}[a-z][a-z-]*$/i.test(String(property));
    },
    registerProperty() {}, highlights: new Map(), paintWorklet: { addModule: async () => {} },
  };
  patchSelectors(g);
  ownMembers(g, made);
  // In a browser HTMLElement has no observedAttributes, and a page may assign
  // one to its element class; linkedom's base class has it as a getter only,
  // and GitHub's elements failed to load on assigning it.
  for (const name of INTERFACES) {
    let cls = g[name];
    while (typeof cls === "function" && cls !== Function.prototype) {
      const own = Object.getOwnPropertyDescriptor(cls, "observedAttributes");
      if (own && own.get && !own.set) {
        Object.defineProperty(cls, "observedAttributes", { configurable: true,
          get: own.get,
          set(value) { Object.defineProperty(this, "observedAttributes", { value, writable: true, configurable: true }); } });
      }
      cls = Object.getPrototypeOf(cls);
    }
  }
  const proto = (g.Element || made.Element).prototype;
  // where Merlin Engine laid the element out, as the window sees it now
  proto.getBoundingClientRect = function () {
    const box = geometry.get(ids.get(this));
    if (!box) return { x: 0, y: 0, top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0, toJSON() { return this; } };
    const [x, y, width, height, fixed] = box;
    const top = fixed ? y : y - state.scrollY, left = fixed ? x : x - state.scrollX;
    return { x: left, y: top, top, left, right: left + width, bottom: top + height, width, height, toJSON() { return this; } };
  };
  // The root element's client size is the viewport's, as browsers define it:
  // Square reads documentElement.clientWidth to choose its layout, got its
  // own box (0 before layout), and drew AlterniTech's site for a phone.
  const pageSize = (index) => {
    let most = index === 2 ? state.width : state.height;
    for (const box of geometry.values()) if (!box[4]) most = Math.max(most, box[index - 2] + box[index]);
    return Math.round(most);
  };
  for (const [name, index] of [["offsetWidth", 2], ["offsetHeight", 3], ["clientWidth", 2], ["clientHeight", 3],
                               ["scrollWidth", 2], ["scrollHeight", 3]]) {
    Object.defineProperty(proto, name, { configurable: true, get() {
      if (this === document_.documentElement || (this === document_.scrollingElement && name.startsWith("client"))) {
        if (name.startsWith("client")) return Math.round(index === 2 ? state.width : state.height);
        if (name.startsWith("scroll")) return pageSize(index);
      }
      // a scrolling panel: what it shows, and all it holds
      const panel = panels.get(ids.get(this));
      if (panel && (name === "clientHeight" || name === "scrollHeight"))
        return Math.round(name === "clientHeight" ? panel[1] : panel[2]);
      const box = geometry.get(ids.get(this)); return box ? Math.round(box[index]) : 0; } });
  }
  // A panel that scrolls inside the page: its position read and set, as
  // scrollTop and scrollTo; the page's own for the root and body.
  const isRoot = (el) => el === document_.documentElement || el === document_.body || el === document_.scrollingElement;
  const panelTo = (el, top) => {
    const id = ids.get(el);
    if (isRoot(el)) { g.scrollTo(state.scrollX, top); return; }
    if (!panels.has(id)) return;
    const panel = panels.get(id);
    panel[0] = Math.max(0, Math.min(panel[2] - panel[1], Number(top) || 0));
    send({ type: "panel_scroll_to", target: id, top: panel[0] });
  };
  Object.defineProperty(proto, "scrollTop", { configurable: true,
    get() { if (isRoot(this)) return state.scrollY || 0; const panel = panels.get(ids.get(this)); return panel ? panel[0] : 0; },
    set(value) { panelTo(this, value); } });
  Object.defineProperty(proto, "scrollLeft", { configurable: true,
    get() { return isRoot(this) ? (state.scrollX || 0) : 0; }, set(_value) {} });
  proto.scrollTo = proto.scroll = function (x, y) {
    const top = (x && typeof x === "object") ? x.top : y;
    if (top !== undefined && top !== null) panelTo(this, top);
  };
  proto.scrollBy = function (x, y) {
    const by = (x && typeof x === "object") ? x.top : y;
    panelTo(this, this.scrollTop + (Number(by) || 0));
  };
  // The form fields' validation, which the DOM had none of: Square's fields
  // set a custom message on theirs, and failed
  const fieldProto = (g.HTMLElement || made.HTMLElement).prototype;
  const validityOf = (el) => {
    const custom = el.__customValidity || "";
    const value = el.value ?? "";
    const missing = el.hasAttribute?.("required") && !value;
    const tooLong = el.hasAttribute?.("maxlength") && String(value).length > Number(el.getAttribute("maxlength"));
    const pattern = el.getAttribute?.("pattern");
    let mismatch = false;
    if (pattern && value) { try { mismatch = !new RegExp(`^(?:${pattern})$`).test(value); } catch (_) {} }
    const type = (el.getAttribute?.("type") || "").toLowerCase();
    const typeMismatch = !!value && ((type === "email" && !/^[^\s@]+@[^\s@]+$/.test(value)) ||
                                     (type === "url" && !/^[a-z][a-z0-9+.-]*:/i.test(value)));
    const state = { valueMissing: !!missing, typeMismatch, patternMismatch: mismatch, tooLong: !!tooLong,
                    tooShort: false, rangeUnderflow: false, rangeOverflow: false, stepMismatch: false,
                    badInput: false, customError: !!custom };
    state.valid = !Object.values(state).some(Boolean);
    return state;
  };
  fieldProto.setCustomValidity = function (message) { this.__customValidity = String(message ?? ""); };
  fieldProto.checkValidity = function () {
    const valid = validityOf(this).valid;
    if (!valid) this.dispatchEvent(new g.Event("invalid", { cancelable: true }));
    return valid;
  };
  fieldProto.reportValidity = function () { return this.checkValidity(); };
  for (const [name, getter] of Object.entries({
    validity() { return validityOf(this); },
    validationMessage() { return this.__customValidity || (validityOf(this).valid ? "" : "Please fill in this field."); },
    willValidate() { return ["INPUT", "SELECT", "TEXTAREA"].includes(this.tagName) && !this.hasAttribute("disabled"); },
  })) { try { Object.defineProperty(fieldProto, name, { configurable: true, get: getter }); } catch (_) {} }
  Object.defineProperty(proto, "offsetTop", { configurable: true, get() { const box = geometry.get(ids.get(this)); return box ? Math.round(box[1]) : 0; } });
  Object.defineProperty(proto, "offsetLeft", { configurable: true, get() { const box = geometry.get(ids.get(this)); return box ? Math.round(box[0]) : 0; } });
  if (!proto.getClientRects) proto.getClientRects = function () { return []; };
  for (const name of ["scrollIntoView", "focus", "blur", "scrollTo", "scrollBy", "animate", "requestFullscreen"]) {
    if (!proto[name]) proto[name] = function () { return name === "animate" ? { finished: Promise.resolve(), cancel() {}, play() {}, pause() {}, addEventListener() {} } : undefined; };
  }
  if (!("isConnected" in proto)) Object.defineProperty(proto, "isConnected", { get() { return this.ownerDocument?.contains?.(this) ?? true; }, configurable: true });
  g.reportError = (error) => {
    send({ type: "console", level: "error", text: `Uncaught ${safeString(error)}${error && error.stack ? "\n" + String(error.stack).split("\n").slice(1, 4).join("\n") : ""}` });
    showWhere(error).catch(() => {});
  };
  // An uncaught error, in a timer or an event, is reported and the page goes
  // on, as in a browser (see the process's handlers in install): one error in
  // GitHub's scripts had once ended all of them.
}

// Event handler properties (oninput, onclick...), as every browser has them on
// elements, the document and the window. React tests 'oninput' in document to
// decide whether the input event exists; linkedom had no such properties, so
// React took Internet Explorer's path and never saw typing.
const HANDLED = ["abort", "animationend", "animationstart", "auxclick", "beforeinput", "blur", "cancel",
  "canplay", "change", "click", "close", "contextmenu", "copy", "cut", "dblclick", "drag", "dragend",
  "dragenter", "dragleave", "dragover", "dragstart", "drop", "error", "focus", "focusin", "focusout",
  "input", "invalid", "keydown", "keypress", "keyup", "load", "loadeddata", "loadedmetadata", "loadstart",
  "mousedown", "mouseenter", "mouseleave", "mousemove", "mouseout", "mouseover", "mouseup", "paste",
  "pause", "play", "playing", "pointercancel", "pointerdown", "pointerenter", "pointerleave", "pointermove",
  "pointerout", "pointerover", "pointerup", "progress", "reset", "resize", "scroll", "select",
  "selectionchange", "submit", "toggle", "transitionend", "wheel", "beforeunload", "hashchange",
  "message", "popstate", "storage", "unload", "readystatechange", "DOMContentLoaded", "visibilitychange"];
const handlers = new WeakMap();

// Selectors linkedom does not know, which a browser does: a state no script
// can be in here (:target, :hover...) matches nothing, as it would at that
// moment; :defined matches all. GitHub's scripts stopped on :target.
const ALWAYS = new Set(["defined", "scope"]);
// states Merlin marks with an attribute (installDialogs): asked for, the attribute
const MARKED = { "modal": "[data-merlin-modal]", "popover-open": "[data-merlin-popover-open]" };
function forgiving(original) {
  return function (selector, ...rest) {
    let text = String(selector);
    for (let tries = 0; tries < 8; tries++) {
      try { return original.call(this, text, ...rest); }
      catch (error) {
        const found = String(error && error.message).match(/Unknown pseudo-class :([\w-]+)/i);
        if (!found) throw error;
        const name = found[1];
        const pattern = new RegExp(":" + name + "(\\([^)]*\\))?", "g");
        const next = text.replace(pattern, ALWAYS.has(name) ? "" : (MARKED[name.toLowerCase()] || ":not(*)"));
        if (next === text) throw error;
        text = next;
      }
    }
    return original.call(this, text, ...rest);
  };
}
// Node.prototype and Element.prototype own what a browser's do. linkedom
// keeps much of it lower down (appendChild and firstChild on a ParentNode
// class under Element, parentNode on each node itself) and leaves stubs on
// Node.prototype: appendChild there returns its argument and adds nothing,
// firstChild is always null. Polyfills keep the browser's own methods by
// copying them from these prototypes, ShadyDOM (YouTube's) among them, and
// got the stubs: YouTube built none of its page, looping for ever. Each is
// now the real one, whichever class of node it is called on.
const OWNED = {
  Node: ["parentNode", "parentElement", "firstChild", "lastChild", "previousSibling", "nextSibling", "childNodes",
         "textContent", "nodeValue", "isConnected", "appendChild", "insertBefore", "removeChild", "replaceChild",
         "cloneNode", "contains", "hasChildNodes", "getRootNode", "normalize", "compareDocumentPosition",
         "isEqualNode", "ownerDocument"],
  Element: ["children", "firstElementChild", "lastElementChild", "childElementCount", "previousElementSibling",
            "nextElementSibling", "querySelector", "querySelectorAll", "getElementsByTagName",
            "getElementsByClassName", "append", "prepend", "replaceChildren", "before", "after", "remove",
            "replaceWith", "matches", "closest", "innerHTML", "outerHTML", "insertAdjacentElement",
            "insertAdjacentHTML", "insertAdjacentText", "getAttribute", "setAttribute", "removeAttribute",
            "hasAttribute", "toggleAttribute", "className", "id", "slot", "shadowRoot", "attachShadow"],
  DocumentFragment: ["children", "firstElementChild", "lastElementChild", "childElementCount", "querySelector",
                     "querySelectorAll", "getElementById", "append", "prepend", "replaceChildren"],
  Document: ["children", "firstElementChild", "lastElementChild", "childElementCount", "querySelector",
             "querySelectorAll", "getElementById", "append", "prepend", "getElementsByTagName",
             "getElementsByClassName", "createElement", "createTextNode", "createComment",
             "createDocumentFragment", "importNode", "adoptNode"],
  CharacterData: ["data", "length", "previousElementSibling", "nextElementSibling", "before", "after", "remove",
                  "replaceWith", "appendData", "deleteData", "insertData", "replaceData", "substringData"],
};
function ownMembers(g, made) {
  const ours = new WeakSet();
  // the DOM's own implementations, as a browser's native code is: found in
  // every class the DOM made, before any page script runs. A polyfill's own
  // accessors on a node's prototype (ShadyDOM puts its logical tree on its
  // ShadowRoot's) are never taken for the native ones.
  const native = new WeakSet();
  const seen = new Set();
  const collect = (cls) => {
    let p = cls && cls.prototype;
    while (p && p !== Object.prototype && !seen.has(p)) {
      seen.add(p);
      for (const key of Object.getOwnPropertyNames(p)) {
        const d = Object.getOwnPropertyDescriptor(p, key);
        for (const fn of [d.get, d.set, d.value]) if (typeof fn === "function") native.add(fn);
      }
      p = Object.getPrototypeOf(p);
    }
  };
  for (const source of [made, made && made.window, g]) {
    if (!source) continue;
    for (const key of Object.getOwnPropertyNames(source)) {
      let value;
      try { value = source[key]; } catch (_) { continue; }
      if (typeof value === "function" && value.prototype) collect(value);
    }
  }
  for (const node of [document_, document_.documentElement, document_.createTextNode(""), document_.createComment(""),
                      document_.createDocumentFragment(), document_.createElement("div"), document_.createElement("template")]) {
    if (node) collect({ prototype: Object.getPrototypeOf(node) });
  }
  const DATA = new Set(["parentNode", "children", "firstElementChild", "lastElementChild", "childElementCount",
                        "previousElementSibling", "nextElementSibling", "innerHTML", "outerHTML", "className", "id",
                        "slot", "shadowRoot", "data", "length", "ownerDocument", "firstChild", "lastChild",
                        "previousSibling", "nextSibling", "childNodes", "parentElement", "textContent", "nodeValue",
                        "isConnected"]);
  for (const [face, names] of Object.entries(OWNED)) {
    const proto = g[face] && g[face].prototype;
    if (!proto) continue;
    for (const name of names) {
      const original = Object.getOwnPropertyDescriptor(proto, name);
      if (original && ours.has(original.get || original.value)) continue;
      // Node's own are linkedom's stubs; elsewhere a prototype's own is real,
      // and kept as it is
      if (original && face !== "Node") continue;
      // the real one for a node: the nearest of the DOM's own in its chain
      // (Node's stub last: a text node's firstChild is null). One already
      // running for this node is passed over: a class calling super.x comes
      // back here, and must go on up rather than round again.
      const running = [];
      const cache = new WeakMap();
      const busy = (node, fn) => running.some((r) => r.node === node && r.fn === fn);
      const real = (node) => {
        if (node === null || node === undefined) throw new TypeError("Illegal invocation");
        const start = Object.getPrototypeOf(node);
        const plain = !running.some((r) => r.node === node);
        if (plain && start && cache.has(start)) return cache.get(start);
        let p = start, found;
        while (p) {
          const here = p === proto ? original : Object.getOwnPropertyDescriptor(p, name);
          const fn = here && (here.get || here.value || here.set);
          if (here && native.has(fn) && !ours.has(fn) && !busy(node, fn)) { found = here; break; }
          p = Object.getPrototypeOf(p);
        }
        if (plain && start) cache.set(start, found);
        return found;
      };
      const run = (node, fn, call) => {
        running.push({ node, fn });
        try { return call(); } finally { running.pop(); }
      };
      const isMethod = original ? typeof original.value === "function" : !DATA.has(name);
      const descriptor = { configurable: true, enumerable: original ? !!original.enumerable : false };
      if (isMethod) {
        const method = function (...args) {
          const found = real(this);
          if (found && typeof found.value === "function") return run(this, found.value, () => found.value.apply(this, args));
          throw new TypeError(`Illegal invocation: ${name}`);
        };
        Object.defineProperty(method, "name", { value: name });
        ours.add(method);
        descriptor.writable = true;
        descriptor.value = method;
      } else {
        const get = function () {
          // parentNode and the like are each node's own: read straight
          if (Object.prototype.hasOwnProperty.call(this, name)) {
            const own = Object.getOwnPropertyDescriptor(this, name);
            return own.get ? own.get.call(this) : own.value;
          }
          const found = real(this);
          if (!found) return name === "parentNode" ? null : undefined;
          if (!found.get) return found.value;
          return run(this, found.get, () => found.get.call(this));
        };
        const set = function (value) {
          const found = real(this);
          if (found && found.set) return run(this, found.get || found.set, () => found.set.call(this, value));
          if (!found || !found.get) Object.defineProperty(this, name, { value, writable: true, configurable: true, enumerable: true });
        };
        ours.add(get); ours.add(set);
        descriptor.get = get; descriptor.set = set;
      }
      try { Object.defineProperty(proto, name, descriptor); } catch (_) {}
    }
  }
}

function patchSelectors(g) {
  const protos = [g.Element?.prototype, g.Document?.prototype, Object.getPrototypeOf(document_),
                  g.DocumentFragment?.prototype].filter(Boolean);
  for (const proto of protos) {
    for (const name of ["querySelector", "querySelectorAll", "matches", "closest", "webkitMatchesSelector"]) {
      let owner = proto;
      while (owner && !Object.prototype.hasOwnProperty.call(owner, name)) owner = Object.getPrototypeOf(owner);
      if (!owner || typeof owner[name] !== "function" || owner[name].__forgiving) continue;
      const wrapped = forgiving(owner[name]);
      wrapped.__forgiving = true;
      try { Object.defineProperty(owner, name, { value: wrapped, configurable: true, writable: true }); } catch (_) {}
    }
  }
}
function installHandlers(g, made) {
  const targets = [g.HTMLElement?.prototype, g.Element?.prototype, made.window?.Document?.prototype,
                   Object.getPrototypeOf(document_), g.SVGElement?.prototype].filter(Boolean);
  for (const name of HANDLED) {
    const property = "on" + name.toLowerCase();
    const descriptor = {
      configurable: true, enumerable: true,
      get() {
        const own = handlers.get(this)?.[property];
        if (own !== undefined) return own;
        // an inline handler written as an attribute becomes a function
        const code = this.getAttribute?.(property);
        if (code) {
          try { const fn = new Function("event", code); setHandler(this, property, name, fn); return fn; }
          catch (e) { reportError(e); }
        }
        return null;
      },
      set(fn) { setHandler(this, property, name, typeof fn === "function" ? fn : null); },
    };
    for (const target of targets) { try { Object.defineProperty(target, property, descriptor); } catch (_) {} }
    try { Object.defineProperty(g, property, { ...descriptor, get() { return handlers.get(g)?.[property] ?? null; },
                                               set(fn) { setHandler(g, property, name, typeof fn === "function" ? fn : null); } }); } catch (_) {}
  }
}
function setHandler(target, property, type, fn) {
  let map = handlers.get(target);
  if (!map) { map = {}; handlers.set(target, map); }
  const wrappers = map.__wrappers || (map.__wrappers = {});
  if (wrappers[property]) target.removeEventListener(type, wrappers[property]);
  map[property] = fn;
  if (fn) {
    wrappers[property] = function (event) {
      const result = fn.call(this, event);
      if (result === false) event.preventDefault?.();
      return result;
    };
    target.addEventListener(type, wrappers[property]);
  } else delete wrappers[property];
}
function bindInlineHandlers(root) {
  for (const element of root.querySelectorAll("*")) {
    for (const attribute of element.attributes || []) {
      const name = attribute.name.toLowerCase();
      if (name.startsWith("on") && HANDLED.some(h => "on" + h.toLowerCase() === name)) void element[name];
    }
  }
}

// ------------------------------------------------------------------ scripts
const JS_TYPES = ["", "text/javascript", "application/javascript", "module", "text/ecmascript", "application/ecmascript"];
const ran = new WeakSet();

const sources = new Map();   // a script's address -> its text, to show the code of an error
async function sourceOf(url) {
  if (sources.has(url)) return sources.get(url);
  try {
    const response = await merlinFetch(url);
    const text = response.ok ? await response.text() : "";
    sources.set(url, text);
    return text;
  } catch (_) { return ""; }
}
async function showWhere(error) {
  // the first place in the stack that is the page's own: its code, around
  // the line and column, so an error says what it tripped on
  const stack = String(error && error.stack || "");
  const found = stack.match(/(https?:\/\/[^\s(),]+?)(?:, <anonymous>)?:(\d+):(\d+)/);
  if (!found) return;
  const [, url, line, column] = found;
  const text = await sourceOf(url);
  const lines = text.split("\n");
  const row = lines[Number(line) - 1];
  if (row === undefined) return;
  const at = Number(column) - 1;
  const excerpt = row.slice(Math.max(0, at - 160), at) + " >>>HERE>>> " + row.slice(at, at + 120);
  send({ type: "console", level: "error", text: `  near ${url.split("/").pop()}:${line}:${column}: ${excerpt}` });
}

// ------------------------------------------------------------------ modules
// A page's modules, fetched by Merlin (with the page's cookies, through the
// content blocker, as Chrome connects) and made here, each with its address
// as import.meta.url; addresses resolved as a browser does, by the page's
// import map too. Deno had fetched them itself.
const modules = new Map();          // an address -> the promise of its module
let inlineModules = 0;
function resolveSpecifier(specifier, base) {
  specifier = String(specifier);
  const map = state.importMap || {};
  const bare = !/^(\/|\.\/|\.\.\/)/.test(specifier) && !/^[a-z][a-z0-9+.-]*:/i.test(specifier);
  const lookup = (table) => {
    if (!table) return null;
    if (table[specifier]) return table[specifier];
    let best = null;
    for (const key of Object.keys(table)) {
      if (key.endsWith("/") && specifier.startsWith(key) && (!best || key.length > best.length)) best = key;
    }
    return best ? table[best] + specifier.slice(best.length) : null;
  };
  if (bare || specifier.startsWith("/") || specifier.startsWith(".")) {
    // scopes first, the nearest scope of the importing module, then imports
    const scopes = Object.keys(map.scopes || {}).filter((scope) => String(base).startsWith(scope))
      .sort((a, b) => b.length - a.length);
    for (const scope of scopes) {
      const found = lookup(map.scopes[scope]);
      if (found) return new URL(found, base).href;
    }
    const found = lookup(map.imports);
    if (found) return new URL(found, base).href;
  }
  if (bare) throw new TypeError(`Failed to resolve module specifier "${specifier}"`);
  return new URL(specifier, base).href;
}
function makeModule(source, url) {
  return new vm.SourceTextModule(source, {
    identifier: url,
    initializeImportMeta(meta) {
      meta.url = url.replace(/#inline-module-\d+$/, "");
      meta.resolve = (specifier) => resolveSpecifier(specifier, meta.url);
    },
    importModuleDynamically: (specifier) => moduleEvaluated(resolveSpecifier(specifier, url)),
  });
}
// A module, fetched and made, not yet linked: what the linker gives back.
// Node links the whole graph itself, cycles and all; waiting here for each
// import to be linked first had left modules that import each other (GitHub's,
// in a circle) each waiting on the other, never run, and GitHub's parts never
// built
const linking = new Map();
function moduleLinked(url) {
  if (linking.has(url)) return linking.get(url);
  const done = (async () => {
    const made = await moduleMade(url);
    if (made.status === "unlinked") {
      await made.link((specifier, referrer) => moduleMade(resolveSpecifier(specifier, referrer.identifier)));
    }
    // being linked as part of another module's graph: until that is done
    while (made.status === "linking") await new Promise((resolve) => setTimeout(resolve, 2));
    return made;
  })();
  linking.set(url, done);
  return done;
}
function moduleMade(url) {
  if (modules.has(url)) return modules.get(url);
  const made = (async () => {
    let source;
    if (url.startsWith("data:")) {
      const comma = url.indexOf(",");
      const head = url.slice(5, comma), body = url.slice(comma + 1);
      source = head.endsWith(";base64") ? decoder.decode(fromBase64(body)) : decodeURIComponent(body);
    } else if (/^https?:/i.test(url)) {
      const response = await merlinFetch(url, { credentials: "same-origin", headers: { "sec-fetch-dest": "script" } });
      if (!response.ok) throw new TypeError(`Failed to fetch dynamically imported module: ${url} (${response.status})`);
      source = await response.text();
      sources.set(url, source);
    } else {
      throw new TypeError(`Failed to fetch dynamically imported module: ${url}`);
    }
    return makeModule(source, url);
  })();
  modules.set(url, made);
  return made;
}
async function moduleEvaluated(url) {
  const made = await moduleLinked(url);
  if (made.status === "linked") await made.evaluate();
  else if (made.status === "evaluating" || made.status === "evaluated") await made.evaluate().catch(() => {});
  if (made.status === "errored") throw made.error;
  return made;
}
async function importModule(url) {
  return (await moduleEvaluated(url)).namespace;
}
async function inlineModule(code) {
  const url = `${state.url.split("#")[0]}#inline-module-${++inlineModules}`;
  const made = makeModule(code, url);
  await made.link((specifier, referrer) => moduleMade(resolveSpecifier(specifier,
    referrer === made ? state.url : referrer.identifier)));
  await made.evaluate();
  return made.namespace;
}
// import() in a plain script is resolved against the page's address, as in a
// browser (Hugging Face's import("/front/build/...")), and fetched as above
globalThis.__merlinImport = (specifier) => importModule(resolveSpecifier(specifier, state.url));
// Rewrites import() to __merlinImport(), and in inline modules the addresses
// of static imports, outside strings, comments, templates and regular
// expressions: a plain pattern had changed the text of a page's own strings.
function pageImports(code, rewriteStatic) {
  const out = [];
  let i = 0, last = "", word = "";
  const n = code.length;
  const templateDepth = [];
  const regexAllowed = () => !last || /[(,=:[!&|?{};+\-*%<>~^]$/.test(last) || /^(return|typeof|case|do|else|in|of|new|delete|void|throw|yield|await)$/.test(word);
  while (i < n) {
    const c = code[i], next = code[i + 1];
    if (c === "/" && next === "/") { const end = code.indexOf("\n", i); const stop = end < 0 ? n : end; out.push(code.slice(i, stop)); i = stop; continue; }
    if (c === "/" && next === "*") { const end = code.indexOf("*/", i + 2); const stop = end < 0 ? n : end + 2; out.push(code.slice(i, stop)); i = stop; continue; }
    if (c === "'" || c === '"') {
      let j = i + 1;
      while (j < n && code[j] !== c) { if (code[j] === "\\") j++; if (code[j] === "\n") break; j++; }
      const literal = code.slice(i, j + 1);
      if (rewriteStatic && (word === "from" || word === "import") && /^["'](\.{0,2}\/)/.test(literal)) {
        try { out.push(c + new URL(literal.slice(1, -1), state.url).href + c); } catch (_) { out.push(literal); }
      } else out.push(literal);
      i = j + 1; last = c; word = ""; continue;
    }
    if (c === "`" || (c === "}" && templateDepth.length && templateDepth[templateDepth.length - 1] === 0)) {
      if (c === "}") templateDepth.pop();
      let j = i + 1;
      while (j < n && code[j] !== "`") {
        if (code[j] === "\\") { j += 2; continue; }
        if (code[j] === "$" && code[j + 1] === "{") { templateDepth.push(0); j += 2; break; }
        j++;
      }
      const closed = code[j] === "`";
      out.push(code.slice(i, closed ? j + 1 : j)); i = closed ? j + 1 : j; last = "`"; word = ""; continue;
    }
    if (c === "{" && templateDepth.length) templateDepth[templateDepth.length - 1]++;
    if (c === "}" && templateDepth.length) templateDepth[templateDepth.length - 1]--;
    if (c === "/" && regexAllowed()) {
      let j = i + 1, inClass = false;
      while (j < n && (code[j] !== "/" || inClass)) {
        if (code[j] === "\\") j++;
        else if (code[j] === "[") inClass = true;
        else if (code[j] === "]") inClass = false;
        else if (code[j] === "\n") break;
        j++;
      }
      j++;
      while (j < n && /[a-z]/i.test(code[j])) j++;
      out.push(code.slice(i, j)); i = j; last = "/"; word = ""; continue;
    }
    if (/[A-Za-z_$]/.test(c)) {
      let j = i + 1;
      while (j < n && /[\w$]/.test(code[j])) j++;
      const name = code.slice(i, j);
      const before = out.length ? out[out.length - 1].slice(-1) : "";
      if (name === "import" && before !== "." && /^\s*\(/.test(code.slice(j, j + 40))) out.push("__merlinImport");
      else out.push(name);
      word = name; last = name.slice(-1); i = j; continue;
    }
    out.push(c);
    if (!/\s/.test(c)) { last = c; word = ""; }
    i++;
  }
  return out.join("");
}

async function classic(script) {
  const src = script.getAttribute("src");
  let code = script.textContent;
  if (src) {
    try {
      const response = await merlinFetch(src, { credentials: "same-origin", headers: { "sec-fetch-dest": "script" } });
      if (!response.ok) { send({ type: "console", level: "warn", text: `script ${src}: ${response.status}` }); return; }
      code = await response.text();
      sources.set(new URL(src, state.url).href, code);
    } catch (e) { send({ type: "console", level: "warn", text: `script ${src}: ${e.message}` }); return; }
  }
  try { document_.currentScript = script; } catch (_) {}
  // named by its whole address, so an error's place can be found in it
  // a real script, as in a browser: its top-level const, let and class are
  // seen by the scripts and modules after it, as eval's were not
  const name = src ? new URL(src, state.url).href : `${state.url}#inline-script`;
  try {
    new vm.Script(pageImports(code, false), { filename: name,
      importModuleDynamically: (specifier) => moduleEvaluated(resolveSpecifier(specifier, state.url)) }).runInThisContext();
  }
  catch (e) { reportError(e); }
  try { document_.currentScript = null; } catch (_) {}
  fire(script, "load");
}

// A module that never arrives is not waited for: one had held up every script
// after it, and the page was never sent back changed at all.
function withinTime(promise, seconds, what) {
  return Promise.race([promise, new Promise((_, fail) =>
    setTimeout(() => fail(new Error(`${what} took over ${seconds}s`)), seconds * 1000))]);
}

async function moduleScript(script) {
  const src = script.getAttribute("src");
  try {
    if (src) await withinTime(importModule(new URL(src, state.url).href), 20, src);
    else await withinTime(inlineModule(script.textContent), 20, "inline module");
    fire(script, "load");
  } catch (e) {
    send({ type: "console", level: "warn", text: `module ${src || "(inline)"}: ${e.message}` });
    showWhere(e).catch(() => {});
    fire(script, "error");
  }
}

function fire(target, type) {
  try { target.dispatchEvent(new globalThis.Event(type)); } catch (_) {}
}

function runnable(script) {
  const type = (script.getAttribute("type") || "").toLowerCase().trim();
  return JS_TYPES.includes(type) && !script.hasAttribute("nomodule") || type === "module";
}

async function runScripts() {
  bindInlineHandlers(document_);
  // As HTML orders them: scripts as the page is read; then, the page read,
  // defer scripts and modules together in their order; then async ones.
  // Square's sites put their main scripts under defer, after the inline ones
  // that set them up: run in plain order, they found nothing set up, and the
  // site was never built.
  const scripts = [...document_.querySelectorAll("script")].filter(runnable);
  const later = [], whenever = [];
  for (const script of scripts) {
    ran.add(script);
    const isModule = (script.getAttribute("type") || "").toLowerCase() === "module";
    const external = script.hasAttribute("src");
    if (script.hasAttribute("async") && (external || isModule)) whenever.push(script);
    else if (isModule || (external && script.hasAttribute("defer"))) later.push(script);
    else await classic(script);
  }
  __setReadyState("interactive");
  changed();                     // what the parser-time scripts made, without waiting for the rest
  for (const script of later) {
    if ((script.getAttribute("type") || "").toLowerCase() === "module") await moduleScript(script);
    else await classic(script);
  }
  for (const script of whenever) {
    if ((script.getAttribute("type") || "").toLowerCase() === "module") await moduleScript(script);
    else await classic(script);
  }
  fire(document_, "DOMContentLoaded");
  await new Promise(r => setTimeout(r, 0));
  __setReadyState("complete");
  state.pageLoad = true;
  fire(globalThis, "load");
  state.pageLoad = false;
  state.loaded = true;
  send({ type: "loaded" });          // messages for this window, held till now, can come
}

// A stylesheet or preload <link>, or an image, a page adds: its load
// fires once it is there, as in a browser (Merlin fetches what it draws).
// Webpack loads a route's CSS with a <link> and waits for that load: none
// came, and Square's router waited for ever on its first page, which was
// never shown.
const LOADING_LINKS = /(^|\s)(stylesheet|preload|prefetch|modulepreload|icon|preconnect|dns-prefetch)(\s|$)/i;
const loadsFired = new WeakSet();
function arrived(node) {
  if (!node || loadsFired.has(node)) return;
  const tag = node.tagName;
  if (tag === "LINK" && !LOADING_LINKS.test(node.getAttribute("rel") || "")) return;
  if (tag === "LINK" && !node.getAttribute("href")) return;
  if (tag === "IMG" && !node.getAttribute("src")) return;
  loadsFired.add(node);
  if (tag === "IMG") {
    // an image waits for Merlin's answer: load or error, with its size
    const src = node.getAttribute("src");
    if (!imageWaiters.has(src)) imageWaiters.set(src, new Set());
    imageWaiters.get(src).add(node);
    send({ type: "want_image", src });
    return;
  }
  setTimeout(() => fire(node, "load"), 10);
}
const imageWaiters = new Map();     // an image's src -> the images waiting on it
function imageAnswered(message) {
  const waiting = imageWaiters.get(message.src) || new Set();
  imageWaiters.delete(message.src);
  for (const img of document_.querySelectorAll("img")) {
    const src = img.getAttribute("src");
    if (src === message.src || (src && new URL(src, state.url).href === message.url)) waiting.add(img);
  }
  for (const img of waiting) {
    for (const [name, value] of [["naturalWidth", message.width], ["naturalHeight", message.height],
                                 ["complete", true], ["width", message.width], ["height", message.height]]) {
      if ((name === "width" || name === "height") && img.hasAttribute(name)) continue;
      try { Object.defineProperty(img, name, { value, configurable: true, writable: true }); } catch (_) {}
    }
    fire(img, message.ok ? "load" : "error");
  }
}

// scripts a page adds later run as they arrive, as in a browser
function watchForScripts() {
  new globalThis.MutationObserver((records) => {
    for (const record of records) for (const node of record.addedNodes || []) {
      if (node.tagName === "LINK" || node.tagName === "IMG") arrived(node);
      for (const inner of node.querySelectorAll ? node.querySelectorAll("link, img") : []) arrived(inner);
      const scripts = node.tagName === "SCRIPT" ? [node] : (node.querySelectorAll ? [...node.querySelectorAll("script")] : []);
      for (const script of scripts) {
        if (ran.has(script) || !runnable(script)) continue;
        ran.add(script);
        ((script.getAttribute("type") || "").toLowerCase() === "module" ? moduleScript : classic)(script);
      }
    }
  }).observe(document_, { childList: true, subtree: true });
}



// <select> and <option> as a browser has them: linkedom's select had a value
// it could not be given, no selectedIndex, and remove(i) removed the whole
// select (Ponder empties its background list with sel.remove(1) in a loop,
// which then never ended); an option without a value attribute had none,
// where a browser's is its text.
function installSelects() {
  const selectProto = Object.getPrototypeOf(document_.createElement("select"));
  const optionProto = Object.getPrototypeOf(document_.createElement("option"));
  const define = (proto, name, descriptor) => {
    try { Object.defineProperty(proto, name, { configurable: true, ...descriptor }); } catch (_) {}
  };
  const optionsOf = (select) => [...select.querySelectorAll("option")];
  const chosen = (select) => {
    const options = optionsOf(select);
    const marked = options.filter((o) => o.hasAttribute("selected"));
    if (marked.length) return marked[marked.length - 1];
    return select.hasAttribute("multiple") ? null : (options.find((o) => !o.hasAttribute("disabled")) || null);
  };
  const choose = (select, option) => {
    for (const other of optionsOf(select)) if (other !== option && other.hasAttribute("selected")) other.removeAttribute("selected");
    if (option && !option.hasAttribute("selected")) option.setAttribute("selected", "");
  };
  define(optionProto, "value", {
    get() { return this.hasAttribute("value") ? this.getAttribute("value") : String(this.textContent || "").replace(/\s+/g, " ").trim(); },
    set(v) { this.setAttribute("value", String(v)); },
  });
  define(optionProto, "text", {
    get() { return String(this.textContent || "").replace(/\s+/g, " ").trim(); },
    set(v) { this.textContent = String(v); },
  });
  define(optionProto, "selected", {
    get() { const select = this.closest("select"); return select ? chosen(select) === this : this.hasAttribute("selected"); },
    set(v) {
      const select = this.closest("select");
      if (v) { if (select && !select.hasAttribute("multiple")) choose(select, this); else this.setAttribute("selected", ""); }
      else this.removeAttribute("selected");
    },
  });
  define(optionProto, "index", { get() { const select = this.closest("select"); return select ? optionsOf(select).indexOf(this) : 0; } });
  define(selectProto, "options", { get() {
    const select = this, list = optionsOf(select);
    return Object.assign(list, {
      item(i) { return list[i] ?? null; }, namedItem(n) { return list.find((o) => o.id === n || o.getAttribute("name") === n) ?? null; },
      add(option, before) { select.add(option, before); }, remove(i) { select.remove(i); },
      get selectedIndex() { return select.selectedIndex; }, set selectedIndex(i) { select.selectedIndex = i; },
    });
  } });
  define(selectProto, "length", { get() { return optionsOf(this).length; } });
  define(selectProto, "value", {
    get() { const option = chosen(this); return option ? option.value : ""; },
    set(v) { choose(this, optionsOf(this).find((o) => o.value === String(v)) || null); },
  });
  define(selectProto, "selectedIndex", {
    get() { const option = chosen(this); return option ? optionsOf(this).indexOf(option) : -1; },
    set(i) { choose(this, optionsOf(this)[Number(i)] || null); },
  });
  define(selectProto, "selectedOptions", { get() { return optionsOf(this).filter((o) => o.selected); } });
  define(selectProto, "type", { get() { return this.hasAttribute("multiple") ? "select-multiple" : "select-one"; } });
  const removeNode = Object.getPrototypeOf(selectProto).remove || selectProto.remove;
  define(selectProto, "remove", { writable: true, value: function (index) {
    // remove() is the element's own; remove(i), its i-th option's
    if (arguments.length === 0) return removeNode.call(this);
    const option = optionsOf(this)[Number(index)];
    if (option) option.parentNode.removeChild(option);
  } });
  define(selectProto, "add", { writable: true, value: function (option, before) {
    const ref = typeof before === "number" ? optionsOf(this)[before] : before;
    if (ref && ref.parentNode) ref.parentNode.insertBefore(option, ref); else this.appendChild(option);
  } });
  define(selectProto, "item", { writable: true, value: function (i) { return optionsOf(this)[i] ?? null; } });
}


// What a browser has, and Deno had, that Node lacks: FileReader (Ponder reads
// a chosen picture with it), and the events ErrorEvent, ProgressEvent and
// PromiseRejectionEvent.
// ------------------------------------------------------------------ <video> and <audio>
// HTMLMediaElement: the page's videos are played by Merlin (mediaview.py), and
// told here what they do. play() and the rest are messages to Merlin; their
// state, and the events (play, timeupdate, ended...), come back in
// media_event. Merlin has no Media Source Extensions yet: there is no
// MediaSource, so players (hls.js, YouTube's) that look for one fall back to
// giving the video a plain address, an HLS playlist too, which Merlin plays.
const mediaState = new WeakMap();
const mediaPlays = new WeakMap();
function mediaOf(el) {
  let found = mediaState.get(el);
  if (!found) {
    found = { paused: true, ended: false, currentTime: 0, duration: NaN, volume: 1, muted: el.hasAttribute("muted"),
              videoWidth: 0, videoHeight: 0, readyState: 0, currentSrc: "", error: null, playbackRate: 1, seeking: false };
    mediaState.set(el, found);
  }
  return found;
}
function mediaCanPlay(kind) {
  kind = String(kind || "").trim().toLowerCase();
  if (!kind) return "";
  const base = kind.split(";")[0].trim();
  const good = ["video/mp4", "video/webm", "video/ogg", "audio/mp4", "audio/mpeg", "audio/mp3", "audio/webm",
    "audio/ogg", "audio/wav", "audio/wave", "audio/x-wav", "audio/flac", "audio/aac", "video/quicktime",
    "application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl", "video/x-m4v", "audio/x-m4a", "audio/opus"];
  if (!good.includes(base)) return "";
  const at = kind.indexOf("codecs");
  if (at < 0) return "maybe";
  const known = ["avc1", "avc3", "mp4a", "vp8", "vp9", "vp09", "opus", "vorbis", "theora", "flac", "mp3", "hev1", "hvc1", "av01"];
  const names = kind.slice(at + 6).split(",").map((c) => c.replace(/[=\s"']/g, "")).filter(Boolean);
  return names.length && names.every((n) => known.some((k) => n.startsWith(k))) ? "probably" : "";
}
function mediaTell(el, action, value) {
  const src = el.getAttribute("src") || (el.querySelector && el.querySelector("source[src]")?.getAttribute("src")) || "";
  let absolute = src;
  try { absolute = src ? new URL(src, state.url).href : ""; } catch (_) {}
  send({ type: "media", target: idOf(el), action, value: value === undefined ? null : value,
         tag: el.tagName.toLowerCase(), src: absolute });
}
function mediaAnswered(message) {
  const el = nodeOf(message.target);
  if (!el) return;
  const now = mediaOf(el);
  const given = message.state || {};
  for (const key of ["paused", "ended", "currentTime", "volume", "muted", "videoWidth", "videoHeight", "readyState",
                     "currentSrc", "playbackRate"]) {
    if (key in given && given[key] !== undefined && given[key] !== null) now[key] = given[key];
  }
  now.duration = given.duration === null || given.duration === undefined ? now.duration : given.duration;
  if (given.error) now.error = Object.assign(new (globalThis.MediaError || Object)(), { code: 4, message: String(given.error) });
  const name = message.event;
  if (name === "statechange") return;
  const waiting = mediaPlays.get(el) || [];
  if (name === "playing" || name === "play") {
    mediaPlays.set(el, []);
    for (const [resolve] of waiting) resolve();
  } else if (name === "error") {
    mediaPlays.set(el, []);
    for (const [, reject] of waiting) reject(new DOMException("The element has no supported sources.", "NotSupportedError"));
  } else if (name === "pause" && !now.ended) {
    mediaPlays.set(el, []);
    for (const [, reject] of waiting) reject(new DOMException("The play() request was interrupted by a call to pause().", "AbortError"));
  }
  try { el.dispatchEvent(new globalThis.Event(name)); } catch (e) { reportError(e); }
}
// <dialog>'s show(), showModal() and close(), the popover API, and the two
// DOM interfaces linkedom lacks that browsers have always had. Google's
// buttons open their menus and panels as dialogs (showModal was not a
// function, and nothing opened); YouTube's web components polyfill reads
// ProcessingInstruction and CDATASection and stopped where they were missing.
// A modal dialog is marked data-merlin-modal for Merlin's styles (:modal), an
// open popover data-merlin-popover-open (:popover-open).
// Window, the window's own interface: window instanceof Window, and
// Window.prototype with the window's event methods, as browsers have it.
// YouTube's web components polyfill patches Window.prototype and stopped,
// with "Window is not defined", before its app could start.
function installWindowClass(g) {
  if (typeof g.Window === "function") return;
  const proto = Object.getPrototypeOf(g);
  if (!proto || proto === Object.prototype) return;
  const Window = function Window() { throw new TypeError("Illegal constructor"); };
  Window.prototype = proto;
  const hidden = (value) => ({ value, configurable: true, writable: true, enumerable: false });
  try {
    Object.defineProperty(proto, "constructor", hidden(Window));
    Object.defineProperty(proto, Symbol.toStringTag, { value: "Window", configurable: true });
    for (const name of ["addEventListener", "removeEventListener", "dispatchEvent"]) {
      const own = g[name];
      if (typeof own !== "function") continue;
      Object.defineProperty(proto, name, hidden(function (...args) { return own.apply(g, args); }));
    }
  } catch (_) {}
  g.Window = Window;
}

// The CSS object model: CSSStyleSheet (constructable too, with replace() and
// replaceSync(), and adoptedStyleSheets), its cssRules parsed from the sheet's
// text as CSSStyleRule, CSSMediaRule, CSSKeyframesRule and the rest, each with
// its style. Google's search box asked whether a rule was a CSSStyleRule, and
// with no such class its buttons (AI Mode's among them) were never set up;
// Lit's components make their styles as constructed sheets.
function installCSSOM(g) {
  const stripComments = (text) => String(text || "").replace(/\/\*[\s\S]*?\*\//g, "");
  // the end of a block or prelude, past strings and brackets
  const scan = (text, i, stops) => {
    let depth = 0, quote = "";
    for (; i < text.length; i++) {
      const c = text[i];
      if (quote) { if (c === "\\") i++; else if (c === quote) quote = ""; continue; }
      if (c === '"' || c === "'") { quote = c; continue; }
      if (c === "(" || c === "[") depth++;
      else if (c === ")" || c === "]") depth--;
      else if (depth <= 0 && stops.includes(c)) return i;
    }
    return text.length;
  };
  const blockEnd = (text, open) => {
    let depth = 0, quote = "";
    for (let i = open; i < text.length; i++) {
      const c = text[i];
      if (quote) { if (c === "\\") i++; else if (c === quote) quote = ""; continue; }
      if (c === '"' || c === "'") { quote = c; continue; }
      if (c === "{") depth++;
      else if (c === "}" && --depth === 0) return i;
    }
    return text.length;
  };
  const camel = (name) => name.replace(/-([a-z])/g, (_m, c) => c.toUpperCase());
  const dashed = (name) => name.startsWith("--") ? name : name.replace(/[A-Z]/g, (c) => "-" + c.toLowerCase()).replace(/^(webkit|moz|ms)-/, "-$1-");

  class CSSStyleDeclaration {
    constructor(text = "", parentRule = null, onChange = null) {
      Object.defineProperty(this, "_map", { value: new Map(), writable: true });
      Object.defineProperty(this, "_change", { value: onChange, writable: true });
      Object.defineProperty(this, "parentRule", { value: parentRule, writable: true });
      this._read(text);
      return new Proxy(this, {
        get(target, key, receiver) {
          if (typeof key === "string" && !(key in target)) {
            if (/^\d+$/.test(key)) return [...target._map.keys()][Number(key)];
            if (/^[a-zA-Z]/.test(key) || key.startsWith("--")) return target.getPropertyValue(dashed(key));
          }
          return Reflect.get(target, key, receiver);
        },
        set(target, key, value, receiver) {
          if (typeof key === "string" && !(key in target) && (/^[a-zA-Z]/.test(key) || key.startsWith("--"))) {
            target.setProperty(dashed(key), value); return true;
          }
          return Reflect.set(target, key, value, receiver);
        },
      });
    }
    _read(text) {
      this._map = new Map();
      const body = stripComments(text);
      let i = 0;
      while (i < body.length) {
        const end = scan(body, i, ";");
        const part = body.slice(i, end); i = end + 1;
        const colon = part.indexOf(":");
        if (colon < 0) continue;
        const name = part.slice(0, colon).trim();
        let value = part.slice(colon + 1).trim(), priority = "";
        const important = value.match(/!\s*important\s*$/i);
        if (important) { value = value.slice(0, important.index).trim(); priority = "important"; }
        if (name) this._map.set(name.startsWith("--") ? name : name.toLowerCase(), { value, priority });
      }
    }
    get length() { return this._map.size; }
    item(i) { return [...this._map.keys()][i] ?? ""; }
    getPropertyValue(name) { return this._map.get(String(name).startsWith("--") ? String(name) : String(name).toLowerCase())?.value ?? ""; }
    getPropertyPriority(name) { return this._map.get(String(name).toLowerCase())?.priority ?? ""; }
    setProperty(name, value, priority = "") {
      name = String(name); if (!name.startsWith("--")) name = name.toLowerCase();
      if (value === null || value === undefined || String(value) === "") { this.removeProperty(name); return; }
      this._map.set(name, { value: String(value), priority: priority ? "important" : "" });
      if (this._change) this._change();
    }
    removeProperty(name) {
      const old = this.getPropertyValue(name);
      this._map.delete(String(name).startsWith("--") ? String(name) : String(name).toLowerCase());
      if (this._change) this._change();
      return old;
    }
    get cssText() { return [...this._map].map(([k, v]) => `${k}: ${v.value}${v.priority ? " !important" : ""};`).join(" "); }
    set cssText(text) { this._read(text); if (this._change) this._change(); }
    [Symbol.iterator]() { return this._map.keys(); }
  }
  for (const name of ["cssFloat"]) Object.defineProperty(CSSStyleDeclaration.prototype, name, {
    get() { return this.getPropertyValue("float"); }, set(v) { this.setProperty("float", v); }, configurable: true });

  class CSSRuleList extends Array {
    item(i) { return this[i] ?? null; }
    static get [Symbol.species]() { return Array; }
  }
  class StyleSheetList extends Array {
    item(i) { return this[i] ?? null; }
    static get [Symbol.species]() { return Array; }
  }
  class MediaList extends Array {
    constructor(text = "") { super(); this.mediaText = text; }
    get mediaText() { return this.join(", "); }
    set mediaText(text) { this.length = 0; for (const m of String(text || "").split(",").map((x) => x.trim()).filter(Boolean)) this.push(m); }
    item(i) { return this[i] ?? null; }
    appendMedium(m) { if (!this.includes(m)) this.push(m); }
    deleteMedium(m) { const i = this.indexOf(m); if (i >= 0) this.splice(i, 1); }
    toString() { return this.mediaText; }
    static get [Symbol.species]() { return Array; }
  }
  class CSSRule {
    constructor(text = "", parentStyleSheet = null, parentRule = null) {
      this._text = text; this.parentStyleSheet = parentStyleSheet; this.parentRule = parentRule;
    }
    get cssText() { return this._text; }
    set cssText(_v) {}
    get type() { return 0; }
  }
  const types = { STYLE_RULE: 1, CHARSET_RULE: 2, IMPORT_RULE: 3, MEDIA_RULE: 4, FONT_FACE_RULE: 5, PAGE_RULE: 6,
                  KEYFRAMES_RULE: 7, KEYFRAME_RULE: 8, NAMESPACE_RULE: 10, COUNTER_STYLE_RULE: 11, SUPPORTS_RULE: 12,
                  FONT_FEATURE_VALUES_RULE: 14 };
  for (const [k, v] of Object.entries(types)) { CSSRule[k] = v; CSSRule.prototype[k] = v; }
  class CSSStyleRule extends CSSRule {
    constructor(selector, body, sheet, parent) {
      super("", sheet, parent);
      this._selector = selector.trim();
      this.style = new CSSStyleDeclaration(body, this, () => sheet && sheet._ruleChanged && sheet._ruleChanged());
      this.cssRules = CSSRuleList.from([]);
    }
    get type() { return 1; }
    get selectorText() { return this._selector; }
    set selectorText(v) { this._selector = String(v); }
    get cssText() { return `${this._selector} { ${this.style.cssText} }`; }
  }
  class CSSGroupingRule extends CSSRule {
    constructor(text, inner, sheet, parent) {
      super(text, sheet, parent);
      this.cssRules = parseRules(inner, sheet, this);
    }
    insertRule(text, index = 0) { const made = parseRules(String(text), this.parentStyleSheet, this); this.cssRules.splice(index, 0, ...made); return index; }
    deleteRule(index) { this.cssRules.splice(index, 1); }
    get cssText() { return this._prelude + " { " + this.cssRules.map((r) => r.cssText).join(" ") + " }"; }
  }
  class CSSConditionRule extends CSSGroupingRule {
    get conditionText() { return this._condition; }
  }
  class CSSMediaRule extends CSSConditionRule {
    constructor(condition, inner, sheet, parent) {
      super("", inner, sheet, parent);
      this._condition = condition; this._prelude = "@media " + condition; this.media = new MediaList(condition);
    }
    get type() { return 4; }
  }
  class CSSSupportsRule extends CSSConditionRule {
    constructor(condition, inner, sheet, parent) { super("", inner, sheet, parent); this._condition = condition; this._prelude = "@supports " + condition; }
    get type() { return 12; }
  }
  class CSSContainerRule extends CSSConditionRule {
    constructor(condition, inner, sheet, parent) { super("", inner, sheet, parent); this._condition = condition; this._prelude = "@container " + condition; }
    get containerName() { return (this._condition.match(/^\s*([a-z][\w-]*)\s+[(]/i) || [])[1] || ""; }
  }
  class CSSLayerBlockRule extends CSSGroupingRule {
    constructor(name, inner, sheet, parent) { super("", inner, sheet, parent); this.name = name; this._prelude = "@layer " + name; }
  }
  class CSSLayerStatementRule extends CSSRule {
    constructor(text, names, sheet, parent) { super(text, sheet, parent); this.nameList = names; }
  }
  class CSSFontFaceRule extends CSSRule {
    constructor(body, sheet, parent) { super("", sheet, parent); this.style = new CSSStyleDeclaration(body, this); }
    get type() { return 5; }
    get cssText() { return `@font-face { ${this.style.cssText} }`; }
  }
  class CSSPageRule extends CSSRule {
    constructor(selector, body, sheet, parent) { super("", sheet, parent); this.selectorText = selector; this.style = new CSSStyleDeclaration(body, this); }
    get type() { return 6; }
    get cssText() { return `@page ${this.selectorText} { ${this.style.cssText} }`.replace("@page  {", "@page {"); }
  }
  class CSSKeyframeRule extends CSSRule {
    constructor(key, body, sheet, parent) { super("", sheet, parent); this.keyText = key; this.style = new CSSStyleDeclaration(body, this); }
    get type() { return 8; }
    get cssText() { return `${this.keyText} { ${this.style.cssText} }`; }
  }
  class CSSKeyframesRule extends CSSRule {
    constructor(name, inner, sheet, parent) {
      super("", sheet, parent);
      this.name = name;
      this.cssRules = CSSRuleList.from(parseBlocks(inner).map(([key, body]) => new CSSKeyframeRule(key, body, sheet, this)));
    }
    get type() { return 7; }
    findRule(key) { return this.cssRules.find((r) => r.keyText === key) || null; }
    appendRule(text) { const [[key, body] = []] = parseBlocks(text); if (key) this.cssRules.push(new CSSKeyframeRule(key, body, this.parentStyleSheet, this)); }
    deleteRule(key) { const i = this.cssRules.findIndex((r) => r.keyText === key); if (i >= 0) this.cssRules.splice(i, 1); }
    get cssText() { return `@keyframes ${this.name} { ${this.cssRules.map((r) => r.cssText).join(" ")} }`; }
  }
  class CSSImportRule extends CSSRule {
    constructor(text, sheet, parent) {
      super(text, sheet, parent);
      const found = text.match(/@import\s+(?:url\(\s*)?["']?([^"')\s]+)["']?\s*\)?\s*([^;]*)/i);
      this.href = found ? found[1] : ""; this.media = new MediaList(found ? found[2].trim() : ""); this.styleSheet = null;
    }
    get type() { return 3; }
  }
  class CSSNamespaceRule extends CSSRule {
    constructor(text, sheet, parent) {
      super(text, sheet, parent);
      const found = text.match(/@namespace\s+(?:([\w-]+)\s+)?(?:url\()?["']?([^"')]+)/i);
      this.prefix = found && found[1] || ""; this.namespaceURI = found ? found[2] : "";
    }
    get type() { return 10; }
  }
  // [prelude, body] for each block in text
  const parseBlocks = (text) => {
    const out = []; text = stripComments(text);
    let i = 0;
    while (i < text.length) {
      const open = scan(text, i, "{");
      if (open >= text.length) break;
      const close = blockEnd(text, open);
      out.push([text.slice(i, open).trim(), text.slice(open + 1, close)]);
      i = close + 1;
    }
    return out;
  };
  const parseRules = (text, sheet, parent) => {
    const rules = CSSRuleList.from([]);
    text = stripComments(text);
    let i = 0;
    while (i < text.length) {
      while (i < text.length && /\s/.test(text[i])) i++;
      if (i >= text.length) break;
      const stop = scan(text, i, "{;}");
      if (stop >= text.length || text[stop] === "}") { i = stop + 1; continue; }
      const prelude = text.slice(i, stop).trim();
      if (text[stop] === ";") {
        const at = prelude.match(/^@([\w-]+)/);
        if (at) {
          const name = at[1].toLowerCase();
          if (name === "import") rules.push(new CSSImportRule(prelude + ";", sheet, parent));
          else if (name === "namespace") rules.push(new CSSNamespaceRule(prelude + ";", sheet, parent));
          else if (name === "layer") rules.push(new CSSLayerStatementRule(prelude + ";", prelude.slice(6).split(",").map((x) => x.trim()), sheet, parent));
        }
        i = stop + 1;
        continue;
      }
      const close = blockEnd(text, stop);
      const body = text.slice(stop + 1, close);
      i = close + 1;
      const at = prelude.match(/^@(-[a-z]+-)?([\w-]+)\s*([\s\S]*)$/i);
      try {
        if (!at) rules.push(new CSSStyleRule(prelude, body, sheet, parent));
        else {
          const name = at[2].toLowerCase(), rest = at[3].trim();
          if (name === "media") rules.push(new CSSMediaRule(rest, body, sheet, parent));
          else if (name === "supports") rules.push(new CSSSupportsRule(rest, body, sheet, parent));
          else if (name === "container") rules.push(new CSSContainerRule(rest, body, sheet, parent));
          else if (name === "layer") rules.push(new CSSLayerBlockRule(rest, body, sheet, parent));
          else if (name === "font-face") rules.push(new CSSFontFaceRule(body, sheet, parent));
          else if (name === "page") rules.push(new CSSPageRule(rest, body, sheet, parent));
          else if (name === "keyframes") rules.push(new CSSKeyframesRule(rest, body, sheet, parent));
          else { const r = new CSSRule(`${prelude} {${body}}`, sheet, parent); rules.push(r); }
        }
      } catch (_) {}
    }
    return rules;
  };
  class StyleSheet {}
  // a <style>'s or <link>'s sheet (owner given), or one a script makes
  class CSSStyleSheet extends StyleSheet {
    constructor(options = {}, owner = null) {
      super();
      Object.defineProperty(this, "_owner", { value: owner, writable: true });
      Object.defineProperty(this, "_text", { value: "", writable: true });
      Object.defineProperty(this, "_rules", { value: null, writable: true });
      Object.defineProperty(this, "_rulesFor", { value: null, writable: true });
      this.disabled = !!options.disabled;
      this.media = new MediaList(typeof options.media === "string" ? options.media : (options.media?.mediaText || ""));
      this.title = owner ? owner.getAttribute("title") : null;
      this.type = "text/css";
      this.parentStyleSheet = null;
      this.ownerRule = null;
    }
    get ownerNode() { return this._owner; }
    get href() {
      const node = this._owner;
      return node && node.tagName === "LINK" ? new URL(node.getAttribute("href") || "", state.url).href : null;
    }
    _source() {
      const node = this._owner;
      if (!node) return this._text;
      return node.tagName === "STYLE" ? (node.textContent || "") : this._text;
    }
    get cssRules() {
      const text = this._source();
      if (this._rules === null || this._rulesFor !== text) { this._rules = parseRules(text, this, null); this._rulesFor = text; }
      return this._rules;
    }
    get rules() { return this.cssRules; }
    _ruleChanged() {
      // a rule's style changed by a script: the sheet's text follows, so the
      // page is styled with it
      const text = this.cssRules.map((r) => r.cssText).join("\n");
      this._rulesFor = text;
      if (this._owner && this._owner.tagName === "STYLE") this._owner.textContent = text; else this._text = text;
      adopted.refresh();
    }
    insertRule(text, index = 0) {
      const rules = this.cssRules;
      if (index < 0 || index > rules.length) throw new DOMException("Failed to execute 'insertRule' on 'CSSStyleSheet': The index provided is outside the bounds.", "IndexSizeError");
      const made = parseRules(String(text), this, null);
      if (!made.length) throw new DOMException(`Failed to execute 'insertRule' on 'CSSStyleSheet': Failed to parse the rule '${text}'.`, "SyntaxError");
      if (this._owner && this._owner.tagName === "STYLE" && index >= rules.length) {
        // added at the end of a <style>: its text, so Merlin Engine styles the page with it
        this._owner.textContent = (this._owner.textContent || "") + "\n" + String(text);
      } else {
        rules.splice(index, 0, ...made);
        const joined = rules.map((r) => r.cssText).join("\n");
        if (this._owner && this._owner.tagName === "STYLE") this._owner.textContent = joined; else this._text = joined;
        this._rulesFor = joined;
      }
      adopted.refresh();
      return index;
    }
    deleteRule(index) {
      const rules = this.cssRules;
      if (index < 0 || index >= rules.length) throw new DOMException("Failed to execute 'deleteRule' on 'CSSStyleSheet': The index provided is outside the bounds.", "IndexSizeError");
      rules.splice(index, 1);
      const joined = rules.map((r) => r.cssText).join("\n");
      if (this._owner && this._owner.tagName === "STYLE") this._owner.textContent = joined; else this._text = joined;
      this._rulesFor = joined;
      adopted.refresh();
    }
    addRule(selector, body, index) { return this.insertRule(`${selector} { ${body} }`, index ?? this.cssRules.length), -1; }
    removeRule(index) { this.deleteRule(index ?? 0); }
    replaceSync(text) {
      if (this._owner) throw new DOMException("Failed to execute 'replaceSync' on 'CSSStyleSheet': Can't call replaceSync on non-constructed CSSStyleSheets.", "NotAllowedError");
      this._text = String(text).replace(/@import[^;]*;/gi, ""); this._rules = null;
      adopted.refresh();
    }
    replace(text) {
      try { this.replaceSync(text); return Promise.resolve(this); } catch (e) { return Promise.reject(e); }
    }
  }
  StyleSheetList.of = (items) => StyleSheetList.from(items);
  // document.adoptedStyleSheets: their text kept in a <style> of Merlin's in
  // the head, so the page is drawn with them. A shadow root's are kept too.
  const adopted = {
    list: [],
    element: null,
    refresh() {
      if (!this.list.length && !this.element) return;
      try {
        if (!this.element) {
          this.element = document_.createElement("style");
          this.element.setAttribute("data-merlin-adopted", "");
        }
        const text = this.list.map((sheet) => sheet._source()).join("\n");
        if (this.element.textContent !== text) this.element.textContent = text;
        if (!this.element.parentNode && document_.head) document_.head.appendChild(this.element);
      } catch (_) {}
    },
  };
  const adoptable = (value) => {
    const list = Array.from(value || []);
    for (const sheet of list) if (!(sheet instanceof CSSStyleSheet)) throw new TypeError("Failed to set the 'adoptedStyleSheets' property: Failed to convert value to 'CSSStyleSheet'.");
    return list;
  };
  try {
    Object.defineProperty(document_, "adoptedStyleSheets", { configurable: true,
      get() { return adopted.list; }, set(value) { adopted.list = adoptable(value); adopted.refresh(); } });
  } catch (_) {}
  const shadowProto = g.ShadowRoot && g.ShadowRoot.prototype;
  if (shadowProto && !Object.getOwnPropertyDescriptor(shadowProto, "adoptedStyleSheets")) {
    const own = new WeakMap();
    try {
      Object.defineProperty(shadowProto, "adoptedStyleSheets", { configurable: true,
        get() { return own.get(this) || []; }, set(value) { own.set(this, adoptable(value)); } });
    } catch (_) {}
  }
  Object.assign(g, { CSSRule, CSSStyleRule, CSSGroupingRule, CSSConditionRule, CSSMediaRule, CSSSupportsRule,
    CSSContainerRule, CSSLayerBlockRule, CSSLayerStatementRule, CSSFontFaceRule, CSSPageRule, CSSKeyframeRule,
    CSSKeyframesRule, CSSImportRule, CSSNamespaceRule, CSSRuleList, StyleSheet, CSSStyleSheet, StyleSheetList,
    MediaList });
  if (typeof g.CSSStyleDeclaration !== "function" || !g.CSSStyleDeclaration.prototype.getPropertyValue) g.CSSStyleDeclaration = CSSStyleDeclaration;
}

function installDialogs(g, made) {
  const CharacterData = g.CharacterData || made.CharacterData;
  if (typeof g.ProcessingInstruction !== "function" && typeof CharacterData === "function") {
    g.ProcessingInstruction = class ProcessingInstruction extends CharacterData {};
  }
  if (typeof g.CDATASection !== "function" && typeof made.CDATASection === "function") g.CDATASection = made.CDATASection;
  if (typeof g.XMLSerializer !== "function") {
    g.XMLSerializer = class XMLSerializer {
      serializeToString(node) {
        if (node == null) throw new TypeError("Failed to execute 'serializeToString' on 'XMLSerializer': parameter 1 is not of type 'Node'.");
        if (node.nodeType === 9) return (node.doctype ? String(node.doctype) : "") + (node.documentElement ? node.documentElement.outerHTML : "");
        if (node.nodeType === 1) return node.outerHTML;
        if (node.nodeType === 11) return [...node.childNodes].map((n) => this.serializeToString(n)).join("");
        if (node.nodeType === 3) return String(node.data ?? node.textContent ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
        if (node.nodeType === 8) return `<!--${node.data ?? ""}-->`;
        return String(node.textContent ?? "");
      }
    };
  }
  const later = (fn) => setTimeout(() => { try { fn(); } catch (e) { reportError(e); } }, 0);
  const fire = (target, type, cancelable) => {
    const event = new (g.Event || NodeEvent)(type, { cancelable: !!cancelable });
    target.dispatchEvent(event);
    return !event.defaultPrevented;
  };
  const invalid = (message) => new DOMException(message, "InvalidStateError");
  const Dialog = g.HTMLDialogElement;
  if (typeof Dialog === "function") {
    const proto = Dialog.prototype;
    const props = {
      open: { get() { return this.hasAttribute("open"); },
              set(v) { if (v) this.setAttribute("open", ""); else this.removeAttribute("open"); } },
      returnValue: { get() { return this.__returnValue ?? ""; }, set(v) { this.__returnValue = String(v); } },
      closedBy: { get() { return this.getAttribute("closedby") || "auto"; }, set(v) { this.setAttribute("closedby", String(v)); } },
    };
    for (const [name, descriptor] of Object.entries(props)) {
      try { Object.defineProperty(proto, name, { configurable: true, ...descriptor }); } catch (_) {}
    }
    const methods = {
      show() {
        if (this.hasAttribute("open")) {
          if (this.hasAttribute("data-merlin-modal")) throw invalid("Failed to execute 'show' on 'HTMLDialogElement': The dialog is already open as a modal dialog, and therefore cannot be opened as a non-modal dialog.");
          return;
        }
        this.setAttribute("open", "");
      },
      showModal() {
        if (this.hasAttribute("open")) {
          if (this.hasAttribute("data-merlin-modal")) return;
          throw invalid("Failed to execute 'showModal' on 'HTMLDialogElement': The element already has an 'open' attribute, and therefore cannot be opened modally.");
        }
        if (!this.isConnected) throw invalid("Failed to execute 'showModal' on 'HTMLDialogElement': The element is not in a Document.");
        this.setAttribute("data-merlin-modal", "");
        this.setAttribute("open", "");
        modals.push(this);
        // focus goes into it, to the first thing marked autofocus, as browsers do
        try { (this.querySelector("[autofocus]") || null)?.focus?.(); } catch (_) {}
      },
      close(value) {
        if (!this.hasAttribute("open")) return;
        if (value !== undefined) this.returnValue = value;
        this.removeAttribute("open");
        this.removeAttribute("data-merlin-modal");
        const at = modals.indexOf(this);
        if (at >= 0) modals.splice(at, 1);
        later(() => fire(this, "close", false));
      },
      requestClose(value) {
        if (!this.hasAttribute("open")) return;
        if (fire(this, "cancel", true)) this.close(value);
      },
    };
    for (const [name, fn] of Object.entries(methods)) {
      try { Object.defineProperty(proto, name, { configurable: true, writable: true, value: fn }); } catch (_) {}
    }
  }
  const modals = [];
  // Esc: the topmost modal dialog is asked to close (cancel, then close), and
  // an open auto popover is hidden, as in browsers
  try {
    document_.addEventListener("keydown", (event) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      const top = modals[modals.length - 1];
      if (top) { top.requestClose(); return; }
      const shown = [...document_.querySelectorAll("[data-merlin-popover-open]")]
        .filter((el) => (el.getAttribute("popover") || "auto") !== "manual");
      for (const el of shown) el.hidePopover();
    });
  } catch (_) {}
  // <form method=dialog>: sent, it closes its dialog with the button's value
  try {
    document_.addEventListener("submit", (event) => {
      const form = event.target;
      if (!form || form.tagName !== "FORM" || (form.getAttribute("method") || "").toLowerCase() !== "dialog") return;
      event.preventDefault();
      const dialog = form.closest("dialog");
      const button = event.submitter;
      if (dialog) dialog.close(button ? (button.getAttribute("value") ?? undefined) : undefined);
    });
  } catch (_) {}
  // the popover API
  const Element = g.HTMLElement;
  if (typeof Element === "function") {
    const proto = Element.prototype;
    const toggle = (el, opening, source) => {
      const open = el.hasAttribute("data-merlin-popover-open");
      if (opening === open) return false;
      if (!el.hasAttribute("popover")) throw new DOMException("Failed to execute on 'HTMLElement': Not supported on elements that do not have a valid value for the 'popover' attribute.", "NotSupportedError");
      const ToggleEvent = g.ToggleEvent || NodeEvent;
      const before = new ToggleEvent("beforetoggle", { cancelable: opening, oldState: open ? "open" : "closed", newState: opening ? "open" : "closed" });
      el.dispatchEvent(before);
      if (opening && before.defaultPrevented) return false;
      if (opening && (el.getAttribute("popover") || "auto") !== "manual") {
        // one auto popover at a time, as browsers keep them, unless it holds this one
        for (const other of document_.querySelectorAll("[data-merlin-popover-open]")) {
          if (other !== el && !other.contains(el) && (other.getAttribute("popover") || "auto") !== "manual") other.hidePopover();
        }
      }
      if (opening) el.setAttribute("data-merlin-popover-open", ""); else el.removeAttribute("data-merlin-popover-open");
      later(() => el.dispatchEvent(new ToggleEvent("toggle", { oldState: open ? "open" : "closed", newState: opening ? "open" : "closed" })));
      return true;
    };
    const methods = {
      showPopover(options) { toggle(this, true, options && options.source); },
      hidePopover() { toggle(this, false); },
      togglePopover(force) {
        const open = this.hasAttribute("data-merlin-popover-open");
        const want = typeof force === "boolean" ? force : (force && typeof force.force === "boolean" ? force.force : !open);
        toggle(this, want);
        return this.hasAttribute("data-merlin-popover-open");
      },
    };
    for (const [name, fn] of Object.entries(methods)) {
      if (typeof proto[name] === "function") continue;
      try { Object.defineProperty(proto, name, { configurable: true, writable: true, value: fn }); } catch (_) {}
    }
    if (!("popover" in proto)) {
      try {
        Object.defineProperty(proto, "popover", { configurable: true,
          get() { const v = this.getAttribute("popover"); return v === null ? null : (v === "manual" || v === "hint" ? v : "auto"); },
          set(v) { if (v === null) this.removeAttribute("popover"); else this.setAttribute("popover", String(v)); } });
      } catch (_) {}
    }
    // <button popovertarget=...>, and commandfor/command for dialogs and popovers
    try {
      document_.addEventListener("click", (event) => {
        const button = event.target && event.target.closest && event.target.closest("button[popovertarget], button[commandfor], input[popovertarget]");
        if (!button || event.defaultPrevented) return;
        const id = button.getAttribute("popovertarget") || button.getAttribute("commandfor");
        const target = id && document_.getElementById(id);
        if (!target) return;
        const action = (button.getAttribute("popovertargetaction") || button.getAttribute("command") || "toggle").toLowerCase();
        try {
          if (action === "show-modal" && target.showModal) target.showModal();
          else if (action === "close" && target.close) target.close();
          else if (action === "request-close" && target.requestClose) target.requestClose();
          else if (action === "show" || action === "show-popover") target.showPopover();
          else if (action === "hide" || action === "hide-popover") target.hidePopover();
          else if (action === "toggle" || action === "toggle-popover") target.togglePopover();
        } catch (e) { reportError(e); }
      });
    } catch (_) {}
  }
}

function installMedia(g) {
  const ranges = (from, to) => ({ length: to > from ? 1 : 0, start: () => from, end: () => to });
  const reflected = (name) => ({ get() { return this.getAttribute(name) || ""; }, set(v) { this.setAttribute(name, String(v)); } });
  const flag = (name, action) => ({
    get() { return this.hasAttribute(name); },
    set(v) { if (v) this.setAttribute(name, ""); else this.removeAttribute(name); if (action) mediaTell(this, action, !!v); },
  });
  class MediaError { static MEDIA_ERR_ABORTED = 1; static MEDIA_ERR_NETWORK = 2; static MEDIA_ERR_DECODE = 3;
                     static MEDIA_ERR_SRC_NOT_SUPPORTED = 4; constructor() { this.code = 4; this.message = ""; } }
  g.MediaError = MediaError;
  const methods = {
    play() {
      const media = mediaOf(this);
      media.paused = false; media.ended = false;
      mediaTell(this, "play");
      return new Promise((resolve, reject) => {
        const list = mediaPlays.get(this) || [];
        list.push([resolve, reject]);
        mediaPlays.set(this, list);
      });
    },
    pause() { const media = mediaOf(this); media.paused = true; mediaTell(this, "pause"); },
    load() { const media = mediaOf(this); media.readyState = 0; mediaTell(this, "load"); },
    canPlayType(kind) { return mediaCanPlay(kind); },
    fastSeek(seconds) { this.currentTime = seconds; },
    addTextTrack() { return { mode: "disabled", cues: [], activeCues: [], addCue() {}, removeCue() {}, addEventListener() {}, removeEventListener() {} }; },
    requestFullscreen() { mediaTell(this, "fullscreen", true); return Promise.resolve(); },
    requestPictureInPicture() { return Promise.reject(new DOMException("Picture-in-picture is not available.", "NotSupportedError")); },
    setMediaKeys() { return Promise.reject(new DOMException("Protected media is not available in Merlin Engine.", "NotSupportedError")); },
    captureStream() { throw new DOMException("captureStream is not available.", "NotSupportedError"); },
    getVideoPlaybackQuality() { return { totalVideoFrames: 0, droppedVideoFrames: 0, corruptedVideoFrames: 0, creationTime: 0 }; },
  };
  const properties = {
    paused: { get() { return mediaOf(this).paused; } },
    ended: { get() { return mediaOf(this).ended; } },
    seeking: { get() { return false; } },
    currentTime: { get() { return mediaOf(this).currentTime; },
                   set(v) { const t = Number(v) || 0; mediaOf(this).currentTime = t; mediaTell(this, "seek", t); } },
    duration: { get() { return mediaOf(this).duration; } },
    volume: { get() { return mediaOf(this).volume; },
              set(v) { const level = Math.max(0, Math.min(1, Number(v))); mediaOf(this).volume = level; mediaTell(this, "volume", level); } },
    muted: { get() { return mediaOf(this).muted; }, set(v) { mediaOf(this).muted = !!v; mediaTell(this, "muted", !!v); } },
    defaultMuted: flag("muted"),
    autoplay: flag("autoplay"), loop: flag("loop", "loop"), controls: flag("controls"), playsInline: flag("playsinline"),
    disablePictureInPicture: flag("disablepictureinpicture"), disableRemotePlayback: flag("disableremoteplayback"),
    preload: reflected("preload"), poster: reflected("poster"), crossOrigin: reflected("crossorigin"),
    src: { get() { const v = this.getAttribute("src"); try { return v ? new URL(v, state.url).href : ""; } catch (_) { return v || ""; } },
           set(v) { this.setAttribute("src", String(v)); mediaOf(this).readyState = 0; mediaTell(this, "src", String(v)); } },
    srcObject: { get() { return null; }, set(_v) { mediaTell(this, "src", "mediasource:"); } },
    currentSrc: { get() { return mediaOf(this).currentSrc || this.src || ""; } },
    readyState: { get() { return mediaOf(this).readyState; } },
    networkState: { get() { return mediaOf(this).readyState ? 1 : (this.src ? 2 : 0); } },
    videoWidth: { get() { return mediaOf(this).videoWidth; } },
    videoHeight: { get() { return mediaOf(this).videoHeight; } },
    error: { get() { return mediaOf(this).error; } },
    playbackRate: { get() { return mediaOf(this).playbackRate; },
                    set(v) { mediaOf(this).playbackRate = Number(v) || 1; mediaTell(this, "rate", Number(v) || 1); } },
    defaultPlaybackRate: { get() { return 1; }, set(_v) {} },
    preservesPitch: { get() { return true; }, set(_v) {} },
    buffered: { get() { const m = mediaOf(this); return ranges(0, Number.isFinite(m.duration) ? m.duration : 0); } },
    seekable: { get() { const m = mediaOf(this); return ranges(0, Number.isFinite(m.duration) ? m.duration : 0); } },
    played: { get() { return ranges(0, mediaOf(this).currentTime); } },
    textTracks: { get() { return Object.assign([], { addEventListener() {}, removeEventListener() {}, getTrackById() { return null; } }); } },
    audioTracks: { get() { return Object.assign([], { addEventListener() {}, removeEventListener() {} }); } },
    videoTracks: { get() { return Object.assign([], { addEventListener() {}, removeEventListener() {} }); } },
    mediaKeys: { get() { return null; } },
    remote: { get() { return { state: "disconnected", watchAvailability() { return Promise.reject(new DOMException("", "NotSupportedError")); } }; } },
  };
  const constants = { NETWORK_EMPTY: 0, NETWORK_IDLE: 1, NETWORK_LOADING: 2, NETWORK_NO_SOURCE: 3,
                      HAVE_NOTHING: 0, HAVE_METADATA: 1, HAVE_CURRENT_DATA: 2, HAVE_FUTURE_DATA: 3, HAVE_ENOUGH_DATA: 4 };
  for (const name of ["HTMLMediaElement", "HTMLVideoElement", "HTMLAudioElement"]) {
    const cls = g[name];
    if (typeof cls !== "function") continue;
    for (const [key, fn] of Object.entries(methods)) {
      try { Object.defineProperty(cls.prototype, key, { configurable: true, writable: true, value: fn }); } catch (_) {}
    }
    for (const [key, descriptor] of Object.entries(properties)) {
      try { Object.defineProperty(cls.prototype, key, { configurable: true, ...descriptor }); } catch (_) {}
    }
    for (const [key, value] of Object.entries(constants)) {
      try { Object.defineProperty(cls, key, { value, configurable: true }); Object.defineProperty(cls.prototype, key, { value, configurable: true }); } catch (_) {}
    }
  }
  // new Audio(src): a sound a script plays, never put in the page
  g.Audio = function Audio(src) {
    const audio = document_.createElement("audio");
    audio.setAttribute("preload", "auto");
    if (src !== undefined) audio.src = src;
    return audio;
  };
  g.Audio.prototype = (g.HTMLAudioElement || g.HTMLElement).prototype;
}

function installWebExtras(g) {
  const Base = NodeEvent;
  const withFields = (name, fields) => class extends Base {
    constructor(type, init = {}) {
      super(type, init);
      for (const [key, value] of Object.entries(fields)) {
        try { Object.defineProperty(this, key, { value: key in init ? init[key] : value, configurable: true, enumerable: true }); } catch (_) {}
      }
    }
    static get name() { return name; }
  };
  if (typeof g.ErrorEvent !== "function") g.ErrorEvent = withFields("ErrorEvent", { message: "", filename: "", lineno: 0, colno: 0, error: null });
  if (typeof g.ProgressEvent !== "function") g.ProgressEvent = withFields("ProgressEvent", { lengthComputable: false, loaded: 0, total: 0 });
  if (typeof g.PromiseRejectionEvent !== "function") g.PromiseRejectionEvent = withFields("PromiseRejectionEvent", { promise: null, reason: undefined });
  if (typeof g.FileReader === "function") return;
  g.FileReader = class FileReader extends NodeEventTarget {
    static EMPTY = 0; static LOADING = 1; static DONE = 2;
    constructor() {
      super();
      this.readyState = 0; this.result = null; this.error = null;
      this.onload = this.onloadend = this.onerror = this.onloadstart = this.onprogress = this.onabort = null;
    }
    _emit(type, extra = {}) {
      const event = new g.ProgressEvent(type, { lengthComputable: true, ...extra });
      try { Object.defineProperty(event, "target", { value: this, configurable: true }); } catch (_) {}
      try { this["on" + type]?.call(this, event); } catch (e) { reportError(e); }
      try { this.dispatchEvent(event); } catch (e) { reportError(e); }
    }
    _read(blob, how) {
      if (this.readyState === 1) throw new DOMException("The object is already busy reading Blobs.", "InvalidStateError");
      this.readyState = 1; this.result = null; this.error = null; this._aborted = false;
      queueMicrotask(() => this._emit("loadstart"));
      Promise.resolve(blob && typeof blob.arrayBuffer === "function" ? blob.arrayBuffer() : new ArrayBuffer(0)).then((buffer) => {
        if (this._aborted) return;
        const bytes = new Uint8Array(buffer);
        if (how === "buffer") this.result = buffer;
        else if (how === "text") this.result = new TextDecoder(this._encoding || "utf-8").decode(bytes);
        else if (how === "binary") { let text = ""; for (let i = 0; i < bytes.length; i += 0x8000) text += String.fromCharCode(...bytes.subarray(i, i + 0x8000)); this.result = text; }
        else this.result = `data:${(blob && blob.type) || "application/octet-stream"};base64,${toBase64(bytes)}`;
        this.readyState = 2;
        this._emit("progress", { loaded: bytes.length, total: bytes.length });
        this._emit("load", { loaded: bytes.length, total: bytes.length });
        this._emit("loadend", { loaded: bytes.length, total: bytes.length });
      }, (error) => {
        this.readyState = 2; this.error = error;
        this._emit("error"); this._emit("loadend");
      });
    }
    readAsArrayBuffer(blob) { this._read(blob, "buffer"); }
    readAsText(blob, encoding) { this._encoding = encoding; this._read(blob, "text"); }
    readAsDataURL(blob) { this._read(blob, "dataurl"); }
    readAsBinaryString(blob) { this._read(blob, "binary"); }
    abort() { if (this.readyState === 1) { this._aborted = true; this.readyState = 2; this.result = null; this._emit("abort"); this._emit("loadend"); } }
  };
}


// ------------------------------------------------------------------ frames
// Each frame's page runs in a process of its own, as Chrome runs each
// site's frames apart. Other windows (the parent, the top, a frame's
// contentWindow) are stand-ins here: their postMessage goes through Merlin to
// that window's process, and a message from one arrives as a MessageEvent
// whose source is the same stand-in, so a reply finds its way back. A
// MessagePort sent across (reCAPTCHA talks to its frames over MessageChannels)
// is joined to its far end through Merlin the same way.
const windowProxies = new Map();      // a window's token -> its stand-in here
const elementProxies = new WeakMap(); // an <iframe> -> its contentWindow
function proxyFor(route, token) {
  const proxy = {
    postMessage(data, targetOrigin, transfer) {
      if (targetOrigin && typeof targetOrigin === "object") { transfer = targetOrigin.transfer; targetOrigin = targetOrigin.targetOrigin; }
      let encoded;
      try { encoded = encodeValue(data); }
      catch (e) { throw new DOMException(String(e && e.message || e), "DataCloneError"); }
      send({ type: "post", ...route(), data: encoded, targetOrigin: targetOrigin ?? "/", ports: exportPorts(transfer) });
    },
    get closed() { return false; }, close() {}, focus() {}, blur() {},
    get window() { return proxy; }, get self() { return proxy; }, get frames() { return proxy; }, length: 0,
    get parent() { return frameTop(); }, get top() { return frameTop(); }, opener: null,
    location: {
      get href() { throw new DOMException("Blocked a frame from accessing a cross-origin frame.", "SecurityError"); },
      set href(v) { send({ type: "navigate_window", ...route(), url: String(v) }); },
      assign(v) { send({ type: "navigate_window", ...route(), url: String(v) }); },
      replace(v) { send({ type: "navigate_window", ...route(), url: String(v) }); },
      toString() { return ""; },
    },
    addEventListener() {}, removeEventListener() {},
  };
  if (token) windowProxies.set(token, proxy);
  return proxy;
}
function windowFor(token) {
  if (!token) return null;
  if (token === state.token) return globalThis;
  return windowProxies.get(token) || proxyFor(() => ({ token }), token);
}
function frameTop() {
  return state.frame ? windowFor(state.frame.top) : globalThis;
}
function frameWindow(iframe) {
  let proxy = elementProxies.get(iframe);
  if (!proxy) { proxy = proxyFor(() => ({ target: idOf(iframe) })); elementProxies.set(iframe, proxy); }
  return proxy;
}
function installFrames(g, made) {
  if (state.frame) {
    // a frame: its parent and top are other windows
    const parent = windowFor(state.frame.parent), top = windowFor(state.frame.top);
    for (const [name, value] of [["parent", parent], ["top", top]]) {
      try { Object.defineProperty(g, name, { get: () => value, configurable: true }); } catch (_) { g[name] = value; }
    }
    try { Object.defineProperty(g, "frameElement", { get: () => null, configurable: true }); } catch (_) {}
  }
  const probe = document_.createElement("iframe");
  const Base = (g.HTMLElement || made.HTMLElement).prototype;
  const own = Object.getPrototypeOf(probe);
  const target = own && own !== Base ? own : Base;
  const isFrame = (el) => el && (el.tagName === "IFRAME" || el.tagName === "FRAME");
  try {
    Object.defineProperty(target, "contentWindow", { configurable: true,
      get() { return isFrame(this) ? frameWindow(this) : undefined; } });
    // another site's document is not this page's to read
    Object.defineProperty(target, "contentDocument", { configurable: true,
      get() { return isFrame(this) ? null : undefined; } });
  } catch (_) {}
}

// a message's data, as structured cloning carries it: JSON with what JSON
// lacks (undefined, binary data, dates, maps, sets, NaN) marked
function encodeValue(value, seen = new Set()) {
  if (value === undefined) return { $u: 1 };
  if (value === null || typeof value === "string" || typeof value === "boolean") return value;
  if (typeof value === "number") return Number.isFinite(value) ? value : { $n: String(value) };
  if (typeof value === "bigint") return { $bi: String(value) };
  if (typeof value === "function" || typeof value === "symbol") throw new Error(`${String(value).slice(0, 40)} could not be cloned.`);
  if (seen.has(value)) return null;
  seen.add(value);
  try {
    if (value instanceof ArrayBuffer) return { $ab: toBase64(new Uint8Array(value)) };
    if (ArrayBuffer.isView(value)) return { $ta: value.constructor.name, $b: toBase64(new Uint8Array(value.buffer, value.byteOffset, value.byteLength)) };
    if (value instanceof Date) return { $d: value.getTime() };
    if (value instanceof Map) return { $m: [...value].map(([k, v]) => [encodeValue(k, seen), encodeValue(v, seen)]) };
    if (value instanceof Set) return { $s: [...value].map((v) => encodeValue(v, seen)) };
    if (value instanceof RegExp) return { $r: [value.source, value.flags] };
    if (value instanceof MessagePort) return null;
    if (Array.isArray(value)) return value.map((v) => encodeValue(v, seen));
    if (value instanceof Error) return { $e: [value.name, value.message] };
    const out = {};
    for (const key of Object.keys(value)) {
      const v = value[key];
      if (typeof v === "function") continue;
      out[key] = encodeValue(v, seen);
    }
    return { $o: out };
  } finally { seen.delete(value); }
}
function decodeValue(value) {
  if (value === null || typeof value !== "object") return value;
  if (Array.isArray(value)) return value.map(decodeValue);
  if ("$o" in value) { const out = {}; for (const [k, v] of Object.entries(value.$o)) out[k] = decodeValue(v); return out; }
  if ("$u" in value) return undefined;
  if ("$n" in value) return Number(value.$n);
  if ("$bi" in value) return BigInt(value.$bi);
  if ("$ab" in value) return fromBase64(value.$ab).buffer;
  if ("$ta" in value) {
    const bytes = fromBase64(value.$b);
    const Kind = globalThis[value.$ta] || Uint8Array;
    return new Kind(bytes.buffer, 0, bytes.byteLength / (Kind.BYTES_PER_ELEMENT || 1));
  }
  if ("$d" in value) return new Date(value.$d);
  if ("$m" in value) return new Map(value.$m.map(([k, v]) => [decodeValue(k), decodeValue(v)]));
  if ("$s" in value) return new Set(value.$s.map(decodeValue));
  if ("$r" in value) return new RegExp(value.$r[0], value.$r[1]);
  if ("$e" in value) { const e = new Error(value.$e[1]); e.name = value.$e[0]; return e; }
  return value;
}

// ports sent to another window: this end is kept here as an anchor, whose
// messages go to Merlin, and what comes from the far end is posted on it
const anchors = new Map();          // a port's id -> the anchor port here
const anchorIds = new WeakMap();
let portSerial = 0;
function anchor(port, id) {
  anchors.set(id, port); anchorIds.set(port, id);
  port.onmessage = (event) => {
    let data;
    try { data = encodeValue(event.data); } catch (e) { reportError(e); return; }
    send({ type: "port_message", port: id, data, ports: exportPorts(event.ports) });
  };
}
function exportPorts(list) {
  const ids = [];
  for (const port of (list && typeof list[Symbol.iterator] === "function") ? list : []) {
    if (!(port instanceof MessagePort)) continue;
    let id = anchorIds.get(port);
    if (!id) { id = `${state.token}:${++portSerial}`; anchor(port, id); }
    ids.push(id);
  }
  return ids;
}
function importPorts(ids) {
  return (ids || []).map((id) => {
    const channel = new MessageChannel();
    anchor(channel.port2, id);
    return channel.port1;
  });
}
// The runtime's MessageEvent takes only a MessagePort as its source: a window is
// given to it afterwards, as a browser's event has it
function messageEvent(data, origin, source, ports) {
  const event = new MessageEvent("message", { data, origin, ports: ports || [] });
  try { Object.defineProperty(event, "source", { value: source ?? null, configurable: true }); } catch (_) {}
  return event;
}
function receiveMessage(message) {
  const ports = importPorts(message.ports);
  globalThis.dispatchEvent(messageEvent(decodeValue(message.data), message.origin || "null",
                                        windowFor(message.source), ports));
}

// ------------------------------------------------------------------ back to Merlin
const VOID = new Set(["area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"]);
function escapeText(t) { return t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
function escapeAttr(t) { return t.replace(/&/g, "&amp;").replace(/"/g, "&quot;"); }
function serialize(node, out) {
  if (node.nodeType === 3) { out.push(escapeText(node.data)); return; }
  if (node.nodeType !== 1) return;
  const tag = node.localName || node.tagName.toLowerCase();
  out.push("<", tag, ' data-mjs="', String(idOf(node)), '"');
  for (const attr of node.attributes || []) out.push(" ", attr.name, '="', escapeAttr(attr.value), '"');
  // a form field's value as the script left it, not only as written
  if ((tag === "input" || tag === "textarea" || tag === "select") && node.value !== undefined && node.value !== null) {
    out.push(' data-mjs-value="', escapeAttr(String(node.value)), '"');
    if (node.checked) out.push(' data-mjs-checked=""');
  }
  out.push(">");
  if (VOID.has(tag)) return;
  // scripts are Merlin Engine's to run, not to lay out: left empty
  if (tag !== "script") {
    const children = tag === "template" && node.content ? node.content.childNodes : node.childNodes;
    for (const child of children) serialize(child, out);
  }
  out.push("</", tag, ">");
}
function pageNow() {
  const out = state.quirks ? [] : ["<!DOCTYPE html>"];   // the page's own mode, kept
  serialize(document_.documentElement, out);
  return out.join("");
}

let pending = null, lastSent = "";
function changed() {
  if (pending) return;
  pending = setTimeout(() => {
    pending = null;
    const html = pageNow();
    if (html !== lastSent) { lastSent = html; send({ type: "dom", html, title: document_.title || "" }); }
  }, 60);
}

// ------------------------------------------------------------------ events from Merlin
function dispatch(message) {
  const target = nodeOf(message.target);
  if (message.type === "scroll") {
    state.scrollX = message.x || 0; state.scrollY = message.y || 0;
    fire(globalThis, "scroll"); fire(document_, "scroll");
    for (const observer of observers) observer._check();
    return;
  }
  if (message.type === "image") { imageAnswered(message); return; }
  if (message.type === "media_event") { mediaAnswered(message); return; }
  if (message.type === "message") { receiveMessage(message); return; }
  if (message.type === "port_message") {
    const port = anchors.get(message.port);
    if (port) port.postMessage(decodeValue(message.data), importPorts(message.ports));
    return;
  }
  if (message.type === "frame_token") {
    // which window an <iframe> holds: messages from it come with its token
    const iframe = nodeOf(message.target);
    if (iframe) windowProxies.set(message.token, frameWindow(iframe));
    return;
  }
  if (message.type === "frame_loaded") { const iframe = nodeOf(message.target); if (iframe) fire(iframe, "load"); return; }
  if (message.type === "files") {
    const input = nodeOf(message.target);
    if (!input) return;
    const files = (message.files || []).map((f) => new File([fromBase64(f.data)], f.name,
      { type: f.type || "", lastModified: f.lastModified || Date.now() }));
    const list = Object.assign([...files], { item(i) { return this[i] ?? null; } });
    try { Object.defineProperty(input, "files", { value: list, configurable: true, writable: true }); } catch (_) {}
    try { Object.defineProperty(input, "value", { value: files.length ? "C:\\fakepath\\" + files[0].name : "",
                                                   configurable: true, writable: true }); } catch (_) {}
    input.dispatchEvent(new globalThis.Event("input", { bubbles: true }));
    input.dispatchEvent(new globalThis.Event("change", { bubbles: true }));
    return;
  }
  if (message.type === "panel_scroll") {
    // a panel moved (the wheel, a finger): its position, and its scroll event
    const id = Number(message.target);
    panels.set(id, [message.top, message.height, message.scroll_height]);
    const element = nodeOf(id);
    if (element) element.dispatchEvent(new globalThis.Event("scroll"));
    return;
  }
  if (message.type === "geometry") {
    geometry.clear();
    for (const [id, box] of Object.entries(message.boxes || {})) geometry.set(Number(id), box);
    panels.clear();
    for (const [id, panel] of Object.entries(message.panels || {})) panels.set(Number(id), panel);
    if (message.height) state.height = message.height;
    for (const observer of observers) observer._check();
    for (const observer of resizers) observer._check();
    return;
  }
  if (message.type === "resize") {
    state.width = message.width; state.height = message.height; fire(globalThis, "resize"); return;
  }
  if (!target) { send({ type: "handled", id: message.id, prevented: false }); return; }
  let prevented = false;
  const E = globalThis.Event;
  // What a person does arrives as the kind of event a browser makes of it (a
  // MouseEvent, a KeyboardEvent...), trusted, with every field pages read:
  // Google's buttons (AI Mode's toggle among them) did nothing for clicks that
  // came as plain, untrusted Events without which, view or pageX.
  const KINDS = { pointerdown: "PointerEvent", pointerup: "PointerEvent", mousedown: "MouseEvent",
                  mouseup: "MouseEvent", click: "MouseEvent", keydown: "KeyboardEvent", keyup: "KeyboardEvent",
                  keypress: "KeyboardEvent", input: "InputEvent", submit: "SubmitEvent" };
  const make = (type, init = {}) => {
    const kind = KINDS[type] && typeof globalThis[KINDS[type]] === "function" ? globalThis[KINDS[type]] : E;
    let event;
    try { event = new kind(type, { bubbles: true, cancelable: true, composed: true, ...init }); }
    catch (_) { event = new E(type, { bubbles: true, cancelable: true, ...init }); }
    const fields = { isTrusted: true, view: globalThis, ...init };
    if (kind !== E && /^(pointer|mouse|click)/.test(type)) {
      const x = init.clientX || 0, y = init.clientY || 0;
      Object.assign(fields, { which: (init.button || 0) + 1, pageX: x + (state.scrollX || 0), pageY: y + (state.scrollY || 0),
        screenX: x, screenY: y, x, y, offsetX: x, offsetY: y, movementX: 0, movementY: 0, ctrlKey: false,
        shiftKey: false, altKey: false, metaKey: false, relatedTarget: null });
      if (type.startsWith("pointer")) Object.assign(fields, { pointerId: 1, pointerType: "mouse", isPrimary: true,
        width: 1, height: 1, pressure: type === "pointerdown" ? 0.5 : 0 });
    }
    if (type.startsWith("key")) Object.assign(fields, { which: init.keyCode || 0, charCode: type === "keypress" ? (init.keyCode || 0) : 0,
      repeat: false, location: 0, isComposing: false, ctrlKey: false, shiftKey: false, altKey: false, metaKey: false });
    for (const [k, v] of Object.entries(fields)) {
      let have;
      try { have = event[k]; } catch (_) { have = undefined; }
      if (k !== "isTrusted" && have !== undefined && have === v) continue;
      try { Object.defineProperty(event, k, { value: v, configurable: true, enumerable: true }); }
      catch (_) { try { event[k] = v; } catch (__) {} }
    }
    return event;
  };
  if (message.type === "click") {
    const extra = { clientX: message.x || 0, clientY: message.y || 0, button: 0, buttons: 1, detail: 1 };
    for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup"]) {
      target.dispatchEvent(make(type, type.endsWith("up") ? { ...extra, buttons: 0 } : extra));
    }
    const click = make("click", { ...extra, buttons: 0 });
    target.dispatchEvent(click);
    prevented = click.defaultPrevented;
    // a click in a <label> works its control, as browsers do: a toggle switch
    // is a label holding a hidden checkbox (Ponder's settings had not moved)
    if (!prevented && activateLabel(target)) prevented = true;
  } else if (message.type === "input") {
    // As typing does in a browser: under any setter a framework put on the
    // element itself. React replaces an input's value setter to track changes,
    // and a value set through it looked like no change, so typing was ignored.
    const setNative = (name, value) => {
      let proto = Object.getPrototypeOf(target);
      while (proto && !Object.getOwnPropertyDescriptor(proto, name)) proto = Object.getPrototypeOf(proto);
      const own = proto && Object.getOwnPropertyDescriptor(proto, name);
      if (own && own.set) own.set.call(target, value); else target[name] = value;
    };
    if ("value" in message) { try { setNative("value", message.value); } catch (_) {} }
    if ("checked" in message) { try { setNative("checked", message.checked); } catch (_) {} }
    target.dispatchEvent(make("input")); target.dispatchEvent(make("change"));
  } else if (message.type === "submit") {
    const submit = make("submit");
    const by = message.submitter != null ? nodeOf(message.submitter) : null;
    try { Object.defineProperty(submit, "submitter", { value: by || null, configurable: true }); } catch (_) {}
    target.dispatchEvent(submit); prevented = submit.defaultPrevented;
  } else if (message.type === "key") {
    const init = { key: message.key, code: message.code || "", keyCode: message.keyCode || 0 };
    const down = make("keydown", init); target.dispatchEvent(down); target.dispatchEvent(make("keyup", init));
    prevented = down.defaultPrevented;
  }
  send({ type: "handled", id: message.id, prevented });
}

function activateLabel(target) {
  const label = target.closest ? target.closest("label") : null;
  if (!label) return false;
  const interactive = target.closest("a[href], button, input, select, textarea");
  if (interactive && label.contains(interactive)) return false;
  const control = label.hasAttribute("for") ? document_.getElementById(label.getAttribute("for"))
    : label.querySelector("input, select, textarea, button");
  if (!control || control.hasAttribute("disabled")) return false;
  const type = (control.getAttribute("type") || "").toLowerCase();
  if (control.tagName === "INPUT" && (type === "checkbox" || type === "radio")) {
    const before = !!control.checked;
    if (type === "checkbox") control.checked = !before; else control.checked = true;
    const click = new globalThis.Event("click", { bubbles: true, cancelable: true });
    control.dispatchEvent(click);
    if (click.defaultPrevented) { control.checked = before; return true; }
    if (control.checked !== before) {
      control.dispatchEvent(new globalThis.Event("input", { bubbles: true }));
      control.dispatchEvent(new globalThis.Event("change", { bubbles: true }));
    }
    changed();
    return true;
  }
  if (control.tagName === "BUTTON" || (control.tagName === "INPUT" && ["submit", "button", "reset", "image"].includes(type))) {
    control.click();
    return true;
  }
  return false;                    // a text field: Merlin puts the cursor in it
}

// ------------------------------------------------------------------ timing, blurred
// With "Blur timing" on (Settings > Shields): the page's clocks tell time in
// steps of 100 milliseconds, as Tor Browser's do, and what a person does
// reaches it a little late (above). Behavioural biometrics tell people apart
// by how long keys are held and the gaps between them; at this precision
// those are gone.
const BLUR_STEP_MS = 100, BLUR_JITTER_MS = 40;
const PEOPLE = new Set(["click", "input", "key", "submit"]);
let lane = Promise.resolve();
function blurTiming(g) {
  const step = coarsenClocks(g, BLUR_STEP_MS);
  // an event's timeStamp
  for (let proto = g.Event && g.Event.prototype; proto; proto = Object.getPrototypeOf(proto)) {
    const found = Object.getOwnPropertyDescriptor(proto, "timeStamp");
    if (!found) continue;
    const read = found.get ? (event) => found.get.call(event) : () => found.value;
    try {
      Object.defineProperty(g.Event.prototype, "timeStamp", { configurable: true, enumerable: true,
        get() { return step(Number(read(this)) || 0); } });
    } catch (_) {}
    break;
  }
  // animation frames are given the time too
  const frame = g.requestAnimationFrame;
  if (typeof frame === "function") {
    g.requestAnimationFrame = function requestAnimationFrame(callback) {
      if (typeof callback !== "function") return frame(callback);
      return frame((t) => callback(step(t)));
    };
  }
}

// ------------------------------------------------------------------ the loop
async function main() {
  let buffer = "";
  for await (const value of proc.stdin) {
    buffer += decoder.decode(value, { stream: true });
    let newline;
    while ((newline = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, newline); buffer = buffer.slice(newline + 1);
      if (!line.trim()) continue;
      let message;
      try { message = JSON.parse(line); } catch (_) { continue; }
      if (message.type === "load") {
        Object.assign(state, message.state || {});
        // a page given without a doctype is in quirks mode, as Merlin found it
        state.quirks = !/^\s*<!doctype\s+html/i.test(String(message.html || ""));
        install(message.html);
        if (state.blurTiming) blurTiming(globalThis);
        lockDown();
        // Merlin's numbering of the elements becomes the ids here, and the
        // attributes go, so the page's scripts never see them; with the layout
        // Merlin has already done, scripts' first measurements are real
        for (const element of document_.querySelectorAll("[data-mjs]")) {
          const number = Number(element.getAttribute("data-mjs"));
          element.removeAttribute("data-mjs");
          ids.set(element, number); byId.set(number, new WeakRef(element));
          if (number > lastId) lastId = number;
        }
        const root = document_.documentElement;
        if (root && root.hasAttribute && root.hasAttribute("data-mjs")) {
          const number = Number(root.getAttribute("data-mjs")); root.removeAttribute("data-mjs");
          ids.set(root, number); byId.set(number, new WeakRef(root));
        }
        for (const [id, box] of Object.entries(message.geometry || {})) geometry.set(Number(id), box);
        send({ type: "ready" });
        watchForScripts();
        new globalThis.MutationObserver(changed).observe(document_, { childList: true, subtree: true, attributes: true, characterData: true });
        runScripts().then(changed).catch(e => reportError(e));
      } else if (message.type === "answer") {
        const resolve = waiting.get(message.id); waiting.delete(message.id); resolve?.(message);
      } else if (state.blurTiming && PEOPLE.has(message.type)) {
        // what a person does reaches the page a little late, by a different
        // amount each time and never out of order: the rhythm of typing and
        // clicking, which can tell people apart, is lost
        lane = lane.then(() => new Promise((resolve) => setTimeout(resolve, Math.random() * BLUR_JITTER_MS)))
          .then(() => { try { dispatch(message); } catch (e) { reportError(e); send({ type: "handled", id: message.id, prevented: false }); } });
      } else {
        try { dispatch(message); } catch (e) { reportError(e); send({ type: "handled", id: message.id, prevented: false }); }
      }
    }
  }
}

// Nothing of Node is left for the page to see or reach: its process and the
// globals no browser has go (install takes most), and nothing more is loaded
// as Node loads modules. Merlin's merlin_fetch is the page's only way out,
// besides what Node's permission model allows this process at all: reading
// these files, nothing else.
function lockDown() {
  locked = true;
  for (const name of ["process", "global", "setImmediate", "clearImmediate", "require", "module",
                      "__filename", "__dirname", "WebSocket", "EventSource", "fetch"]) {
    if (name === "fetch") continue;                 // the page's own, made by install
    try { delete globalThis[name]; } catch (_) {}
  }
}
proc.stdin.on("end", () => proc.exit(0));         // Merlin has gone: so does this
main().catch(e => { try { send({ type: "console", level: "error", text: "host: " + safeString(e) + " " + String(e && e.stack).split("\n").slice(1, 9).join(" <- ") }); } catch (_) {} });
