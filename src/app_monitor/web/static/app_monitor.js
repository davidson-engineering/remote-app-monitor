// app_monitor browser client: keeps [data-bind] elements in sync with the monitor.
//
//   <span data-bind="position_x"></span>                            text (default)
//   <div data-bind="machine.estop" data-mode="state"></div>         data-state="on" | "off"
//   <div data-bind="X.velocity.ratio" data-mode="width"></div>      style.width = value * 100%
//   <div data-bind="rate.values" data-mode="sparkline"></div>       line chart of a number list
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
  let lastData = null;
  let staleAfter = null;

  function collectBindings() {
    bindings = new Map();
    for (const el of document.querySelectorAll("[data-bind]")) {
      const key = el.dataset.bind;
      if (!bindings.has(key)) bindings.set(key, []);
      bindings.get(key).push(el);
    }
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
    const span = Math.max(...numbers) - low || 1;
    const step = 100 / (numbers.length - 1);
    line.setAttribute(
      "points",
      numbers.map((v, i) => `${(i * step).toFixed(2)},${(19 - ((v - low) / span) * 18).toFixed(2)}`).join(" "),
    );
  }
  function show(el, value) {
    switch (el.dataset.mode) {
      case "state":
        el.dataset.state = value ? "on" : "off";
        break;
      case "width": {
        const ratio = typeof value === "number" ? Math.min(Math.max(value, 0), 1) : 0;
        el.style.width = `${ratio * 100}%`;
        break;
      }
      case "sparkline":
        drawSparkline(el, value);
        break;
      default:
        el.textContent = value ?? "";
    }
  }

  function apply(values) {
    const flat = {};
    for (const [id, value] of Object.entries(values)) flatten(id, value, flat);
    for (const [key, value] of Object.entries(flat)) {
      for (const el of bindings.get(key) ?? []) show(el, value);
    }
    lastData = Date.now();
    tick();
    document.dispatchEvent(new CustomEvent("monitor:update", { detail: flat }));
  }

  function tick() {
    const age = lastData === null ? null : (Date.now() - lastData) / 1000;
    const stale = staleAfter !== null && age !== null && age > staleAfter;
    if (stale) root.dataset.stale = "true";
    else delete root.dataset.stale;
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
      apply(message.values);
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
