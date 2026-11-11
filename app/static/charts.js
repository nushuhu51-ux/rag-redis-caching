/* Small SVG charts for the Q&A page: hit-rate ring and per-request latency bars.
 * Self-contained, no libraries. Geometry goes in SVG attributes (not style="") to satisfy the CSP. */
(() => {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";
  const s = (tag, attrs, text) => {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, String(v));
    if (text != null) n.textContent = text;
    return n;
  };

  /** Ring showing a 0..1 rate; the arc sweeps from the previous value. */
  const R = 48, C = 2 * Math.PI * R;
  let prev = 0;
  function drawGauge(svg, rate) {
    const v = typeof rate === "number" ? Math.max(0, Math.min(1, rate)) : 0;
    const arc = s("circle", { class: "g-arc", cx: 60, cy: 60, r: R, transform: "rotate(-90 60 60)",
      "stroke-dasharray": `${C * prev} ${C}` });
    svg.replaceChildren(
      s("circle", { class: "g-track", cx: 60, cy: 60, r: R }), arc,
      s("text", { class: "g-val", x: 60, y: 62, "text-anchor": "middle" },
        typeof rate === "number" ? `${Number((v * 100).toFixed(1))}%` : "–"),
      s("text", { class: "g-lab", x: 60, y: 80, "text-anchor": "middle" }, "hit rate"));
    requestAnimationFrame(() => requestAnimationFrame(() =>
      arc.setAttribute("stroke-dasharray", `${C * v} ${C}`)));
    prev = v;
  }

  /** Bars of latency (oldest left). items: [{ms, tone}] */
  function drawTrend(svg, items) {
    const W = 260, H = 110, pad = 14, base = H - 20, top = 10;
    const kids = [s("line", { class: "t-axis", x1: pad, x2: W - pad, y1: base, y2: base })];
    if (items.length) {
      const max = Math.max(...items.map((i) => i.ms), 1);
      const slot = (W - 2 * pad) / Math.max(items.length, 8);
      items.forEach((it, i) => {
        const h = Math.max(3, ((base - top) * it.ms) / max);
        kids.push(s("rect", { class: `t-bar tone-${it.tone}`, x: pad + i * slot + 3, y: base - h,
          width: Math.max(slot - 6, 4), height: h, rx: 2 },
        ));
        kids[kids.length - 1].append(s("title", {}, `${it.ms.toFixed(it.ms < 10 ? 2 : 0)} ms`));
        kids.push(s("text", { class: "t-lab", x: pad + i * slot + slot / 2, y: H - 6, "text-anchor": "middle" }, String(i + 1)));
      });
      kids.push(s("text", { class: "t-lab", x: pad, y: top - 1 }, `max ${max >= 1000 ? (max / 1000).toFixed(2) + " s" : Math.round(max) + " ms"}`));
    }
    svg.replaceChildren(...kids);
  }

  window.Charts = { drawGauge, drawTrend };
})();
