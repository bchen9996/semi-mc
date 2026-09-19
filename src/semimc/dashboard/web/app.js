"use strict";

// Every handler below builds a `semimc <command> ...` argument list from form input --
// the same list a terminal user would type -- and posts it to /api/jobs, which hands it to
// `semimc.dashboard.jobs.launch` (see server.py's module docstring). No analysis logic
// lives here; this file only ever assembles CLI arguments and renders what comes back.

const chips = {};

function el(id) {
  return document.getElementById(id);
}

function overridesList() {
  return el("cfg-overrides")
    .value.split("\n")
    .map((s) => s.trim())
    .filter((s) => s && !s.startsWith("#"));
}

function configArgs() {
  const args = [];
  const path = el("cfg-path").value.trim();
  if (path) args.push("--config", path);
  for (const o of overridesList()) args.push("--set", o);
  return args;
}

function configParams() {
  const params = new URLSearchParams();
  const path = el("cfg-path").value.trim();
  if (path) params.set("config", path);
  for (const o of overridesList()) params.append("set", o);
  return params;
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `${res.status} ${res.statusText}`);
  return body;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// -- a small multi-select rendered as toggle "chips", since a handful of native
// -- <select multiple> listboxes is what this UI is specifically trying to improve on
function chipGroup(container, options, { multiple = true } = {}) {
  container.innerHTML = "";
  const selected = new Set();
  const buttons = new Map();
  options.forEach((opt) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "chip";
    btn.textContent = opt;
    btn.addEventListener("click", () => {
      if (selected.has(opt)) {
        selected.delete(opt);
        btn.classList.remove("selected");
      } else {
        if (!multiple) {
          selected.forEach((v) => buttons.get(v).classList.remove("selected"));
          selected.clear();
        }
        selected.add(opt);
        btn.classList.add("selected");
      }
    });
    buttons.set(opt, btn);
    container.appendChild(btn);
  });
  return {
    get: () => options.filter((o) => selected.has(o)),
    set: (values) => {
      const wanted = new Set(values);
      options.forEach((opt) => {
        const on = wanted.has(opt);
        if (on) selected.add(opt);
        else selected.delete(opt);
        buttons.get(opt).classList.toggle("selected", on);
      });
    },
  };
}

// -----------------------------------------------------------------------------------------
// Job runner: shared by every panel's "Run ..." button
// -----------------------------------------------------------------------------------------

async function pollJob(jobId, logEl) {
  let since = 0;
  for (;;) {
    const data = await api(`/api/jobs/${jobId}?since=${since}`);
    if (data.lines.length) {
      logEl.textContent += (since ? "\n" : "") + data.lines.join("\n");
      logEl.scrollTop = logEl.scrollHeight;
    }
    since = data.total;
    if (data.done) return data;
    await sleep(500);
  }
}

async function runCommand(label, args, { showReport = true } = {}) {
  const fullArgs = [...args, ...configArgs()];
  const logEl = el("run-log");
  const statusEl = el("run-status");
  const titleEl = el("run-title");
  const reportWrap = el("run-report-wrap");

  el("run-console").classList.remove("hidden");
  reportWrap.classList.add("hidden");
  logEl.textContent = "";
  titleEl.textContent = `${label}: semimc ${fullArgs.join(" ")}`;
  statusEl.textContent = "running";
  statusEl.className = "badge badge-running";

  try {
    const started = await api("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ args: fullArgs }),
    });
    const data = await pollJob(started.id, logEl);
    if (data.returncode === 0) {
      statusEl.textContent = "done";
      statusEl.className = "badge badge-ok";
      if (showReport) {
        if (data.run) await showReportFor(data.run);
        else {
          reportWrap.classList.remove("hidden");
          el("run-report").srcdoc =
            "<p style='font-family:sans-serif;padding:1rem'>Finished, but couldn't find " +
            "which run folder was written. Check Runs &amp; Reports.</p>";
        }
      }
      loadRunsTable("runs-table");
      loadRunsTable("ov-runs-table", { limit: 10 });
    } else {
      statusEl.textContent = `failed (exit ${data.returncode})`;
      statusEl.className = "badge badge-error";
    }
  } catch (err) {
    statusEl.textContent = "error";
    statusEl.className = "badge badge-error";
    logEl.textContent += `\n${err}`;
  }
}

async function showReportFor(runName) {
  const reportWrap = el("run-report-wrap");
  const frame = el("run-report");
  const url = `/api/runs/${encodeURIComponent(runName)}/report?${configParams()}`;
  let res = await fetch(url);
  if (res.status === 404) {
    await runReportJob(runName);
    res = await fetch(url);
  }
  reportWrap.classList.remove("hidden");
  if (!res.ok) {
    frame.srcdoc = "<p style='font-family:sans-serif;padding:1rem'>This run produced no report.html.</p>";
    return;
  }
  frame.srcdoc = await res.text();
  el("run-report-regenerate").onclick = () => runReportJob(runName).then(() => showReportFor(runName));
}

async function runReportJob(runName) {
  const args = ["report", "--run", runName, ...configArgs()];
  const started = await api("/api/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ args }),
  });
  return pollJob(started.id, el("run-log"));
}

// -----------------------------------------------------------------------------------------
// Runs table (Overview's "recent runs" and the full Runs & Reports panel)
// -----------------------------------------------------------------------------------------

async function loadRunsTable(tbodyId, { limit } = {}) {
  const tbody = el(tbodyId);
  try {
    const data = await api(`/api/runs?${configParams()}`);
    let rows = data.runs;
    if (limit) rows = rows.slice(0, limit);
    tbody.innerHTML = "";
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="4" class="muted">No runs yet.</td></tr>`;
      return;
    }
    rows.forEach((r) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${r.name}</td><td>${r.command}</td><td>${r.created_at}</td><td>${r.seed ?? "-"}</td>`;
      if (tbodyId === "runs-table") {
        tr.classList.add("clickable");
        tr.addEventListener("click", () => {
          el("run-console").classList.remove("hidden");
          el("run-title").textContent = `Viewing run: ${r.name}`;
          el("run-status").textContent = "";
          el("run-status").className = "badge";
          el("run-log").textContent = "";
          showReportFor(r.name);
        });
      }
      tbody.appendChild(tr);
    });
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="4" class="muted">Failed to load runs: ${err}</td></tr>`;
  }
}

// -----------------------------------------------------------------------------------------
// Config: loaded once at startup and whenever "Apply config" is clicked
// -----------------------------------------------------------------------------------------

function renderOverview(cfg) {
  el("ov-tickers").textContent = cfg.universe_tickers.length;
  el("ov-benchmarks").textContent = cfg.benchmark_tickers.join(", ");
  el("ov-drift").textContent = cfg.drift_policy;
  const groupsEl = el("ov-groups");
  groupsEl.innerHTML = "";
  Object.entries(cfg.groups).forEach(([name, members]) => {
    const p = document.createElement("p");
    p.innerHTML = `<strong>${name}</strong>: ${members.join(", ")}`;
    groupsEl.appendChild(p);
  });
  loadRunsTable("ov-runs-table", { limit: 10 });
}

function renderFetchPanel(cfg) {
  chips.fetchTickers = chipGroup(el("fetch-tickers"), cfg.all_tickers);
}

function updateSimMode() {
  const mode = document.querySelector('input[name="sim-mode"]:checked').value;
  el("sim-ticker-field").classList.toggle("hidden", mode !== "single");
  el("sim-portfolio-field").classList.toggle("hidden", mode !== "portfolio");
  const impliedVol = el("sim-implied-vol");
  impliedVol.disabled = !(el("sim-model").value === "gbm" && mode === "single");
  if (impliedVol.disabled) impliedVol.checked = false;
}

function renderSimulatePanel(cfg) {
  el("sim-ticker").value = cfg.universe_tickers[0] || "";
  el("sim-universe-hint").textContent = `Configured universe: ${cfg.universe_tickers.join(", ")}`;
  const modelSel = el("sim-model");
  modelSel.innerHTML = cfg.models.map((m) => `<option value="${m}">${m}</option>`).join("");
  modelSel.value = "gbm";
  el("sim-paths").value = cfg.simulation.n_paths;
  el("sim-seed").value = cfg.simulation.seed;
  updateSimMode();
}

function renderBacktestPanel(cfg) {
  chips.btModels = chipGroup(el("bt-models"), cfg.models);
  chips.btModels.set(cfg.backtest.models);
  chips.btHorizons = chipGroup(el("bt-horizons"), cfg.horizons.map(String));
  chips.btHorizons.set(cfg.horizons.map(String));
  chips.btTickers = chipGroup(el("bt-tickers"), cfg.universe_tickers);
  chips.btTickers.set(cfg.universe_tickers);
  el("bt-start").value = cfg.backtest.start;
  el("bt-end").value = "";
  el("bt-paths").value = cfg.backtest.n_paths;
  el("bt-seed").value = cfg.simulation.seed;
}

function renderSignalsPanel(cfg) {
  chips.sigTickers = chipGroup(el("sig-tickers"), cfg.universe_tickers);
  chips.sigTickers.set(cfg.universe_tickers);
  el("sig-horizon").innerHTML = cfg.signals_horizons
    .map((h) => `<option value="${h}">${h}</option>`)
    .join("");
}

function renderScenarioPanel(cfg) {
  el("scn-name").innerHTML = cfg.scenarios.map((s) => `<option value="${s}">${s}</option>`).join("");
  chips.scnTickers = chipGroup(el("scn-tickers"), cfg.universe_tickers);
  chips.scnTickers.set(cfg.universe_tickers);
  const modelSel = el("scn-model");
  modelSel.innerHTML = cfg.models.map((m) => `<option value="${m}">${m}</option>`).join("");
  modelSel.value = "gbm";
  el("scn-paths").value = cfg.simulation.n_paths;
  el("scn-seed").value = cfg.simulation.seed;
}

async function loadConfig() {
  const summary = el("cfg-summary");
  try {
    const cfg = await api(`/api/config?${configParams()}`);
    summary.textContent = `${cfg.universe_tickers.length} tickers, drift policy: ${cfg.drift_policy}`;
    summary.classList.remove("error");
    renderOverview(cfg);
    renderFetchPanel(cfg);
    renderSimulatePanel(cfg);
    renderBacktestPanel(cfg);
    renderSignalsPanel(cfg);
    renderScenarioPanel(cfg);
    if (!cfg.scenarios.length) {
      el("scn-run").disabled = true;
      el("scn-run").title = "No scenarios configured.";
    }
  } catch (err) {
    summary.textContent = `Config failed to load: ${err}`;
    summary.classList.add("error");
  }
}

// -----------------------------------------------------------------------------------------
// Wiring
// -----------------------------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".nav-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".nav-btn").forEach((b) => b.classList.remove("active"));
      document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
      btn.classList.add("active");
      el(`panel-${btn.dataset.panel}`).classList.add("active");
    });
  });

  el("cfg-apply").addEventListener("click", loadConfig);
  el("run-console-close").addEventListener("click", () => el("run-console").classList.add("hidden"));

  el("fetch-run").addEventListener("click", () => {
    const args = ["fetch"];
    const tickers = chips.fetchTickers.get();
    if (tickers.length) args.push("--tickers", tickers.join(","));
    if (el("fetch-force").checked) args.push("--force");
    if (el("fetch-riskfree").checked) args.push("--risk-free");
    if (el("fetch-earnings").checked) args.push("--earnings");
    if (el("fetch-industry").checked) args.push("--industry");
    runCommand("fetch", args, { showReport: false });
  });
  el("validate-run").addEventListener("click", () => runCommand("validate", ["validate"]));

  document.querySelectorAll('input[name="sim-mode"]').forEach((r) => r.addEventListener("change", updateSimMode));
  el("sim-model").addEventListener("change", updateSimMode);
  el("sim-run").addEventListener("click", () => {
    const mode = document.querySelector('input[name="sim-mode"]:checked').value;
    const ticker = el("sim-ticker").value.trim();
    const portfolio = el("sim-portfolio").value.trim();
    if (mode === "single" && !ticker) {
      alert("Enter a ticker.");
      return;
    }
    const args = [
      "simulate",
      "--horizon",
      el("sim-horizon").value,
      "--model",
      el("sim-model").value,
      "--paths",
      el("sim-paths").value,
    ];
    args.push(...(mode === "single" ? ["--ticker", ticker.toUpperCase()] : ["--portfolio", portfolio]));
    args.push("--seed", el("sim-seed").value);
    const above = parseFloat(el("sim-above").value || "0");
    if (above > 0) args.push("--above", String(above));
    if (el("sim-tilt").checked) args.push("--tilt");
    if (el("sim-implied-vol").checked) args.push("--implied-vol");
    runCommand("simulate", args);
  });

  el("bt-run").addEventListener("click", () => {
    const models = chips.btModels.get();
    const horizons = chips.btHorizons.get();
    const tickers = chips.btTickers.get();
    if (!models.length || !horizons.length || !tickers.length) {
      alert("Pick at least one model, horizon, and ticker.");
      return;
    }
    const args = [
      "backtest",
      "--models",
      models.join(","),
      "--horizons",
      horizons.join(","),
      "--tickers",
      tickers.join(","),
      "--start",
      el("bt-start").value,
      "--paths",
      el("bt-paths").value,
      "--seed",
      el("bt-seed").value,
    ];
    if (el("bt-end").value) args.push("--end", el("bt-end").value);
    runCommand("backtest", args);
  });

  el("sig-run").addEventListener("click", () => {
    const tickers = chips.sigTickers.get();
    if (!tickers.length) {
      alert("Pick at least one ticker.");
      return;
    }
    runCommand("signals", ["signals", "--tickers", tickers.join(","), "--horizon", el("sig-horizon").value]);
  });

  el("scn-run").addEventListener("click", () => {
    const tickers = chips.scnTickers.get();
    if (!tickers.length) {
      alert("Pick at least one ticker.");
      return;
    }
    const args = [
      "scenario",
      "--name",
      el("scn-name").value,
      "--tickers",
      tickers.join(","),
      "--horizon",
      el("scn-horizon").value,
      "--model",
      el("scn-model").value,
      "--paths",
      el("scn-paths").value,
      "--seed",
      el("scn-seed").value,
      el("scn-equal-weight").checked ? "--equal-weight" : "--no-equal-weight",
    ];
    runCommand("scenario", args);
  });

  el("runs-refresh").addEventListener("click", () => loadRunsTable("runs-table"));

  loadConfig();
  loadRunsTable("runs-table");
});
