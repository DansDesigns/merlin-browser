"""Behavioural-biometrics protections for Chromium tabs.

Behavioural biometrics recognise a person by how they use the machine rather
than by what it is: the rhythm of their typing (how long each key is held, the
gaps between keys), how the mouse moves, and on laptops and tablets how the
device is held and tilted. Banks use it against fraud; analytics and ad
companies use it to follow people.

Merlin's protections, from Settings > Shields:

  * the companies that do this are blocked (adblock.BEHAVIOURAL_RULES)
  * motion and orientation sensors, and the battery, are gone from pages:
    SENSOR_SCRIPT, on by default. Desktop sites have no use for them.
  * Blur timing, off by default: the page's clocks tell time in steps of 100
    milliseconds, as Tor Browser's do, so the typing rhythm is lost:
    TIMING_SCRIPT. JavaScript animations step at the same rate, which is why it
    is a choice rather than the default.

Merlin Engine's tabs do the same in js/host.js (and its workers).
"""
from __future__ import annotations

# Motion and orientation (devicemotion, deviceorientation and the Generic
# Sensor API), ambient light, and the battery, gone before a page's own
# scripts run. A site asking whether they exist is told no; one listening for
# the events anyway hears nothing, as on a computer without the sensors.
SENSOR_SCRIPT = r"""
(function () {
  'use strict';
  var w = window;
  var names = ['DeviceMotionEvent', 'DeviceOrientationEvent', 'DeviceMotionEventAcceleration',
               'DeviceMotionEventRotationRate', 'Sensor', 'OrientationSensor', 'Accelerometer',
               'LinearAccelerationSensor', 'GravitySensor', 'Gyroscope', 'Magnetometer',
               'AbsoluteOrientationSensor', 'RelativeOrientationSensor', 'AmbientLightSensor',
               'SensorErrorEvent', 'BatteryManager'];
  for (var i = 0; i < names.length; i++) {
    try { delete w[names[i]]; } catch (e) {}
  }
  var handlers = ['ondevicemotion', 'ondeviceorientation', 'ondeviceorientationabsolute'];
  for (var j = 0; j < handlers.length; j++) {
    try { delete Window.prototype[handlers[j]]; } catch (e) {}
    try { delete w[handlers[j]]; } catch (e) {}
  }
  try { delete Navigator.prototype.getBattery; } catch (e) {}
  // listened for anyway: this listener is the first, and nothing after it
  // hears the event
  var events = ['devicemotion', 'deviceorientation', 'deviceorientationabsolute'];
  for (var k = 0; k < events.length; k++) {
    try {
      w.addEventListener(events[k], function (event) { event.stopImmediatePropagation(); }, true);
    } catch (e) {}
  }
})();
"""

# Tor Browser's precision, in milliseconds
STEP_MS = 100

TIMING_SCRIPT = r"""
(function () {
  'use strict';
  var STEP = %(step)d;
  function step(t) { return Math.floor(t / STEP) * STEP; }
  var w = window;

  // performance.now(), and the times of performance entries (marks, measures)
  try {
    var realNow = Performance.prototype.now;
    Performance.prototype.now = function now() { return step(realNow.call(this)); };
  } catch (e) {}
  ['startTime', 'duration'].forEach(function (name) {
    try {
      var found = Object.getOwnPropertyDescriptor(PerformanceEntry.prototype, name);
      if (found && found.get) {
        Object.defineProperty(PerformanceEntry.prototype, name, {
          configurable: true, enumerable: true,
          get: function () { return step(found.get.call(this)); } });
      }
    } catch (e) {}
  });

  // Date.now() and new Date()
  try {
    var RealDate = Date, realDateNow = Date.now;
    var MerlinDate = function Date() {
      if (!new.target) { return RealDate(); }
      var args = Array.prototype.slice.call(arguments);
      return Reflect.construct(RealDate, args.length ? args : [step(realDateNow())], new.target);
    };
    MerlinDate.prototype = RealDate.prototype;
    Object.setPrototypeOf(MerlinDate, RealDate);
    MerlinDate.now = function now() { return step(realDateNow()); };
    MerlinDate.UTC = RealDate.UTC;
    MerlinDate.parse = RealDate.parse;
    Object.defineProperty(RealDate.prototype, 'constructor',
                          { value: MerlinDate, configurable: true, writable: true });
    w.Date = MerlinDate;
  } catch (e) {}

  // an event's timeStamp: when a key went down and came up
  try {
    var stamp = Object.getOwnPropertyDescriptor(Event.prototype, 'timeStamp');
    if (stamp && stamp.get) {
      Object.defineProperty(Event.prototype, 'timeStamp', {
        configurable: true, enumerable: true,
        get: function () { return step(stamp.get.call(this)); } });
    }
  } catch (e) {}

  // the time given to animation frames
  try {
    var realFrame = w.requestAnimationFrame;
    w.requestAnimationFrame = function requestAnimationFrame(callback) {
      if (typeof callback !== 'function') { return realFrame.call(w, callback); }
      return realFrame.call(w, function (t) { return callback(step(t)); });
    };
  } catch (e) {}
})();
"""


def sensor_script() -> str:
    return SENSOR_SCRIPT


def timing_script(step_ms: int = STEP_MS) -> str:
    return TIMING_SCRIPT % {"step": int(step_ms)}


def install_page_guards(profile, settings) -> list:
    """Put Merlin's page protections into a Chromium profile: the graphics
    card hidden, sensors gone, timing blurred, each as Settings > Shields has
    it. Run before a page's own scripts, in its world, in every frame. Gives
    back the names installed."""
    from PyQt6.QtWebEngineCore import QWebEngineScript

    from . import fingerprint

    wanted = []
    if settings.get("gpu_protection", True):
        wanted.append(("merlin-gpu-guard", fingerprint.build_script(fingerprint.new_seed())))
    if settings.get("block_motion_sensors", True):
        wanted.append(("merlin-sensor-guard", sensor_script()))
    if settings.get("blur_timing", False):
        wanted.append(("merlin-timing-guard", timing_script()))
    collection = profile.scripts()
    for name, source in wanted:
        guard = QWebEngineScript()
        guard.setName(name)
        guard.setSourceCode(source)
        # before the page's own scripts, or a tracker could read the real
        # values first, and in the page's world so that page scripts see it
        guard.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        guard.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        guard.setRunsOnSubFrames(True)
        collection.insert(guard)
    return [name for name, _ in wanted]


def refresh_page_guards(profile, settings) -> list:
    """The guards again, after a setting changed: for pages loaded from now
    on (a page already open keeps what it was given)."""
    collection = profile.scripts()
    for name in ("merlin-gpu-guard", "merlin-sensor-guard", "merlin-timing-guard"):
        for script in list(collection.find(name)):
            collection.remove(script)
    return install_page_guards(profile, settings)
