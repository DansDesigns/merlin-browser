// Merlin Engine's JavaScript host: one per tab, run by Deno with no permissions.
//
// It holds the page's DOM (linkedom), runs the page's scripts against it, and
// talks to Merlin one JSON message per line on stdin and stdout. Merlin does
// everything that reaches outside: fetching (with the page's cookies, through
// the content blocker), storage, navigation. After scripts change the page,
// the new page is sent back for Merlin Engine to style and lay out.
import { parseHTML } from "./linkedom.bundle.js";
import vm from "node:vm";

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
    if (level === "debug") return;
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
const geometry = new Map();      // element id -> [x, y, width, height, fixed] in page terms
const observers = new Set();     // the live IntersectionObservers
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
  // what a browser's document has and linkedom's did not: GitHub's scripts
  // stopped on "Unable to get document domain"
  const documentProperties = {
    domain: () => new URL(state.url).hostname, characterSet: () => "UTF-8", charset: () => "UTF-8",
    inputEncoding: () => "UTF-8", compatMode: () => "CSS1Compat", contentType: () => "text/html",
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
        const visible = (rect.width > 0 || rect.height > 0) && w * h > 0 || (area === 0 && rect.bottom >= view.top && rect.top <= view.bottom && rect.height + rect.width > 0);
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
  // document.styleSheets: the page's stylesheets, a live list, as Google's
  // CSS loader reads its length. A rule inserted into a <style> sheet is added
  // to its text, so Merlin Engine styles the page with it.
  const sheets = new WeakMap();
  const sheetOf = (node) => {
    let sheet = sheets.get(node);
    if (sheet) return sheet;
    const rules = [];
    sheet = {
      ownerNode: node, type: "text/css", disabled: false, title: node.getAttribute("title"),
      get href() { return node.tagName === "LINK" ? new URL(node.getAttribute("href") || "", state.url).href : null; },
      media: { mediaText: node.getAttribute("media") || "", length: 0 },
      get cssRules() { return rules; }, get rules() { return rules; },
      insertRule(text, index = 0) {
        rules.splice(index, 0, { cssText: String(text) });
        if (node.tagName === "STYLE") node.textContent = (node.textContent || "") + "\n" + String(text);
        return index;
      },
      deleteRule(index) { rules.splice(index, 1); },
      addRule(selector, body, index) { return this.insertRule(`${selector} { ${body} }`, index ?? rules.length); },
      removeRule(index) { this.deleteRule(index ?? 0); },
    };
    sheets.set(node, sheet);
    return sheet;
  };
  try {
    Object.defineProperty(document_, "styleSheets", { configurable: true, get() {
      return [...document_.querySelectorAll("style, link[rel~=stylesheet]")].map(sheetOf);
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
    registry.define = (name, constructor, options) => {
      if (registry.get(name)) {
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
  // Node's globals, which Deno has and no browser does, go: GitHub's code saw
  // process, took itself to be in Node, and failed reading process.env
  for (const name of ["process", "Buffer", "global", "setImmediate", "clearImmediate"]) {
    try { delete g[name]; } catch (_) {}
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
  for (const [name, index] of [["offsetWidth", 2], ["offsetHeight", 3], ["clientWidth", 2], ["clientHeight", 3],
                               ["scrollWidth", 2], ["scrollHeight", 3]]) {
    Object.defineProperty(proto, name, { configurable: true, get() { const box = geometry.get(ids.get(this)); return box ? Math.round(box[index]) : 0; } });
  }
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
  // on, as in a browser. Deno ends the process on one by default: one error in
  // GitHub's scripts had ended all of them.
  g.addEventListener("error", (event) => { event.preventDefault?.(); g.reportError(event.error ?? event.message); });
  g.addEventListener("unhandledrejection", (event) => { event.preventDefault?.(); g.reportError(event.reason); });
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
        const next = text.replace(pattern, ALWAYS.has(name) ? "" : ":not(*)");
        if (next === text) throw error;
        text = next;
      }
    }
    return original.call(this, text, ...rest);
  };
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

// import() in a plain script, or an inline module, is resolved against the
// page's address, as in a browser. Deno resolved it against this file on the
// disk: Hugging Face's import("/front/build/...") asked to read C:\front\...
globalThis.__merlinImport = (specifier) => import(new URL(String(specifier), state.url).href);
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
      const response = await merlinFetch(src, { credentials: "same-origin" });
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
  try { new vm.Script(pageImports(code, false), { filename: name }).runInThisContext(); }
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
    if (src) await withinTime(import(new URL(src, state.url).href), 20, src);
    else await withinTime(import("data:text/javascript;charset=utf-8," + encodeURIComponent(pageImports(script.textContent, true))), 20, "inline module");
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
    fire(globalThis, "scroll"); fire(document_, "scroll");
    for (const observer of observers) observer._check();
    return;
  }
  if (message.type === "geometry") {
    geometry.clear();
    for (const [id, box] of Object.entries(message.boxes || {})) geometry.set(Number(id), box);
    if (message.height) state.height = message.height;
    for (const observer of observers) observer._check();
    return;
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
