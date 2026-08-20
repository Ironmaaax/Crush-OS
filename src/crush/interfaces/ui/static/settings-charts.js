/* settings-charts.js — SVG charts vanilla pour la page Conso */
(function () {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";

  function svgEl(tag, attrs) {
    const el = document.createElementNS(NS, tag);
    for (const k in attrs) el.setAttribute(k, attrs[k]);
    return el;
  }
  function mkSvg(w, h) {
    return svgEl("svg", { width: w, height: h, viewBox: `0 0 ${w} ${h}` });
  }

  /* ── Sparkline inline (petit, sans axes) ─────────────────────── */
  function sparkline(data, opts) {
    opts = opts || {};
    const w = opts.width || 80, h = opts.height || 28;
    const color = opts.color || "var(--accent)";
    const max = Math.max(...data) || 1;
    const step = w / Math.max(data.length - 1, 1);
    const pts = data.map((v, i) =>
      `${(i * step).toFixed(1)},${(h - (v / max) * (h - 3) - 1).toFixed(1)}`
    ).join(" ");
    const root = mkSvg(w, h);
    root.style.display = "block";
    root.style.width = "100%";
    root.style.height = h + "px";
    root.appendChild(svgEl("polyline", {
      points: pts, fill: "none", stroke: color,
      "stroke-width": "1.2", "stroke-linejoin": "round", "stroke-linecap": "round",
    }));
    return root;
  }

  /* ── Area chart (grande évolution) ──────────────────────────── */
  function areaChart(data, opts) {
    opts = opts || {};
    const color = opts.color || "#4A9EFF";
    const gradId = "conso-area-grad-" + Math.random().toString(36).slice(2, 6);
    const max = Math.max(...data) * 1.1 || 1;

    // Wrapper : position relative pour coller les labels HTML par-dessus
    const wrap = document.createElement("div");
    wrap.style.cssText = "position:relative;width:100%;height:100%;";

    // Labels Y en HTML (pas distordus)
    const labelW = 36;
    [0.25, 0.5, 0.75, 1].forEach(p => {
      const lbl = document.createElement("span");
      const val = max * p;
      lbl.textContent = val >= 1 ? "$" + val.toFixed(0) : "$" + val.toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
      lbl.style.cssText = [
        "position:absolute",
        "left:0",
        "width:" + labelW + "px",
        "text-align:right",
        "top:" + ((1 - p) * 100).toFixed(1) + "%",
        "transform:translateY(-50%)",
        "font-family:var(--mono)",
        "font-size:9.5px",
        "letter-spacing:0.04em",
        "color:rgba(220,232,255,0.36)",
        "pointer-events:none",
      ].join(";");
      wrap.appendChild(lbl);
    });

    // SVG chart (grid + area), décalé à droite des labels
    const svgWrap = document.createElement("div");
    svgWrap.style.cssText = "position:absolute;left:" + labelW + "px;right:0;top:0;bottom:0;";

    // viewBox normalisé 0-1000 x 0-100 (indépendant des pixels)
    const vw = 1000, vh = 100;
    const svg = mkSvg(vw, vh);
    svg.setAttribute("width", "100%");
    svg.setAttribute("height", "100%");
    svg.setAttribute("preserveAspectRatio", "none");

    const len = data.length;
    const px = (i) => (i / Math.max(len - 1, 1)) * vw;
    const py = (v) => vh - (v / max) * vh;

    // Grid lines
    [0.25, 0.5, 0.75, 1].forEach(p => {
      const y = (vh * (1 - p)).toFixed(1);
      svg.appendChild(svgEl("line", {
        x1: 0, x2: vw, y1: y, y2: y,
        stroke: "rgba(220,232,255,0.05)", "stroke-dasharray": "4 6",
      }));
    });

    // Gradient
    const defs = document.createElementNS(NS, "defs");
    const grad = svgEl("linearGradient", { id: gradId, x1: "0", y1: "0", x2: "0", y2: "1" });
    grad.appendChild(svgEl("stop", { offset: "0%",   "stop-color": color, "stop-opacity": "0.52" }));
    grad.appendChild(svgEl("stop", { offset: "100%", "stop-color": color, "stop-opacity": "0.02" }));
    defs.appendChild(grad);
    svg.appendChild(defs);

    const pts = data.map((v, i) => `${px(i).toFixed(1)},${py(v).toFixed(1)}`).join(" ");
    svg.appendChild(svgEl("polygon", {
      points: `0,${vh} ${pts} ${vw},${vh}`,
      fill: `url(#${gradId})`,
    }));
    svg.appendChild(svgEl("polyline", {
      points: pts, fill: "none", stroke: color,
      "stroke-width": "1.5", "stroke-linejoin": "round",
    }));

    // ── Crosshair vertical ──────────────────────────────────────────
    const xLine = svgEl("line", {
      x1: 0, x2: 0, y1: 0, y2: vh,
      stroke: "rgba(220,232,255,0.28)",
      "stroke-width": "1",
      "stroke-dasharray": "3 5",
      display: "none",
    });
    svg.appendChild(xLine);

    const xDot = svgEl("circle", {
      cx: 0, cy: 0, r: "4",
      fill: color,
      stroke: "rgba(8,11,22,0.92)",
      "stroke-width": "2",
      display: "none",
    });
    svg.appendChild(xDot);

    // ── Tooltip HTML ─────────────────────────────────────────────────
    const tip = document.createElement("div");
    tip.style.cssText = [
      "position:absolute",
      "pointer-events:none",
      "opacity:0",
      "transition:opacity 0.12s ease",
      "background:rgba(8,11,22,0.93)",
      "border:0.5px solid rgba(220,232,255,0.14)",
      "border-radius:8px",
      "padding:6px 11px",
      "font-family:var(--mono)",
      "font-size:11px",
      "letter-spacing:0.04em",
      "color:var(--fg-0)",
      "white-space:nowrap",
      "backdrop-filter:blur(12px)",
      "-webkit-backdrop-filter:blur(12px)",
      "box-shadow:0 4px 18px rgba(0,0,0,0.5)",
      "z-index:10",
    ].join(";");
    wrap.appendChild(tip);

    // ── Interactions souris ──────────────────────────────────────────
    svgWrap.style.cursor = "crosshair";

    svgWrap.addEventListener("mousemove", function (e) {
      const frac = Math.max(0, Math.min(1, e.offsetX / svgWrap.offsetWidth));
      const idx  = Math.min(len - 1, Math.max(0, Math.round(frac * (len - 1))));
      const val  = data[idx];
      const sx   = px(idx);
      const sy   = py(val);

      xLine.setAttribute("x1", sx.toFixed(1));
      xLine.setAttribute("x2", sx.toFixed(1));
      xLine.removeAttribute("display");

      xDot.setAttribute("cx", sx.toFixed(1));
      xDot.setAttribute("cy", sy.toFixed(1));
      xDot.removeAttribute("display");

      const dayStr = idx === len - 1
        ? "Aujourd’hui"
        : "J−" + (len - 1 - idx);
      const valStr = val >= 1
        ? "$" + val.toFixed(2)
        : val > 0
          ? "$" + val.toFixed(4).replace(/0+$/, "").replace(/\.$/, "")
          : "$0";
      tip.innerHTML =
        "<span style='color:rgba(220,232,255,.38);margin-right:8px'>" +
        dayStr + "</span>" + valStr;
      tip.style.opacity = "1";

      // Positionnement : à droite du curseur, flip si bord droit trop proche
      const tipW  = tip.offsetWidth  || 120;
      const tipH  = tip.offsetHeight || 30;
      const wrapW = wrap.offsetWidth  || 400;
      const wrapH = wrap.offsetHeight || 180;
      let left = labelW + e.offsetX + 14;
      let top  = e.offsetY - Math.round(tipH / 2);
      if (left + tipW > wrapW - 8) left = labelW + e.offsetX - tipW - 10;
      top = Math.max(6, Math.min(wrapH - tipH - 6, top));
      tip.style.left = left + "px";
      tip.style.top  = top + "px";
    });

    svgWrap.addEventListener("mouseleave", function () {
      xLine.setAttribute("display", "none");
      xDot.setAttribute("display", "none");
      tip.style.opacity = "0";
    });

    svgWrap.appendChild(svg);
    wrap.appendChild(svgWrap);
    return wrap;
  }

  /* ── Donut (strokeDasharray style, comme le prototype) ───────── */
  function donut(slices, total, opts) {
    opts = opts || {};
    const r = 64, stroke = 18, cx = 80, cy = 80;
    const C = 2 * Math.PI * r;
    const sum = slices.reduce((s, x) => s + (x.value || 0), 0) || 1;

    const root = mkSvg(160, 160);

    // Background track
    root.appendChild(svgEl("circle", {
      cx, cy, r, fill: "none",
      stroke: "rgba(220,232,255,0.06)", "stroke-width": stroke,
    }));

    // Slices
    let acc = 0;
    slices.forEach((s) => {
      const frac = (s.value || 0) / sum;
      const len = C * frac;
      const gap = len > 2 ? 1 : 0;
      const dash = `${Math.max(0, len - gap)} ${C}`;
      root.appendChild(svgEl("circle", {
        cx, cy, r, fill: "none",
        stroke: s.color || "var(--accent)",
        "stroke-width": stroke,
        "stroke-dasharray": dash,
        "stroke-dashoffset": -acc,
        transform: `rotate(-90 ${cx} ${cy})`,
      }));
      acc += len;
    });

    // Center label
    const t1 = svgEl("text", {
      x: cx, y: cy - 2, "text-anchor": "middle",
      "font-family": "var(--serif)", "font-size": "32", "font-weight": "300",
      "letter-spacing": "-0.025em", fill: "var(--fg-0)",
    });
    t1.textContent = total || ("$" + sum.toFixed(0));
    root.appendChild(t1);

    const t2 = svgEl("text", {
      x: cx, y: cy + 18, "text-anchor": "middle",
      "font-family": "var(--mono)", "font-size": "9", "letter-spacing": "0.24em",
      fill: "var(--fg-3)",
    });
    t2.textContent = "TOTAL";
    root.appendChild(t2);

    return root;
  }

  /* ── Heatmap 24h ─────────────────────────────────────────────── */
  function heatRow(values, opts) {
    opts = opts || {};
    const wrap = document.createElement("div");
    wrap.className = "conso-heat-row";
    const max = Math.max(...values) || 1;
    values.forEach(v => {
      const cell = document.createElement("i");
      const norm = v / max;
      cell.style.background = `rgba(74,158,255,${(0.12 + norm * 0.88).toFixed(2)})`;
      wrap.appendChild(cell);
    });
    return wrap;
  }

  window.CrushCharts = { sparkline, areaChart, donut, heatRow };
})();
