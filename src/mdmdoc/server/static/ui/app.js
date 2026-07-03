/* mdmdoc operator console — vanilla JS, no build step */
window.mdmdoc = (() => {
  const tokenMeta = document.querySelector('meta[name="mdmdoc-token"]');
  const TOKEN = tokenMeta ? tokenMeta.content : "";

  async function api(path, opts = {}) {
    opts.headers = Object.assign({}, opts.headers);
    if (TOKEN) opts.headers["Authorization"] = "Bearer " + TOKEN;
    const r = await fetch(path, opts);
    const ct = r.headers.get("content-type") || "";
    const body = ct.includes("json") ? await r.json() : await r.text();
    if (!r.ok) {
      const msg = body && body.error ? body.error.message : (typeof body === "string" ? body : r.statusText);
      throw new Error(msg);
    }
    return body;
  }

  function pollJob(jobId, onLine, onDone, onError) {
    let offset = 0;
    const tick = async () => {
      try {
        const j = await api(`/api/v1/jobs/${jobId}?after=${offset}`);
        (j.progress || []).forEach(onLine);
        offset = j.progress_len;
        if (j.status === "done") return onDone(j.result || {});
        if (j.status === "error") return onError(j.error || "job failed");
        setTimeout(tick, 2000);
      } catch (e) { onError(String(e.message || e)); }
    };
    tick();
  }

  /* ---------- dashboard: drop zone -------------------------------------- */
  function initDropZone() {
    const drop = document.getElementById("drop");
    const input = document.getElementById("file-input");
    const log = document.getElementById("job-log");
    const seg = document.getElementById("class-seg");
    if (!drop) return;
    seg.querySelectorAll("button").forEach(b => b.onclick = () => {
      seg.querySelectorAll("button").forEach(x => x.classList.remove("active"));
      b.classList.add("active");
    });
    const docClass = () => seg.querySelector(".active").dataset.v;
    const say = (l) => { log.hidden = false; log.textContent += l + "\n"; log.scrollTop = 1e9; };

    // optional SAP screenshot (bank only)
    let sapFile = null;
    const sapBtn = document.getElementById("sap-attach");
    const sapInput = document.getElementById("sap-file");
    const sapName = document.getElementById("sap-name");
    const sapRow = document.getElementById("sap-row");
    const syncSapRow = () => { if (sapRow) sapRow.style.display = docClass() === "bank" ? "" : "none"; };
    if (sapBtn) {
      sapBtn.onclick = () => sapInput.click();
      sapInput.onchange = () => {
        sapFile = sapInput.files[0] || null;
        sapName.textContent = sapFile ? sapFile.name + " ✓ (will be compared)" : "";
      };
      seg.querySelectorAll("button").forEach(b => b.addEventListener("click", syncSapRow));
      syncSapRow();
    }

    async function send(file) {
      log.hidden = false; log.textContent = "";
      say(`uploading ${file.name} (${docClass()})…`);
      const fd = new FormData();
      fd.append("file", file);
      fd.append("doc_class", docClass());
      fd.append("lang", document.getElementById("lang").value);
      fd.append("wait", "false");
      const q = document.getElementById("quality");
      if (q && q.checked) fd.append("quality", "true");
      if (sapFile && docClass() === "bank") fd.append("sap_file", sapFile);
      try {
        const { job_id } = await api("/api/v1/check", { method: "POST", body: fd });
        pollJob(job_id, say,
          (res) => { say("opening run page…"); location = `/ui/runs/${res.run_id}`; },
          (err) => say("ERROR: " + err));
      } catch (e) { say("ERROR: " + e.message); }
    }
    drop.onclick = () => input.click();
    input.onchange = () => input.files[0] && send(input.files[0]);
    drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("hover"); };
    drop.ondragleave = () => drop.classList.remove("hover");
    drop.ondrop = (e) => {
      e.preventDefault(); drop.classList.remove("hover");
      const f = e.dataTransfer.files[0];
      if (f) send(f);
    };
  }

  /* ---------- run page: lazy artifacts ----------------------------------- */
  function initArtifacts() {
    document.querySelectorAll("details[data-artifact]").forEach(d => {
      d.addEventListener("toggle", async () => {
        if (!d.open || d.dataset.loaded) return;
        d.dataset.loaded = "1";
        const pre = d.querySelector(".artifact-body");
        try {
          const body = await api(`/api/v1/runs/${d.dataset.run}/artifacts/${d.dataset.artifact}`);
          pre.textContent = typeof body === "string" ? body : JSON.stringify(body, null, 2);
        } catch (e) { pre.textContent = "ERROR: " + e.message; }
      });
    });
  }

  /* ---------- run page: compare with SAP --------------------------------- */
  function initSapCompare() {
    const btn = document.getElementById("btn-sap");
    if (!btn) return;
    const input = document.getElementById("sap-input");
    const log = document.getElementById("job-log");
    const say = (l) => { log.hidden = false; log.textContent += l + "\n"; log.scrollTop = 1e9; };
    btn.onclick = () => input.click();
    input.onchange = async () => {
      const f = input.files[0];
      if (!f) return;
      btn.disabled = true;
      log.textContent = "";
      say(`re-running with SAP screenshot ${f.name}… (full pipeline — 1-3 min)`);
      const fd = new FormData();
      fd.append("doc_class", "bank");
      fd.append("wait", "false");
      fd.append("rerun_run_id", location.pathname.split("/")[3]);
      fd.append("sap_file", f);
      try {
        const { job_id } = await api("/api/v1/check", { method: "POST", body: fd });
        pollJob(job_id, say,
          (res) => location = `/ui/runs/${res.run_id}`,
          (err) => { say("ERROR: " + err); btn.disabled = false; });
      } catch (e) { say("ERROR: " + e.message); btn.disabled = false; }
    };
  }

  /* ---------- review form ------------------------------------------------ */
  function initReview() {
    const form = document.getElementById("review-form");
    if (!form) return;
    // boolean segments
    form.querySelectorAll("[data-bool]").forEach(seg => {
      seg.querySelectorAll("button").forEach(b => b.onclick = () => {
        seg.querySelectorAll("button").forEach(x => x.classList.remove("active"));
        b.classList.add("active");
      });
    });
    // sensitive radios reveal the input
    form.querySelectorAll("[data-sensitive]").forEach(row => {
      const k = row.dataset.sensitive;
      const inp = document.getElementById("f-" + k);
      const tinRow = k === "tin_raw" ? document.getElementById("tin-type-row") : null;
      row.querySelectorAll("input[type=radio]").forEach(r => r.onchange = () => {
        const set = row.querySelector("input[value=set]").checked;
        inp.hidden = !set;
        if (tinRow) tinRow.hidden = !set;
        if (set) inp.focus();
      });
    });
    // plain inputs highlight when changed
    form.querySelectorAll("input[data-original]").forEach(i =>
      i.addEventListener("input", () => i.classList.toggle("changed", i.value !== i.dataset.original)));

    form.onsubmit = async (e) => {
      e.preventDefault();
      const fields = {};
      form.querySelectorAll("input[data-original]").forEach(i => {
        const k = i.id.slice(2);
        if (i.value === i.dataset.original) return;              // keep
        fields[k] = i.value === "" ? { action: "clear" } : { action: "set", value: i.value };
      });
      form.querySelectorAll("[data-bool]").forEach(seg => {
        const v = seg.querySelector(".active").dataset.v;
        if (v !== seg.dataset.original) fields[seg.dataset.bool] = { action: "set", value: v === "yes" };
      });
      form.querySelectorAll("[data-sensitive]").forEach(row => {
        const k = row.dataset.sensitive;
        const mode = row.querySelector("input[type=radio]:checked").value;
        if (mode === "keep") return;
        if (mode === "clear") { fields[k] = { action: "clear" }; return; }
        const val = document.getElementById("f-" + k).value.trim();
        if (!val) return;                                        // empty replace -> keep
        fields[k] = { action: "set", value: val };
        if (k === "tin_raw") {
          const tt = document.getElementById("f-tin_type").value;
          if (tt) fields["tin_type"] = { action: "set", value: tt };
        }
      });
      const payload = {
        fields,
        doc_type_gold: document.getElementById("f-doc_type").value,
        verdict_gold: document.getElementById("f-verdict").value,
        notes: document.getElementById("f-notes").value,
      };
      const err = document.getElementById("review-error");
      const btn = form.querySelector("button[type=submit]");
      try {
        btn.disabled = true; btn.textContent = "Saving…";
        const res = await api(`/api/v1/runs/${form.dataset.run}/label`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const q = res.retrain_job_id ? `retrain=${res.retrain_job_id}` : "flash=labeled";
        location = `/ui/runs/${res.doc_sha256}?${q}`;
      } catch (ex) {
        err.hidden = false; err.textContent = "ERROR: " + ex.message;
        btn.disabled = false; btn.textContent = "Save & retrain";
      }
    };
  }

  /* ---------- run page: live retrain progress ----------------------------- */
  function initRetrainWatch() {
    const jobId = new URLSearchParams(location.search).get("retrain");
    if (!jobId) return;
    const log = document.getElementById("job-log");
    const say = (l) => { log.hidden = false; log.textContent += l + "\n"; log.scrollTop = 1e9; };
    say("correction saved — retraining and re-checking this document…");
    pollJob(jobId, say,
      () => location = location.pathname + "?flash=retrained",
      (e) => say("ERROR: " + e));
  }

  /* ---------- training page ---------------------------------------------- */
  function initTraining() {
    const log = document.getElementById("train-log");
    const say = (l) => { log.hidden = false; log.textContent += l + "\n"; log.scrollTop = 1e9; };
    const btnF = document.getElementById("btn-fewshot");
    if (btnF) btnF.onclick = async () => {
      log.textContent = "";
      try {
        const r = await api("/api/v1/train/fewshot", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
        (r.log || []).forEach(say);
        say("rc=" + r.rc);
      } catch (e) { say("ERROR: " + e.message); }
    };
    const btnM = document.getElementById("btn-modelfile");
    if (btnM) btnM.onclick = async () => {
      log.textContent = "";
      const apply = document.getElementById("mf-apply").checked;
      try {
        const { job_id } = await api("/api/v1/train/modelfile", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ apply }) });
        pollJob(job_id, say, (r) => say("done rc=" + r.rc), (e) => say("ERROR: " + e));
      } catch (e) { say("ERROR: " + e.message); }
    };
    const btnE = document.getElementById("btn-eval");
    if (btnE) btnE.onclick = async () => {
      log.textContent = "";
      btnE.disabled = true;
      const body = { tag: document.getElementById("eval-tag").value,
                     only: document.getElementById("eval-only").value || null };
      try {
        const { job_id } = await api("/api/v1/eval", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body) });
        say("eval started (full pipeline per label — minutes, not seconds)…");
        pollJob(job_id, say,
          () => { say("done — reloading…"); setTimeout(() => location.reload(), 800); },
          (e) => { say("ERROR: " + e); btnE.disabled = false; });
      } catch (e) { say("ERROR: " + e.message); btnE.disabled = false; }
    };
    // sparklines
    const el = document.getElementById("eval-series");
    if (el) {
      const series = JSON.parse(el.textContent);
      const strip = document.getElementById("metric-strip");
      Object.entries(series).forEach(([name, values]) => {
        values = values.filter(v => v !== null && v !== undefined);
        if (!values.length) return;
        const cur = values[values.length - 1], prev = values.length > 1 ? values[values.length - 2] : null;
        const d = document.createElement("div");
        d.className = "metric";
        const delta = prev === null ? "" :
          (cur > prev ? `<span class="delta-up">▲ ${(cur - prev).toFixed(3)}</span>` :
           cur < prev ? `<span class="delta-down">▼ ${(prev - cur).toFixed(3)}</span>` :
           `<span class="hint">=</span>`);
        d.innerHTML = `<div class="meta">${name.replace(/_/g, " ")}</div>
                       <div class="val">${cur} ${delta}</div>`;
        d.appendChild(sparkline(values));
        strip.appendChild(d);
      });
    }
    // lazy eval report
    const rep = document.getElementById("eval-report");
    if (rep) rep.closest("details").addEventListener("toggle", async function () {
      if (!this.open || this.dataset.loaded) return;
      this.dataset.loaded = "1";
      try { rep.textContent = await api("/api/v1/eval/report"); }
      catch (e) { rep.textContent = "no report yet"; }
    });
  }

  function sparkline(values, w = 120, h = 28) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("width", w); svg.setAttribute("height", h);
    const min = Math.min(...values), max = Math.max(...values), span = (max - min) || 1;
    const pts = values.map((v, i) =>
      `${(i / Math.max(values.length - 1, 1)) * (w - 4) + 2},${h - 3 - ((v - min) / span) * (h - 6)}`);
    const line = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
    line.setAttribute("points", pts.join(" "));
    svg.appendChild(line);
    return svg;
  }

  /* ---------- dashboard: run filters -------------------------------------- */
  function initRunFilters() {
    const seg = document.querySelector("#run-filters .seg");
    const list = document.getElementById("runs-list");
    if (!seg || !list) return;
    seg.querySelectorAll("button").forEach(b => b.onclick = () => {
      seg.querySelectorAll("button").forEach(x => x.classList.remove("active"));
      b.classList.add("active");
      const f = b.dataset.f;
      list.querySelectorAll("li").forEach(li => {
        let show = true;
        if (f === "bank" || f === "w9") show = li.dataset.class === f;
        else if (f === "review") show = ["NEED_MANUAL_REVIEW", "WARNING"].includes(li.dataset.verdict);
        else if (f === "unlabeled") show = li.dataset.labeled === "0";
        li.style.display = show ? "" : "none";
      });
    });
  }

  /* ---------- run page: copy report ---------------------------------------- */
  function initCopyReport() {
    const btn = document.getElementById("btn-copy");
    if (!btn) return;
    btn.onclick = async () => {
      try {
        const md = await api(`/api/v1/runs/${btn.dataset.run}/artifacts/report.md`);
        await navigator.clipboard.writeText(typeof md === "string" ? md : JSON.stringify(md));
        btn.textContent = "Copied ✓";
        setTimeout(() => btn.textContent = "Copy report", 1500);
      } catch (e) { btn.textContent = "copy failed"; }
    };
  }

  /* ---------- header host chip ------------------------------------------- */
  async function refreshChip() {
    const chip = document.getElementById("host-chip");
    if (!chip) return;
    try {
      const d = await api("/api/v1/doctor");
      const ok = d.model_host && d.model_host.reachable;
      chip.innerHTML = `<span class="dot ${ok ? "dot--ok" : "dot--down"}"></span>` +
        (ok ? d.model_host.source : "model host down");
    } catch (e) { chip.textContent = ""; }
  }
  refreshChip();
  setInterval(refreshChip, 60000);

  initRunFilters();
  initCopyReport();

  return { api, pollJob, initDropZone, initArtifacts, initReview, initTraining,
           initSapCompare, initRetrainWatch };
})();
