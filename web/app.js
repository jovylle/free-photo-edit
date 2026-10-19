/* Free Photo Edit — 1-photo self-serve UI. No framework, no build step.
 * Backend contract (same-origin /api/* only — no keys in browser):
 *   GET  /api/config -> {turnstile_sitekey} (null = bot check off, dev)
 *   GET  /api/presets -> {presets: [{id,label,hint}]}
 *   POST /api/edit  multipart(image x1, prompt, preset, user,
 *     cf-turnstile-response)
 *     -> 202 {job_id, status:running, credits_left} | 400 validation
 *        | 402 out of credits / IP cap | 403 bot check | 429 rate limit
 *        | 503 daily budget spent | slot-429 (Retry-After)
 *   GET  /api/result/{id} -> {status, image_url, credits_left}
 *   GET  /api/result/{id}/file -> output PNG when done
 *   GET  /api/result/{id}/input/0 -> stored input thumb
 *   GET  /api/jobs?user= -> own history, newest-first
 *   GET  /api/credits?user= -> {credits_left}
 */
(() => {
  "use strict";
  const MAX_BYTES = 5 * 1024 * 1024;
  const OK_TYPES = ["image/jpeg", "image/png", "image/webp"];
  const USER_KEY = "freephotoedit_user";
  const PROMPT_KEY = "freephotoedit_prompt";
  const PRESET_KEY = "freephotoedit_preset";
  const POLL_MS = 10000;

  const $ = (id) => document.getElementById(id);
  const dropzone = $("dropzone"), fileInput = $("fileInput"), thumbs = $("thumbs"),
        presetsBox = $("presets"), promptEl = $("prompt"), promptLabel = $("promptLabel"),
        submitBtn = $("submitBtn"), statusEl = $("status"),
        creditsBadge = $("creditsBadge"), historyEl = $("history"),
        formBody = $("formBody"), editAnother = $("editAnother"),
        resultBox = $("result"), histCount = $("histCount");

  let file = null;
  let activePreset = "custom";

  const TS_ACTION = "edit";
  let tsWidgetId = null; // explicit render id (page stays alive: reset per try)

  let user = localStorage.getItem(USER_KEY);
  if (!user) {
    user = "u-" + Math.random().toString(36).slice(2, 10);
    localStorage.setItem(USER_KEY, user);
  }
  const timers = new Map();

  const PRESET_COPY = {
    background: { label: "New background (required)",
                  ph: "e.g. beach sunset, blurred city lights at night" },
    clothes: { label: "Clothing change (describe it)",
               ph: "e.g. red dress, barong, formal suit" },
    restore: { label: "Detail (optional — what to fix)",
               ph: "e.g. fix scratches, ayusin ang kulay" },
    custom: { label: "Instruction (required)",
              ph: "e.g. gawing formal ID-style portrait, plain background" },
  };

  const uq = "?user=" + encodeURIComponent(user);

  function setStatus(msg, isError) {
    statusEl.textContent = msg || "";
    statusEl.classList.toggle("error", !!isError);
  }

  function setCredits(n) {
    creditsBadge.textContent = "credits left: " + (n == null ? "…" : n);
  }

  async function initTurnstile() {
    let sitekey = null;
    try {
      const r = await fetch("/api/config");
      if (r.ok) sitekey = (await r.json()).turnstile_sitekey || null;
    } catch (e) { /* dev: widget stays off */ }
    if (!sitekey) return;
    await new Promise((resolve) => {
      const s = document.createElement("script");
      s.src = "https://challenges.cloudflare.com/turnstile/v0/api.js";
      s.async = true;
      s.defer = true;
      s.onload = resolve;
      s.onerror = resolve; // offline dev: submit still works, server decides
      document.head.appendChild(s);
    });
    if (!window.turnstile) return;
    try {
      tsWidgetId = window.turnstile.render("#tsWidget", {
        sitekey: sitekey, action: TS_ACTION, theme: "dark",
      });
    } catch (e) { tsWidgetId = null; }
  }

  function tsToken() {
    try {
      return (tsWidgetId !== null && window.turnstile)
        ? window.turnstile.getResponse(tsWidgetId) || "" : "";
    } catch (e) { return ""; }
  }

  function tsReset() {
    try {
      if (tsWidgetId !== null && window.turnstile)
        window.turnstile.reset(tsWidgetId);
    } catch (e) { /* next submit re-renders state anyway */ }
  }

  async function refreshCredits() {
    try {
      const r = await fetch("/api/credits?user=" + encodeURIComponent(user));
      if (!r.ok) throw new Error("credits " + r.status);
      setCredits((await r.json()).credits_left);
    } catch (e) {
      creditsBadge.textContent = "credits left: ?";
      creditsBadge.title = "Could not reach backend: " + e.message;
    }
  }

  function selectPreset(id, save = true) {
    activePreset = id;
    [...presetsBox.children].forEach((el) =>
      el.classList.toggle("active", el.dataset.id === id));
    const copy = PRESET_COPY[id] || PRESET_COPY.custom;
    promptLabel.textContent = copy.label;
    promptEl.placeholder = copy.ph;
    if (!promptEl.value.trim()) // chip fills an empty box; typed text always wins
      promptEl.value = (copy.ph || "").replace(/^e\.g\.\s*/, "");
    if (save) { try { localStorage.setItem(PRESET_KEY, id); } catch (e) { /* private mode */ } }
  }

  // Reload-proof form: prompt+preset in localStorage, photo blob in IndexedDB
  // (localStorage is too small for images). Failures fall back to a fresh form.
  function idb() {
    return new Promise((resolve, reject) => {
      const q = indexedDB.open("freephotoedit", 1);
      q.onupgradeneeded = () => q.result.createObjectStore("form");
      q.onsuccess = () => resolve(q.result);
      q.onerror = () => reject(q.error);
    });
  }
  async function idbPut(photo) {
    try {
      const db = await idb();
      await new Promise((res, rej) => {
        const tx = db.transaction("form", "readwrite");
        tx.objectStore("form").put(photo, "photo");
        tx.oncomplete = res;
        tx.onerror = () => rej(tx.error);
      });
      db.close();
    } catch (e) { /* form just won't survive reload */ }
  }
  async function idbGet() {
    const db = await idb();
    const v = await new Promise((res, rej) => {
      const q = db.transaction("form").objectStore("form").get("photo");
      q.onsuccess = () => res(q.result);
      q.onerror = () => rej(q.error);
    });
    db.close();
    return v;
  }
  async function idbDel() {
    try {
      const db = await idb();
      await new Promise((res, rej) => {
        const tx = db.transaction("form", "readwrite");
        tx.objectStore("form").delete("photo");
        tx.oncomplete = res;
        tx.onerror = () => rej(tx.error);
      });
      db.close();
    } catch (e) { /* no-op */ }
  }

  async function loadPresets() {
    let list = null;
    try {
      const r = await fetch("/api/presets");
      if (r.ok) list = (await r.json()).presets;
    } catch (e) { /* builtin fallback */ }
    if (!list) {
      list = [
        {id: "background", label: "🏙 Background", hint: "1 photo"},
        {id: "clothes", label: "👔 Clothes", hint: "1 photo"},
        {id: "restore", label: "✨ Restore", hint: "1 photo"},
        {id: "custom", label: "✏ Custom", hint: "free-form"},
      ];
    }
    presetsBox.innerHTML = "";
    list.forEach((p) => {
      const el = document.createElement("div");
      el.className = "preset";
      el.dataset.id = p.id;
      el.setAttribute("role", "radio");
      el.tabIndex = 0;
      el.innerHTML = "<b></b><small></small>";
      el.querySelector("b").textContent = p.label;
      el.querySelector("small").textContent = p.hint;
      const pick = () => selectPreset(p.id);
      el.onclick = pick;
      el.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); } };
      presetsBox.appendChild(el);
    });
    const sp = savedPreset(); // reload keeps the picked chip; typed prompt untouched
    selectPreset(["background", "clothes", "restore", "custom"].includes(sp) ? sp : "custom", false);
  }

  function savedPreset() {
    try { return localStorage.getItem(PRESET_KEY); }
    catch (e) { return null; }
  }

  function renderThumbs() {
    thumbs.innerHTML = "";
    if (!file) return;
    const wrap = document.createElement("div");
    wrap.className = "thumb";
    const img = document.createElement("img");
    img.src = URL.createObjectURL(file);
    img.alt = "upload";
    const rm = document.createElement("button");
    rm.type = "button";
    rm.textContent = "×";
    rm.setAttribute("aria-label", "Remove image");
    rm.onclick = (e) => { e.stopPropagation(); file = null; renderThumbs(); idbDel(); };
    wrap.append(img, rm);
    thumbs.appendChild(wrap);
  }

  function addFile(f) {
    if (!f) return;
    if (!OK_TYPES.includes(f.type)) { setStatus("Rejected: only jpeg/png/webp.", true); return; }
    if (f.size > MAX_BYTES) { setStatus("Rejected: over 5MB.", true); return; }
    file = f;
    renderThumbs();
    idbPut({blob: f, name: f.name, type: f.type});
    setStatus("Photo ready.");
  }

  dropzone.addEventListener("click", (e) => {
    if (e.target.closest(".thumb button")) return;
    fileInput.click();
  });
  dropzone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); }
  });
  fileInput.addEventListener("change", () => { addFile(fileInput.files[0]); fileInput.value = ""; });
  ["dragenter", "dragover"].forEach((ev) => dropzone.addEventListener(ev, (e) => {
    e.preventDefault(); dropzone.classList.add("over");
  }));
  ["dragleave", "drop"].forEach((ev) => dropzone.addEventListener(ev, (e) => {
    e.preventDefault(); dropzone.classList.remove("over");
  }));
  dropzone.addEventListener("drop", (e) => addFile(e.dataTransfer.files[0]));

  document.addEventListener("paste", (e) => {
    const cd = e.clipboardData;
    if (!cd) return;
    const found = [];
    if (cd.items && cd.items.length) {
      for (const it of cd.items) {
        if (it.kind === "file") { const f = it.getAsFile(); if (f) found.push(f); }
      }
    }
    if (!found.length && cd.files && cd.files.length) {
      for (const f of cd.files) found.push(f);
    }
    if (found.length) { e.preventDefault(); addFile(found[0]); }
  });

  function fmtTime(ts) {
    try { return new Date(ts * 1000).toLocaleString(); }
    catch (e) { return ""; }
  }

  function stopPoll(jobId) {
    const t = timers.get(jobId);
    if (t) { clearInterval(t); timers.delete(jobId); }
  }

  function stageResult(j) { // newest result takes the stage under the button
    if (!j || j.status !== "done" || !j.image_url) return;
    resultBox.innerHTML = "";
    resultBox.appendChild(cardFor(j));
    resultBox.classList.remove("hidden");
  }

  async function pollOne(jobId) {
    try {
      const r = await fetch("/api/result/" + jobId + uq);
      if (!r.ok) return;
      const j = await r.json();
      if (j.status === "done" || j.status === "failed") {
        stopPoll(jobId);
        await loadHistory();
        stageResult(j);
        setStatus(j.status === "done" ? "Job " + jobId + " done ✨"
                                      : "Job " + jobId + " failed — credit refunded.", j.status === "failed");
        refreshCredits();
      }
    } catch (e) { /* next tick */ }
  }

  function startPoll(jobId) {
    if (timers.has(jobId)) return;
    timers.set(jobId, setInterval(() => pollOne(jobId), POLL_MS));
  }

  function cardFor(j) {
    const card = document.createElement("article");
    card.className = "job";
    const head = document.createElement("div");
    head.className = "job-head";
    head.innerHTML = "<b></b><small></small>";
    head.querySelector("b").textContent =
      (j.status === "done" ? "✅ " : j.status === "failed" ? "❌ " : "⏳ Building… ") + j.preset;
    head.querySelector("small").textContent = fmtTime(j.created_at);
    card.appendChild(head);

    const inRow = document.createElement("div");
    inRow.className = "job-thumbs";
    (j.inputs || []).forEach((src) => {
      const img = document.createElement("img");
      img.src = src + uq;
      img.alt = "input";
      img.loading = "lazy";
      inRow.appendChild(img);
    });
    card.appendChild(inRow);

    if (j.status === "done" && j.image_url) {
      const after = document.createElement("img");
      after.alt = "edited result";
      after.loading = "lazy";
      after.style.cssText = "width:100%;height:auto;border-radius:10px;border:1px solid var(--line);background:#0d0b09";
      after.src = j.image_url + uq;
      card.appendChild(after);
      const row = document.createElement("div");
      row.className = "row";
      const dl = document.createElement("button");
      dl.type = "button";
      dl.className = "dl solid";
      dl.textContent = "⬇ Download result";
      dl.onclick = async () => {
        dl.disabled = true;
        const old = dl.textContent;
        dl.textContent = "⏳ Saving…";
        try {
          const r = await fetch(j.image_url + uq);
          if (!r.ok) throw new Error("download " + r.status);
          const blob = await r.blob();
          const url = URL.createObjectURL(blob);
          const a = document.createElement("a");
          a.href = url;
          a.download = "free-photo-edit-" + j.job_id + ".png";
          document.body.appendChild(a);
          a.click();
          a.remove();
          setTimeout(() => URL.revokeObjectURL(url), 5000);
        } catch (e) {
          setStatus("Download failed: " + e.message, true);
        } finally {
          dl.disabled = false;
          dl.textContent = old;
        }
      };
      row.appendChild(dl);
      card.appendChild(row);
    } else if (j.status === "failed") {
      const p = document.createElement("p");
      p.className = "job-fail";
      p.textContent = "Failed — credit was refunded. Try again.";
      card.appendChild(p);
    } else {
      const p = document.createElement("p");
      p.className = "job-spin";
      p.textContent = "⏳ Editing… you can close this tab — it will be here when you return.";
      card.appendChild(p);
    }
    return card;
  }

  async function loadHistory() {
    try {
      const r = await fetch("/api/jobs?user=" + encodeURIComponent(user));
      if (!r.ok) throw new Error("jobs " + r.status);
      const jobs = (await r.json()).jobs || [];
      historyEl.innerHTML = "";
      histCount.textContent = jobs.length ? "(" + jobs.length + ")" : "";
      if (!jobs.length) {
        historyEl.innerHTML = "<p class='muted'>No edits yet — try one above.</p>";
        return;
      }
      const seen = new Set();
      jobs.forEach((j) => {
        historyEl.appendChild(cardFor(j));
        seen.add(j.job_id);
        if (j.status !== "done" && j.status !== "failed") startPoll(j.job_id);
      });
      stageResult(jobs.find((j) => j.status === "done")); // survives tab close
      [...timers.keys()].forEach((id) => { if (!seen.has(id)) stopPoll(id); });
    } catch (e) {
      historyEl.innerHTML = "<p class='muted'>History unavailable: " + e.message + "</p>";
    }
  }

  submitBtn.addEventListener("click", async () => {
    if (!file) { setStatus("Add 1 photo first.", true); return; }
    const prompt = promptEl.value.trim();
    if (!prompt) { setStatus("Describe what you want first.", true); return; }
    const fd = new FormData();
    fd.append("image", file, file.name);
    fd.append("prompt", prompt);
    fd.append("preset", activePreset);
    fd.append("user", user);
    fd.append("cf-turnstile-response", tsToken());
    setStatus("Queued… ⚡");
    try {
      const r = await fetch("/api/edit", { method: "POST", body: fd });
      tsReset(); // single-use token: fresh widget state for any retry
      const data = await r.json().catch(() => ({}));
      if (r.status === 402) {
        setCredits(0);
        setStatus(data.detail || "Free runs used up — try again tomorrow.", true);
        return;
      }
      if (r.status === 403) {
        setStatus(data.detail || "Bot check failed — tick the box and retry.", true);
        return;
      }
      if (r.status === 503) {
        setStatus(data.detail || "Today's free budget is used up — try again tomorrow.", true);
        return;
      }
      if (r.status === 429) {
        setStatus("Slot full — " + (data.detail || "try again shortly") + ".", true);
        return;
      }
      if (r.status !== 202 && !r.ok) throw new Error(data.detail || ("edit failed: " + r.status));
      setCredits(data.credits_left);
      setStatus("Job " + data.job_id + " editing… ⏳");
      formBody.classList.add("hidden"); // photo + prompt kept as-is for retry
      editAnother.classList.remove("hidden");
      resultBox.classList.add("hidden");
      resultBox.innerHTML = "";
      await loadHistory();
      startPoll(data.job_id);
      refreshCredits();
    } catch (e) {
      tsReset();
      setStatus("Error: " + e.message, true);
    }
  });

  editAnother.addEventListener("click", () => {
    editAnother.classList.add("hidden");
    formBody.classList.remove("hidden");
  });

  try { promptEl.value = localStorage.getItem(PROMPT_KEY) || ""; } catch (e) { /* private mode */ }
  promptEl.addEventListener("input", () => {
    try { localStorage.setItem(PROMPT_KEY, promptEl.value); } catch (e) { /* no-op */ }
  });
  idbGet().then((saved) => {
    if (saved && saved.blob && !file) {
      const blob = saved.blob;
      addFile(blob instanceof File ? blob
        : new File([blob], saved.name || "photo", {type: saved.type || blob.type}));
    }
  }).catch(() => { /* fresh form */ });

  initTurnstile();
  loadPresets();
  refreshCredits();
  loadHistory();
})();
