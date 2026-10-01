// Merlin Engine's JavaScript host: one per tab, run by Deno with no permissions.
//
// It holds the page's DOM (linkedom), runs the page's scripts against it, and
// talks to Merlin one JSON message per line on stdin and stdout. Merlin does
// everything that reaches outside: fetching (with the page's cookies, through
// the content blocker), storage, navigation. After scripts change the page,
// the new page is sent back for Merlin Engine to style and lay out.
import { parseHTML } from "./linkedom.bundle.js";

const encoder = new TextEncoder();
const decoder = new TextDecoder();
const stdout = Deno.stdout;
const stdinReader = Deno.stdin.readable.getReader();

function send(message) {
  const line = encoder.encode(JSON.stringify(message) + "\n");
  let written = 0;
  while (written < line.length) written += stdout.writeSync(line.subarray(written));
}

// console goes to Merlin as messages: anything printed would break the channel
for (const level of ["log", "info", "warn", "error", "debug"]) {
  console[level] = (...args) => {
    try {
      send({ type: "console", level, text: args.map(a => typeof a === "string" ? a : safeString(a)).join(" ") });
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
    else if (typeof FormData !== "undefined" && body instanceof FormData) {
      const encoded = new URLSearchParams(); for (const [k, v] of body) encoded.append(k, String(v));
      bodyB64 = toBase64(encoder.encode(encoded.toString())); headers["content-type"] ??= "application/x-www-form-urlencoded;charset=UTF-8";
    } else bodyB64 = toBase64(encoder.encode(String(body)));
  }
  const answer = await ask({ type: "fetch", url, method, headers, body: bodyB64, credentials: init.credentials || "same-origin" });
  if (answer.error) throw new TypeError(`Failed to fetch: ${answer.error}`);
  const response = new NativeResponse([101, 204, 205, 304].includes(answer.status) ? null : fromBase64(answer.body),
    { status: answer.status, statusText: answer.statusText || "", headers: answer.headers || {} });
  Object.defineProperty(response, "url", { value: answer.url || url });
  return response;
}

// ------------------------------------------------------------------ the page
let window_, document_;
const state = { url: "about:blank", width: 1280, height: 800, scrollX: 0, scrollY: 0, cookie: "", scheme: "light",
                userAgent: "Mozilla/5.0", language: "en-GB", storage: {}, loaded: false };
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

// Deno has its own localStorage, navigator, location and more, some read-only:
// assigned, the page's were silently ignored and it got Deno's. Defined instead.
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
];

function install(html) {
  const made = parseHTML(html);
  window_ = made.window; document_ = made.document;
  const g = globalThis;
  // Deno's own versions of these go first (they are configurable), so the
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
    if (typeof found !== "function") found = name.endsWith("Event") ? class extends (made.Event || g.Event) {} : class extends Base {};
    try { Object.defineProperty(found, "name", { value: name }); } catch (_) {}
    if (!(name in g) || typeof g[name] !== "function") g[name] = found;
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
    platform: state.userAgent.includes("Windows") ? "Win32" : "Linux x86_64", vendor: "Google Inc.",
    language: state.language, languages: [state.language, state.language.split("-")[0]], onLine: true,
    cookieEnabled: true, hardwareConcurrency: 4, maxTouchPoints: 0, doNotTrack: null, webdriver: false,
    userAgentData: undefined, clipboard: { writeText: async () => {}, readText: async () => "" },
    sendBeacon(url, data) { merlinFetch(url, { method: "POST", body: data ?? null }).catch(() => {}); return true; },
    permissions: { query: async () => ({ state: "prompt", addEventListener() {} }) },
    mediaDevices: undefined, serviceWorker: undefined, geolocation: undefined,
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
  const frame = (cb) => setTimeout(() => cb(performance.now()), 16);
  g.requestAnimationFrame = frame; g.cancelAnimationFrame = (id) => clearTimeout(id);
  g.requestIdleCallback = (cb) => setTimeout(() => cb({ didTimeout: false, timeRemaining: () => 10 }), 1);
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
    // Merlin Engine has no scroll positions for scripts yet: everything
    // observed is reported in view, which shows content revealed on scroll
    constructor(callback, options = {}) { this._cb = callback; this._targets = new Set(); this.root = options.root ?? null; this.rootMargin = options.rootMargin ?? "0px"; this.thresholds = [].concat(options.threshold ?? 0); }
    observe(target) {
      this._targets.add(target);
      queueMicrotask(() => { try { this._cb([{ target, isIntersecting: true, intersectionRatio: 1, time: performance.now(),
        boundingClientRect: target.getBoundingClientRect?.() ?? {}, intersectionRect: {}, rootBounds: null }], this); } catch (e) { reportError(e); } });
    }
    unobserve(target) { this._targets.delete(target); }
    disconnect() { this._targets.clear(); }
    takeRecords() { return []; }
  };
  g.ResizeObserver = class {
    constructor(callback) { this._cb = callback; }
    observe(target) { queueMicrotask(() => { try { this._cb([{ target, contentRect: { x: 0, y: 0, width: state.width, height: 0, top: 0, left: 0, right: state.width, bottom: 0 },
      borderBoxSize: [{ inlineSize: state.width, blockSize: 0 }], contentBoxSize: [{ inlineSize: state.width, blockSize: 0 }] }], this); } catch (e) { reportError(e); } }); }
    unobserve() {} disconnect() {}
  };
  g.PerformanceObserver = class { constructor() {} observe() {} disconnect() {} takeRecords() { return []; } static get supportedEntryTypes() { return []; } };
  g.alert = (text) => send({ type: "alert", text: String(text) });
  g.confirm = () => false; g.prompt = () => null; g.print = () => {};
  g.open = (url) => { if (url) send({ type: "open", url: new URL(String(url), state.url).href }); return null; };
  g.close = () => {}; g.focus = () => {}; g.blur = () => {};
  g.getSelection = () => ({ rangeCount: 0, toString: () => "", removeAllRanges() {}, addRange() {} });
  g.WebSocket = class { constructor() { throw new Error("WebSocket is not available in Merlin Engine yet"); } };
  g.EventSource = class { constructor() { this.readyState = 2; } close() {} addEventListener() {} };
  g.Worker = class { constructor() { throw new Error("Workers are not available in Merlin Engine yet"); } };
  g.XMLHttpRequest = makeXHR();
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
  const proto = (g.Element || made.Element).prototype;
  if (!proto.getBoundingClientRect) proto.getBoundingClientRect = function () {
    return { x: 0, y: 0, top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0, toJSON() { return this; } };
  };
  if (!proto.getClientRects) proto.getClientRects = function () { return []; };
  for (const name of ["scrollIntoView", "focus", "blur", "scrollTo", "scrollBy", "animate", "requestFullscreen"]) {
    if (!proto[name]) proto[name] = function () { return name === "animate" ? { finished: Promise.resolve(), cancel() {}, play() {}, pause() {}, addEventListener() {} } : undefined; };
  }
  for (const name of ["offsetWidth", "offsetHeight", "clientWidth", "clientHeight", "scrollWidth", "scrollHeight", "offsetTop", "offsetLeft"]) {
    if (!(name in proto)) Object.defineProperty(proto, name, { get() { return 0; }, configurable: true });
  }
  if (!("isConnected" in proto)) Object.defineProperty(proto, "isConnected", { get() { return this.ownerDocument?.contains?.(this) ?? true; }, configurable: true });
  g.reportError = (error) => send({ type: "console", level: "error", text: `Uncaught ${safeString(error)}${error && error.stack ? "\n" + String(error.stack).split("\n").slice(1, 4).join("\n") : ""}` });
  g.addEventListener("error", (event) => { g.reportError(event.error ?? event.message); });
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

function makeXHR() {
  return class XMLHttpRequest {
    static UNSENT = 0; static OPENED = 1; static HEADERS_RECEIVED = 2; static LOADING = 3; static DONE = 4;
    constructor() { this.readyState = 0; this.status = 0; this.statusText = ""; this.response = null; this.responseText = "";
      this.responseType = ""; this.responseURL = ""; this.withCredentials = false; this.timeout = 0; this._headers = {};
      this._listeners = {}; this.upload = { addEventListener() {} }; this._responseHeaders = new NativeHeaders(); }
    open(method, url) { this._method = method; this._url = url; this.readyState = 1; this._emit("readystatechange"); }
    setRequestHeader(k, v) { this._headers[k] = v; }
    getResponseHeader(k) { return this._responseHeaders.get(k); }
    getAllResponseHeaders() { let text = ""; this._responseHeaders.forEach((v, k) => { text += `${k}: ${v}\r\n`; }); return text; }
    overrideMimeType() {}
    addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); }
    removeEventListener(type, fn) { this._listeners[type] = (this._listeners[type] || []).filter(f => f !== fn); }
    _emit(type) { const event = { type, target: this, currentTarget: this };
      try { this["on" + type]?.(event); } catch (e) { reportError(e); }
      for (const fn of this._listeners[type] || []) { try { fn.call(this, event); } catch (e) { reportError(e); } } }
    abort() { this._aborted = true; }
    async send(body = null) {
      try {
        const response = await merlinFetch(this._url, { method: this._method, headers: this._headers, body });
        if (this._aborted) return;
        this.status = response.status; this.statusText = response.statusText; this.responseURL = response.url;
        this._responseHeaders = response.headers; this.readyState = 2; this._emit("readystatechange");
        const buffer = await response.arrayBuffer();
        const text = decoder.decode(buffer);
        this.responseText = text;
        this.response = this.responseType === "json" ? (() => { try { return JSON.parse(text); } catch (_) { return null; } })()
          : this.responseType === "arraybuffer" ? buffer : text;
        this.readyState = 4; this._emit("readystatechange"); this._emit("load"); this._emit("loadend");
      } catch (e) { this.readyState = 4; this._emit("readystatechange"); this._emit("error"); this._emit("loadend"); }
    }
  };
}

// ------------------------------------------------------------------ scripts
const JS_TYPES = ["", "text/javascript", "application/javascript", "module", "text/ecmascript", "application/ecmascript"];
const ran = new WeakSet();

async function classic(script) {
  const src = script.getAttribute("src");
  let code = script.textContent;
  if (src) {
    try {
      const response = await merlinFetch(src, { credentials: "same-origin" });
      if (!response.ok) { send({ type: "console", level: "warn", text: `script ${src}: ${response.status}` }); return; }
      code = await response.text();
    } catch (e) { send({ type: "console", level: "warn", text: `script ${src}: ${e.message}` }); return; }
  }
  try { document_.currentScript = script; } catch (_) {}
  try { (0, eval)(code + (src ? `\n//# sourceURL=${src}` : "")); }
  catch (e) { reportError(e); }
  try { document_.currentScript = null; } catch (_) {}
  fire(script, "load");
}

async function moduleScript(script) {
  const src = script.getAttribute("src");
  try {
    if (src) await import(new URL(src, state.url).href);
    else await import("data:text/javascript;charset=utf-8," + encodeURIComponent(script.textContent));
    fire(script, "load");
  } catch (e) {
    send({ type: "console", level: "warn", text: `module ${src || "(inline)"}: ${e.message}` });
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
  const scripts = [...document_.querySelectorAll("script")].filter(runnable);
  const modules = [];
  for (const script of scripts) {
    ran.add(script);
    if ((script.getAttribute("type") || "").toLowerCase() === "module") modules.push(script);
    else await classic(script);
  }
  __setReadyState("interactive");
  for (const script of modules) await moduleScript(script);
  fire(document_, "DOMContentLoaded");
  await new Promise(r => setTimeout(r, 0));
  __setReadyState("complete");
  fire(globalThis, "load");
  state.loaded = true;
}

// scripts a page adds later run as they arrive, as in a browser
function watchForScripts() {
  new globalThis.MutationObserver((records) => {
    for (const record of records) for (const node of record.addedNodes || []) {
      const scripts = node.tagName === "SCRIPT" ? [node] : (node.querySelectorAll ? [...node.querySelectorAll("script")] : []);
      for (const script of scripts) {
        if (ran.has(script) || !runnable(script)) continue;
        ran.add(script);
        ((script.getAttribute("type") || "").toLowerCase() === "module" ? moduleScript : classic)(script);
      }
    }
  }).observe(document_, { childList: true, subtree: true });
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
  const out = ["<!DOCTYPE html>"];
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
    fire(globalThis, "scroll"); fire(document_, "scroll"); return;
  }
  if (message.type === "resize") {
    state.width = message.width; state.height = message.height; fire(globalThis, "resize"); return;
  }
  if (!target) { send({ type: "handled", id: message.id, prevented: false }); return; }
  let prevented = false;
  const E = globalThis.Event;
  const make = (type, init = {}) => {
    const event = new E(type, { bubbles: true, cancelable: true, ...init });
    for (const [k, v] of Object.entries(init)) { try { if (!(k in event)) event[k] = v; } catch (_) {} }
    return event;
  };
  if (message.type === "click") {
    const extra = { clientX: message.x || 0, clientY: message.y || 0, button: 0, buttons: 1, detail: 1 };
    for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup"]) target.dispatchEvent(make(type, extra));
    const click = make("click", extra);
    target.dispatchEvent(click);
    prevented = click.defaultPrevented;
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
    const submit = make("submit"); target.dispatchEvent(submit); prevented = submit.defaultPrevented;
  } else if (message.type === "key") {
    const init = { key: message.key, code: message.code || "", keyCode: message.keyCode || 0 };
    const down = make("keydown", init); target.dispatchEvent(down); target.dispatchEvent(make("keyup", init));
    prevented = down.defaultPrevented;
  }
  send({ type: "handled", id: message.id, prevented });
}

// ------------------------------------------------------------------ the loop
async function main() {
  let buffer = "";
  for (;;) {
    const { value, done } = await stdinReader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let newline;
    while ((newline = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, newline); buffer = buffer.slice(newline + 1);
      if (!line.trim()) continue;
      let message;
      try { message = JSON.parse(line); } catch (_) { continue; }
      if (message.type === "load") {
        Object.assign(state, message.state || {});
        install(message.html);
        send({ type: "ready" });
        watchForScripts();
        new globalThis.MutationObserver(changed).observe(document_, { childList: true, subtree: true, attributes: true, characterData: true });
        runScripts().then(changed).catch(e => reportError(e));
      } else if (message.type === "answer") {
        const resolve = waiting.get(message.id); waiting.delete(message.id); resolve?.(message);
      } else {
        try { dispatch(message); } catch (e) { reportError(e); send({ type: "handled", id: message.id, prevented: false }); }
      }
    }
  }
}

// nothing of Deno is left for the page to see
const keep = { stdin: stdinReader };
delete globalThis.Deno;
main().catch(e => { try { send({ type: "console", level: "error", text: "host: " + safeString(e) }); } catch (_) {} });
void keep;
