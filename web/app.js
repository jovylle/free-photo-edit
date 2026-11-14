/* Free Photo Edit — multi-photo self-serve UI. No framework, no build step.
 * Backend contract (same-origin /api/* only — no keys in browser):
 *   GET  /api/config -> {turnstile_sitekey} (null = bot check off, dev)
 *   GET  /api/presets -> {presets: [{id,label,hint}]}
 *   POST /api/edit  multipart(image x1–5, prompt, preset, user,
 *     cf-turnstile-response)
 *     -> 202 {job_id, status:running, credits_left} | 400 validation
 *        | 402 out of credits / IP cap | 403 bot check | 429 rate limit
 *        | 503 daily budget spent | slot-429 (Retry-After)
 *   GET  /api/result/{id} -> {status, image_url, credits_left}
 *   GET  /api/result/{id}/file -> output PNG when done
 *   GET  /api/result/{id}/input/0 -> stored input thumb
 *   GET  /api/jobs?user= -> own history, newest-first
 *   GET  /api/credits?user= -> {credits_left}
 *   GET  /api/packs -> {packs: [{id,label,price,credits}]} (price = centavos)
 *   POST /api/checkout {pack, user} -> {checkout_url, session_id}
 */
(() => {
  "use strict";
  const MAX_BYTES = 5 * 1024 * 1024, MAX_IMAGES = 5;
  const OK_TYPES = ["image/jpeg", "image/png", "image/webp"];
  // Same-origin by default; a deployed static frontend sets meta[name=api-base].
  const API_BASE = (document.querySelector('meta[name="api-base"]')?.content || "").replace(/\/+$/, "");
  const api = (path) => API_BASE + path;

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
        resultBox = $("result"), histCount = $("histCount"),
        packsBox = $("packs"), buyStatus = $("buyStatus"), buyCard = $("buyCard"),
        buyEmail = $("buyEmail");

  let files = [];
  let draftChanged = false;
  let thumbUrls = [];
  let draftWrites = Promise.resolve();
  let activePreset = "custom";

  const TS_ACTION = "edit";
  let tsWidgetId = null; // explicit render id (page stays alive: reset per try)

  let user;
  try { user = localStorage.getItem(USER_KEY); } catch (e) { /* private mode */ }
  if (!user) {
    user = "u-" + Math.random().toString(36).slice(2, 10);
    try { localStorage.setItem(USER_KEY, user); } catch (e) { /* tab-only history */ }
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
              ph: "TL: Gawing formal ang damit. EN: Make the clothes formal. Bisaya: Himog pormal ang sinina." },
  };

  // Inline icons from icons.svg — the site uses no emoji.
  const PRESET_ICONS = { background: "i-landscape", clothes: "i-shirt", restore: "i-sparkles", custom: "i-pencil" };
  function ico(name) {
    return '<svg class="ico" aria-hidden="true"><use href="icons.svg#' + name + '"></use></svg>';
  }
  function plain(text) {
    return String(text || "").replace(/[\p{Extended_Pictographic}\uFE0F]/gu, "").trim();
  }

  const uq = "?user=" + encodeURIComponent(user);

  function setStatus(msg, isError) {
    statusEl.textContent = msg || "";
    statusEl.classList.toggle("error", !!isError);
  }

  function setCredits(n) {
    creditsBadge.textContent = n == null ? "free edits: …" : n + (n === 1 ? " free edit left" : " free edits left");
  }

  async function initTurnstile() {
    let sitekey = null;
    try {
      const r = await fetch(api("/api/config"));
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
        sitekey: sitekey, action: TS_ACTION, size: "flexible",
        // Only show a widget when a challenge is actually required; a green
        // "Success!" box above the CTA reads as an ad to first-time users.
        appearance: "interaction-only",
        theme: document.documentElement.dataset.theme === "dark" ? "dark" : "light",
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
      const r = await fetch(api("/api/credits?user=" + encodeURIComponent(user)));
      if (!r.ok) throw new Error("credits " + r.status);
      setCredits((await r.json()).credits_left);
    } catch (e) {
      creditsBadge.textContent = "free edits: ?";
      creditsBadge.title = "Could not reach backend: " + e.message;
    }
  }

  function setBuyStatus(msg, isError) {
    buyStatus.textContent = msg || "";
    buyStatus.classList.toggle("error", !!isError);
  }

  function peso(centavos) {
    return "₱" + (centavos / 100).toLocaleString("en-PH", {
      minimumFractionDigits: 2, maximumFractionDigits: 2});
  }

  let packCount = 0;
  function packsAvailable() {
    return packCount > 0;
  }

  function showBuyCard() {
    buyCard.scrollIntoView({ behavior: "smooth", block: "center" });
    const first = packsBox.querySelector("button");
    if (first) first.focus({ preventScroll: true });
  }

  async function startCheckout(pack, btn) {
    const email = buyEmail.value.trim();
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
      setBuyStatus("Enter a valid email first — it is how we recover your edits if this browser loses its data.", true);
      buyEmail.focus();
      return;
    }
    btn.disabled = true;
    setBuyStatus("Opening secure checkout…");
    try {
      const r = await fetch(api("/api/checkout"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pack: pack.id, user: user, email: email }),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok || !data.checkout_url)
        throw new Error(data.detail || ("checkout failed: " + r.status));
      location.href = data.checkout_url; // Xendit hosted checkout
    } catch (e) {
      setBuyStatus("Could not start checkout: " + e.message, true);
      btn.disabled = false;
    }
  }

  // Owner-curated gallery: edit web/showcase.json, drop files in web/showcase/.
  // Entries are only what the owner chose to publish — never a user's own job.
  async function loadShowcase() {
    const card = document.getElementById("showcase");
    const box = document.getElementById("showcaseGrid");
    if (!card || !box) return;
    let items = null;
    try {
      const r = await fetch("showcase.json", { cache: "no-cache" });
      if (r.ok) items = (await r.json());
    } catch (e) { /* no showcase */ }
    if (!Array.isArray(items) || !items.length) return;
    box.innerHTML = "";
    const shot = (src, alt) => {
      const img = document.createElement("img");
      img.loading = "lazy";
      img.decoding = "async";
      img.src = src;
      img.alt = alt;
      return img;
    };
    items.forEach((it) => {
      const fig = document.createElement("figure");
      const label = it.preset ? it.preset + " edit" : "Edited photo";
      if (it.before && it.after) { // paired before/after, labelled
        const pair = document.createElement("div");
        pair.className = "pair";
        [["Before", it.before], ["After", it.after]].forEach(([tag, src]) => {
          const cell = document.createElement("div");
          const mark = document.createElement("span");
          mark.className = "tag";
          mark.textContent = tag;
          cell.append(shot(src, tag + " — " + label), mark);
          pair.appendChild(cell);
        });
        fig.appendChild(pair);
      } else {
        fig.appendChild(shot(it.image, it.note || label));
      }
      const cap = document.createElement("figcaption");
      cap.textContent = [it.preset, it.note].filter(Boolean).join(" — ");
      fig.appendChild(cap);
      box.appendChild(fig);
    });
    card.hidden = false;
  }

  async function loadPacks() {
    let packs = null;
    try {
      const r = await fetch(api("/api/packs"));
      if (r.ok) packs = (await r.json()).packs;
    } catch (e) { /* offline: keep the note */ }
    packCount = (packs || []).length;
    if (!packs || !packs.length) {
      packsBox.innerHTML = "<p class='muted'>Edit packs are unavailable right now.</p>";
      buyCard.style.display = "none"; // unconfigured: no dead offer on the page
      return;
    }
    packsBox.innerHTML = "";
    packs.forEach((p) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "pack";
      btn.innerHTML = "<b></b><small></small>";
      btn.querySelector("b").textContent = p.label + " — " + peso(p.price);
      btn.querySelector("small").textContent = p.credits + " edits";
      btn.onclick = () => startCheckout(p, btn);
      packsBox.appendChild(btn);
    });
  }

  // success_url carries ?paid=1: poll credits until the webhook lands.
  async function confirmPayment() {
    const qp = new URLSearchParams(location.search);
    if (qp.get("paid") !== "1") return;
    qp.delete("paid");
    try {
      history.replaceState(null, "", location.pathname
        + (qp.toString() ? "?" + qp : "") + location.hash);
    } catch (e) { /* file:// */ }
    let start = null;
    try {
      const r = await fetch(api("/api/credits?user=" + encodeURIComponent(user)));
      if (r.ok) start = (await r.json()).credits_left;
    } catch (e) { /* baseline unknown */ }
    setStatus("Confirming your payment…");
    setBuyStatus("Confirming your payment…");
    showBuyCard();
    const deadline = Date.now() + 30000;
    while (Date.now() < deadline) {
      await new Promise((res) => setTimeout(res, 3000));
      try {
        const r = await fetch(api("/api/credits?user=" + encodeURIComponent(user)));
        if (!r.ok) continue;
        const n = (await r.json()).credits_left;
        setCredits(n);
        if (start === null || n > start) {
          setStatus("Credits added");
          setBuyStatus("Credits added — thanks for supporting free edits.");
          return;
        }
      } catch (e) { /* next tick */ }
    }
    const msg = "Still confirming — it can take a minute; the credits appear automatically.";
    setStatus(msg);
    setBuyStatus(msg);
  }

  function selectPreset(id, save = true) {
    activePreset = id;
    [...presetsBox.children].forEach((el) => {
      el.classList.toggle("active", el.dataset.id === id);
      el.setAttribute("aria-pressed", String(el.dataset.id === id));
    });
    const copy = PRESET_COPY[id] || PRESET_COPY.custom;
    promptLabel.textContent = copy.label;
    promptEl.placeholder = copy.ph;
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
      const r = await fetch(api("/api/presets"));
      if (r.ok) list = (await r.json()).presets;
    } catch (e) { /* builtin fallback */ }
    if (!list) {
      list = [
        {id: "background", label: "Background", hint: "up to 5 photos"},
        {id: "clothes", label: "Clothes", hint: "up to 5 photos"},
        {id: "restore", label: "Restore", hint: "up to 5 photos"},
        {id: "custom", label: "Custom", hint: "free-form"},
      ];
    }
    presetsBox.innerHTML = "";
    list.forEach((p) => {
      const el = document.createElement("button");
      el.type = "button";
      el.className = "preset";
      el.dataset.id = p.id;
      el.innerHTML = ico(PRESET_ICONS[p.id] || "i-pencil") + "<b></b><small></small>";
      el.querySelector("b").textContent = plain(p.label);
      el.querySelector("small").textContent = p.hint;
      const pick = () => selectPreset(p.id);
      el.onclick = pick;
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
    thumbUrls.forEach((url) => URL.revokeObjectURL(url));
    thumbUrls = [];
    thumbs.innerHTML = "";
    files.forEach((file, index) => {
    const wrap = document.createElement("div");
    wrap.className = "thumb";
    const img = document.createElement("img");
    img.src = URL.createObjectURL(file);
    thumbUrls.push(img.src);
    img.alt = "Photo " + (index + 1);
    const rm = document.createElement("button");
    rm.type = "button";
    rm.textContent = "×";
    rm.setAttribute("aria-label", "Remove photo " + (index + 1));
    rm.onclick = (e) => { e.stopPropagation(); files.splice(index, 1); renderThumbs(); saveDraft(); };
    wrap.append(img, rm);
    thumbs.appendChild(wrap);
    });
  }

  function saveDraft() {
    draftChanged = true;
    const photos = files.map((f) => ({blob: f, name: f.name, type: f.type}));
    draftWrites = draftWrites.then(() => photos.length ? idbPut(photos) : idbDel());
  }

  function addFiles(incoming) {
    const added = Array.from(incoming);
    if (!added.length) return;
    const room = MAX_IMAGES - files.length;
    if (room <= 0) { setStatus("Rejected: maximum 5 photos.", true); return; }
    // A picker selection larger than the remaining room keeps the first ones
    // instead of throwing the whole selection away.
    const over = added.length > room;
    const take = over ? added.slice(0, room) : added;
    if (take.some((f) => !OK_TYPES.includes(f.type))) { setStatus("Rejected: only jpeg/png/webp.", true); return; }
    if (take.some((f) => !f.size || f.size > MAX_BYTES)) { setStatus("Rejected: empty photo or over 5MB.", true); return; }
    files.push(...take);
    renderThumbs();
    saveDraft();
    if (over) setStatus("Kept 5 photos — maximum is 5 per edit.", true);
    else setStatus(files.length + (files.length === 1 ? " photo ready." : " photos ready."));
  }

  dropzone.addEventListener("click", (e) => {
    if (e.target.closest(".thumb button")) return;
    fileInput.click();
  });
  dropzone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); }
  });
  fileInput.addEventListener("change", () => { addFiles(fileInput.files); fileInput.value = ""; });
  ["dragenter", "dragover"].forEach((ev) => dropzone.addEventListener(ev, (e) => {
    e.preventDefault(); dropzone.classList.add("over");
  }));
  ["dragleave", "drop"].forEach((ev) => dropzone.addEventListener(ev, (e) => {
    e.preventDefault(); dropzone.classList.remove("over");
  }));
  dropzone.addEventListener("drop", (e) => addFiles(e.dataTransfer.files));

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
    if (found.length) { e.preventDefault(); addFiles(found); }
  });

  function fmtTime(ts) {
    try { return new Date(ts * 1000).toLocaleString(); }
    catch (e) { return ""; }
  }

  function stopPoll(jobId) {
    const t = timers.get(jobId);
    if (t) { clearInterval(t); timers.delete(jobId); }
  }

  function showRunning() { // prominent "working" state, not a one-line status
    resultBox.innerHTML = "";
    const card = document.createElement("div");
    card.className = "run-card";
    card.setAttribute("role", "status");
    const dot = document.createElement("span");
    dot.className = "spin";
    dot.setAttribute("aria-hidden", "true");
    const head = document.createElement("b");
    head.textContent = "Editing your photo…";
    const note = document.createElement("span");
    note.className = "muted";
    note.textContent = "Usually under a minute. You can close this tab — it will be here when you return.";
    card.append(dot, head, note);
    resultBox.appendChild(card);
    resultBox.classList.remove("hidden");
  }

  function stageResult(j) { // newest result takes the stage under the button
    if (!j || j.status !== "done" || !j.image_url) return;
    resultBox.innerHTML = "";
    prefetchShare(j);
    resultBox.appendChild(cardFor(j));
    resultBox.classList.remove("hidden");
  }

  async function pollOne(jobId) {
    try {
      const r = await fetch(api("/api/result/" + jobId) + uq);
      if (r.status === 404) {
        stopPoll(jobId);
        setStatus("This edit is no longer available.", true);
        return;
      }
      if (!r.ok) { setStatus("Unable to check your edit. Retrying in 10 seconds…", true); return; }
      const j = await r.json();
      if (j.status === "done" || j.status === "failed") {
        stopPoll(jobId);
        runningJobs = Math.max(0, runningJobs - 1);
        await loadHistory();
        stageResult(j);
        if (j.status !== "done" || j.artifacts_expired) {
          resultBox.innerHTML = "";
          resultBox.classList.add("hidden");
        }
        setStatus(j.artifacts_expired ? "Photos expired after seven days. Upload a photo to edit again."
                  : j.status === "done" ? "Done — your edited photo is below."
                                      : "That edit failed — your credit was refunded.", j.status === "failed");
        refreshCredits();
      }
    } catch (e) { /* next tick */ }
  }

  function startPoll(jobId) {
    if (timers.has(jobId)) return;
    timers.set(jobId, setInterval(() => pollOne(jobId), POLL_MS));
  }

  function resultUrl(path) {
    if (!/^\/api\/result\/[a-zA-Z0-9_-]+\/(file|input\/\d+)$/.test(path))
      throw new Error("Invalid result URL");
    return api(path) + uq;
  }

  // Share blobs are cached per job and only prefetched for the card on the
  // stage; history cards must not pull 30 full PNGs on first paint.
  const shareBlobs = new Map();
  const creditBlobs = new Map();

  function saveBlob(blob, name) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  }

  // Link posts get a person flagged in groups, so the attribution lives IN the
  // image: a quiet footer strip added under the photo, never a URL in the text.
  async function creditedBlob(jobId, blob) {
    if (creditBlobs.has(jobId)) return creditBlobs.get(jobId);
    const img = await createImageBitmap(blob);
    const pad = Math.round(Math.max(24, Math.min(img.width, img.height) * 0.045));
    const bar = Math.round(Math.max(52, img.height * 0.085));
    const size = Math.round(bar * 0.33);
    const canvas = document.createElement("canvas");
    canvas.width = img.width;
    canvas.height = img.height + bar;
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#eeedf5";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(img, 0, 0);
    ctx.textBaseline = "middle";
    ctx.fillStyle = "#3d3c43";
    ctx.font = '700 ' + size + 'px Manrope, system-ui, sans-serif';
    ctx.textAlign = "left";
    ctx.fillText("freephoto.uft1.com", pad, img.height + bar / 2);
    ctx.fillStyle = "#6e6d78";
    ctx.font = '500 ' + Math.round(size * 0.82) + 'px Karla, system-ui, sans-serif';
    ctx.textAlign = "right";
    ctx.fillText("free photo edit", canvas.width - pad, img.height + bar / 2);
    const out = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
    if (!out) throw new Error("could not build the image");
    creditBlobs.set(jobId, out);
    return out;
  }
  function prefetchShare(j) {
    if (!j.image_url || shareBlobs.has(j.job_id)) return;
    shareBlobs.set(j.job_id, fetch(resultUrl(j.image_url))
      .then((r) => (r.ok ? r.blob() : null))
      .catch(() => null));
  }

  function cardFor(j) {
    const card = document.createElement("article");
    card.className = "job";
    const head = document.createElement("div");
    head.className = "job-head";
    head.innerHTML = "<b></b><small></small>";
    head.querySelector("b").textContent =
      (j.status === "running" || !j.status ? "Building… " : "") + j.preset;
    head.querySelector("small").textContent = fmtTime(j.created_at);
    card.appendChild(head);

    if (j.artifacts_expired) {
      const p = document.createElement("p");
      p.className = "muted";
      p.textContent = "Photos expired after seven days. Edit history is retained.";
      card.appendChild(p);
      return card;
    }

    const inRow = document.createElement("div");
    inRow.className = "job-thumbs";
    (j.inputs || []).forEach((src) => {
      const img = document.createElement("img");
      img.src = resultUrl(src);
      img.alt = "input";
      img.loading = "lazy";
      inRow.appendChild(img);
    });
    card.appendChild(inRow);

    if (j.status === "done" && j.image_url) {
      // Inputs stay above, result below. A slider overlaid them, which made it
      // unclear which photo was the edit.
      const after = document.createElement("img");
      after.alt = "edited result";
      after.loading = "lazy";
      after.style.cssText = "width:100%;height:auto;border-radius:10px;border:0;background:var(--bg);margin-top:var(--space-2xs)";
      after.src = resultUrl(j.image_url);
      card.appendChild(after);
      const row = document.createElement("div");
      row.className = "row";
      const dl = document.createElement("button");
      dl.type = "button";
      dl.className = "dl solid";
      dl.innerHTML = ico("i-download") + " Download result";
      dl.onclick = async () => {
        dl.disabled = true;
        const old = dl.innerHTML;
        dl.innerHTML = ico("i-clock") + " Saving…";
        try {
          const r = await fetch(resultUrl(j.image_url));
          if (!r.ok) throw new Error("download " + r.status);
          saveBlob(await r.blob(), "free-photo-edit-" + j.job_id + ".png");
        } catch (e) {
          setStatus("Download failed: " + e.message, true);
        } finally {
          dl.disabled = false;
          dl.innerHTML = old;
        }
      };
      row.appendChild(dl);
      // Share the actual PNG, never resultUrl(): that URL is a bearer link to
      // this browser's job. Files keep the recipient off the API entirely.
      const sh = document.createElement("button");
      sh.type = "button";
      sh.className = "dl";
      sh.innerHTML = ico("i-share") + " Share";
      sh.onclick = async () => {
        sh.disabled = true;
        const old = sh.innerHTML;
        sh.innerHTML = ico("i-clock") + " Preparing…";
        try {
          const blob = await (shareBlobs.get(j.job_id)
            || fetch(resultUrl(j.image_url)).then((r) => (r.ok ? r.blob() : null)));
          if (!blob) throw new Error("could not load the photo");
          await document.fonts.ready.catch(() => {}); // footer text uses the site fonts
          const shared = await creditedBlob(j.job_id, blob);
          const name = "free-photo-edit-" + j.job_id + ".png";
          const file = new File([shared], name, { type: "image/png" });
          if (navigator.canShare && navigator.canShare({ files: [file] })) {
            // Some desktops report canShare(files) but never settle the share
            // promise, which used to leave the button stuck on "Preparing…".
            await Promise.race([
              navigator.share({ files: [file], text: "Edited with Free Photo Edit" }),
              new Promise((_, reject) => setTimeout(
                () => reject(Object.assign(new Error("share sheet did not open"), { name: "TimeoutError" })),
                15000)),
            ]);
            return;
          }
          // No share sheet (desktop, in-app webview): save the credited image
          // instead of posting a link — links are what get flagged in groups.
          saveBlob(shared, name);
          setStatus("Saved the shareable image — it carries a small credit footer.");
        } catch (e) {
          if (e.name === "AbortError") return; // user closed the sheet
          if (e.name === "TimeoutError") {
            saveBlob(await creditedBlob(j.job_id, await (shareBlobs.get(j.job_id)
              || fetch(resultUrl(j.image_url)).then((r) => r.blob()))), name);
            setStatus("Share sheet did not open — the image was saved instead.");
            return;
          }
          setStatus("Could not share: " + e.message, true);
        } finally {
          sh.disabled = false;
          sh.innerHTML = old;
        }
      };
      row.appendChild(sh);
      card.appendChild(row);
      const note = document.createElement("p");
      note.className = "muted";
      note.textContent = "Share saves a copy with a small credit footer. Download is the clean photo, no text added.";
      card.appendChild(note);
    } else if (j.status === "failed") {
      const p = document.createElement("p");
      p.className = "job-fail";
      p.textContent = "Failed — credit was refunded. Try again.";
      card.appendChild(p);
    } else {
      const p = document.createElement("p");
      p.className = "job-spin";
      p.textContent = "Editing… you can close this tab — it will be here when you return.";
      card.appendChild(p);
    }
    return card;
  }

  async function loadHistory() {
    try {
      const r = await fetch(api("/api/jobs?user=" + encodeURIComponent(user)));
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
      resultBox.innerHTML = "";
      resultBox.classList.add("hidden");
      stageResult(jobs.find((j) => j.status === "done" && !j.artifacts_expired)); // survives tab close
      [...timers.keys()].forEach((id) => { if (!seen.has(id)) stopPoll(id); });
    } catch (e) {
      historyEl.innerHTML = "<p class='muted'>History unavailable: " + e.message + "</p>";
    }
  }

  let submitting = false;
  let runningJobs = 0;
  submitBtn.addEventListener("click", async () => {
    if (submitting || submitBtn.disabled) return;
    if (!files.length) { setStatus("Add a photo first.", true); return; }
    const prompt = promptEl.value.trim();
    if (!prompt && activePreset !== "restore") { setStatus("Describe what you want first.", true); return; }
    const fd = new FormData();
    files.forEach((file) => fd.append("image", file, file.name));
    fd.append("prompt", prompt);
    fd.append("preset", activePreset);
    fd.append("user", user);
    fd.append("cf-turnstile-response", tsToken());
    submitting = true;
    submitBtn.disabled = true;
    setStatus("Sending photo…");
    try {
      const r = await fetch(api("/api/edit"), { method: "POST", body: fd });
      tsReset(); // single-use token: fresh widget state for any retry
      const data = await r.json().catch(() => ({}));
      if (r.status === 402) {
        setCredits(0);
        if (packsAvailable()) {
          setStatus((data.detail || "Out of edits") + " — pick a pack below to keep going.", true);
          showBuyCard();
        } else {
          setStatus((data.detail || "Out of edits") + " — your free edits reset tomorrow.", true);
        }
        return;
      }
      if (r.status === 403) {
        setStatus(data.detail || "Bot check didn't finish — tap Edit photo again.", true);
        return;
      }
      if (r.status === 503) {
        setStatus(data.detail || "Today's free budget is used up — try again tomorrow.", true);
        return;
      }
      if (r.status === 429) {
        setStatus((data.detail || "Server busy") + ". Retry after " + (r.headers.get("Retry-After") || "10") + " seconds.", true);
        return;
      }
      if (r.status !== 202 || !data.job_id) throw new Error(data.detail || ("edit failed: " + r.status));
      setCredits(data.credits_left);
      runningJobs += 1;
      setStatus("");
      formBody.classList.add("hidden"); // photo + prompt kept as-is for retry
      editAnother.classList.remove("hidden");
      await loadHistory(); // clears the stage; the running card must come after it
      showRunning();
      pollOne(data.job_id); // instant engine: replace the card as soon as it is done
      startPoll(data.job_id);
      refreshCredits();
    } catch (e) {
      tsReset();
      setStatus("Error: " + e.message + ". Check Past edits before retrying — the server may have received your photo.", true);
      loadHistory();
      refreshCredits();
    } finally {
      submitting = false;
      // On success the form is hidden behind "Edit another photo…", which
      // re-enables the button; keeping it disabled here closes the window where
      // a second click started a second job and spent another credit.
      if (!formBody.classList.contains("hidden")) submitBtn.disabled = false;
    }
  });

  editAnother.addEventListener("click", () => {
    editAnother.classList.add("hidden");
    formBody.classList.remove("hidden");
    if (runningJobs > 0) {
      submitBtn.disabled = true;
      setStatus("Your previous edit is still running — it will be here when it's done.");
    } else {
      submitBtn.disabled = false;
      setStatus("");
    }
  });

  try { promptEl.value = localStorage.getItem(PROMPT_KEY) || ""; } catch (e) { /* private mode */ }
  promptEl.addEventListener("input", () => {
    try { localStorage.setItem(PROMPT_KEY, promptEl.value); } catch (e) { /* no-op */ }
  });
  idbGet().then((saved) => {
    if (saved && !draftChanged) {
      const photos = Array.isArray(saved) ? saved : [saved];
      addFiles(photos.filter((photo) => photo && photo.blob).map((photo) => {
        const blob = photo.blob;
        return blob instanceof File ? blob
          : new File([blob], photo.name || "photo", {type: photo.type || blob.type});
      }));
    }
  }).catch(() => { /* fresh form */ });

  initTurnstile();
  loadPresets();
  refreshCredits();
  loadHistory();
  loadPacks();
  loadShowcase();
  confirmPayment();
})();
