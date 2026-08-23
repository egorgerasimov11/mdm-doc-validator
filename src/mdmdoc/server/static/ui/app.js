/* mdmdoc operator console — vanilla JS, no build step */
window.mdmdoc = (() => {
  const tokenMeta = document.querySelector('meta[name="mdmdoc-token"]');
  const TOKEN = tokenMeta ? tokenMeta.content : "";

  // "action accepted" feedback: pop a green ✓ over a button (see app.css .flash-ok)
  function flashOk(el) {
    if (!el || !el.classList) return;
    el.classList.remove("flash-ok");
    void el.offsetWidth;                 // reflow so the animation restarts each time
    el.classList.add("flash-ok");
    setTimeout(() => el.classList.remove("flash-ok"), 900);
  }
  // remember the last button/control the operator clicked, so a successful action
  // fired from its handler can flash IT — universal, no per-handler wiring needed
  let _lastActionEl = null;
  document.addEventListener("click", (e) => {
    const el = e.target && e.target.closest && e.target.closest("button, .btn, summary");
    if (el) _lastActionEl = el;
  }, true);

  async function api(path, opts = {}) {
    opts.headers = Object.assign({}, opts.headers);
    if (TOKEN) opts.headers["Authorization"] = "Bearer " + TOKEN;
    const btn = _lastActionEl;           // capture before the await (may change during)
    const r = await fetch(path, opts);
    const ct = r.headers.get("content-type") || "";
    const body = ct.includes("json") ? await r.json() : await r.text();
    if (!r.ok) {
      const msg = body && body.error ? body.error.message : (typeof body === "string" ? body : r.statusText);
      throw new Error(msg);
    }
    // any mutating call that SUCCEEDED = the operator's action was accepted -> ✓
    const method = (opts.method || "GET").toUpperCase();
    if (method !== "GET" && method !== "HEAD") flashOk(btn);
    return body;
  }

  function pollJob(jobId, onLine, onDone, onError, onTick) {
    let offset = 0;
    const tick = async () => {
      try {
        const j = await api(`/api/v1/jobs/${jobId}?after=${offset}`);
        (j.progress || []).forEach(onLine);
        offset = j.progress_len;
        // a UI-progress fault must never kill the poll loop (E0: a NaN once
        // reached <progress>.value, threw, and looked like a dead analysis)
        if (onTick) { try { onTick(j); } catch (e) { console.warn("onTick:", e); } }
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
    // analysis MODE segment (Auto/Fast/Regular/Ultra) — replaces the effort slider
    const modeSeg = document.getElementById("mode-seg");
    if (modeSeg) modeSeg.querySelectorAll("button").forEach(b => b.onclick = () => {
      modeSeg.querySelectorAll("button").forEach(x => x.classList.remove("active"));
      b.classList.add("active");
    });
    const modeVal = () => {
      const a = modeSeg && modeSeg.querySelector(".active");
      return a ? a.dataset.v : "";
    };
    const say = (l) => { log.hidden = false; log.textContent += l + "\n"; log.scrollTop = 1e9; };

    // ---- packet state: documents to analyze + one optional SAP/form compare ----
    // Analysis no longer fires on drop — files accumulate here and the operator
    // presses "Start analysis", so they can add more documents or a comparison first.
    let docs = [];                 // File[]
    let compare = null;            // { file, kind: 'sap' | 'tpl' }
    const wrap = document.getElementById("loaded-wrap");
    const listEl = document.getElementById("loaded");
    const countEl = document.getElementById("loaded-count");
    const startBtn = document.getElementById("btn-start");
    const startHint = document.getElementById("start-hint");

    const typeOf = (n) => {
      const e = (n.split(".").pop() || "").toLowerCase();
      if (e === "xlsx" || e === "xlsm") return "form";
      if (e === "eml" || e === "msg") return "email";
      if (["png", "jpg", "jpeg", "tif", "tiff", "bmp", "webp"].includes(e)) return "image";
      if (e === "zip") return "archive";
      return "pdf";
    };

    function render() {
      if (!docs.length && !compare) { wrap.hidden = true; return; }
      wrap.hidden = false;
      listEl.querySelectorAll(".loaded-row").forEach(r => r.remove());
      docs.forEach((f, i) => {
        const row = document.createElement("div");
        row.className = "loaded-row";
        row.innerHTML = `<span class="fname"></span><span class="tag"></span>` +
          `<button class="rm" title="remove" data-i="${i}">&times;</button>`;
        row.querySelector(".fname").textContent = f.name;
        row.querySelector(".tag").textContent = typeOf(f.name);
        listEl.appendChild(row);
      });
      if (compare) {
        const row = document.createElement("div");
        row.className = "loaded-row";
        row.innerHTML = `<span class="fname"></span>` +
          `<span class="chip chip--NOTE">${compare.kind === "sap" ? "compare · SAP" : "compare · form"}</span>` +
          `<button class="rm" title="remove" data-cmp="1">&times;</button>`;
        row.querySelector(".fname").textContent = compare.file.name;
        listEl.appendChild(row);
      }
      countEl.textContent = docs.length + " doc" + (docs.length === 1 ? "" : "s") + (compare ? " + compare" : "");
      startBtn.disabled = docs.length === 0;
      startHint.textContent = docs.length ? `ready: ${docs.length} document(s)` : "add at least one document";
    }

    // primary drop: accumulate documents
    drop.onclick = () => input.click();
    input.onchange = () => { if (input.files.length) { docs.push(...input.files); input.value = ""; render(); } };
    drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("hover"); };
    drop.ondragleave = () => drop.classList.remove("hover");
    drop.ondrop = (e) => {
      e.preventDefault(); drop.classList.remove("hover");
      const fs = [...e.dataTransfer.files]; if (fs.length) { docs.push(...fs); render(); }
    };

    // secondary "compare with SAP" drop: an image -> sap_file (a SAP screenshot),
    // a workbook -> template_file (the filled request form). One compare per packet.
    const sapDrop = document.getElementById("drop-sap");
    const sapInput = document.getElementById("sap-file");
    const setCompare = (f) => {
      if (!f) return;
      const e = (f.name.split(".").pop() || "").toLowerCase();
      compare = { file: f, kind: (e === "xlsx" || e === "xlsm") ? "tpl" : "sap" };
      render();
    };
    if (sapDrop) {
      sapDrop.onclick = () => sapInput.click();
      sapInput.onchange = () => { setCompare(sapInput.files[0]); sapInput.value = ""; };
      sapDrop.ondragover = (e) => { e.preventDefault(); sapDrop.classList.add("hover"); };
      sapDrop.ondragleave = () => sapDrop.classList.remove("hover");
      sapDrop.ondrop = (e) => { e.preventDefault(); sapDrop.classList.remove("hover"); setCompare(e.dataTransfer.files[0]); };
    }

    listEl.onclick = (e) => {
      const b = e.target.closest(".rm"); if (!b) return;
      if (b.dataset.cmp) compare = null; else docs.splice(+b.dataset.i, 1);
      render();
    };
    document.getElementById("btn-clear").onclick = () => { docs = []; compare = null; render(); };

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
        // j.started is already ISO-with-Z (runstore.now_iso) — appending a
        // second "Z" made Date.parse return NaN and <progress>.value THROW
        const startedMs = Date.parse(j.started);
        if (Number.isFinite(startedMs)) {
          const elapsed = (Date.now() - startedMs) / 1000;
          const eta = Math.min(96, Math.round(100 * elapsed / j.estimate_s));
          if (Number.isFinite(eta)) pct = Math.max(pct, Math.min(pct + 12, eta));
        }
      }
      if (!Number.isFinite(pct)) pct = 0;
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
      // E2: EVERY active kind is visible (retrain/note-rule/skill-import/eval…),
      // and page reloads pick running work back up — nothing is "lost"
      const rows = all.filter(j => tracked.has(j.id) ||
                                   j.status === "queued" || j.status === "running");
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

    // one document -> one /check job; resolves with the run result (or null)
    function send(file) {
      say(`uploading ${file.name} — class: ${docClass()}, mode: ${modeVal() || "default"}…`);
      const fd = new FormData();
      fd.append("file", file);
      fd.append("doc_class", docClass());
      const langEl = document.getElementById("lang");
      fd.append("lang", (langEl && langEl.value) || "en");
      fd.append("wait", "false");
      const md = modeVal();
      if (md) fd.append("mode", md);
      if (compare) fd.append(compare.kind === "sap" ? "sap_file" : "template_file", compare.file);
      return api("/api/v1/check", { method: "POST", body: fd }).then(({ job_id }) => {
        trackJob(job_id);
        return new Promise((resolve) => {
          pollJob(job_id, say,
            (res) => resolve(res),
            (err) => { say("ERROR: " + err); resolve(null); },
            (j) => updateProgress(j));
        });
      });
    }

    startBtn.onclick = async () => {
      if (!docs.length) return;
      startBtn.disabled = true;
      log.hidden = false; log.textContent = "";
      const packet = docs.slice();
      let firstRun = null;
      for (const f of packet) {
        try {
          const res = await send(f);
          if (res && res.run_id && !firstRun) firstRun = res.run_id;
        } catch (e) { say("ERROR: " + e.message); }
      }
      if (packet.length === 1 && firstRun) { say("opening run page…"); location = `/ui/runs/${firstRun}`; }
      else { say("done — see Activity or Recent checks below"); docs = []; compare = null; render(); }
    };
  }

  /* ---------- settings: defaults (engine / effort / language) ------------- */
  function initDefaults() {
    const save = async (body, stateEl) => {
      try {
        await api("/api/v1/settings", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        if (stateEl) { stateEl.textContent = "✓ saved"; setTimeout(() => stateEl.textContent = "", 1500); }
      } catch (e) { if (stateEl) stateEl.textContent = "ERROR: " + e.message; }
    };
    const mode = document.getElementById("mode-default");
    const modeState = document.getElementById("mode-default-state");
    if (mode) mode.onchange = () => save({ default_mode: mode.value }, modeState);

    const engine = document.getElementById("engine-default");
    const engineState = document.getElementById("engine-default-state");
    if (engine && !engine.disabled) engine.onchange = () => save({ engine: engine.value }, engineState);

    const lang = document.getElementById("lang-default");
    const langState = document.getElementById("lang-default-state");
    if (lang) lang.onchange = () => save({ lang: lang.value }, langState);
  }

  /* ---------- settings: tag manager -------------------------------------- */
  function initTagsAdmin() {
    const list = document.getElementById("tag-list");
    if (!list) return;
    const flash = document.getElementById("tag-flash");
    const add = document.getElementById("new-tag-add");
    add.onclick = async () => {
      const name = document.getElementById("new-tag-name");
      const group = document.getElementById("new-tag-group");
      if (!name.value.trim()) { name.focus(); return; }
      add.disabled = true;
      try {
        await api("/api/v1/tags", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: name.value.trim(), group: group.value }),
        });
        location.reload();
      } catch (e) { flash.textContent = "ERROR: " + e.message; add.disabled = false; }
    };
    list.addEventListener("click", async (e) => {
      const b = e.target.closest("[data-deltag]");
      if (!b) return;
      if (!confirm("Delete this tag? It will be removed from every run it's on.")) return;
      b.disabled = true;
      try { await api(`/api/v1/tags/${b.dataset.deltag}`, { method: "DELETE" }); location.reload(); }
      catch (err) { flash.textContent = "ERROR: " + err.message; b.disabled = false; }
    });
  }

  /* ---------- activity: active-jobs poller (unified feed page) ------------ */
  function initActivityJobs() {
    const ul = document.getElementById("act-active");
    if (!ul) return;
    const tick = async () => {
      let all = [];
      try { all = await api("/api/v1/jobs"); } catch (e) { return; }
      const act = all.filter(j => j.status === "queued" || j.status === "running");
      ul.innerHTML = "";
      if (!act.length) { ul.innerHTML = '<li class="meta">nothing running</li>'; return; }
      act.forEach(j => {
        const li = document.createElement("li");
        li.className = "queue-row";
        const state = j.status === "running"
          ? (j.stage ? `running · ${j.stage} ${j.percent || 0}%` : "running")
          : (j.queue_pos > 0 ? `waiting #${j.queue_pos}` : "starting…");
        li.innerHTML = `<span class="dot dot--WARNING"></span>` +
          `<span class="grow"><strong>${j.label || j.kind}</strong> — ${state}</span>` +
          (j.status === "running" ? `<progress max="100" value="${j.percent || 0}"></progress>` : "");
        if (j.cancelable) {
          const btn = document.createElement("button");
          btn.type = "button"; btn.className = "queue-cancel"; btn.textContent = "Cancel";
          btn.onclick = () => { btn.disabled = true; api(`/api/v1/jobs/${j.id}/cancel`, { method: "POST" }).catch(() => {}); };
          li.appendChild(btn);
        }
        ul.appendChild(li);
      });
    };
    tick();
    setInterval(tick, 2000);
  }

  /* ---------- activity: unified run feed + tag filter/assign -------------- */
  function initActivity() {
    const listEl = document.getElementById("act-list");
    if (!listEl) return;
    const feedEl = document.getElementById("feed-data");
    const tagsEl = document.getElementById("tags-data");
    let feed = [], tags = [];
    try { feed = JSON.parse(feedEl.textContent || "[]"); } catch (e) { feed = []; }
    try { tags = JSON.parse(tagsEl.textContent || "[]"); } catch (e) { tags = []; }
    const tagFilterEl = document.getElementById("act-tagfilter");
    const countEl = document.querySelector("[data-actcount]");
    const filter = { type: "", vbucket: "", q: "", tag: "" };
    const byId = (id) => tags.filter(t => t.id === id)[0];
    const esc = (s) => String(s == null ? "" : s);

    function renderTagFilter() {
      tagFilterEl.querySelectorAll(".tagchip,.emptytag").forEach(n => n.remove());
      if (!tags.length) {
        const s = document.createElement("span");
        s.className = "hint emptytag";
        s.innerHTML = 'no tags yet — create one in <a href="/ui/settings/tags">Settings → Tags</a>';
        tagFilterEl.appendChild(s);
        return;
      }
      tags.forEach(t => {
        const c = document.createElement("span");
        c.className = "tagchip" + (filter.tag === t.id ? " on" : "");
        c.innerHTML = `<span class="swatch" style="background:${t.color}"></span>`;
        c.appendChild(document.createTextNode(t.name));
        c.title = (t.group ? t.group + " · " : "") + t.name;
        c.onclick = () => { filter.tag = filter.tag === t.id ? "" : t.id; renderTagFilter(); renderRows(); };
        tagFilterEl.appendChild(c);
      });
    }

    function renderRows() {
      listEl.innerHTML = "";
      let shown = 0;
      feed.forEach(r => {
        if (filter.type && r.type !== filter.type) return;
        if (filter.vbucket && r.vbucket !== filter.vbucket) return;
        if (filter.tag && (r.tags || []).indexOf(filter.tag) < 0) return;
        if (filter.q && (esc(r.name) + " " + esc(r.entity)).toLowerCase().indexOf(filter.q) < 0) return;
        shown++;
        const li = document.createElement("li");
        li.className = "run-row";
        li.dataset.entity = r.entity;
        const chips = (r.tags || []).map(id => {
          const t = byId(id); if (!t) return "";
          return `<span class="run-tag"><span class="swatch" style="background:${t.color}"></span>${esc(t.name)}` +
                 ` <span class="x" data-untag="${id}" title="remove">&times;</span></span>`;
        }).join("");
        const opts = tags.length
          ? tags.map(t => {
              const on = (r.tags || []).indexOf(t.id) >= 0;
              return `<button class="menu-item" data-tagopt="${t.id}"><span class="swatch" style="background:${t.color}"></span>` +
                     (t.group ? `<span class="meta">${esc(t.group)}·</span>` : "") + esc(t.name) + (on ? " ✓" : "") + `</button>`;
            }).join("")
          : '<span class="menu-item meta">create tags in Settings → Tags</span>';
        const dot = r.verdict ? `dot--${r.verdict}` : "";
        const link = r.link || "#";
        li.innerHTML =
          `<span class="dot ${dot}"></span>` +
          `<span class="meta" style="min-width:118px">${esc(r.section)}</span>` +
          `<a class="grow" href="${link}"></a>` +
          `<span class="row-tags">${chips}</span>` +
          `<details class="menu"><summary class="add-tag">+ tag</summary><div class="menu-body">${opts}</div></details>` +
          `<span class="meta">${esc(r.ts)}</span>`;
        li.querySelector("a.grow").textContent = r.sub ? `${r.name} · ${r.sub}` : r.name;
        listEl.appendChild(li);
      });
      if (countEl) countEl.textContent = shown + " of " + feed.length;
    }

    async function assign(entity, tagId, on) {
      const r = feed.filter(x => x.entity === entity)[0];
      if (!r) return;
      try {
        const res = await api("/api/v1/tags/assign", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ entity, tag: tagId, on }),
        });
        r.tags = res.tags || [];
        renderRows();
      } catch (e) { /* leave the row as-is on failure */ }
    }

    listEl.addEventListener("click", (e) => {
      const li = e.target.closest("li[data-entity]"); if (!li) return;
      const entity = li.dataset.entity;
      const opt = e.target.closest("[data-tagopt]");
      if (opt) {
        const r = feed.filter(x => x.entity === entity)[0];
        const on = !(r && (r.tags || []).indexOf(opt.dataset.tagopt) >= 0);
        const d = li.querySelector("details"); if (d) d.open = false;
        assign(entity, opt.dataset.tagopt, on);
        return;
      }
      const x = e.target.closest("[data-untag]");
      if (x) assign(entity, x.dataset.untag, false);
    });

    document.querySelectorAll("#act-filter .seg[data-fkey]").forEach(sg => {
      sg.addEventListener("click", (e) => {
        const b = e.target.closest("button"); if (!b) return;
        sg.querySelectorAll("button").forEach(x => x.classList.remove("active"));
        b.classList.add("active");
        filter[sg.dataset.fkey] = b.dataset.v;
        renderRows();
      });
    });
    const search = document.getElementById("act-search");
    if (search) search.addEventListener("input", () => { filter.q = search.value.trim().toLowerCase(); renderRows(); });

    renderTagFilter();
    renderRows();
  }

  /* ---------- run page: section tabs (N3) --------------------------------- */
  function initRunTabs() {
    const seg = document.getElementById("run-tabs");
    if (!seg) return;
    const panels = [...document.querySelectorAll("[data-panel]")];
    const btns = [...seg.querySelectorAll("button")];
    if (!btns.length) return;
    const first = btns[0].dataset.panelTarget;

    function show(name) {
      const target = btns.some(b => b.dataset.panelTarget === name) ? name : first;
      btns.forEach(b => b.classList.toggle("active", b.dataset.panelTarget === target));
      panels.forEach(p => { p.hidden = p.dataset.panel !== target; });
      return target;
    }
    btns.forEach(b => b.onclick = () => {
      show(b.dataset.panelTarget);
      history.replaceState(null, "", "#" + b.dataset.panelTarget);
    });
    // deep links: #findings, and #propose-box (the dispute anchor) -> Findings
    const h = (location.hash || "").slice(1);
    show(h === "propose-box" ? "findings" : (h || first));
    mdmdoc.showRunPanel = show;
  }

  /* ---------- run page: rate + save gold + revise verdict (D11) ---------- */
  function initValidRate() {
    const seg = document.getElementById("rate-seg");
    const goldBtn = document.getElementById("btn-gold");
    // 👍 / 👎 rating — a 👍 reveals the explicit "Save gold case" action
    if (seg) {
      seg.querySelectorAll("button").forEach(b => b.onclick = async () => {
        try {
          await api(`/api/v1/runs/${seg.dataset.run}/rating`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ rating: b.dataset.r }),
          });
          seg.querySelectorAll("button").forEach(x => x.classList.remove("active"));
          b.classList.add("active");
          if (goldBtn) goldBtn.hidden = b.dataset.r !== "up";
        } catch (e) { /* non-fatal */ }
      });
    }
    // Save gold case — keep-all confirm: no verdict opinion, no challenges, no retrain
    if (goldBtn) {
      goldBtn.onclick = async () => {
        goldBtn.disabled = true;
        try {
          await api(`/api/v1/runs/${goldBtn.dataset.run}/confirm-gold`, { method: "POST" });
          // api() already popped the ✓ on this button — let it play before we leave
          setTimeout(() => {
            window.location.href = `/ui/runs/${goldBtn.dataset.run}?flash=` +
              encodeURIComponent("saved as a gold case — operator confirmed ✓");
          }, 600);
        } catch (e) { goldBtn.textContent = "ERROR: " + e.message; goldBtn.disabled = false; }
      };
    }
    // Revise ▾ → Revise verdict — pick the correct verdict; "I confirm" softens a stricter one
    const rvForm = document.getElementById("revise-verdict-form");
    if (rvForm) {
      rvForm.addEventListener("submit", async (ev) => {
        ev.preventDefault();
        const btn = rvForm.querySelector("button[type=submit]");
        btn.disabled = true;
        try {
          const res = await api(`/api/v1/runs/${rvForm.dataset.run}/revise-verdict`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              verdict: document.getElementById("verdict-pick").value,
              confirmed: document.getElementById("verdict-confirm").checked,
            }),
          });
          showChallenged(res.challenged || [], rvForm.dataset.class || "bank");
          if (res.rerun_job_id) watchValidRerun(res.rerun_job_id);
        } catch (e) { btn.textContent = "ERROR: " + e.message; btn.disabled = false; }
      });
    }
    // dispute-box "document is VALID" checkbox: fires mark-valid on propose
    const fbValid = document.getElementById("fb-valid");
    const fbGo = document.getElementById("fb-go");
    if (fbValid && fbGo) {
      fbGo.addEventListener("click", async () => {
        if (fbValid.checked) {
          try { await api(`/api/v1/runs/${fbGo.dataset.run}/mark-valid`, { method: "POST" }); }
          catch (e) { /* non-fatal */ }
        }
      });
    }
  }

  /* ---------- run page: teach the document TYPE (F4) --------------------- */
  function initTeachType() {
    const box = document.getElementById("type-teach");
    if (!box) return;
    const sel = document.getElementById("type-select");
    const btn = document.getElementById("btn-teach-type");
    const initial = sel.value;
    sel.onchange = () => { btn.hidden = sel.value === initial; };
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        const res = await api(`/api/v1/runs/${box.dataset.run}/teach-type`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ doc_type: sel.value }),
        });
        btn.textContent = "Taught ✓";
        if (res.rerun_job_id) watchValidRerun(res.rerun_job_id);
      } catch (e) { btn.textContent = "ERROR: " + e.message; btn.disabled = false; }
    };
  }

  /* ---------- run page: mark-valid aftermath (E4) ------------------------ */
  // the rules the valid-mark overrode, each with a way to act on it NOW
  function showChallenged(challenged, cls) {
    let box = document.getElementById("challenged-box");
    if (!box) {
      box = document.createElement("section");
      box.id = "challenged-box";
      const anchor = document.getElementById("gate-panel") ||
                     document.querySelector("[data-panel='findings']") || document.body;
      anchor.parentNode.insertBefore(box, anchor);
    }
    if (!challenged.length) {
      box.innerHTML = `<h2>Verdict revised</h2><p class="meta">no rules were overridden — re-running to refresh the verdict…</p>`;
      return;
    }
    box.innerHTML = `<h2>Challenged rules</h2>
      <p class="meta">your verdict contradicts these rules — recorded in the challenge
      ledger; edit, reject or delete them (or leave them — the count stays visible on Approvals)</p>
      <ul class="timeline">` + challenged.map(c => `
      <li class="queue-row" data-rule="${c.id}">
        <code>${c.id}</code><span class="meta">${c.name || ""}</span>
        ${c.source ? `<span class="chip">${c.source}</span>` : ""}
        ${c.tier ? `<span class="chip">${c.tier}</span>` : ""}
        <span class="grow"></span>
        <button type="button" class="ch-act" data-act="rejected" data-rule="${c.id}">Reject</button>
        <button type="button" class="ch-act ch-del" data-act="delete" data-rule="${c.id}">Delete</button>
        <a href="/ui/rules" title="open the rules editor">Edit</a>
      </li>`).join("") + `</ul><p class="meta" id="ch-flash"></p>`;
    box.querySelectorAll(".ch-act").forEach(btn => btn.onclick = async () => {
      const rule = btn.dataset.rule, act = btn.dataset.act;
      if (act === "delete" &&
          !confirm(`Rule ${rule} will be PHYSICALLY removed from the rules file ` +
                   `(a backup lands in rules/deleted/). Delete?`)) return;
      const row = btn.closest("li");
      row.querySelectorAll("button").forEach(b => b.disabled = true);
      try {
        if (act === "delete") {
          await api(`/api/v1/rules/${cls}/delete`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ rule_id: rule }),
          });
        } else {
          await api(`/api/v1/rules/${cls}/approve`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ rule_id: rule, decision: act }),
          });
        }
        row.style.opacity = 0.45;
        document.getElementById("ch-flash").textContent = `${rule}: ${act === "delete" ? "deleted" : act}`;
      } catch (e) {
        document.getElementById("ch-flash").textContent = "ERROR: " + e.message;
        row.querySelectorAll("button").forEach(b => b.disabled = false);
      }
    });
  }

  // poll the auto re-run; when it lands, jump to the fresh run (new verdict)
  function watchValidRerun(jobId) {
    const log = document.getElementById("job-log");
    const say = (l) => { if (log) { log.hidden = false; log.textContent += l + "\n"; log.scrollTop = 1e9; } };
    say("re-running to apply your change…");
    pollJob(jobId,
      say,
      (result) => {
        const rid = result && result.run_id;
        if (rid) window.location.href = `/ui/runs/${rid}?flash=` +
          encodeURIComponent("re-run after your revision — this is the fresh verdict");
        else window.location.reload();
      },
      (err) => say("re-run failed (" + err + ") — press Re-analyze to retry"));
  }

  /* ---------- run page: gate panel + finding challenges (E3/E5) ---------- */
  function initGatePanel() {
    const panel = document.getElementById("gate-panel");
    const rerun = document.getElementById("gate-rerun");
    const flash = document.getElementById("gate-flash");
    if (panel) {
      panel.querySelectorAll(".gate-act").forEach(btn => btn.onclick = async () => {
        const row = btn.closest(".gate-row");
        const rule = row.dataset.rule, cls = row.dataset.class;
        const act = btn.dataset.act;
        if (act === "delete" &&
            !confirm(`Rule ${rule} will be PHYSICALLY removed from the rules file ` +
                     `(a backup lands in rules/deleted/). Delete?`)) return;
        row.querySelectorAll("button").forEach(b => b.disabled = true);
        try {
          if (act === "delete") {
            await api(`/api/v1/rules/${cls}/delete`, {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ rule_id: rule }),
            });
            row.style.opacity = 0.45;
            flash.textContent = `${rule} deleted (backup in rules/deleted/)`;
          } else {
            await api(`/api/v1/rules/${cls}/approve`, {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ rule_id: rule, decision: act }),
            });
            row.style.opacity = 0.45;
            flash.textContent = `${rule}: ${act}`;
          }
          if (rerun) rerun.hidden = false;
        } catch (e) {
          flash.textContent = "ERROR: " + e.message;
          row.querySelectorAll("button").forEach(b => b.disabled = false);
        }
      });
      if (rerun) rerun.onclick = () => {
        const btn = document.getElementById("btn-rerun");
        if (btn) btn.click();
        else location.reload();
      };
    }
    // per-finding 👎: record a challenge, then show the contradiction warning
    document.querySelectorAll(".fb-vote").forEach(btn => btn.onclick = async () => {
      btn.disabled = true;
      try {
        const res = await api(`/api/v1/runs/${btn.dataset.run}/findings/${btn.dataset.rule}/vote`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ vote: "down" }),
        });
        const li = btn.closest("li");
        const warn = document.createElement("div");
        warn.className = "meta";
        warn.style.cssText = "flex-basis:100%;color:var(--warn,#b58900)";
        warn.innerHTML = `⚠ recorded: this response contradicts rule <code>${btn.dataset.rule}</code> ` +
          `(challenged ×${res.challenges}) — edit or delete it: ` +
          `<a href="/ui/rules/approve">Approvals</a> · <a href="/ui/rules">Editor</a>`;
        li.appendChild(warn);
        btn.textContent = "👎✓";
      } catch (e) { btn.textContent = "ERR"; btn.title = e.message; }
    });
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

  /* ---------- bulk page (V-wave) ----------------------------------------- */
  function initBulk() {
    const drop = document.getElementById("bulk-drop");
    if (!drop) return;
    const input = document.getElementById("bulk-file-input");
    const log = document.getElementById("bulk-log");
    const seg = document.getElementById("bulk-case-seg");
    const webBox = document.getElementById("bulk-web");
    const refBtn = document.getElementById("bulk-ref-attach");
    const refInput = document.getElementById("bulk-ref-input");
    const refNames = document.getElementById("bulk-ref-names");
    const progRow = document.getElementById("bulk-progress-row");
    const bar = document.getElementById("bulk-progress");
    const stageEl = document.getElementById("bulk-stage");
    const cancelBtn = document.getElementById("bulk-cancel");
    let refs = [];

    seg.querySelectorAll("button").forEach(b => b.onclick = () => {
      seg.querySelectorAll("button").forEach(x => x.classList.remove("active"));
      b.classList.add("active");
    });
    const caseOf = () => seg.querySelector(".active").dataset.v;
    const say = (l) => { log.hidden = false; log.textContent += l + "\n"; log.scrollTop = 1e9; };

    refBtn.onclick = () => refInput.click();
    refInput.onchange = () => {
      refs = [...refInput.files];
      refNames.textContent = refs.length ? refs.map(f => f.name).join(", ") + " ✓" : "";
    };

    async function send(file) {
      log.hidden = false; log.textContent = "";
      say(`uploading ${file.name} (case: ${caseOf()})…`);
      const fd = new FormData();
      fd.append("file", file);
      fd.append("case", caseOf());
      if (webBox && webBox.checked) fd.append("web", "true");
      refs.forEach(r => fd.append("refs", r));
      try {
        const { job_id } = await api("/api/v1/bulk", { method: "POST", body: fd });
        progRow.hidden = false; cancelBtn.hidden = false;
        cancelBtn.onclick = async () => {
          cancelBtn.disabled = true;
          try { await api(`/api/v1/jobs/${job_id}/cancel`, { method: "POST" }); }
          catch (e) { /* already done */ }
        };
        let n = 0;
        pollJob(job_id, (l) => { say(l); n += 1; bar.value = Math.min(95, n * 6); },
          (res) => { cancelBtn.hidden = true; bar.value = 100; renderResult(res); },
          (err) => { cancelBtn.hidden = true; say("ERROR: " + err); },
          (j) => { if (j.stage) stageEl.textContent = j.stage; });
      } catch (e) { say("ERROR: " + e.message); }
    }

    async function renderResult(res) {
      const box = document.getElementById("bulk-result");
      box.hidden = false;
      document.getElementById("bulk-result-file").textContent =
        `${res.summary.source_file} — ${res.case} (${res.summary.source_kind}), ${res.summary.total} rows`;
      document.getElementById("bulk-dl-result").href = `/api/v1/bulk/${res.bulk_id}/result`;
      document.getElementById("bulk-dl-report").href = `/api/v1/runs/${res.bulk_id}/artifacts/bulk_report.md`;
      document.getElementById("bulk-dl-json").href = `/api/v1/runs/${res.bulk_id}/artifacts/bulk_report.json`;
      const counts = document.querySelector("#bulk-counts tbody");
      counts.innerHTML = "";
      Object.entries(res.summary.counts).forEach(([b, n]) => {
        counts.innerHTML += `<tr><td>${b}</td><td>${n}</td></tr>`;
      });
      const top = document.querySelector("#bulk-top tbody");
      top.innerHTML = "";
      (res.summary.top_rules || []).forEach(t => {
        top.innerHTML += `<tr><td><code>${t.rule_id}</code></td><td>${t.rows} rows</td></tr>`;
      });
      try {
        const rep = await api(`/api/v1/runs/${res.bulk_id}/artifacts/bulk_report.json`);
        const tb = document.querySelector("#bulk-problems tbody");
        tb.innerHTML = "";
        (rep.problem_rows || []).slice(0, 200).forEach(r => {
          const tr = document.createElement("tr");
          tr.innerHTML = `<td>${r.row}</td><td>${r.partner}</td>` +
            `<td><span class="chip chip--${r.bucket === "INVALID" ? "CRITICAL" : r.bucket === "SUSPICIOUS" ? "WARNING" : "NOTE"}">${r.bucket}</span></td>` +
            `<td><code>${(r.rules || []).join(", ")}</code></td><td></td>`;
          tr.lastElementChild.textContent = (r.reasons || []).join(" | ");
          tb.appendChild(tr);
        });
      } catch (e) { say("report fetch failed: " + e.message); }
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

  /* ---------- run page: file this run under Test runs, or take it back ---- */
  function initTestToggle() {
    const btn = document.getElementById("btn-test");
    if (!btn) return;
    btn.onclick = async () => {
      btn.disabled = true;
      const next = btn.dataset.test !== "1";
      try {
        await api(`/api/v1/runs/${btn.dataset.run}/test`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ test: next }),
        });
        location = next ? "/ui/test" : "/ui";
      } catch (e) { alert("could not move the run: " + e.message); btn.disabled = false; }
    };
  }

  /* ---------- run page: re-analyze against the CURRENT rules -------------- */
  function initRerun() {
    const btn = document.getElementById("btn-rerun");
    if (!btn) return;
    const log = document.getElementById("job-log");
    const say = (l) => { log.hidden = false; log.textContent += l + "\n"; log.scrollTop = 1e9; };
    btn.onclick = async () => {
      btn.disabled = true;
      log.textContent = "";
      say("re-analyzing this document with the rules as they stand now…");
      const fd = new FormData();
      fd.append("doc_class", btn.dataset.class || "auto");
      fd.append("wait", "false");
      fd.append("rerun_run_id", btn.dataset.run);
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
          ' <a href="' + (res.route || ("/ui/runs/" + go.dataset.run + "/review")) + '">Revise fields →</a>';
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
  /* One filter bar for every list in the console (N2).
     Markup contract:
       <div class="filterbar" data-filter-root data-filter-list="runs-list">
         <input data-filter-search>                       -> matches row data-search
         <span class="seg" data-filter-key="verdict">     -> matches row data-verdict
           <button class="active" data-filter-val="">any</button>
           <button data-filter-val="accept">accept</button>
     A row shows when the search substring matches AND every seg whose active
     button carries a non-empty value equals that row's data-<key>. */
  function initFilterBar(root) {
    root = root || document.querySelector("[data-filter-root]");
    if (!root) return;
    const list = document.getElementById(root.dataset.filterList);
    if (!list) return;
    const search = root.querySelector("[data-filter-search]");
    const segs = [...root.querySelectorAll(".seg[data-filter-key]")];
    const count = root.querySelector("[data-filter-count]");

    function apply() {
      const q = search ? search.value.trim().toLowerCase() : "";
      const cons = segs.map(s => {
        const b = s.querySelector("button.active");
        return [s.dataset.filterKey, b ? (b.dataset.filterVal || "") : ""];
      });
      let shown = 0;
      [...list.children].forEach(row => {
        let show = !q || (row.dataset.search || "").toLowerCase().includes(q);
        for (const [k, v] of cons) if (show && v) show = row.dataset[k] === v;
        row.style.display = show ? "" : "none";
        if (show) shown += 1;
      });
      if (count) count.textContent = shown + " of " + list.children.length;
    }

    segs.forEach(s => s.querySelectorAll("button").forEach(b => b.onclick = () => {
      s.querySelectorAll("button").forEach(x => x.classList.remove("active"));
      b.classList.add("active");
      apply();
    }));
    if (search) search.addEventListener("input", apply);
    apply();
    return apply;   // callers that mutate rows (approvals) re-apply after a repaint
  }

  // legacy name kept so the export list and any cached inline caller keep working
  function initRunFilters() { initFilterBar(); }

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
      // the dashboard used to repeat this in its own strip; it lives here now
      const roles = d.roles || {};
      chip.title = [
        ok ? "model host: " + d.model_host.source : "model host unreachable",
        roles.TEXT ? "TEXT " + roles.TEXT.resolved : "",
        roles.VISION ? "VISION " + roles.VISION.resolved : "",
        "tesseract " + ((d.tesseract && d.tesseract.path) ? "ok" : "missing"),
      ].filter(Boolean).join(" · ");
    } catch (e) { chip.textContent = ""; }
  }
  refreshChip();
  setInterval(refreshChip, 60000);

  initFilterBar();
  initCopyReport();

  /* ---------- Activity nav badge (all pages, E2) -------------------------- */
  function initActivityBadge() {
    const badge = document.getElementById("activity-badge");
    if (!badge) return;
    const tick = async () => {
      try {
        const all = await api("/api/v1/jobs");
        const n = (all || []).filter(j => j.status === "queued" || j.status === "running").length;
        badge.hidden = n === 0;
        badge.textContent = n ? " ●" + n : "";
      } catch (e) { /* ignore */ }
    };
    tick();
    setInterval(tick, 5000);
  }
  initActivityBadge();

  /* ---------- consolidation: analyze a document & attach it (Stage 2) ---- */
  function initConsolidationAttach() {
    document.querySelectorAll("[data-attach-vkey]").forEach(box => {
      const vkey = box.dataset.attachVkey, caseId = box.dataset.case;
      const btn = box.querySelector(".attach-analyze");
      const fileInput = box.querySelector(".attach-file");
      const clsSel = box.querySelector(".attach-class");
      const log = box.querySelector(".attach-log");
      if (!btn) return;
      const say = (l) => { log.hidden = false; log.textContent += l + "\n"; log.scrollTop = 1e9; };
      btn.onclick = async () => {
        const file = fileInput.files[0];
        if (!file) { say("pick a document first"); return; }
        btn.disabled = true; log.hidden = false; log.textContent = "";
        say("analyzing " + file.name + " …");
        try {
          const fd = new FormData();
          fd.append("file", file);
          fd.append("doc_class", clsSel.value);
          fd.append("wait", "false");
          const { job_id } = await api("/api/v1/check", { method: "POST", body: fd });
          pollJob(job_id, say,
            async (res) => {
              say("analysis done — attaching run " + res.run_id);
              const af = new FormData();
              af.append("run_id", res.run_id);
              af.append("confirm", "on");   // WARNING runs allowed from here
              try {
                await api(`/ui/consolidation/${caseId}/vendor/${vkey}/attach-run`,
                          { method: "POST", body: af });
                location.reload();
              } catch (e) { say("attach failed: " + e.message); btn.disabled = false; }
            },
            (err) => { say("failed: " + err); btn.disabled = false; },
            null);
        } catch (e) { say("failed: " + e.message); btn.disabled = false; }
      };
    });
  }
  initConsolidationAttach();

  return { api, flashOk, pollJob, initDropZone, initTplCompare, initValidRate, initGatePanel, initTeachType, initArtifacts, initReview, initTraining,
           initSapCompare, initWebVerify, initProposeFix, initFieldCopy, initBankCheck,
           initRetrainWatch, initBulk, initFilterBar, initRunFilters, initRunTabs, initRerun, initTestToggle,
           initConsolidationAttach, initDefaults, initTagsAdmin, initActivity, initActivityJobs };
})();

/* ======================================================================
   Shell (2026-08-23, consolidator design system): theme toggle, split
   divider, document viewer highlight, live extract queue, reveal stagger.
   Independent of window.mdmdoc — runs on every page.
   ====================================================================== */
(() => {
  // ---- light / dark theme ------------------------------------------------
  const btn = document.querySelector("[data-theme-toggle]");
  const paint = () => { if (btn) btn.textContent = document.body.classList.contains("dark") ? "☀" : "☾"; };
  paint();
  if (btn) btn.addEventListener("click", () => {
    const dark = !document.body.classList.contains("dark");
    document.body.classList.toggle("dark", dark);
    try { localStorage.setItem("mdmdoc.theme", dark ? "dark" : "light"); } catch (e) {}
    paint();
  });

  // ---- reveal stagger: rows/sections fade in one after another ----------
  document.querySelectorAll(".f-row, .timeline li, .list tbody tr, main > section").forEach((el, i) => {
    if (!el.style.getPropertyValue("--i")) el.style.setProperty("--i", String(Math.min(i, 24)));
  });

  // ---- draggable divider (port of the consolidator's) ---------------------
  const divider = document.querySelector("[data-split-divider]");
  const left = document.querySelector("[data-split-left]");
  if (divider && left) {
    const saved = parseInt(localStorage.getItem("mdmdoc.leftW") || "", 10);
    if (saved) left.style.width = Math.max(320, saved) + "px";
    const setCollapsed = (on) => {
      left.classList.toggle("is-collapsed", on);
      divider.classList.toggle("is-collapsed", on);
      localStorage.setItem("mdmdoc.leftCollapsed", on ? "1" : "0");
    };
    if (localStorage.getItem("mdmdoc.leftCollapsed") === "1") setCollapsed(true);
    let dragged = false;
    const start = (ev) => {
      ev.preventDefault();
      const startX = ev.touches ? ev.touches[0].clientX : ev.clientX;
      const startW = left.getBoundingClientRect().width;
      dragged = false;
      document.body.style.userSelect = "none";
      divider.classList.add("is-dragging");
      const move = (e) => {
        const x = e.touches ? e.touches[0].clientX : e.clientX;
        const raw = startW + (x - startX);
        if (Math.abs(x - startX) > 3) dragged = true;
        if (raw < 150) { setCollapsed(true); return; }
        setCollapsed(false);
        const max = Math.max(360, window.innerWidth - 360);
        left.style.width = Math.max(320, Math.min(raw, max)) + "px";
      };
      const up = () => {
        window.removeEventListener("mousemove", move); window.removeEventListener("mouseup", up);
        window.removeEventListener("touchmove", move); window.removeEventListener("touchend", up);
        document.body.style.userSelect = "";
        divider.classList.remove("is-dragging");
        if (!left.classList.contains("is-collapsed"))
          localStorage.setItem("mdmdoc.leftW", String(Math.round(left.getBoundingClientRect().width)));
      };
      window.addEventListener("mousemove", move); window.addEventListener("mouseup", up);
      window.addEventListener("touchmove", move, { passive: false }); window.addEventListener("touchend", up);
    };
    divider.addEventListener("mousedown", start);
    divider.addEventListener("touchstart", start, { passive: false });
    divider.addEventListener("dblclick", () => setCollapsed(!left.classList.contains("is-collapsed")));
    divider.addEventListener("click", () => { if (!dragged && left.classList.contains("is-collapsed")) setCollapsed(false); });
  }

  // ---- document viewer: zoom, page bars, value → highlight on the page ----
  const viewer = document.querySelector("[data-doc-viewer]");
  if (viewer) {
    const pages = [...viewer.querySelectorAll(".doc-page")];
    viewer.querySelectorAll("[data-zoom]").forEach((b) => b.addEventListener("click", () => {
      viewer.querySelectorAll("[data-zoom]").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      pages.forEach((p) => p.style.setProperty("--zoom", b.dataset.zoom));
    }));
    pages.forEach((p) => {
      const img = p.querySelector("img");
      if (img) {
        const done = () => p.classList.remove("is-loading");
        if (img.complete) done(); else { img.addEventListener("load", done); img.addEventListener("error", done); }
      }
    });
    const bars = [...document.querySelectorAll("[data-vnav] a")];
    const io = new IntersectionObserver((entries) => entries.forEach((en) => {
      if (en.isIntersecting) {
        bars.forEach((a) => a.classList.toggle("current", a.dataset.page === en.target.dataset.page));
      }
    }), { root: viewer, threshold: 0.4 });
    pages.forEach((p) => io.observe(p));
    document.querySelectorAll(".f-row[data-page], .t-seg[data-page]").forEach((row) => row.addEventListener("click", () => {
      document.querySelectorAll(".f-row.is-active, .t-seg.is-active").forEach((r) => r.classList.remove("is-active"));
      document.querySelectorAll(".hl.is-on").forEach((h) => h.classList.remove("is-on"));
      row.classList.add("is-active");
      const page = pages.find((p) => p.dataset.page === row.dataset.page);
      if (!page) return;
      const box = (row.dataset.bbox || "").split(",").map(Number);
      let hl = page.querySelector(".hl");
      if (!hl) { hl = document.createElement("div"); hl.className = "hl"; page.appendChild(hl); }
      hl.classList.toggle("is-review", row.classList.contains("is-review"));
      if (box.length === 4 && !box.some(isNaN)) {
        hl.style.left = box[0] + "%"; hl.style.top = box[1] + "%";
        hl.style.width = (box[2] - box[0]) + "%"; hl.style.height = (box[3] - box[1]) + "%";
        hl.classList.remove("is-on"); void hl.offsetWidth;      // restart the settle animation
        hl.classList.add("is-on");
        const top = page.offsetTop + page.offsetHeight * box[1] / 100 - viewer.clientHeight / 2;
        viewer.scrollTo({ top: Math.max(0, top), behavior: "smooth" });
      } else {
        viewer.scrollTo({ top: page.offsetTop - 12, behavior: "smooth" });
      }
    }));
  }

  // ---- extract queue: live without a full reload --------------------------
  const q = document.querySelector("[data-extract-queue]");
  if (q) {
    const chip = (s) => `<span class="chip chip--${s}">${s}</span>`;
    const render = (d) => {
      if (!d.jobs.length) { q.innerHTML = '<li class="meta">nothing running</li>'; return false; }
      q.innerHTML = d.jobs.map((j) => `<li class="${j.status}"><span class="dot dot--${j.status === "running" ? "running" : j.status === "done" ? "ok" : j.status === "error" ? "down" : ""}"></span>` +
        `<span class="grow"><b>${j.label || j.id}</b>${j.progress.length ? ` <span class="meta">— ${j.progress[j.progress.length - 1]}</span>` : ""}` +
        `${j.error ? `<div class="error">${j.error}</div>` : ""}</span>${chip(j.status)}</li>`).join("");
      return d.jobs.some((j) => j.status === "queued" || j.status === "running");
    };
    const tick = async () => {
      try {
        const r = await fetch("/ui/extract/jobs", { credentials: "same-origin" });
        const d = await r.json();
        const busy = render(d);
        if (busy) setTimeout(tick, 3000);
        else if (q.dataset.wasBusy === "1") location.reload();
        q.dataset.wasBusy = busy ? "1" : "0";
      } catch (e) { setTimeout(tick, 8000); }
    };
    tick();
  }
})();
