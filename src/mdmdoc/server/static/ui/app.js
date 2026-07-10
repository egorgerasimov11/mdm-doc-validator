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

  function pollJob(jobId, onLine, onDone, onError, onTick) {
    let offset = 0;
    const tick = async () => {
      try {
        const j = await api(`/api/v1/jobs/${jobId}?after=${offset}`);
        (j.progress || []).forEach(onLine);
        offset = j.progress_len;
        if (onTick) onTick(j);
        if (j.status === "done") return onDone(j.result || {});
        if (j.status === "canceled") return onError("canceled by operator");
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

    // optional SAP data: a Bank Details screenshot (bank docs) OR a BUT0BK/BUT000
    // table export (.xlsx — works for w9 too). The API validates the pairing.
    let sapFile = null;
    const sapBtn = document.getElementById("sap-attach");
    const sapInput = document.getElementById("sap-file");
    const sapName = document.getElementById("sap-name");
    const sapBp = document.getElementById("sap-bp");
    if (sapBtn) {
      sapBtn.onclick = () => sapInput.click();
      sapInput.onchange = () => {
        sapFile = sapInput.files[0] || null;
        sapName.textContent = sapFile ? sapFile.name + " ✓ (will be compared)" : "";
        if (sapBp) sapBp.hidden = !sapFile;
      };
    }

    // optional request-form TEMPLATE (D8): the filled MDM workbook — the
    // document is compared against what the requestor typed into the form
    let tplFile = null;
    const tplBtn = document.getElementById("tpl-attach");
    const tplInput = document.getElementById("tpl-file");
    const tplName = document.getElementById("tpl-name");
    if (tplBtn) {
      tplBtn.onclick = () => tplInput.click();
      tplInput.onchange = () => {
        tplFile = tplInput.files[0] || null;
        tplName.textContent = tplFile ? tplFile.name + " ✓ (form values will be compared)" : "";
      };
    }

    async function send(file) {
      log.hidden = false; log.textContent = "";
      say(`uploading ${file.name} (${docClass()})…`);
      const fd = new FormData();
      fd.append("file", file);
      fd.append("doc_class", docClass());
      fd.append("lang", document.getElementById("lang").value);
      fd.append("wait", "false");
      const eff = document.getElementById("effort");
      if (eff) fd.append("effort", eff.value);
      if (sapFile) {
        fd.append("sap_file", sapFile);
        if (sapBp && sapBp.value.trim()) fd.append("sap_bp", sapBp.value.trim());
      }
      if (tplFile) fd.append("template_file", tplFile);
      try {
        const { job_id } = await api("/api/v1/check", { method: "POST", body: fd });
        trackJob(job_id);
        pollJob(job_id, say,
          (res) => {
            if (tracked.size === 1) { say("opening run page…"); location = `/ui/runs/${res.run_id}`; }
            else say(`done: ${file.name} -> run ${res.run_id}`);
          },
          (err) => say("ERROR: " + err),
          (j) => updateProgress(j));
      } catch (e) { say("ERROR: " + e.message); }
    }

    /* queue panel (D3): tracked check jobs, live positions, cancel buttons */
    const tracked = new Set();
    let queueTimer = null;
    const bar = document.getElementById("job-progress");
    const stageEl = document.getElementById("job-stage");
    const progRow = document.getElementById("progress-row");

    function updateProgress(j) {
      if (!bar || j.status !== "running") return;
      progRow.hidden = false;
      let pct = j.percent || 0;
      if (j.estimate_s && j.started) {
        const elapsed = (Date.now() - Date.parse(j.started + "Z")) / 1000;
        const eta = Math.min(96, Math.round(100 * elapsed / j.estimate_s));
        pct = Math.max(pct, Math.min(pct + 12, eta));   // ETA glide, capped near the next stage
      }
      bar.value = pct;
      stageEl.textContent = (j.stage || "starting") + " · " + pct + "%";
    }

    function trackJob(id) {
      tracked.add(id);
      renderQueue();
      if (!queueTimer) queueTimer = setInterval(renderQueue, 1500);
    }

    async function renderQueue() {
      const panel = document.getElementById("queue-panel");
      const list = document.getElementById("queue-list");
      if (!panel || !list) return;
      let all = [];
      try { all = await api("/api/v1/jobs"); } catch (e) { return; }
      const rows = all.filter(j => tracked.has(j.id) ||
                                   (j.kind === "check" && (j.status === "queued" || j.status === "running")));
      if (!rows.length) { panel.hidden = true; clearInterval(queueTimer); queueTimer = null; return; }
      panel.hidden = false;
      list.innerHTML = "";
      rows.forEach(j => {
        const li = document.createElement("li");
        li.className = "queue-row";
        const state = j.status === "running" ? (j.stage ? `running · ${j.stage} ${j.percent || 0}%` : "running")
                    : j.status === "queued" ? (j.queue_pos > 0 ? `waiting #${j.queue_pos}` : "starting…")
                    : j.status;
        const runId = j.result && j.result.run_id;
        li.innerHTML = `<span class="dot ${j.status === "running" ? "dot--WARNING" : j.status === "done" ? "dot--ok" : j.status === "error" ? "dot--down" : ""}"></span>
          <span class="grow"><strong>${(j.label || j.kind)}</strong> — ${state}</span>` +
          (j.status === "running" ? `<progress max="100" value="${j.percent || 0}"></progress>` : "") +
          (runId ? ` <a href="/ui/runs/${runId}">open run →</a>` : "");
        if (j.cancelable) {
          const btn = document.createElement("button");
          btn.type = "button"; btn.textContent = "Cancel"; btn.className = "queue-cancel";
          btn.onclick = async () => {
            btn.disabled = true;
            try { await api(`/api/v1/jobs/${j.id}/cancel`, { method: "POST" }); }
            catch (e) { btn.textContent = "…"; }
          };
          li.appendChild(btn);
        }
        list.appendChild(li);
      });
    }
    renderQueue();

    async function sendAll(files) {
      for (const f of files) await send(f);
    }
    drop.onclick = () => input.click();
    input.onchange = () => input.files.length && sendAll([...input.files]);
    drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("hover"); };
    drop.ondragleave = () => drop.classList.remove("hover");
    drop.ondrop = (e) => {
      e.preventDefault(); drop.classList.remove("hover");
      const fs = [...e.dataTransfer.files];
      if (fs.length) sendAll(fs);
    };

    // effort slider (D4): live label + persisted default
    const eff = document.getElementById("effort");
    const effLabel = document.getElementById("effort-label");
    const EFFORT_NAMES = { 1: "1 · instant (no LLM)", 2: "2 · fast", 3: "3 · standard",
                           4: "4 · thorough", 5: "5 · maximum (~slow)" };
    if (eff && effLabel) {
      const paint = () => effLabel.textContent = EFFORT_NAMES[eff.value] || eff.value;
      paint();
      eff.oninput = paint;
      eff.onchange = async () => {
        paint();
        try {
          await api("/api/v1/settings", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ default_effort: parseInt(eff.value, 10) }),
          });
        } catch (e) { /* non-fatal */ }
      };
    }

    // default-engine setting (persists on the server; env pin disables it)
    const defSel = document.getElementById("engine-default");
    const defState = document.getElementById("engine-default-state");
    if (defSel && !defSel.disabled) {
      defSel.addEventListener("change", async () => {
        try {
          await api("/api/v1/settings", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ engine: defSel.value }),
          });
          if (defState) { defState.textContent = "✓ saved"; setTimeout(() => defState.textContent = "", 1500); }
        } catch (e) { if (defState) defState.textContent = "ERROR: " + e.message; }
      });
    }
  }

  /* ---------- run page: compare with template (D8) ----------------------- */
  function initTplCompare() {
    const btn = document.getElementById("btn-tpl");
    const input = document.getElementById("tpl-input");
    const log = document.getElementById("job-log");
    if (!btn || !input) return;
    const say = (l) => { log.hidden = false; log.textContent += l + "\n"; log.scrollTop = 1e9; };
    btn.onclick = () => input.click();
    input.onchange = async () => {
      const f = input.files[0];
      if (!f) return;
      log.hidden = false; log.textContent = "";
      say(`re-running against template ${f.name}…`);
      const fd = new FormData();
      fd.append("rerun_run_id", location.pathname.split("/").pop());
      fd.append("doc_class", btn.dataset.class || "auto");
      fd.append("wait", "false");
      fd.append("template_file", f);
      try {
        const { job_id } = await api("/api/v1/check", { method: "POST", body: fd });
        pollJob(job_id, say,
          (res) => { location = `/ui/runs/${res.run_id}`; },
          (err) => say("ERROR: " + err));
      } catch (e) { say("ERROR: " + e.message); }
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
    const bp = document.getElementById("sap-bp-input");
    const log = document.getElementById("job-log");
    const say = (l) => { log.hidden = false; log.textContent += l + "\n"; log.scrollTop = 1e9; };
    btn.onclick = () => input.click();
    input.onchange = async () => {
      const f = input.files[0];
      if (!f) return;
      if (bp && /\.xlsx?$/i.test(f.name)) { bp.hidden = false; }
      btn.disabled = true;
      log.textContent = "";
      say(`re-running with SAP data ${f.name}… (full pipeline — 1-3 min)`);
      const fd = new FormData();
      fd.append("doc_class", btn.dataset.class || "bank");
      fd.append("wait", "false");
      fd.append("rerun_run_id", location.pathname.split("/")[3]);
      fd.append("sap_file", f);
      if (bp && bp.value.trim()) fd.append("sap_bp", bp.value.trim());
      try {
        const { job_id } = await api("/api/v1/check", { method: "POST", body: fd });
        pollJob(job_id, say,
          (res) => location = `/ui/runs/${res.run_id}`,
          (err) => { say("ERROR: " + err); btn.disabled = false; });
      } catch (e) { say("ERROR: " + e.message); btn.disabled = false; }
    };
  }

  /* ---------- run page: external web evidence ----------------------------- */
  function initWebVerify() {
    const btn = document.getElementById("btn-web");
    if (!btn) return;
    const log = document.getElementById("job-log");
    const say = (l) => { log.hidden = false; log.textContent += l + "\n"; log.scrollTop = 1e9; };
    btn.onclick = async () => {
      btn.disabled = true;
      log.textContent = "";
      say("re-running with external registry checks (ABA/FDIC, SWIFT, entity match)…");
      say("advisory only — the web never decides the verdict");
      const fd = new FormData();
      fd.append("doc_class", btn.dataset.class || "bank");
      fd.append("wait", "false");
      fd.append("web", "true");
      fd.append("rerun_run_id", location.pathname.split("/")[3]);
      try {
        const { job_id } = await api("/api/v1/check", { method: "POST", body: fd });
        pollJob(job_id, say,
          (res) => location = `/ui/runs/${res.run_id}`,
          (err) => { say("ERROR: " + err); btn.disabled = false; });
      } catch (e) { say("ERROR: " + e.message); btn.disabled = false; }
    };
  }

  /* ---------- dashboard: bank keys quick check --------------------------- */
  function initBankCheck() {
    const btn = document.getElementById("bc-run");
    if (!btn) return;
    const box = document.getElementById("bc-result");
    const dot = document.getElementById("bc-dot");
    const verdictEl = document.getElementById("bc-verdict");
    const nextEl = document.getElementById("bc-next");
    const hintEl = document.getElementById("bc-hint");
    const list = document.getElementById("bc-findings");
    const run = async () => {
      const routing = document.getElementById("bc-routing").value.trim();
      const account = document.getElementById("bc-account").value.trim();
      if (!routing && !account) return;
      btn.disabled = true;
      verdictEl.textContent = "checking…";
      nextEl.textContent = ""; hintEl.hidden = true; list.innerHTML = "";
      dot.className = "dot"; box.hidden = false;
      const fd = new FormData();
      fd.append("routing", routing);
      fd.append("account", account);
      fd.append("bank_name", document.getElementById("bc-bank").value.trim());
      fd.append("web", document.getElementById("bc-web").checked ? "true" : "false");
      try {
        const res = await api("/api/v1/check-routing", { method: "POST", body: fd });
        dot.className = "dot dot--" + res.verdict;
        verdictEl.textContent = res.verdict;
        nextEl.textContent = res.next_step || "";
        if (res.web_hint) { hintEl.textContent = "⚠ " + res.web_hint; hintEl.hidden = false; }
        (res.findings || []).forEach((f) => {
          const li = document.createElement("li");
          const d = document.createElement("span");
          d.className = "dot dot--" + (f.verdict_effect || (f.severity === "NOTE" ? "ACCEPT" : f.severity));
          li.appendChild(d);
          const s = document.createElement("span");
          s.className = "grow";
          s.textContent = `${f.rule_id}: ${f.message}`;
          li.appendChild(s);
          list.appendChild(li);
        });
        (res.web || []).forEach((w) => {
          const li = document.createElement("li");
          const d = document.createElement("span");
          d.className = "dot dot--" + (w.status === "found" ? "ACCEPT" : w.status === "not_found" ? "REJECT" : "WARNING");
          li.appendChild(d);
          const s = document.createElement("span");
          s.className = "grow";
          s.textContent = `${w.check} (${w.status}): ${w.label} `;
          if (w.url) {
            const a = document.createElement("a");
            a.href = w.url; a.target = "_blank"; a.rel = "noopener";
            a.textContent = "source ↗";
            s.appendChild(a);
          }
          li.appendChild(s);
          list.appendChild(li);
        });
      } catch (e) {
        dot.className = "dot dot--REJECT";
        verdictEl.textContent = "error";
        nextEl.textContent = e.message;
      } finally { btn.disabled = false; }
    };
    btn.onclick = run;
    ["bc-routing", "bc-account", "bc-bank"].forEach((id) => {
      document.getElementById(id).addEventListener("keydown", (e) => {
        if (e.key === "Enter") run();
      });
    });
  }

  /* ---------- run page: per-field copy ------------------------------------ */
  function initFieldCopy() {
    const tbl = document.querySelector(".data-table");
    if (!tbl) return;
    const flash = (b) => {
      const t = b.textContent;
      b.textContent = "✓"; b.classList.add("copied");
      setTimeout(() => { b.textContent = t; b.classList.remove("copied"); }, 900);
    };
    tbl.addEventListener("click", (e) => {
      const b = e.target.closest(".copy-field");
      if (!b) return;
      navigator.clipboard.writeText(b.dataset.copy).then(() => flash(b));
    });
    const all = document.getElementById("btn-copy-fields");
    if (all) all.onclick = () => {
      const rows = [...tbl.querySelectorAll(".copy-field")]
        .map(b => `${b.dataset.field}\t${b.dataset.copy}`);
      if (rows.length) navigator.clipboard.writeText(rows.join("\n")).then(() => flash(all));
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
      const scenarios = [...form.querySelectorAll("#scenario-tags input[type=checkbox]:checked")]
        .map(c => c.value);
      const extra = document.getElementById("f-scenarios-extra");
      if (extra) extra.value.split(",").map(s => s.trim()).filter(Boolean)
        .forEach(s => scenarios.push(s));
      const payload = {
        fields,
        doc_type_gold: document.getElementById("f-doc_type").value,
        verdict_gold: document.getElementById("f-verdict").value,
        notes: document.getElementById("f-notes").value,
        scenarios,
        error_source: document.getElementById("f-error_source").value,
        verdict_confirmed: !!document.getElementById("f-verdict-confirmed")?.checked,
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

  /* ---------- run page: feedback -> proposed rule fix --------------------- */
  function initProposeFix() {
    const go = document.getElementById("fb-go");
    if (!go) return;
    const log = document.getElementById("job-log");
    const say = (l) => { log.hidden = false; log.textContent += l + "\n"; log.scrollTop = 1e9; };
    const box = document.getElementById("fb-proposal");
    const ruleBox = document.getElementById("fb-rule");
    const route = document.getElementById("fb-route");
    const issues = document.getElementById("fb-issues");
    const apply = document.getElementById("fb-apply");
    const targetEl = document.getElementById("fb-target");
    let targetRule = "";

    function setTarget(rid) {
      targetRule = rid || "";
      if (targetRule) {
        targetEl.hidden = false;
        targetEl.innerHTML = 'targeting rule: <code>' + targetRule +
          '</code> — <a href="#" id="fb-untarget">whole verdict instead</a>';
        document.getElementById("fb-untarget").onclick = (e) => { e.preventDefault(); setTarget(""); };
      } else { targetEl.hidden = true; targetEl.textContent = ""; }
    }
    // per-finding "dispute" buttons pre-target that rule and jump to the box
    document.querySelectorAll(".fb-dispute").forEach((b) => {
      b.onclick = () => {
        setTarget(b.dataset.rule);
        document.getElementById("propose-box").scrollIntoView({ behavior: "smooth", block: "center" });
        document.getElementById("fb-text").focus();
      };
    });

    function render(res) {
      box.hidden = false;
      ruleBox.hidden = true; route.hidden = true; issues.hidden = true;
      document.getElementById("fb-kind").textContent = res.kind || "?";
      document.getElementById("fb-rationale").textContent = res.rationale || "";
      if (res.kind === "rule") {
        ruleBox.hidden = false;
        document.getElementById("fb-diff").textContent = res.diff || "(no textual change)";
        document.getElementById("fb-yaml").value = res.proposed_yaml || res.current_yaml || "";
        if ((res.validation || []).length) {
          issues.hidden = false;
          issues.textContent = "do not apply as-is: " + res.validation.join("; ");
          apply.disabled = false;   // operator may still hand-fix the YAML, then apply
        } else { apply.disabled = false; }
      } else if (res.kind === "extraction") {
        route.hidden = false;
        route.innerHTML = (res.hint || "a field was read wrong — fix it in the teach flow") +
          ' <a href="' + (res.route || ("/ui/runs/" + go.dataset.run + "/review")) + '">Correct — teach the model →</a>';
      } else if (res.kind === "needs_code") {
        route.hidden = false;
        route.textContent = (res.hint || "new predicate logic is required") +
          (res.needs_code ? (" — " + res.needs_code) : "");
      } else {
        route.hidden = false;
        route.textContent = res.rationale || "couldn't parse the feedback — please rephrase.";
      }
    }

    go.onclick = async () => {
      const fb = document.getElementById("fb-text").value.trim();
      if (!fb) { say("write what's wrong with the analysis"); return; }
      go.disabled = true; log.textContent = "";
      say("sending feedback to the model (strong tier, ~15-40s)…");
      try {
        const { job_id } = await api(`/api/v1/runs/${go.dataset.run}/propose-fix`,
          { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ feedback: fb, rule_id: targetRule }) });
        pollJob(job_id, say,
          (res) => { render(res); go.disabled = false; },
          (e) => { say("ERROR: " + e); go.disabled = false; });
      } catch (e) { say("ERROR: " + e.message); go.disabled = false; }
    };

    document.getElementById("fb-discard").onclick = () => { box.hidden = true; };

    apply.onclick = async () => {
      const cls = apply.dataset.class, run = apply.dataset.run;
      const yaml = document.getElementById("fb-yaml").value;
      apply.disabled = true;
      say("saving the rule…");
      try {
        const r = await api(`/api/v1/rules/${cls}/raw`,
          { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ yaml }) });
        say(`rule saved — ${r.rules} rule(s) in the file`);
        if (document.getElementById("fb-regen").checked) {
          say("updating the ABAP side (regenerate)…");
          const g = await api("/api/v1/rules/regenerate", { method: "POST" });
          say(g.ok ? "ABAP rules updated" : "regenerate: " + (g.detail || g.stderr || "skipped"));
        }
        say("re-running the document with the new rule…");
        const fd = new FormData();
        fd.append("doc_class", cls); fd.append("wait", "false"); fd.append("rerun_run_id", run);
        const { job_id } = await api("/api/v1/check", { method: "POST", body: fd });
        pollJob(job_id, say,
          (res) => location = `/ui/runs/${res.run_id}?flash=rule-applied`,
          (e) => { say("ERROR: " + e); apply.disabled = false; });
      } catch (e) { say("ERROR: " + e.message); apply.disabled = false; }
    };
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
    const btnC = document.getElementById("btn-candidate");
    if (btnC) btnC.onclick = async () => {
      log.textContent = "";
      btnC.disabled = true;
      try {
        const { job_id } = await api("/api/v1/train/candidate", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
        say("building candidate + gated eval (full pipeline per label — minutes)…");
        pollJob(job_id, say,
          () => { say("done — reloading…"); setTimeout(() => location.reload(), 800); },
          (e) => { say("ERROR: " + e); btnC.disabled = false; });
      } catch (e) { say("ERROR: " + e.message); btnC.disabled = false; }
    };
    const btnA = document.getElementById("btn-adopt");
    if (btnA) btnA.onclick = async () => {
      log.textContent = "";
      btnA.disabled = true;
      try {
        const r = await api("/api/v1/train/adopt", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
        say("adopted: " + JSON.stringify(r.adopted));
        setTimeout(() => location.reload(), 800);
      } catch (e) { say("ERROR: " + e.message); btnA.disabled = false; }
    };
    const btnR = document.getElementById("btn-rollback");
    if (btnR) btnR.onclick = async () => {
      if (!confirm("Rebuild mdmdoc-extract from the PREVIOUS Modelfile?")) return;
      log.textContent = "";
      btnR.disabled = true;
      try {
        const r = await api("/api/v1/train/rollback", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
        say("rolled back: " + JSON.stringify(r.adopted));
        setTimeout(() => location.reload(), 800);
      } catch (e) { say("ERROR: " + e.message); btnR.disabled = false; }
    };
    const btnE = document.getElementById("btn-eval");
    if (btnE) btnE.onclick = async () => {
      log.textContent = "";
      btnE.disabled = true;
      const body = { tag: document.getElementById("eval-tag").value,
                     only: document.getElementById("eval-only").value || null,
                     scenario: (document.getElementById("eval-scenario") || {}).value || null };
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

  return { api, pollJob, initDropZone, initTplCompare, initArtifacts, initReview, initTraining,
           initSapCompare, initWebVerify, initProposeFix, initFieldCopy, initBankCheck,
           initRetrainWatch };
})();
