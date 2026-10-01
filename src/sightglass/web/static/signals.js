// The signals panel: every value the dashboard has accepted, by id, as sent
// (before an element scales or formats it), with a chart of its last minute.
// For checking what a feed actually sends. sightglass.js loads this module
// the first time the panel opens: the ` key, or an element marked
// data-signals-open. In the panel, ` and Escape close it, and typing filters
// the signals by id.
//
// The panel lives in a shadow root, so the page's CSS and its own stay apart.
// It follows the page's theme (html[data-theme], html[data-crt]) and its
// color-scheme: dark on a dark-only page, otherwise as the system. While it
// is open it streams /ws/signals: a snapshot with every signal's last minute
// in fixed time slots, then the signals that changed with their slots since
// the previous message, at least once a slot. A slot is the value, [lowest,
// highest] when it varied, or null when there was no number.

const STYLESHEET = "/_sightglass/signals.css";

const MARKUP = `
  <dialog aria-labelledby="title" tabindex="-1">
    <header>
      <h2 id="title">Signals</h2>
      <span class="count"></span>
      <span class="status" role="status"></span>
      <input type="search" placeholder="Filter" aria-label="Filter signals"
             autocomplete="off" spellcheck="false">
      <button type="button" class="close">Close</button>
    </header>
    <div class="scroll">
      <table>
        <colgroup><col class="name"><col class="value"><col><col class="age"></colgroup>
        <thead>
          <tr><th scope="col">Signal</th><th scope="col">Value</th>
              <th scope="col">Last minute</th><th scope="col" class="age">Updated</th></tr>
        </thead>
        <tbody></tbody>
      </table>
      <p class="empty"></p>
    </div>
  </dialog>`;

const SVG = "http://www.w3.org/2000/svg";
const order = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

let host = null; // <sightglass-signals>, built on first open
let parts; // its dialog, table body, filter, ...
let socket = null;
let retry = null;
let ticker = null;
let frame = 0;
let observer = null;
let loaded = false; // a snapshot has arrived since the panel opened

let slots = 120; // per chart, from the server
let slot = 0; // the newest slot the charts show
const signals = new Map(); // id -> {id, row, value, chart, path, age, trace, number, arrived}
const dirty = new Set(); // signals whose chart needs drawing
const visible = new Set(); // signals whose row is (nearly) in view

export async function toggle() {
  if (host?.isConnected && parts.dialog.open) parts.dialog.close();
  else await open();
}

export async function open() {
  if (!host?.isConnected) await build();
  const { dialog } = parts;
  if (dialog.open) return;
  dialog.showModal();
  dialog.focus(); // not the filter: ` closes, and no keyboard pops up on phones
  loaded = false;
  host.dataset.connection = "connecting";
  connect(250);
  ticker = setInterval(showAges, 1000);
  render();
}

async function build() {
  host = document.createElement("sightglass-signals");
  const root = host.attachShadow({ mode: "open" });
  const style = Object.assign(document.createElement("link"), { rel: "stylesheet", href: STYLESHEET });
  const styled = new Promise((resolve) => {
    style.onload = style.onerror = resolve;
  });
  const template = document.createElement("template");
  template.innerHTML = MARKUP;
  root.append(style, template.content);
  parts = {
    dialog: root.querySelector("dialog"),
    scroll: root.querySelector(".scroll"),
    table: root.querySelector("table"),
    body: root.querySelector("tbody"),
    count: root.querySelector(".count"),
    status: root.querySelector(".status"),
    filter: root.querySelector("input"),
    empty: root.querySelector(".empty"),
  };
  document.body.append(host);

  const followTheme = () => {
    const page = document.documentElement;
    host.dataset.theme = page.dataset.theme ?? "classic";
    host.dataset.crt = page.dataset.crt ?? "off";
    const schemes = getComputedStyle(page).colorScheme.split(" ");
    const only = (scheme) => schemes.includes(scheme) && schemes.length === 1;
    host.dataset.scheme = only("dark") ? "dark" : only("light") ? "light" : "system";
  };
  followTheme();
  new MutationObserver(followTheme).observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-theme", "data-crt"],
  });

  const { dialog, filter } = parts;
  root.querySelector(".close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (event) => {
    // A click on the backdrop lands on the dialog itself, outside its box.
    if (event.target !== dialog) return;
    const box = dialog.getBoundingClientRect();
    const inside =
      event.clientX >= box.left && event.clientX <= box.right &&
      event.clientY >= box.top && event.clientY <= box.bottom;
    if (!inside) dialog.close();
  });
  dialog.addEventListener("close", shut);
  dialog.addEventListener("keydown", (event) => {
    // Just closed, the dialog can keep focus a moment: a ` then is to reopen it.
    if (!dialog.open || event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.key === "`") {
      event.preventDefault();
      event.stopPropagation(); // sightglass.js would open it again
      dialog.close();
    } else if (event.key.length === 1 && event.key !== " " && !event.target.closest("input, button")) {
      filter.focus(); // the key types into the filter
    }
  });
  filter.addEventListener("input", render);

  observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        const signal = entry.target.signal;
        if (entry.isIntersecting) visible.add(signal);
        else visible.delete(signal);
      }
      schedule();
    },
    { root: parts.scroll, rootMargin: "200px 0px" },
  );
  await styled; // not drawn before it's styled
}

function shut() {
  clearTimeout(retry);
  clearInterval(ticker);
  cancelAnimationFrame(frame);
  frame = 0;
  if (socket) {
    socket.onclose = null;
    socket.close();
    socket = null;
  }
  clear();
}

function connect(delay) {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${scheme}://${location.host}/ws/signals`);
  socket.onopen = () => {
    host.dataset.connection = "open";
    delay = 250;
    render();
  };
  socket.onmessage = (event) => receive(JSON.parse(event.data));
  socket.onclose = () => {
    host.dataset.connection = "closed";
    render();
    retry = setTimeout(() => connect(Math.min(delay * 2, 5000)), delay);
  };
}

function receive(message) {
  if (message.type === "snapshot") {
    clear();
    slots = message.slots;
    slot = message.slot;
    loaded = true;
  } else {
    roll(message.slot);
  }
  const first = slot - slots + 1; // the slot at the left edge
  const added = [];
  for (const [id, data] of Object.entries(message.signals)) {
    let signal = signals.get(id);
    if (!signal) {
      signal = create(id);
      added.push(signal);
    }
    data.trace.forEach((point, i) => {
      const at = message.from + i - first;
      if (at >= 0 && at < slots) signal.trace[at] = point;
    });
    signal.number = data.number;
    signal.arrived = Date.now() - data.age * 1000;
    signal.value.textContent = data.text;
    signal.value.title = data.text;
    signal.age.textContent = ago(signal.arrived);
    dirty.add(signal);
  }
  if (added.length || message.type === "snapshot") place();
  schedule();
}

// Move the charts on to the server's slot; a value that didn't change since
// holds through the new slots.
function roll(to) {
  const steps = to - slot;
  if (steps <= 0) return;
  for (const signal of signals.values()) {
    const { trace, number } = signal;
    trace.splice(0, Math.min(steps, slots));
    while (trace.length < slots) trace.push(number);
    dirty.add(signal);
  }
  slot = to;
}

function create(id) {
  const row = document.createElement("tr");
  const name = document.createElement("th");
  name.scope = "row";
  name.title = id;
  const dot = id.lastIndexOf(".");
  if (dot > 0) {
    const group = Object.assign(document.createElement("span"), { className: "group" });
    group.textContent = id.slice(0, dot + 1);
    name.append(group);
  }
  name.append(id.slice(dot + 1));
  const value = Object.assign(document.createElement("td"), { className: "value" });
  const chart = Object.assign(document.createElement("td"), { className: "chart" });
  const age = Object.assign(document.createElement("td"), { className: "age" });
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("viewBox", `-0.5 0 ${slots} 20`); // slot i spans i - 0.5 to i + 0.5
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(SVG, "path");
  svg.append(path);
  chart.append(svg);
  row.append(name, value, chart, age);
  const signal = { id, row, value, chart, path, age, trace: new Array(slots).fill(null) };
  row.signal = signal;
  signals.set(id, signal);
  observer.observe(row);
  return signal;
}

function clear() {
  for (const signal of signals.values()) observer.unobserve(signal.row);
  signals.clear();
  dirty.clear();
  visible.clear();
  parts.body.replaceChildren();
}

// Rows in id order, numbers in ids counted as numbers (aTemps[2] before aTemps[10]).
function place() {
  const sorted = [...signals.values()].sort(
    (a, b) => order.compare(a.id, b.id) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0),
  );
  parts.body.replaceChildren(...sorted.map((signal) => signal.row));
  render();
}

function render() {
  const { count, status, empty, table, filter } = parts;
  const query = filter.value.trim().toLowerCase();
  let shown = 0;
  for (const signal of signals.values()) {
    const hidden = query !== "" && !signal.id.toLowerCase().includes(query);
    signal.row.hidden = hidden;
    if (!hidden) shown += 1;
  }
  const total = signals.size;
  count.textContent = !loaded ? "" : query ? `${shown} of ${total}` : String(total);
  const connection = host.dataset.connection;
  status.textContent =
    connection === "closed" ? "Disconnected, retrying" : connection === "open" ? "" : "Connecting";
  table.hidden = shown === 0;
  empty.textContent = !loaded
    ? ""
    : total === 0
      ? "Nothing has been sent yet. Each value sent to this dashboard appears here, by id, with its last minute."
      : shown === 0
        ? `No signal matches “${filter.value.trim()}”.`
        : "";
  empty.hidden = empty.textContent === "";
}

function schedule() {
  if (!frame) frame = requestAnimationFrame(draw);
}

function draw() {
  frame = 0;
  for (const signal of dirty) {
    if (!visible.has(signal)) continue; // drawn when scrolled into view
    drawChart(signal);
    dirty.delete(signal);
  }
}

// One line, left to right. Each slot runs from its left edge to its right,
// between its lowest and highest values, starting from the end nearer where
// the line is: a steady value is flat, a switch is one step, and a value that
// swings within half a second still shows its whole swing.
function drawChart(signal) {
  const { trace, path, chart } = signal;
  let low = Infinity;
  let high = -Infinity;
  for (const point of trace) {
    if (point === null) continue;
    low = Math.min(low, Array.isArray(point) ? point[0] : point);
    high = Math.max(high, Array.isArray(point) ? point[1] : point);
  }
  if (low > high) {
    path.removeAttribute("d");
    chart.removeAttribute("title");
    return;
  }
  const span = high - low;
  const y = (v) => Math.round((span ? 19 - ((v - low) / span) * 18 : 10) * 100) / 100;
  let d = "";
  let at = null; // where the line is; null at the start and after a gap
  trace.forEach((point, i) => {
    if (point === null) {
      at = null;
      return;
    }
    let [from, to] = Array.isArray(point) ? point.map(y) : [y(point), y(point)];
    if (at !== null && Math.abs(at - to) < Math.abs(at - from)) [from, to] = [to, from];
    if (from !== at) d += `${at === null ? "M" : "L"}${i - 0.5},${from}`;
    d += `L${i + 0.5},${to}`;
    at = to;
  });
  path.setAttribute("d", d);
  chart.title = span ? `Lowest ${short(low)}, highest ${short(high)}` : `Steady at ${short(low)}`;
}

const short = (number) => String(Number(number.toPrecision(6)));

function showAges() {
  for (const signal of visible) signal.age.textContent = ago(signal.arrived);
}

function ago(arrived) {
  const seconds = (Date.now() - arrived) / 1000;
  if (seconds < 1) return "now";
  if (seconds < 90) return `${Math.floor(seconds)} s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
  return `${Math.round(seconds / 3600)} h`;
}
