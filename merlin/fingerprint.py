"""GPU fingerprinting protections.

WebGL and WebGPU report the exact graphics card, driver and feature set, which
is stable for years and highly distinctive: a strong way to recognise the same
machine across unrelated sites without cookies.

This follows the approach Brave shipped in 1.93, which is described at
https://brave.com/privacy-updates/38-webgl-webgpu-fingerprinting-protections/
and implemented in brave-core (MPL 2.0). The three measures are theirs; the
code here is written from the description rather than copied.

  1. WebGL vendor and renderer strings are replaced with one generic value, so
     every Merlin user reports the same thing.
  2. WebGPU adapter descriptors are emptied, removing architecture and device.
  3. The WebGL extension list is randomised per site, so a hash of it is not
     stable across sites.

The first two remove entropy. The third does the opposite: it adds noise, so a
tracker hashing the list gets a different answer on every site and cannot join
them up. The randomisation is seeded per session and per eTLD+1, matching
Brave's scope, so a single site sees a consistent list while it is open and two
sites never see the same one.

Only diagnostic extensions are ever withheld. Dropping a real capability would
break the page, which is the opposite of useful.
"""
from __future__ import annotations

import secrets

# What every Merlin user reports. Deliberately plausible and deliberately
# uninformative: something has to be returned, or sites that read it break.
GENERIC_VENDOR = "Merlin"
GENERIC_RENDERER = "Merlin Graphics"

# Safe to leave out: these report on the driver or help debug shaders, and no
# page needs them to draw. Anything that affects rendering is never touched.
OPTIONAL_EXTENSIONS = [
    "WEBGL_debug_renderer_info",
    "WEBGL_debug_shaders",
    "EXT_disjoint_timer_query",
    "EXT_disjoint_timer_query_webgl2",
]

SCRIPT = r"""
(function () {
  'use strict';
  if (window.__merlinGpuGuard) { return; }
  window.__merlinGpuGuard = true;

  var SESSION = '%(seed)s';
  var VENDOR = %(vendor)s;
  var RENDERER = %(renderer)s;
  var OPTIONAL = %(optional)s;

  // eTLD+1, near enough without shipping the public suffix list: the last two
  // labels, or three when the second last is a known two-part suffix.
  function siteKey() {
    var host = '';
    try { host = String(location.hostname || ''); } catch (e) { host = ''; }
    var parts = host.split('.').filter(Boolean);
    if (parts.length <= 2) { return host; }
    var twoPart = ['co', 'com', 'org', 'net', 'gov', 'edu', 'ac', 'sch'];
    var last = parts[parts.length - 1];
    var beforeLast = parts[parts.length - 2];
    if (last.length === 2 && twoPart.indexOf(beforeLast) !== -1) {
      return parts.slice(-3).join('.');
    }
    return parts.slice(-2).join('.');
  }

  // A small deterministic hash, so the same site and session always agree.
  function seeded(text) {
    var h = 2166136261;
    for (var i = 0; i < text.length; i++) {
      h ^= text.charCodeAt(i);
      h = (h * 16777619) >>> 0;
    }
    return function () {
      h ^= h << 13; h >>>= 0;
      h ^= h >> 17;
      h ^= h << 5;  h >>>= 0;
      return h / 4294967296;
    };
  }

  var random = seeded(SESSION + '|' + siteKey());

  // ---- 1 and 3: WebGL -----------------------------------------------------
  var UNMASKED_VENDOR = 0x9245;      // 37445
  var UNMASKED_RENDERER = 0x9246;    // 37446
  var VENDOR_ENUM = 0x1F00;          // 7936
  var RENDERER_ENUM = 0x1F01;        // 7937

  function guardContext(proto) {
    if (!proto || proto.__merlinGuarded) { return; }
    proto.__merlinGuarded = true;

    var realGetParameter = proto.getParameter;
    proto.getParameter = function (parameter) {
      if (parameter === UNMASKED_VENDOR || parameter === VENDOR_ENUM) {
        return VENDOR;
      }
      if (parameter === UNMASKED_RENDERER || parameter === RENDERER_ENUM) {
        return RENDERER;
      }
      return realGetParameter.apply(this, arguments);
    };

    var realExtensions = proto.getSupportedExtensions;
    proto.getSupportedExtensions = function () {
      var list = realExtensions.apply(this, arguments);
      if (!list) { return list; }
      var kept = [];
      for (var i = 0; i < list.length; i++) {
        // withhold some diagnostic extensions, never a rendering one
        if (OPTIONAL.indexOf(list[i]) !== -1 && random() < 0.5) { continue; }
        kept.push(list[i]);
      }
      // and shuffle, so the order carries nothing either
      for (var j = kept.length - 1; j > 0; j--) {
        var k = Math.floor(random() * (j + 1));
        var swap = kept[j]; kept[j] = kept[k]; kept[k] = swap;
      }
      return kept;
    };
  }

  if (window.WebGLRenderingContext) {
    guardContext(WebGLRenderingContext.prototype);
  }
  if (window.WebGL2RenderingContext) {
    guardContext(WebGL2RenderingContext.prototype);
  }

  // ---- 2: WebGPU ----------------------------------------------------------
  try {
    if (navigator.gpu && navigator.gpu.requestAdapter) {
      var blank = {vendor: '', architecture: '', device: '', description: ''};

      function scrubAdapter(adapter) {
        if (!adapter) { return adapter; }
        try {
          Object.defineProperty(adapter, 'info', {
            get: function () { return blank; },
            configurable: true,
          });
        } catch (e) { /* some builds make info non-configurable */ }
        if (adapter.requestAdapterInfo) {
          adapter.requestAdapterInfo = function () {
            return Promise.resolve(blank);
          };
        }
        return adapter;
      }

      var realRequestAdapter = navigator.gpu.requestAdapter.bind(navigator.gpu);
      navigator.gpu.requestAdapter = function () {
        return realRequestAdapter.apply(null, arguments).then(scrubAdapter);
      };
    }
  } catch (e) { /* no WebGPU here */ }
})();
"""


def build_script(session_seed: str) -> str:
    """The protection script, with this session's seed baked in."""
    import json

    return SCRIPT % {
        "seed": session_seed,
        "vendor": json.dumps(GENERIC_VENDOR),
        "renderer": json.dumps(GENERIC_RENDERER),
        "optional": json.dumps(OPTIONAL_EXTENSIONS),
    }


def new_seed() -> str:
    """A fresh seed each time Merlin starts, so lists differ between sessions."""
    return secrets.token_hex(8)
