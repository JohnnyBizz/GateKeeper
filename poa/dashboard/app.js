/* Dashboard client.
 *
 * Receives engine state over a WebSocket and renders it. Deliberately
 * dependency-free: the whole point of this tool is that it runs locally with a
 * short install, and a build step would work against that.
 */

(function () {
  "use strict";

  const state = {
    signal: null,
    candles: [],
    heikinAshi: [],
    chartMode: "candles",
    options: null,
    expiresAt: null,
    settingsDirty: false,
  };

  const el = (id) => document.getElementById(id);

  // ---------------------------------------------------------------- utils

  function fmt(value, digits = 1) {
    if (value === null || value === undefined || Number.isNaN(value)) return "--";
    return Number(value).toFixed(digits);
  }

  function fmtPrice(value) {
    if (value === null || value === undefined || Number.isNaN(value)) return "--";
    const magnitude = Math.abs(value);
    if (magnitude >= 1000) return value.toFixed(2);
    if (magnitude >= 10) return value.toFixed(3);
    if (magnitude >= 1) return value.toFixed(5);
    return value.toFixed(6);
  }

  function titleCase(text) {
    if (!text) return "--";
    return String(text)
      .replace(/_/g, " ")
      .toLowerCase()
      .replace(/\b\w/g, (c) => c.toUpperCase());
  }

  function scoreColor(score) {
    if (score >= 80) return "var(--call)";
    if (score >= 65) return "var(--accent)";
    if (score >= 50) return "var(--wait)";
    return "var(--put)";
  }

  function biasColor(bias) {
    if (bias === "BULLISH") return "var(--call)";
    if (bias === "BEARISH") return "var(--put)";
    return "var(--text-dim)";
  }

  function clearChildren(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  // ------------------------------------------------------------ websocket

  let socket = null;
  let reconnectDelay = 1000;

  function connect() {
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    socket = new WebSocket(`${proto}://${window.location.host}/ws`);

    socket.onopen = () => {
      reconnectDelay = 1000;
      setConnection("live", "live");
    };

    socket.onmessage = (event) => {
      let payload;
      try {
        payload = JSON.parse(event.data);
      } catch (err) {
        return;
      }
      if (payload.type === "state") {
        if (payload.options && !state.options) applyOptions(payload.options);
        if (payload.disclaimer) el("disclaimer").textContent = payload.disclaimer;
        renderState(payload.state);
      } else if (payload.type === "alert") {
        prependAlert(payload.alert);
      }
    };

    socket.onclose = () => {
      setConnection("idle", "reconnecting…");
      // Back off so a stopped server does not produce a connection storm.
      setTimeout(connect, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 1.6, 15000);
    };

    socket.onerror = () => setConnection("error", "error");
  }

  function setConnection(kind, label) {
    const node = el("connection");
    node.className = `status-pill status-${kind}`;
    node.textContent = label;
  }

  // -------------------------------------------------------------- options

  async function loadOptions() {
    const response = await fetch("/api/options");
    applyOptions(await response.json());
  }

  function applyOptions(options) {
    state.options = options;

    fillSelect(el("chart-timeframe"), options.chart_timeframes, options.current.chart_timeframe);
    fillSelect(el("trade-duration"), options.trade_durations, options.current.trade_duration);
    el("asset-input").value = options.current.asset || "";

    const minConfidence = el("min-confidence");
    const target = String(Math.round(options.current.min_confidence || 75));
    if (![...minConfidence.options].some((o) => o.value === target)) {
      const extra = document.createElement("option");
      extra.value = target;
      extra.textContent = target;
      minConfidence.appendChild(extra);
    }
    minConfidence.value = target;
  }

  function fillSelect(select, entries, selected) {
    clearChildren(select);
    entries.forEach((entry) => {
      const option = document.createElement("option");
      option.value = entry.seconds;
      option.textContent = entry.label;
      if (Number(entry.seconds) === Number(selected)) option.selected = true;
      select.appendChild(option);
    });
  }

  async function pushSettings(changes) {
    try {
      const response = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(changes),
      });
      if (!response.ok) {
        const detail = await response.json().catch(() => ({}));
        showBanner(detail.detail || "Could not apply that setting.", true);
        return;
      }
      hideBanner();
    } catch (err) {
      showBanner("Could not reach the analysis engine.", true);
    }
  }

  function showBanner(message, isError) {
    const banner = el("banner");
    banner.textContent = message;
    banner.className = `banner${isError ? " error" : ""}`;
  }

  function hideBanner() {
    el("banner").className = "banner hidden";
  }

  // ---------------------------------------------------------------- render

  function renderState(snapshot) {
    if (!snapshot) return;

    state.candles = snapshot.candles || [];
    state.heikinAshi = snapshot.heikin_ashi || [];

    if (snapshot.last_error) {
      showBanner(snapshot.last_error, true);
    } else if (snapshot.capture && snapshot.capture.quality && !snapshot.capture.quality.ok) {
      showBanner(
        `Chart data quality ${fmt(snapshot.capture.quality.confidence, 0)}% — ` +
          (snapshot.capture.quality.issues || []).join("; "),
        false
      );
    } else if (snapshot.capture && snapshot.capture.demo) {
      showBanner("Running on synthetic demo data — not a live market feed.", false);
    } else {
      hideBanner();
    }

    const signal = snapshot.signal;
    state.signal = signal;
    state.expiresAt = signal && signal.expires_at ? new Date(signal.expires_at) : null;

    renderSummary(signal, snapshot);
    renderSignalCard(signal);
    renderTimeframes(signal);
    renderReadings(signal);
    renderLevels(signal);
    renderComponents(signal);
    renderDurationLadder(signal);
    renderChartFooter(snapshot, signal);
    drawChart();
  }

  function renderSummary(signal, snapshot) {
    if (!signal) return;
    el("sum-asset").textContent = signal.asset || "--";
    el("sum-chart").textContent = signal.chart_timeframe_label || "--";
    el("sum-duration").textContent = signal.trade_duration_label || "--";

    const regime =
      snapshot.signal && snapshot.signal.timeframes
        ? snapshot.signal.timeframes.current.regime.label
        : "--";
    el("sum-regime").textContent = regime;

    const sumSignal = el("sum-signal");
    sumSignal.textContent = `${signal.direction_emoji || ""} ${signal.direction}`.trim();
    sumSignal.style.color = directionColor(signal.direction);

    const confidence = el("sum-confidence");
    confidence.textContent = `${fmt(signal.overall_confidence, 0)}%`;
    confidence.style.color = scoreColor(signal.overall_confidence);
  }

  function directionColor(direction) {
    if (direction === "CALL") return "var(--call)";
    if (direction === "PUT") return "var(--put)";
    if (direction === "NO_TRADE") return "var(--put)";
    return "var(--wait)";
  }

  function renderSignalCard(signal) {
    if (!signal) return;
    const card = el("signal-card");

    const classes = {
      CALL: "signal-call",
      PUT: "signal-put",
      WAIT: "signal-wait",
      NO_TRADE: "signal-none",
    };
    card.className = `signal-card ${classes[signal.direction] || "signal-wait"}`;

    const labels = {
      CALL: "🟢 CALL / BUY",
      PUT: "🔴 PUT / SELL",
      WAIT: "🟡 WAIT",
      NO_TRADE: "🛑 NO TRADE",
    };
    el("signal-direction").textContent = labels[signal.direction] || signal.direction;
    el("signal-headline").textContent = signal.headline || "";

    setMeter("direction", signal.direction_confidence);
    setMeter("duration", signal.duration_confidence);
    setMeter("overall", signal.overall_confidence);

    el("setup-quality").textContent = signal.setup_quality_label || "--";
    el("selected-duration").textContent = signal.trade_duration_label || "--";

    const recommended = el("recommended-duration");
    if (signal.duration) {
      recommended.textContent = signal.duration.recommended_label;
      recommended.style.color = signal.duration.matches_recommendation
        ? "var(--call)"
        : "var(--wait)";
    } else {
      recommended.textContent = "--";
      recommended.style.color = "";
    }

    const stateNode = el("signal-state");
    stateNode.textContent = titleCase(signal.state);
    stateNode.style.color =
      signal.state === "INVALIDATED"
        ? "var(--put)"
        : signal.state === "WEAKENING"
        ? "var(--wait)"
        : "var(--text)";

    el("signal-reason").textContent = signal.reason || "--";
    el("invalidation").textContent = signal.invalidation || "--";

    const why = el("why-body");
    clearChildren(why);
    const list = document.createElement("ul");
    (signal.why || []).forEach((line) => {
      const item = document.createElement("li");
      item.textContent = line;
      list.appendChild(item);
    });
    if (signal.gates && signal.gates.results) {
      const heading = document.createElement("li");
      heading.style.color = "var(--text)";
      heading.style.marginTop = "8px";
      heading.textContent = "Confirmation requirements:";
      list.appendChild(heading);
      signal.gates.results.forEach((gate) => {
        const item = document.createElement("li");
        item.textContent = `${gate.passed ? "✓" : "✗"} ${gate.detail}`;
        item.style.color = gate.passed ? "var(--call)" : gate.blocking ? "var(--put)" : "var(--wait)";
        list.appendChild(item);
      });
    }
    why.appendChild(list);

    const warnings = el("warnings");
    clearChildren(warnings);
    (signal.warnings || []).forEach((text) => {
      const item = document.createElement("div");
      item.className = "warning-item";
      item.textContent = `⚠️ ${text}`;
      warnings.appendChild(item);
    });
  }

  function setMeter(name, value) {
    const score = Number(value) || 0;
    const fill = el(`meter-${name}`);
    fill.style.width = `${Math.max(0, Math.min(100, score))}%`;
    fill.style.background = scoreColor(score);
    el(`value-${name}`).textContent = `${fmt(score, 0)}`;
  }

  function renderTimeframes(signal) {
    const container = el("tf-stack");
    clearChildren(container);
    if (!signal || !signal.timeframes) return;

    const rows = [
      ["Higher", signal.timeframes.higher],
      ["Current", signal.timeframes.current],
      ["Entry", signal.timeframes.entry],
    ];

    rows.forEach(([name, view]) => {
      const row = document.createElement("div");
      row.className = "tf-row";

      const label = document.createElement("div");
      label.className = "tf-name";
      label.textContent = `${name} · ${view.label}`;

      const barWrap = document.createElement("div");
      barWrap.className = "tf-bar";
      const bar = document.createElement("span");
      bar.style.width = `${Math.max(2, Math.min(100, view.strength))}%`;
      bar.style.background = biasColor(view.trend);
      barWrap.appendChild(bar);

      const score = document.createElement("div");
      score.className = "tf-score";
      score.textContent = `${fmt(view.strength, 0)}`;
      score.style.color = biasColor(view.trend);

      row.append(label, barWrap, score);
      container.appendChild(row);
    });
  }

  function renderReadings(signal) {
    const container = el("readings");
    clearChildren(container);
    if (!signal || !signal.timeframes) return;

    const current = signal.timeframes.current;
    const indicators = current.indicators;

    const entries = [
      ["Trend", `${titleCase(current.trend)} · ${fmt(current.strength, 0)}/100`, biasColor(current.trend)],
      ["Structure", current.structure.label, biasColor(current.structure.bias)],
      ["Heikin Ashi", current.heikin_ashi.pattern, biasColor(current.heikin_ashi.bias)],
      ["Momentum", `${titleCase(current.momentum.label)} ${titleCase(current.momentum.bias)}`, biasColor(current.momentum.bias)],
      ["Volatility", `${titleCase(current.volatility.regime)} · ATR pct ${fmt(current.volatility.atr_percentile, 0)}`, null],
      ["EMA stack", titleCase(indicators.ema_alignment), biasColor(indicators.ema_alignment)],
      ["RSI", fmt(indicators.rsi, 1), biasColor(indicators.rsi_bias)],
      ["MACD", titleCase(indicators.macd_bias), biasColor(indicators.macd_bias)],
      ["ADX", fmt(indicators.adx, 1), null],
      ["ATR", fmtPrice(indicators.atr), null],
      ["Reversal risk", `${fmt(current.regime.reversal_risk * 100, 0)}%`, current.regime.reversal_risk >= 0.5 ? "var(--put)" : null],
      ["TF agreement", `${fmt(signal.timeframes.agreement * 100, 0)}%`, null],
    ];

    entries.forEach(([key, value, color]) => {
      const cell = document.createElement("div");
      cell.className = "reading";
      const k = document.createElement("span");
      k.className = "k";
      k.textContent = key;
      const v = document.createElement("span");
      v.className = "v";
      v.textContent = value;
      if (color) v.style.color = color;
      cell.append(k, v);
      container.appendChild(cell);
    });
  }

  function renderLevels(signal) {
    const container = el("levels");
    clearChildren(container);
    if (!signal || !signal.timeframes || !signal.timeframes.current.levels) return;

    const levels = signal.timeframes.current.levels.levels || [];
    if (!levels.length) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "No significant levels identified yet.";
      container.appendChild(empty);
      return;
    }

    levels
      .slice()
      .sort((a, b) => b.price - a.price)
      .forEach((level) => {
        const row = document.createElement("div");
        row.className = "level-row";

        const left = document.createElement("div");
        const price = document.createElement("span");
        price.className = "level-price";
        price.textContent = fmtPrice(level.price);
        const kind = document.createElement("span");
        kind.className = `level-tag ${level.kind === "SUPPORT" ? "tag-support" : "tag-resistance"}`;
        kind.textContent = level.kind;
        kind.style.marginLeft = "8px";
        left.append(price, kind);

        const right = document.createElement("div");
        const importance = document.createElement("span");
        importance.className = `level-tag tag-${level.importance.toLowerCase()}`;
        importance.textContent = level.importance;
        const strength = document.createElement("span");
        strength.style.marginLeft = "8px";
        strength.style.fontFamily = "var(--mono)";
        strength.textContent = `${fmt(level.strength, 0)}/100`;
        right.append(importance, strength);

        row.append(left, right);
        container.appendChild(row);
      });
  }

  function renderComponents(signal) {
    const container = el("score-components");
    clearChildren(container);
    if (!signal || !signal.score) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "No score available — the chart could not be analysed.";
      container.appendChild(empty);
      return;
    }

    signal.score.components.forEach((component) => {
      const row = document.createElement("div");
      row.className = "component";

      const name = document.createElement("div");
      name.className = "component-name";
      name.textContent = titleCase(component.name);
      name.title = component.detail || "";

      const barWrap = document.createElement("div");
      barWrap.className = "component-bar";
      const bar = document.createElement("span");
      const ratio = component.weight ? component.points / component.weight : 0;
      bar.style.width = `${Math.max(2, Math.min(100, ratio * 100))}%`;
      bar.style.background = scoreColor(ratio * 100);
      barWrap.appendChild(bar);

      const points = document.createElement("div");
      points.className = "component-points";
      points.textContent = `${fmt(component.points, 1)}/${fmt(component.weight, 0)}`;

      row.append(name, barWrap, points);
      container.appendChild(row);
    });

    const total = document.createElement("div");
    total.className = "component";
    total.style.borderTop = "1px solid var(--border)";
    total.style.marginTop = "6px";
    total.style.paddingTop = "8px";
    const label = document.createElement("div");
    label.textContent = "Total";
    label.style.fontWeight = "600";
    const spacer = document.createElement("div");
    const value = document.createElement("div");
    value.className = "component-points";
    value.style.color = scoreColor(signal.score.total);
    value.textContent = `${fmt(signal.score.total, 0)}/100`;
    total.append(label, spacer, value);
    container.appendChild(total);
  }

  function renderDurationLadder(signal) {
    const container = el("duration-ladder");
    clearChildren(container);
    if (!signal || !signal.duration) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "Duration analysis is unavailable without chart data.";
      container.appendChild(empty);
      return;
    }

    signal.duration.candidates.forEach((candidate) => {
      const row = document.createElement("div");
      row.className = "duration-row";
      if (candidate.seconds === signal.duration.selected_seconds) row.classList.add("selected");
      if (candidate.seconds === signal.duration.recommended_seconds) row.classList.add("recommended");
      row.title = candidate.reason || "";

      const label = document.createElement("div");
      label.className = "duration-label";
      label.textContent = candidate.label;

      const barWrap = document.createElement("div");
      barWrap.className = "duration-bar";
      const bar = document.createElement("span");
      bar.style.width = `${Math.max(2, Math.min(100, candidate.score))}%`;
      bar.style.background = scoreColor(candidate.score);
      barWrap.appendChild(bar);

      const score = document.createElement("div");
      score.className = "duration-score";
      score.textContent = fmt(candidate.score, 0);

      const emoji = document.createElement("div");
      emoji.textContent = candidate.emoji;

      row.append(label, barWrap, score, emoji);
      container.appendChild(row);
    });

    const note = document.createElement("p");
    note.className = "muted";
    note.style.marginTop = "8px";
    note.style.fontSize = "11.5px";
    note.textContent = signal.duration.reason;
    container.appendChild(note);
  }

  function renderChartFooter(snapshot, signal) {
    const source = snapshot.source || {};
    el("chart-source").textContent = `source: ${source.name || "--"}${
      source.demo ? " (demo)" : ""
    }`;
    el("chart-price").textContent = signal ? signal.price_display : "--";

    const quality = (snapshot.capture && snapshot.capture.quality) || null;
    const node = el("chart-quality");
    if (quality) {
      node.textContent = `data quality: ${fmt(quality.confidence, 0)}% · ${quality.candle_count} candles`;
      node.style.color = quality.ok ? "var(--text-dim)" : "var(--put)";
    } else {
      node.textContent = "data quality: --";
    }
  }

  // ------------------------------------------------------------- alerts

  const alertHistory = [];

  function prependAlert(alert) {
    alertHistory.unshift(alert);
    if (alertHistory.length > 25) alertHistory.pop();

    const container = el("alert-list");
    clearChildren(container);
    alertHistory.forEach((entry) => {
      const item = document.createElement("div");
      item.className = `alert-item ${entry.severity || "info"}`;

      const title = document.createElement("div");
      title.className = "alert-title";
      const text = document.createElement("span");
      text.textContent = `${entry.emoji || ""} ${entry.title}`.trim();
      const time = document.createElement("span");
      time.className = "alert-time";
      time.textContent = entry.timestamp
        ? new Date(entry.timestamp).toLocaleTimeString()
        : "";
      title.append(text, time);

      const body = document.createElement("div");
      body.className = "alert-body";
      body.textContent = entry.body || "";

      item.append(title, body);
      container.appendChild(item);
    });
    loadJournal();
  }

  // ------------------------------------------------------------- journal

  async function loadJournal() {
    try {
      const [journalResponse, statsResponse] = await Promise.all([
        fetch("/api/journal?limit=60"),
        fetch("/api/statistics"),
      ]);
      const journal = await journalResponse.json();
      const stats = await statsResponse.json();
      renderJournal(journal.entries || []);
      renderJournalStats(stats);
    } catch (err) {
      /* the journal is non-critical; leave the last render in place */
    }
  }

  function renderJournalStats(stats) {
    if (!stats) return;
    const parts = [
      `${stats.total_signals} signals`,
      `${stats.settled} settled`,
    ];
    if (stats.win_rate !== null && stats.win_rate !== undefined) {
      parts.push(`${fmt(stats.win_rate, 1)}% won (break-even ${fmt(stats.breakeven_rate, 1)}%)`);
    }
    if (stats.sample_warning) parts.push(stats.sample_warning);
    el("journal-stats").textContent = parts.join(" · ");
  }

  function renderJournal(entries) {
    const body = el("journal-body");
    clearChildren(body);

    if (!entries.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 10;
      cell.className = "muted";
      cell.textContent = "No signals recorded yet.";
      row.appendChild(cell);
      body.appendChild(row);
      return;
    }

    entries.forEach((entry) => {
      const row = document.createElement("tr");
      const cells = [
        new Date(entry.timestamp).toLocaleTimeString(),
        entry.asset,
        secondsLabel(entry.chart_timeframe),
        secondsLabel(entry.trade_duration),
        null, // signal pill
        `${fmt(entry.overall_confidence, 0)}%`,
        titleCase(entry.market_regime),
        entry.heikin_ashi || "--",
        null, // outcome pill
        entry.reason || "",
      ];

      cells.forEach((value, index) => {
        const cell = document.createElement("td");
        if (index === 4) {
          const pill = document.createElement("span");
          pill.className = `pill pill-${entry.direction}`;
          pill.textContent = entry.direction;
          cell.appendChild(pill);
        } else if (index === 8) {
          if (entry.outcome) {
            const pill = document.createElement("span");
            pill.className = `pill pill-${entry.outcome}`;
            pill.textContent = entry.outcome;
            cell.appendChild(pill);
          } else {
            cell.textContent = entry.direction === "CALL" || entry.direction === "PUT" ? "pending" : "--";
            cell.className = "muted";
          }
        } else if (index === 9) {
          cell.className = "reason-cell";
          cell.textContent = value;
        } else {
          cell.textContent = value;
        }
        row.appendChild(cell);
      });

      body.appendChild(row);
    });
  }

  function secondsLabel(seconds) {
    if (!seconds) return "--";
    if (seconds < 60) return `${seconds} SEC`;
    if (seconds % 3600 === 0) return `${seconds / 3600} HR`;
    if (seconds % 60 === 0) return `${seconds / 60} MIN`;
    return `${seconds}s`;
  }

  // --------------------------------------------------------------- chart

  function drawChart() {
    const canvas = el("chart");
    const context = canvas.getContext("2d");
    const width = canvas.width;
    const height = canvas.height;

    context.clearRect(0, 0, width, height);
    context.fillStyle = "#0f131b";
    context.fillRect(0, 0, width, height);

    const data = state.chartMode === "heikin" ? state.heikinAshi : state.candles;
    if (!data || data.length < 2) {
      context.fillStyle = "#5d6878";
      context.font = "13px sans-serif";
      context.fillText("Waiting for chart data…", 18, 28);
      return;
    }

    const padding = { left: 10, right: 78, top: 14, bottom: 20 };
    const plotWidth = width - padding.left - padding.right;
    const plotHeight = height - padding.top - padding.bottom;

    const visible = data.slice(-120);
    let high = -Infinity;
    let low = Infinity;
    visible.forEach((candle) => {
      high = Math.max(high, candle.high);
      low = Math.min(low, candle.low);
    });
    const span = high - low || Math.abs(high) * 0.001 || 1;
    high += span * 0.06;
    low -= span * 0.06;

    const rowFor = (price) =>
      padding.top + ((high - price) / (high - low)) * plotHeight;
    const pitch = plotWidth / visible.length;
    const bodyWidth = Math.max(1.5, pitch * 0.62);

    // gridlines and price axis
    context.strokeStyle = "#1c2330";
    context.fillStyle = "#5d6878";
    context.font = "10px ui-monospace, monospace";
    context.lineWidth = 1;
    for (let i = 0; i <= 4; i += 1) {
      const price = high - ((high - low) * i) / 4;
      const y = Math.round(rowFor(price)) + 0.5;
      context.beginPath();
      context.moveTo(padding.left, y);
      context.lineTo(padding.left + plotWidth, y);
      context.stroke();
      context.fillText(fmtPrice(price), padding.left + plotWidth + 6, y + 3);
    }

    // support / resistance overlays
    const signal = state.signal;
    if (signal && signal.timeframes && signal.timeframes.current.levels) {
      (signal.timeframes.current.levels.levels || []).forEach((level) => {
        if (level.price > high || level.price < low) return;
        const y = rowFor(level.price);
        context.save();
        context.setLineDash([4, 4]);
        context.strokeStyle =
          level.kind === "SUPPORT" ? "rgba(41,194,106,0.45)" : "rgba(240,83,63,0.45)";
        context.lineWidth = level.importance === "MAJOR" ? 1.6 : 1;
        context.beginPath();
        context.moveTo(padding.left, y);
        context.lineTo(padding.left + plotWidth, y);
        context.stroke();
        context.restore();
      });
    }

    // candles
    visible.forEach((candle, index) => {
      const x = padding.left + index * pitch + pitch / 2;
      const bullish = candle.close >= candle.open;
      const color = bullish ? "#29c26a" : "#f0533f";

      context.strokeStyle = color;
      context.fillStyle = color;
      context.lineWidth = 1;

      context.beginPath();
      context.moveTo(Math.round(x) + 0.5, rowFor(candle.high));
      context.lineTo(Math.round(x) + 0.5, rowFor(candle.low));
      context.stroke();

      const top = rowFor(Math.max(candle.open, candle.close));
      const bottom = rowFor(Math.min(candle.open, candle.close));
      context.fillRect(x - bodyWidth / 2, top, bodyWidth, Math.max(1, bottom - top));
    });

    // last price marker
    const last = visible[visible.length - 1];
    const lastY = rowFor(last.close);
    context.save();
    context.setLineDash([2, 3]);
    context.strokeStyle = "#4a8fe7";
    context.beginPath();
    context.moveTo(padding.left, lastY);
    context.lineTo(padding.left + plotWidth, lastY);
    context.stroke();
    context.restore();
    context.fillStyle = "#4a8fe7";
    context.fillRect(padding.left + plotWidth, lastY - 8, padding.right - 4, 16);
    context.fillStyle = "#0d1017";
    context.font = "10px ui-monospace, monospace";
    context.fillText(fmtPrice(last.close), padding.left + plotWidth + 5, lastY + 3);
  }

  // ----------------------------------------------------------- countdown

  function tickCountdown() {
    const node = el("countdown");
    if (!state.expiresAt || !state.signal || !state.signal.actionable) {
      node.textContent = "--";
      return;
    }
    const remaining = Math.max(0, state.expiresAt - new Date());
    if (remaining === 0) {
      node.textContent = "elapsed";
      node.style.color = "var(--text-dim)";
      return;
    }
    const totalSeconds = Math.floor(remaining / 1000);
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;
    node.textContent = `${minutes}:${String(seconds).padStart(2, "0")}`;
    node.style.color = totalSeconds < 15 ? "var(--wait)" : "var(--text)";
  }

  // -------------------------------------------------------------- wiring

  function wire() {
    el("why-button").addEventListener("click", () => {
      const body = el("why-body");
      const expanded = !body.classList.contains("hidden");
      body.classList.toggle("hidden", expanded);
      el("why-button").setAttribute("aria-expanded", String(!expanded));
      el("why-button").textContent = expanded ? "WHY? ▾" : "WHY? ▴";
    });

    document.querySelectorAll(".toggle").forEach((button) => {
      button.addEventListener("click", () => {
        document.querySelectorAll(".toggle").forEach((b) => b.classList.remove("active"));
        button.classList.add("active");
        state.chartMode = button.dataset.mode;
        drawChart();
      });
    });

    el("chart-timeframe").addEventListener("change", (event) =>
      pushSettings({ chart_timeframe: Number(event.target.value) })
    );
    el("trade-duration").addEventListener("change", (event) =>
      pushSettings({ trade_duration: Number(event.target.value) })
    );
    el("min-confidence").addEventListener("change", (event) =>
      pushSettings({ min_confidence: Number(event.target.value) })
    );

    let assetTimer = null;
    el("asset-input").addEventListener("input", (event) => {
      clearTimeout(assetTimer);
      const value = event.target.value.trim();
      assetTimer = setTimeout(() => {
        if (value) pushSettings({ asset: value });
      }, 700);
    });

    el("refresh-journal").addEventListener("click", loadJournal);

    window.addEventListener("resize", drawChart);
  }

  // ---------------------------------------------------------------- boot

  wire();
  loadOptions().catch(() => showBanner("Could not load settings from the engine.", true));
  loadJournal();
  connect();
  setInterval(tickCountdown, 500);
  setInterval(loadJournal, 30000);
})();
