// app_monitor browser client: keeps [data-bind] elements in sync with the monitor.
//
//   <span data-bind="position_x"></span>                            text (default)
//   <div data-bind="machine.estop" data-mode="state"></div>         data-state="on" | "off"
//                                                                   (text "off", "0", "false" count as off)
//   <div data-bind="X.velocity.ratio" data-mode="width"></div>      style.width = value * 100%
//   <div data-bind="rate.values" data-mode="sparkline"></div>       line chart of a number list
//   <div data-bind="q.ratio" data-mode="var"></div>                 CSS variable --value: the number
//                                                                   (unset if not a number; true = 1)
//
// "var" lets CSS draw anything from a number: a needle rotated by
// calc(var(--value) * 270deg), a tank filled to calc(var(--value) * 100%).
//
// Any bound element may also have data-stale-after="<seconds>": it gets
// data-stale="true" while its value hasn't arrived for that long (or ever),
// e.g. to show that one of several feeds has gone quiet. A value sent again
// unchanged still counts as arriving. While the page is disconnected it
// can't tell, so the attribute stays as it was until it reconnects.
//
// Object values are flattened, so {"machine": {"estop": true}} binds as
// "machine.estop". State is exposed on <html>:
//   data-connection  "connecting" | "open" | "closed"
//   data-stale       "true" once no data has arrived for the dashboard's
//                    stale_after seconds (only if it sets one)
//
// Events dispatched on document:
//   "monitor:snapshot"  detail = {title, layout, values}; fires before bindings
//                       are collected, so a listener can build the page from
//                       layout. Sent on connect and whenever elements are added.
//   "monitor:update"    detail = the flattened values that changed.
//   "monitor:tick"      every second; detail = {age}: seconds since data arrived.
(() => {
  "use strict";

  const root = document.documentElement;
  let bindings = new Map();
  let watched = []; // bound elements with data-stale-after
  const arrived = new Map(); // bound key -> Date.now() when its value last arrived
  let lastData = null;
  let staleAfter = null;

  function collectBindings() {
    bindings = new Map();
    for (const el of document.querySelectorAll("[data-bind]")) {
      const key = el.dataset.bind;
      if (!bindings.has(key)) bindings.set(key, []);
      bindings.get(key).push(el);
    }
    watched = [...document.querySelectorAll("[data-bind][data-stale-after]")];
  }

  function flatten(key, value, out) {
    if (value !== null && typeof value === "object" && !Array.isArray(value)) {
      for (const [field, inner] of Object.entries(value)) {
        flatten(`${key}.${field}`, inner, out);
      }
    } else {
      out[key] = value;
    }
    return out;
  }

  const SVG = "http://www.w3.org/2000/svg";

  function drawSparkline(el, values) {
    let line = el.querySelector("polyline");
    if (!line) {
      const svg = document.createElementNS(SVG, "svg");
      svg.setAttribute("viewBox", "0 0 100 20");
      svg.setAttribute("preserveAspectRatio", "none");
      svg.setAttribute("width", "100%");
      svg.setAttribute("height", "100%");
      line = document.createElementNS(SVG, "polyline");
      line.setAttribute("fill", "none");
      line.setAttribute("stroke", "currentColor");
      line.setAttribute("stroke-width", "1.5");
      line.setAttribute("stroke-linejoin", "round");
      line.setAttribute("vector-effect", "non-scaling-stroke");
      svg.append(line);
      el.replaceChildren(svg);
    }
    const numbers = Array.isArray(values) ? values.filter(Number.isFinite) : [];
    if (numbers.length < 2) {
      line.setAttribute("points", "");
      return;
    }
    const low = Math.min(...numbers);
    const span = Math.max(...numbers) - low;
    const step = 100 / (numbers.length - 1);
    const y = (v) => (span ? 19 - ((v - low) / span) * 18 : 10); // steady: mid-height
    line.setAttribute(
      "points",
      numbers.map((v, i) => `${(i * step).toFixed(2)},${y(v).toFixed(2)}`).join(" "),
    );
  }
  // Numbers for CSS; text from devices ("+45.5") counts, "" and "n/a" don't.
  function asNumber(value) {
    if (typeof value === "boolean") return Number(value);
    if (typeof value === "string" && value.trim() !== "") value = Number(value);
    return typeof value === "number" && Number.isFinite(value) ? value : null;
  }

  // Text sources (curl, serial, pipes) send "off" / "0": those are off too.
  const OFF = new Set(["", "0", "false", "off", "no"]);
  const isOn = (value) =>
    typeof value === "string" ? !OFF.has(value.trim().toLowerCase()) : Boolean(value);

  function show(el, value) {
    switch (el.dataset.mode) {
      case "state":
        el.dataset.state = isOn(value) ? "on" : "off";
        break;
      case "width": {
        const ratio = typeof value === "number" ? Math.min(Math.max(value, 0), 1) : 0;
        el.style.width = `${ratio * 100}%`;
        break;
      }
      case "sparkline":
        drawSparkline(el, value);
        break;
      case "var": {
        const number = asNumber(value);
        if (number === null) el.style.removeProperty("--value");
        else el.style.setProperty("--value", number);
        break;
      }
      default:
        el.textContent = value ?? "";
    }
  }

  // ages: seconds since each element was updated (null: never), sent with
  // snapshots so a page that has just connected knows what's old; values in
  // updates have just arrived.
  function apply(values, ages = {}) {
    const now = Date.now();
    const flat = {};
    for (const [id, value] of Object.entries(values)) {
      const age = ages[id] === undefined ? 0 : ages[id];
      for (const [key, inner] of Object.entries(flatten(id, value, {}))) {
        flat[key] = inner;
        if (age === null) arrived.delete(key);
        else arrived.set(key, now - age * 1000);
      }
    }
    for (const [key, value] of Object.entries(flat)) {
      for (const el of bindings.get(key) ?? []) show(el, value);
    }
    lastData = now;
    tick();
    document.dispatchEvent(new CustomEvent("monitor:update", { detail: flat }));
  }

  function tick() {
    const now = Date.now();
    const age = lastData === null ? null : (now - lastData) / 1000;
    const stale = staleAfter !== null && age !== null && age > staleAfter;
    if (stale) root.dataset.stale = "true";
    else delete root.dataset.stale;
    // Cut off from the dashboard, the page can't tell whether values still
    // arrive there: keep what it last knew until it reconnects.
    for (const el of root.dataset.connection === "open" ? watched : []) {
      const since = arrived.get(el.dataset.bind);
      if (since === undefined || now - since > Number(el.dataset.staleAfter) * 1000) {
        el.dataset.stale = "true";
      } else {
        delete el.dataset.stale;
      }
    }
    document.dispatchEvent(new CustomEvent("monitor:tick", { detail: { age } }));
  }

  function connect(retryDelay) {
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(`${scheme}://${location.host}/ws`);
    socket.onopen = () => {
      root.dataset.connection = "open";
      retryDelay = 250;
    };
    socket.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (message.type === "snapshot") {
        staleAfter = message.staleAfter ?? null;
        document.dispatchEvent(new CustomEvent("monitor:snapshot", { detail: message }));
        collectBindings();
      }
      apply(message.values, message.ages);
    };
    socket.onclose = () => {
      root.dataset.connection = "closed";
      setTimeout(() => connect(Math.min(retryDelay * 2, 5000)), retryDelay);
    };
  }

  root.dataset.connection = "connecting";
  setInterval(tick, 1000);
  connect(250);
})();
