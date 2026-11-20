"use strict";

// llm-console: tabs and section cards built from /api/config; Run/Stop start a
// job on the server, then the card polls it until it ends.

const POLL_MS = 2000;
const POST_HEADERS = { "Content-Type": "application/json", "X-LLM-Console": "1" };
const TAB_KEY = "llm-console.tab";

const ACTION_TEXT = {
  running: (label) => `${label} running…`,
  succeeded: (label) => `${label} done`,
  failed: (label, job) => `${label} failed` + (job.exit_code != null ? ` (exit ${job.exit_code})` : ""),
  timeout: (label) => `${label} timed out`,
};

const ICON = { succeeded: "✓", failed: "✗", timeout: "⏱" };

function storageGet(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}

function storageSet(key, value) {
  try { localStorage.setItem(key, value); } catch { /* private mode: not remembered */ }
}

async function api(method, path, body) {
  const options = { method, headers: method === "GET" ? {} : POST_HEADERS };
  if (body !== undefined) options.body = JSON.stringify(body);
  const res = await fetch(path, options);
  let data = null;
  try { data = await res.json(); } catch { /* not JSON */ }
  return { ok: res.ok, status: res.status, data };
}

function errorMessage(res) {
  const detail = res.data && res.data.detail;
  if (typeof detail === "string") return detail;
  if (detail && detail.message) return detail.message;
  return `Errore HTTP ${res.status}`;
}

function timeOf(iso) {
  return iso ? new Date(iso).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "";
}

class SectionCard {
  constructor(tab, section, dialogs) {
    this.tab = tab;
    this.section = section;
    this.dialogs = dialogs;
    this.base = `/api/sections/${encodeURIComponent(tab.id)}/${encodeURIComponent(section.id)}`;
    this.job = null;
    this.pollTimer = null;
    this.pending = false;

    const node = document.getElementById("section-template").content.firstElementChild.cloneNode(true);
    this.el = node;
    node.querySelector(".card-title").textContent = section.label;
    node.querySelector(".card-host").textContent = `${tab.label} · ${tab.host.address}`;
    this.runBtn = node.querySelector('[data-action="run"]');
    this.stopBtn = node.querySelector('[data-action="stop"]');
    this.statusEl = node.querySelector(".card-status");
    this.outputEl = node.querySelector(".card-output");
    this.preEl = node.querySelector(".card-output pre");
    const configured = section.can_run || section.can_stop;
    node.querySelector(".card-note").hidden = configured;
    this.statusEl.hidden = !configured;

    this.runBtn.addEventListener("click", () => this.requestRun());
    this.stopBtn.addEventListener("click", () => this.start("stop"));
    this.refreshButtons();
  }

  get busy() {
    return this.pending || (this.job !== null && this.job.state === "running");
  }

  refreshButtons() {
    this.runBtn.disabled = !this.section.can_run || this.busy;
    this.stopBtn.disabled = !this.section.can_stop || this.busy;
  }

  async loadLastJob() {
    if (!this.section.can_run && !this.section.can_stop) return;
    const res = await api("GET", `${this.base}/job`).catch(() => null);
    if (res && res.ok && res.data.job) this.showJob(res.data.job);
  }

  // Labels of the sections that cannot be on together with this one.
  get exclusiveLabels() {
    const ids = this.section.exclusive_with || [];
    return this.tab.sections.filter((s) => ids.includes(s.id)).map((s) => s.label).join(" and ");
  }

  // Run: if another engine that excludes this one is on, say so instead of
  // opening the Run dialog. The server checks again when the Run is sent.
  async requestRun() {
    if (this.busy) return;
    if (!this.exclusiveLabels) {
      this.dialogs.run.open(this);
      return;
    }
    const saved = this.saveStatus();
    this.pending = true;
    this.refreshButtons();
    this.setStatus("running", `Checking that ${this.exclusiveLabels} is off…`);
    let res;
    try {
      res = await api("GET", `${this.base}/conflicts`);
    } catch {
      res = { ok: false, status: 0, data: null };
    } finally {
      this.pending = false;
      this.refreshButtons();
    }
    if (!res.ok) {
      this.setStatus("error", res.status ? errorMessage(res) : "Server unreachable");
      return;
    }
    saved();
    if (res.data.conflicts.length > 0) this.dialogs.notice.show(this, res.data.conflicts);
    else this.dialogs.run.open(this);
  }

  // Returns a function that puts the status line back as it is now.
  saveStatus() {
    const state = this.statusEl.dataset.state;
    const nodes = [...this.statusEl.childNodes];
    return () => {
      if (state === undefined) delete this.statusEl.dataset.state;
      else this.statusEl.dataset.state = state;
      this.statusEl.replaceChildren(...nodes);
    };
  }

  async start(action, variant) {
    if (this.busy) return;
    const saved = this.saveStatus();
    this.pending = true;
    this.refreshButtons();
    this.setStatus("running", `${action === "run" ? `Run ${variant}` : "Stop"}: sending…`);
    try {
      const res = await api("POST", `${this.base}/${action}`, action === "run" ? { variant } : undefined);
      if (res.status === 409 && res.data && res.data.detail && res.data.detail.conflicts) {
        // Another engine was switched on after the dialog opened.
        saved();
        this.dialogs.notice.show(this, res.data.detail.conflicts);
      } else if (res.status === 409 && res.data && res.data.detail && res.data.detail.job) {
        // Someone else (another tab, another device) started one: follow that.
        this.showJob(res.data.detail.job);
        this.setStatus("error", errorMessage(res));
      } else if (!res.ok) {
        this.setStatus("error", errorMessage(res));
      } else {
        this.showJob(res.data.job);
      }
    } catch (err) {
      this.setStatus("error", `Server unreachable: ${err.message}`);
    } finally {
      this.pending = false;
      this.refreshButtons();
    }
  }

  showJob(job) {
    this.job = job;
    const label = job.action === "run" ? `Run ${job.variant}` : "Stop";
    const text = ACTION_TEXT[job.state](label, job);
    const when = job.state === "running" ? `since ${timeOf(job.started_at)}` : timeOf(job.ended_at);
    this.setStatus(job.state, `${text} · ${when}`);

    this.outputEl.hidden = false;
    const atBottom = this.preEl.scrollHeight - this.preEl.scrollTop - this.preEl.clientHeight < 8;
    this.preEl.textContent = job.output_tail || "(no output)";
    if (atBottom) this.preEl.scrollTop = this.preEl.scrollHeight;
    if (job.state === "failed" || job.state === "timeout") this.outputEl.open = true;

    this.refreshButtons();
    clearTimeout(this.pollTimer);
    if (job.state === "running") this.pollTimer = setTimeout(() => this.poll(), POLL_MS);
  }

  async poll() {
    try {
      const res = await api("GET", `/api/jobs/${encodeURIComponent(this.job.id)}`);
      if (res.ok) {
        this.showJob(res.data);
        return;
      }
      if (res.status === 404) {
        // The server restarted and forgot the job.
        this.job = null;
        this.setStatus("error", "Operation status lost (server restarted?)");
        this.refreshButtons();
        return;
      }
      this.setStatus("error", `${errorMessage(res)}: retrying…`, true);
    } catch {
      this.setStatus("error", "Connection lost: retrying…", true);
    }
    this.pollTimer = setTimeout(() => this.poll(), POLL_MS);
  }

  setStatus(state, text, spinner = state === "running") {
    this.statusEl.dataset.state = state;
    this.statusEl.replaceChildren();
    if (spinner) {
      const s = document.createElement("span");
      s.className = "spinner";
      s.setAttribute("aria-hidden", "true");
      this.statusEl.append(s);
    } else if (ICON[state]) {
      this.statusEl.append(`${ICON[state]} `);
    }
    this.statusEl.append(text);
  }
}

class RunDialog {
  constructor() {
    this.el = document.getElementById("run-dialog");
    this.form = document.getElementById("run-form");
    this.radios = document.getElementById("run-radios");
    this.confirm = document.getElementById("run-confirm");
    this.card = null;

    document.getElementById("run-cancel").addEventListener("click", () => this.el.close());
    this.radios.addEventListener("change", () => { this.confirm.disabled = !this.selected(); });
    // A click on the backdrop (outside the form) closes the dialog.
    this.el.addEventListener("click", (e) => { if (e.target === this.el) this.el.close(); });
    this.form.addEventListener("submit", (e) => {
      e.preventDefault();
      const variant = this.selected();
      if (!variant || !this.card) return;
      this.el.close();
      this.card.start("run", variant);
    });
  }

  selected() {
    const input = this.radios.querySelector("input:checked");
    return input ? input.value : null;
  }

  open(card) {
    this.card = card;
    document.getElementById("run-title").textContent = `Run ${card.section.label}`;
    document.getElementById("run-host").textContent =
      `${card.tab.label} · ${card.tab.host.user}@${card.tab.host.address}`;
    const preselected = card.section.default_variant;
    this.radios.replaceChildren(...card.section.variants.map((variant) => {
      const label = document.createElement("label");
      label.className = "radio";
      const input = document.createElement("input");
      input.type = "radio";
      input.name = "variant";
      input.value = variant.id;
      input.checked = variant.id === preselected;
      label.append(input, variant.label);
      return label;
    }));
    this.confirm.disabled = !this.selected();
    this.el.showModal();
  }
}

// "Strata is on": shown instead of the Run dialog. OK closes it and points
// at the card to stop.
class NoticeDialog {
  constructor() {
    this.el = document.getElementById("notice-dialog");
    this.titleEl = document.getElementById("notice-title");
    this.textEl = document.getElementById("notice-text");
    this.cards = [];
    this.target = null;
    this.el.addEventListener("click", (e) => { if (e.target === this.el) this.el.close(); });
    this.el.addEventListener("close", () => this.pointAtTarget());
  }

  show(card, conflicts) {
    const labels = conflicts.map((c) => c.label).join(" and ");
    const many = conflicts.length > 1;
    if (conflicts.every((c) => c.reason === "on")) {
      this.titleEl.textContent = `${labels} ${many ? "are on" : "is on"}`;
      this.textEl.textContent =
        `${card.section.label} and ${labels} cannot be on at the same time. ` +
        `Stop ${labels} first with the Stop button on ${many ? "their cards" : "its card"}, ` +
        `then click Run again.`;
    } else {
      this.titleEl.textContent = `Operation in progress on ${labels}`;
      this.textEl.textContent =
        `Wait for it to finish. If ${labels} ${many ? "are" : "is"} still on afterwards, ` +
        `stop ${many ? "them" : "it"} with Stop before starting ${card.section.label}.`;
    }
    this.target = this.cards.find((c) => c.tab.id === card.tab.id && c.section.id === conflicts[0].id) || null;
    this.el.showModal();
  }

  pointAtTarget() {
    const card = this.target;
    this.target = null;
    if (!card) return;
    card.el.scrollIntoView({ block: "nearest", behavior: "smooth" });
    card.stopBtn.focus({ preventScroll: true });
    card.el.classList.add("attention");
    setTimeout(() => card.el.classList.remove("attention"), 1800);
  }
}

function buildTabs(config, dialogs) {
  const tabsEl = document.getElementById("tabs");
  const panelsEl = document.getElementById("panels");
  panelsEl.replaceChildren();
  const cards = [];
  const buttons = [];

  for (const tab of config.tabs) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "tab";
    button.id = `tab-${tab.id}`;
    button.setAttribute("role", "tab");
    button.setAttribute("aria-controls", `panel-${tab.id}`);
    button.textContent = tab.label;
    button.dataset.tab = tab.id;
    tabsEl.append(button);
    buttons.push(button);

    const panel = document.createElement("section");
    panel.className = "panel";
    panel.id = `panel-${tab.id}`;
    panel.setAttribute("role", "tabpanel");
    panel.setAttribute("aria-labelledby", button.id);
    if (tab.sections.length === 0) {
      const empty = document.createElement("p");
      empty.className = "panel-empty muted";
      empty.textContent = `No sections configured for ${tab.label} (${tab.host.address}).`;
      panel.append(empty);
    }
    for (const section of tab.sections) {
      const card = new SectionCard(tab, section, dialogs);
      panel.append(card.el);
      cards.push(card);
    }
    panelsEl.append(panel);
  }

  const select = (tabId) => {
    for (const b of buttons) {
      const active = b.dataset.tab === tabId;
      b.setAttribute("aria-selected", String(active));
      b.tabIndex = active ? 0 : -1;
      document.getElementById(`panel-${b.dataset.tab}`).hidden = !active;
    }
    storageSet(TAB_KEY, tabId);
  };

  tabsEl.addEventListener("click", (e) => {
    const b = e.target.closest(".tab");
    if (b) select(b.dataset.tab);
  });
  tabsEl.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    const i = buttons.indexOf(document.activeElement);
    if (i < 0) return;
    const next = buttons[(i + (e.key === "ArrowRight" ? 1 : -1) + buttons.length) % buttons.length];
    select(next.dataset.tab);
    next.focus();
  });

  const stored = storageGet(TAB_KEY);
  select(config.tabs.some((t) => t.id === stored) ? stored : config.tabs[0]?.id);
  return cards;
}

async function main() {
  let res;
  try {
    res = await api("GET", "/api/config");
  } catch (err) {
    res = { ok: false, status: 0, data: null };
  }
  if (!res.ok) {
    document.getElementById("loading").textContent =
      `Cannot load the configuration (${res.status ? errorMessage(res) : "server unreachable"}).`;
    return;
  }
  const config = res.data;
  document.getElementById("dry-run-badge").hidden = !config.dry_run;
  document.getElementById("footer").textContent = `llm-console ${config.version}`;

  const dialogs = { run: new RunDialog(), notice: new NoticeDialog() };
  const cards = buildTabs(config, dialogs);
  dialogs.notice.cards = cards;
  await Promise.all(cards.map((card) => card.loadLastJob()));
}

main();
