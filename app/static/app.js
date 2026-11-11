/* Scaling guide Q&A: talks to POST /ask, GET /stats and GET /health on the same origin.
 * No build step and no dependencies. Server text is only ever inserted with textContent
 * (via the h() helper), never innerHTML, so answers and sources cannot inject markup. */
(() => {
  "use strict";

  const MAX_CHARS = 1000; // keep in step with MAX_QUESTION_CHARS in app/api.py
  const COUNTER_FROM = 800;
  const HISTORY_LIMIT = 8;
  const SNIPPET_CUT = 200; // the API cuts source snippets at this many characters
  const SAMPLES = [
    "Why can an index slow down writes?",
    "How does sharding spread writes?",
    "Why is TTL not a guarantee of freshness?",
    "What is hierarchical aggregation?",
  ];

  // How each cache_status from the API is described to the person using the page.
  const STATUS = {
    hit: {
      tone: "hit", badge: "Served from Redis",
      detail: "Redis already had this answer, so retrieval and generation were skipped.",
    },
    hit_after_wait: {
      tone: "hit", badge: "Reused a concurrent answer",
      detail: "An identical question was already being answered. This request waited and reused that result instead of repeating the work.",
    },
    miss: {
      tone: "miss", badge: "Answered, then cached",
      detail: "Redis had no answer, so the full pipeline ran and the result was stored. Ask again to see it served from Redis.",
    },
    miss_not_stored: {
      tone: "fail", badge: "Answered, not cached",
      detail: "The answer was built, but Redis could not store it, so the next request will repeat the work.",
    },
    unavailable: {
      tone: "fail", badge: "Redis unavailable",
      detail: "Redis could not be reached, so the answer was built without the cache. It is correct, but it was not cached.",
    },
    disabled: {
      tone: "idle", badge: "Cache is off",
      detail: "Caching is turned off on the server, so every question runs retrieval and generation.",
    },
  };

  const $ = (id) => document.getElementById(id);
  const form = $("ask-form");
  const input = $("q");
  const submitBtn = $("submit");
  const countEl = $("count");
  const errorEl = $("error");
  const resultEl = $("result");
  const pipelineEl = $("pipeline");
  const captionEl = $("path-caption");
  const statsEl = $("stats");
  const historyEl = $("history");
  const connEl = $("conn");
  const connText = $("conn-text");
  const compareEl = $("compare");
  const gaugeEl = $("gauge");
  const trendEl = $("trend");
  const trend = []; // oldest first: {ms, tone}

  let pending = false;
  const firstComputed = new Map(); // normalised question -> latency of the first full run in this tab
  const history = [];              // newest first

  // ---------- helpers ----------

  /** Build an element. Strings and numbers become text nodes. */
  function h(tag, props, ...kids) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null || v === false) continue;
      if (k === "class") node.className = v;
      else node.setAttribute(k, v === true ? "" : String(v));
    }
    for (const kid of kids.flat()) if (kid != null && kid !== false) node.append(kid);
    return node;
  }

  const normalise = (q) => q.trim().replace(/\s+/g, " ").toLowerCase();

  function fmtMs(ms) {
    if (typeof ms !== "number" || !Number.isFinite(ms)) return "–";
    if (ms >= 1000) return (ms / 1000).toFixed(2) + " s";
    if (ms >= 100) return Math.round(ms) + " ms";
    if (ms >= 10) return ms.toFixed(1) + " ms";
    return ms.toFixed(2) + " ms";
  }

  const fmtPercent = (rate) =>
    typeof rate === "number" ? `${Number((rate * 100).toFixed(1))}%` : "–";

  /** The API hard-cuts snippets at SNIPPET_CUT characters, often mid-word. Show a cut one as
   *  whole words with an ellipsis; leave snippets that end cleanly alone. */
  function tidySnippet(text) {
    const t = String(text || "").trim();
    if (t.length < SNIPPET_CUT || /[.!?)"'”’]$/.test(t)) return t;
    const lastSpace = t.lastIndexOf(" ");
    return (lastSpace > 0 ? t.slice(0, lastSpace) : t).replace(/[\s,;:–-]+$/, "") + "…";
  }

  const statusInfo = (resp) =>
    STATUS[resp.cache_status] ||
    { tone: "idle", badge: String(resp.cache_status || "Answered"), detail: "" };

  /** Answer text as per-word spans so the answer fades in; whitespace stays as text nodes. */
  function revealWords(text) {
    let i = 0;
    return String(text).split(/(\s+)/).map((tok) => {
      if (!tok || /^\s+$/.test(tok)) return tok;
      const w = h("span", { class: "w" }, tok);
      w.style.setProperty("--i", String(Math.min(i++, 160)));
      return w;
    });
  }

  /** Two bars scaled to the slower run: first full run versus this cached request. */
  function renderCompare(first, now) {
    const row = (label, ms, ratio, tone) => {
      const bar = h("div", { class: `cmp-bar tone-${tone}` });
      bar.style.transform = "scaleX(0)";
      requestAnimationFrame(() => requestAnimationFrame(() => { bar.style.transform = `scaleX(${ratio})`; }));
      return h("div", { class: "cmp-row" }, h("span", {}, label), h("div", { class: "cmp-track" }, bar),
        h("span", { class: "cmp-val" }, fmtMs(ms)));
    };
    const max = Math.max(first, now, 0.0001);
    const x = now > 0 ? first / now : 0;
    compareEl.replaceChildren(
      row("First full run", first, first / max, "miss"),
      row("This request", now, Math.max(now / max, 0.012), "hit"),
      x >= 1.5 ? h("p", { class: "cmp-sum" }, "Redis served this ", h("strong", {}, `${x >= 10 ? Math.round(x) : x.toFixed(1)}× faster`), " than the full pipeline.") : null);
    compareEl.hidden = false;
  }

  // ---------- theme ----------

  const themeBtn = $("theme");
  function setTheme(mode, save) {
    document.documentElement.dataset.theme = mode;
    themeBtn.setAttribute("aria-label", mode === "dark" ? "Switch to light theme" : "Switch to dark theme");
    if (save) { try { localStorage.setItem("theme", mode); } catch { /* storage may be blocked */ } }
  }
  (() => {
    let saved = null;
    try { saved = localStorage.getItem("theme"); } catch { /* ignore */ }
    setTheme(saved || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"), false);
    themeBtn.addEventListener("click", () =>
      setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark", true));
  })();

  // ---------- connection indicator ----------

  function setConn(up) {
    connEl.dataset.state = up ? "up" : "down";
    connText.textContent = up ? "Server connected" : "Server unreachable";
  }

  // ---------- path strip ----------

  const step = (label, tone, note) => ({ label, tone, note });

  /** Which pipeline steps this response went through, derived from its cache_status. */
  function pathSteps(resp) {
    const st = resp.cache_status;
    const answered = resp.answered !== false;
    const fromCache = st === "hit" || st === "hit_after_wait";

    let redis;
    if (st === "hit") redis = step("Check Redis", "hit", "hit");
    else if (st === "hit_after_wait") redis = step("Check Redis", "miss", "miss, then waited");
    else if (st === "unavailable") redis = step("Check Redis", "fail", "unavailable");
    else if (st === "disabled") redis = step("Check Redis", "skipped", "cache off");
    else redis = step("Check Redis", "miss", "miss");

    const retrieve = fromCache
      ? step("Retrieve passages", "skipped", "not needed")
      : step("Retrieve passages", "done", "ran");

    let generate;
    if (fromCache) generate = step("Generate answer", "skipped", "not needed");
    else if (!answered) generate = step("Generate answer", "skipped", "no answer");
    else generate = step("Generate answer", "done", "ran");

    let store;
    if (st === "hit") store = step("Store in Redis", "skipped", "already cached");
    else if (st === "hit_after_wait") store = step("Store in Redis", "skipped", "stored by other request");
    else if (!answered) store = step("Store in Redis", "skipped", "nothing to store");
    else if (st === "miss") store = step("Store in Redis", "done", "stored");
    else if (st === "miss_not_stored") store = step("Store in Redis", "fail", "store failed");
    else if (st === "unavailable") store = step("Store in Redis", "skipped", "not attempted");
    else store = step("Store in Redis", "skipped", "cache off");

    return [redis, retrieve, generate, store,
      step("Return answer", "done", fmtMs(resp.latency_ms))];
  }

  function renderPipeline(steps, live) {
    pipelineEl.replaceChildren(
      ...steps.map((s, i) => {
        const li = h("li", { class: `node is-${s.tone}` },
          h("span", { class: "node-label" }, s.label),
          h("span", { class: "node-note" }, s.note));
        // CSSOM, not a style="" attribute: the page's CSP blocks inline style attributes.
        li.style.setProperty("--i", String(i));
        return li;
      }));
    if (live) pipelineEl.setAttribute("data-live", "");
    else pipelineEl.removeAttribute("data-live");
  }

  function renderIdlePath() {
    renderPipeline([
      step("Check Redis", "idle", ""),
      step("Retrieve passages", "idle", ""),
      step("Generate answer", "idle", ""),
      step("Store in Redis", "idle", ""),
      step("Return answer", "idle", ""),
    ], false);
    captionEl.textContent =
      "Ask a question to see which steps it takes. A cached answer skips retrieval and generation.";
  }

  // ---------- result ----------

  function renderResult(resp, question) {
    const info = statusInfo(resp);
    const answered = resp.answered !== false;
    const key = normalise(question);
    const fromCache = resp.cache_status === "hit" || resp.cache_status === "hit_after_wait";

    // Remember how long the first full run of this question took, to compare against later hits.
    let compare = null;
    if (!fromCache && answered && !firstComputed.has(key)) firstComputed.set(key, resp.latency_ms);
    if (fromCache && firstComputed.has(key)) compare = { first: firstComputed.get(key) };

    const copyBtn = h("button", { type: "button", class: "copy" }, "Copy answer");
    copyBtn.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(String(resp.answer ?? "")); copyBtn.textContent = "Copied"; }
      catch { copyBtn.textContent = "Copy failed"; }
      setTimeout(() => { copyBtn.textContent = "Copy answer"; }, 1600);
    });
    const head = h("div", { class: "result-head" },
      h("h2", { class: "result-title" },
        answered ? "Answer" : "No answer found",
        h("span", { class: `badge tone-${info.tone}` }, info.badge)),
      h("div", { class: "head-right" },
        h("p", { class: "latency" }, "Answered in ", h("strong", {}, fmtMs(resp.latency_ms))),
        answered ? copyBtn : null));

    const nodes = [
      head,
      h("p", { class: "asked" }, "You asked: ", resp.question || question),
      h("p", { class: "answer" }, revealWords(resp.answer ?? "")),
    ];

    const sources = Array.isArray(resp.sources) ? resp.sources : [];
    if (answered && sources.length) {
      nodes.push(h("section", { class: "sources", "aria-labelledby": "sources-title" },
        h("h3", { id: "sources-title" }, `Sources (${sources.length})`),
        h("div", { class: "source-list" },
          sources.map((s, i) =>
            h("details", { class: "source", open: i === 0 },
              h("summary", {}, h("span", {}, s.heading || "Untitled passage"),
                typeof s.score === "number" ? h("span", { class: "score" }, `Match ${s.score.toFixed(2)}`) : null),
              h("div", { class: "source-body" },
                h("blockquote", {}, tidySnippet(s.snippet)),
                s.source ? h("div", { class: "source-meta" }, s.source) : null))))));
    }

    if (compare) renderCompare(compare.first, resp.latency_ms); else compareEl.hidden = true;
    resultEl.replaceChildren(...nodes.filter(Boolean));
    captionEl.textContent = info.detail;
  }

  // ---------- errors ----------

  function showError(message) {
    errorEl.textContent = message;
    errorEl.hidden = false;
  }
  function clearError() {
    errorEl.hidden = true;
    errorEl.textContent = "";
  }

  function httpMessage(status, body) {
    const detail = body && typeof body.error === "string" ? body.error : "";
    if (status === 400 || status === 413) {
      return `The server could not accept that question${detail ? `: ${detail}` : "."}`;
    }
    if (status === 502) return "The answer generator failed. Try again in a moment.";
    return "Something went wrong on the server. Try again in a moment.";
  }

  // ---------- stats ----------

  async function refreshStats() {
    try {
      const res = await fetch("/stats", { headers: { Accept: "application/json" } });
      if (!res.ok) throw new Error(String(res.status));
      renderStats(await res.json());
    } catch {
      statsEl.replaceChildren(h("div", { class: "stats-empty" }, "Totals are unavailable right now."));
      window.Charts.drawGauge(gaugeEl, null);
    }
  }

  function renderStats(s) {
    const rows = [
      ["Hit rate", fmtPercent(s.hit_rate)],
      ["Requests", s.requests],
      ["Cache hits", s.cache_hits],
      ["Cache misses", s.cache_misses],
      ["Retrievals run", s.retrievals],
      ["Generations run", s.generations],
    ];
    if (s.coalesced_hits > 0) rows.push(["Waited for another request", s.coalesced_hits]);
    if (s.cache_unavailable > 0) rows.push(["Redis unavailable", s.cache_unavailable]);
    window.Charts.drawGauge(gaugeEl, s.hit_rate);
    statsEl.replaceChildren(
      ...rows.map(([label, value]) => h("div", {}, h("dt", {}, label), h("dd", {}, value ?? "–"))));
  }

  // ---------- history ----------

  function renderHistory() {
    if (!history.length) return;
    historyEl.replaceChildren(
      ...history.map((item) =>
        h("li", {},
          h("button", { type: "button", class: "history-item", "data-q": item.question, disabled: pending },
            h("span", { class: "history-q" }, item.question),
            h("span", { class: "history-meta" },
              h("span", { class: `badge tone-${item.tone}` }, item.badge),
              h("span", { class: "ms" }, fmtMs(item.latency)))))));
    historyEl.querySelectorAll(".history-item").forEach((btn) =>
      btn.addEventListener("click", () => {
        input.value = btn.dataset.q;
        updateCount();
        ask(btn.dataset.q);
      }));
  }

  // ---------- asking ----------

  function setPending(on) {
    pending = on;
    submitBtn.disabled = on;
    submitBtn.textContent = on ? "Asking…" : "Ask";
    submitBtn.classList.toggle("busy", on);
    resultEl.setAttribute("aria-busy", on ? "true" : "false");
    document.querySelectorAll(".sample, .history-item").forEach((b) => { b.disabled = on; });
  }

  async function ask(raw) {
    const question = String(raw || "").trim();
    if (!question || pending) return;
    if (question.length > MAX_CHARS) {
      showError(`Questions can be up to ${MAX_CHARS} characters. This one is ${question.length}.`);
      return;
    }
    clearError();
    setPending(true);
    try {
      const res = await fetch("/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ question }),
      });
      let body = null;
      try { body = await res.json(); } catch { /* non-JSON error page */ }
      setConn(true);
      if (!res.ok || !body) {
        showError(httpMessage(res.status, body));
        return;
      }
      renderPipeline(pathSteps(body), true);
      renderResult(body, question);
      const info = statusInfo(body);
      history.unshift({
        question, tone: info.tone, badge: info.badge, latency: body.latency_ms,
      });
      history.length = Math.min(history.length, HISTORY_LIMIT);
      trend.push({ ms: body.latency_ms, tone: info.tone });
      if (trend.length > 12) trend.shift();
      window.Charts.drawTrend(trendEl, trend);
    } catch {
      setConn(false);
      showError("Can't reach the server. Check that it is running (python -m app.main), then ask again.");
    } finally {
      setPending(false);
      renderHistory();
      refreshStats();
    }
  }

  // ---------- wiring ----------

  function updateCount() {
    const n = input.value.length;
    countEl.textContent = n >= COUNTER_FROM ? `${n} / ${MAX_CHARS}` : "";
    countEl.classList.toggle("over", n > MAX_CHARS);
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    ask(input.value);
  });

  input.addEventListener("input", updateCount);
  input.addEventListener("keydown", (e) => {
    // Enter asks; Shift+Enter adds a line. Leave Enter alone while an IME is composing.
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      form.requestSubmit();
    }
  });

  const samplesEl = $("samples");
  for (const q of SAMPLES) {
    const btn = h("button", { type: "button", class: "sample" }, q);
    btn.addEventListener("click", () => {
      input.value = q;
      updateCount();
      ask(q);
    });
    samplesEl.append(btn);
  }

  renderIdlePath();
  window.Charts.drawTrend(trendEl, trend);
  fetch("/health").then((r) => setConn(r.ok)).catch(() => setConn(false));
  refreshStats();
})();
