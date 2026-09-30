// app_monitor browser client: keeps [data-bind] elements in sync with the monitor.
//
//   <span data-bind="position_x"></span>                        text (default)
//   <div data-bind="machine.estop" data-mode="state"></div>     data-state="on" | "off"
//   <div data-bind="X.velocity.ratio" data-mode="width"></div>  style.width = value * 100%
//
// Object values are flattened, so {"machine": {"estop": true}} binds as
// "machine.estop". <html data-connection> is "connecting", "open" or "closed".
//
// Events dispatched on document:
//   "monitor:snapshot"  detail = {layout, values}; fires before bindings are
//                       collected, so a listener can build the page from layout.
//   "monitor:update"    detail = the flattened values that changed.
(() => {
  "use strict";

  const root = document.documentElement;
  let bindings = new Map();

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
    document.dispatchEvent(new CustomEvent("monitor:update", { detail: flat }));
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
  connect(250);
})();
