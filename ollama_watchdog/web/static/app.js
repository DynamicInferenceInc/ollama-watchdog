"use strict";

const REFRESH_INTERVAL_MS = 30_000;
const UNKNOWN_MODEL = "не определена";
const OTHER_MODELS = "другие";
const MAX_CHART_MODELS = 8;
const RECENT_LIMIT = 100;

/* ============================== Утилиты ============================== */

function escapeHtml(value) {
  const entities = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  return String(value ?? "").replace(/[&<>"']/g, (ch) => entities[ch]);
}

const numberFormat = new Intl.NumberFormat("ru-RU");
const compactFormat = new Intl.NumberFormat("ru-RU", { notation: "compact", maximumFractionDigits: 1 });
const percentFormat = new Intl.NumberFormat("ru-RU", { style: "percent", maximumFractionDigits: 1 });

/** Даты от API уже приведены к часовому поясу дашборда: "2026-10-15T12:37:57+03:00". */
const Format = {
  number: (value) => numberFormat.format(value ?? 0),
  compact: (value) => (value < 10_000 ? numberFormat.format(value ?? 0) : compactFormat.format(value)),
  percent: (part, total) => (total ? percentFormat.format(part / total) : "—"),

  duration(seconds) {
    if (seconds == null) return "—";
    if (seconds < 0.001) return "<1 мс";
    if (seconds < 1) return `${Math.round(seconds * 1000)} мс`;
    if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 2 : 1).replace(".", ",")} с`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes} мин ${Math.round(seconds % 60)} с`;
    return `${Math.floor(minutes / 60)} ч ${minutes % 60} мин`;
  },

  bytes(value) {
    const units = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
    let size = value ?? 0;
    let unit = 0;
    while (size >= 1024 && unit < units.length - 1) {
      size /= 1024;
      unit += 1;
    }
    return `${size.toFixed(unit >= 3 ? 1 : 0).replace(".", ",")} ${units[unit]}`;
  },

  time: (iso) => (iso ? iso.slice(11, 19) : "—"),
  hourMinute: (iso) => iso.slice(11, 16),
  dayMonth: (iso) => `${iso.slice(8, 10)}.${iso.slice(5, 7)}`,
  fullDate: (iso) => `${iso.slice(8, 10)}.${iso.slice(5, 7)}.${iso.slice(0, 4)}`,
  dateTime: (iso) => (iso ? `${iso.slice(8, 10)}.${iso.slice(5, 7)} ${iso.slice(11, 16)}` : "—"),

  relative(iso) {
    if (!iso) return "—";
    const seconds = Math.round((Date.now() - Date.parse(iso)) / 1000);
    if (seconds < 60) return "только что";
    if (seconds < 3600) return `${Math.floor(seconds / 60)} мин назад`;
    if (seconds < 86_400) return `${Math.floor(seconds / 3600)} ч назад`;
    return `${Math.floor(seconds / 86_400)} дн назад`;
  },

  plural(count, one, few, many) {
    const mod10 = count % 10;
    const mod100 = count % 100;
    if (mod10 === 1 && mod100 !== 11) return one;
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
    return many;
  },
};

function niceStep(rough) {
  if (rough <= 1) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(rough));
  for (const factor of [1, 2, 2.5, 5, 10]) {
    if (factor * magnitude >= rough) return factor * magnitude;
  }
  return 10 * magnitude;
}

function svgElement(width, height, content) {
  return `<svg viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" role="img">${content}</svg>`;
}

function emptyState(text) {
  return `<div class="empty">${escapeHtml(text)}</div>`;
}

/* ============================== Инфраструктура ============================== */

class ApiClient {
  async report(query) {
    return this.#get(`api/report?${new URLSearchParams(query)}`);
  }

  async status() {
    return this.#get("api/status");
  }

  async aliases() {
    return this.#get("api/aliases");
  }

  async saveAlias(ip, name) {
    return this.#request("api/aliases", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ip, name }),
    });
  }

  async #get(url) {
    return this.#request(url, {});
  }

  async #request(url, options) {
    const response = await fetch(url, { ...options, headers: { Accept: "application/json", ...options.headers } });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    return payload;
  }
}

/** Понятные имена клиентов вместо IP; сам IP виден в подсказке над именем. */
class ClientNames {
  constructor() {
    this.aliases = {};
    this.fixed = new Set();
  }

  update({ aliases, fixed }) {
    this.aliases = aliases;
    this.fixed = new Set(fixed);
  }

  name(ip) {
    return this.aliases[ip] ?? ip;
  }

  alias(ip) {
    return this.aliases[ip] ?? "";
  }

  isFixed(ip) {
    return this.fixed.has(ip);
  }

  label(ip) {
    const alias = this.aliases[ip];
    if (!alias) return `<span class="mono">${escapeHtml(ip)}</span>`;
    return `<span class="client-alias" data-client-ip="${escapeHtml(ip)}">${escapeHtml(alias)}</span>`;
  }
}

/** Закрепляет за каждой моделью свой цвет, чтобы он не «прыгал» между обновлениями и периодами. */
class ColorRegistry {
  static PALETTE = [
    "#7c5cff", "#22d3ee", "#34d399", "#fbbf24", "#f472b6", "#60a5fa",
    "#a3e635", "#fb923c", "#c084fc", "#2dd4bf", "#f87171", "#e879f9",
  ];
  static STORAGE_KEY = "ollama-watchdog:model-colors";

  constructor() {
    try {
      this.assigned = new Map(JSON.parse(localStorage.getItem(ColorRegistry.STORAGE_KEY) || "[]"));
    } catch {
      this.assigned = new Map();
    }
  }

  colorFor(model) {
    if (model === UNKNOWN_MODEL) return "#4b5168";
    if (model === OTHER_MODELS) return "#6b7280";
    if (!this.assigned.has(model)) {
      this.assigned.set(model, ColorRegistry.PALETTE[this.assigned.size % ColorRegistry.PALETTE.length]);
      localStorage.setItem(ColorRegistry.STORAGE_KEY, JSON.stringify([...this.assigned]));
    }
    return this.assigned.get(model);
  }

  swatch(model) {
    return `<span class="swatch" style="background:${this.colorFor(model)}"></span>`;
  }
}

class Tooltip {
  constructor(element) {
    this.element = element;
  }

  show(content, event) {
    this.element.innerHTML = content;
    this.element.hidden = false;
    this.move(event);
  }

  move(event) {
    const { innerWidth, innerHeight } = window;
    const rect = this.element.getBoundingClientRect();
    let left = event.clientX + 16;
    let top = event.clientY + 16;
    if (left + rect.width > innerWidth - 8) left = event.clientX - rect.width - 16;
    if (top + rect.height > innerHeight - 8) top = event.clientY - rect.height - 16;
    this.element.style.left = `${Math.max(8, left)}px`;
    this.element.style.top = `${Math.max(8, top)}px`;
  }

  /** Над элементом по центру, а если сверху не помещается — под ним. */
  showAbove(content, target) {
    this.element.innerHTML = content;
    this.element.hidden = false;
    const anchor = target.getBoundingClientRect();
    const rect = this.element.getBoundingClientRect();
    const left = Math.min(Math.max(8, anchor.left + anchor.width / 2 - rect.width / 2), window.innerWidth - rect.width - 8);
    const top = anchor.top - rect.height - 8 >= 8 ? anchor.top - rect.height - 8 : anchor.bottom + 8;
    this.element.style.left = `${left}px`;
    this.element.style.top = `${top}px`;
  }

  hide() {
    this.element.hidden = true;
  }
}

/** Набор серий для графиков: топ моделей + «другие» + «не определена». */
class ModelSeries {
  constructor(models, limit) {
    const known = models.filter((m) => m.model !== UNKNOWN_MODEL);
    this.top = known.slice(0, limit).map((m) => m.model);
    this.hasOther = known.length > limit;
    this.hasUnknown = models.some((m) => m.model === UNKNOWN_MODEL);
    this.names = [...this.top];
    if (this.hasOther) this.names.push(OTHER_MODELS);
    if (this.hasUnknown) this.names.push(UNKNOWN_MODEL);
  }

  /** Распределяет словарь {модель: число} по сериям. */
  split(byModel) {
    const values = Object.fromEntries(this.names.map((name) => [name, 0]));
    for (const [model, count] of Object.entries(byModel)) {
      if (model in values) values[model] += count;
      else values[OTHER_MODELS] = (values[OTHER_MODELS] ?? 0) + count;
    }
    return values;
  }

  /** Модель в фокусе может прятаться внутри «других». */
  focusedSeries(focus) {
    if (!focus) return null;
    return this.names.includes(focus) ? focus : OTHER_MODELS;
  }
}

/* ============================== Графики ============================== */

class TimelineChart {
  static HEIGHT = 280;
  static PADDING = { top: 16, right: 8, bottom: 30, left: 46 };

  constructor({ container, legend, hint, colors, tooltip, onFocus }) {
    Object.assign(this, { container, legend, hint, colors, tooltip, onFocus });
    this.container.addEventListener("mousemove", (event) => this.#onHover(event));
    this.container.addEventListener("mouseleave", () => this.tooltip.hide());
    this.legend.addEventListener("click", (event) => {
      const item = event.target.closest("[data-model]");
      if (item) this.onFocus(item.dataset.model);
    });
  }

  render(report, focus) {
    this.report = report;
    this.series = new ModelSeries(report.models, MAX_CHART_MODELS);
    const buckets = report.timeline;
    const total = buckets.reduce((sum, b) => sum + b.requests, 0);
    const stepName = report.timeline_step === "day" ? "по дням" : "по часам";
    this.hint.textContent = `${Format.number(total)} ${Format.plural(total, "запрос", "запроса", "запросов")} ${stepName}`;

    this.#renderLegend(focus);
    if (!total) {
      this.container.innerHTML = emptyState("За выбранный период запросов к моделям нет");
      return;
    }
    this.container.innerHTML = this.#svg(buckets, focus);
  }

  #renderLegend(focus) {
    const focused = this.series.focusedSeries(focus);
    this.legend.innerHTML = this.series.names
      .map((name) => {
        const state = focused ? (name === focused ? "is-focused" : "is-dimmed") : "";
        return `<button class="legend__item ${state}" data-model="${escapeHtml(name)}">
          ${this.colors.swatch(name)}${escapeHtml(name)}</button>`;
      })
      .join("");
  }

  #svg(buckets, focus) {
    const { top, right, bottom, left } = TimelineChart.PADDING;
    const width = Math.max(320, this.container.clientWidth);
    const height = TimelineChart.HEIGHT;
    const innerWidth = width - left - right;
    const innerHeight = height - top - bottom;

    const step = niceStep(Math.max(...buckets.map((b) => b.requests)) / 4);
    const maxValue = step * 4;
    const y = (value) => top + innerHeight - (value / maxValue) * innerHeight;
    const slot = innerWidth / buckets.length;
    const barWidth = Math.max(1, slot * (buckets.length > 60 ? 0.8 : 0.66));
    const focused = this.series.focusedSeries(focus);

    const parts = [];
    for (let i = 0; i <= 4; i += 1) {
      const value = step * i;
      parts.push(`<line class="grid-line" x1="${left}" x2="${width - right}" y1="${y(value)}" y2="${y(value)}"/>`);
      parts.push(`<g class="axis"><text x="${left - 8}" y="${y(value) + 4}" text-anchor="end">${Format.compact(value)}</text></g>`);
    }

    buckets.forEach((bucket, index) => {
      const x = left + index * slot + (slot - barWidth) / 2;
      let stacked = 0;
      const values = this.series.split(bucket.by_model);
      for (const name of this.series.names) {
        const value = values[name];
        if (!value) continue;
        const y1 = y(stacked + value);
        const segmentHeight = Math.max(0.5, y(stacked) - y1);
        const dimmed = focused && name !== focused ? "is-dimmed" : "";
        parts.push(`<rect class="bar-segment ${dimmed}" x="${x}" y="${y1}" width="${barWidth}" height="${segmentHeight}"
          rx="${barWidth > 6 ? 2 : 0}" fill="${this.colors.colorFor(name)}"/>`);
        stacked += value;
      }
      if (bucket.errors) {
        parts.push(`<circle class="error-mark" cx="${x + barWidth / 2}" cy="${y(stacked) - 7}" r="3"/>`);
      }
      parts.push(`<rect class="hover-column" data-index="${index}" x="${left + index * slot}" y="${top}" width="${slot}" height="${innerHeight}"/>`);
    });

    const maxLabels = Math.max(2, Math.floor(innerWidth / 64));
    const labelEvery = Math.ceil(buckets.length / maxLabels);
    const multiDay = buckets.length > 24;
    buckets.forEach((bucket, index) => {
      if (index % labelEvery !== 0) return;
      const label = this.#axisLabel(bucket.start, multiDay);
      const x = left + index * slot + slot / 2;
      parts.push(`<g class="axis"><text x="${x}" y="${height - 8}" text-anchor="middle">${label}</text></g>`);
    });

    return svgElement(width, height, parts.join(""));
  }

  #axisLabel(iso, multiDay) {
    if (this.report.timeline_step === "day") return Format.dayMonth(iso);
    const hour = Format.hourMinute(iso);
    return multiDay && hour === "00:00" ? Format.dayMonth(iso) : multiDay ? `${Format.dayMonth(iso)} ${hour.slice(0, 2)}ч` : hour;
  }

  #onHover(event) {
    const column = event.target.closest("[data-index]");
    if (!column) {
      this.tooltip.hide();
      return;
    }
    const bucket = this.report.timeline[Number(column.dataset.index)];
    const title = this.report.timeline_step === "day"
      ? Format.fullDate(bucket.start)
      : `${Format.dayMonth(bucket.start)}, ${Format.hourMinute(bucket.start)}–${String((Number(bucket.start.slice(11, 13)) + 1) % 24).padStart(2, "0")}:00`;
    const rows = Object.entries(bucket.by_model)
      .sort((a, b) => b[1] - a[1])
      .map(([model, count]) => `<div class="tooltip__row">${this.colors.swatch(model)}<span>${escapeHtml(model)}</span><strong>${Format.number(count)}</strong></div>`)
      .join("");
    const errors = bucket.errors ? `<span class="text-bad">ошибок: ${Format.number(bucket.errors)}</span>` : "";
    this.tooltip.show(
      `<div class="tooltip__title">${title}</div>${rows || '<div class="muted">запросов нет</div>'}
       <div class="tooltip__total"><span>Всего: <strong>${Format.number(bucket.requests)}</strong></span>${errors}</div>`,
      event,
    );
  }
}

class DonutChart {
  static RADIUS = 80;
  static STROKE = 26;

  constructor({ container, colors, onFocus }) {
    Object.assign(this, { container, colors, onFocus });
    this.container.addEventListener("click", (event) => {
      const item = event.target.closest("[data-model]");
      if (item) this.onFocus(item.dataset.model);
    });
  }

  render(report, focus) {
    const total = report.inference_requests;
    if (!total) {
      this.container.innerHTML = emptyState("Нет данных");
      return;
    }
    const series = new ModelSeries(report.models, 6);
    const byModel = Object.fromEntries(report.models.map((m) => [m.model, m.requests]));
    const values = series.split(byModel);
    const focused = series.focusedSeries(focus);
    const circumference = 2 * Math.PI * DonutChart.RADIUS;

    let offset = 0;
    const segments = series.names.map((name) => {
      const length = (values[name] / total) * circumference;
      const visible = Math.max(0, length - (series.names.length > 1 ? 2 : 0));
      const dimmed = focused && name !== focused ? "is-dimmed" : "";
      const segment = `<circle class="donut__segment ${dimmed}" data-model="${escapeHtml(name)}" r="${DonutChart.RADIUS}" cx="105" cy="105"
        fill="none" stroke="${this.colors.colorFor(name)}" stroke-width="${DonutChart.STROKE}"
        stroke-dasharray="${visible} ${circumference - visible}" stroke-dashoffset="${-offset}"
        transform="rotate(-90 105 105)"><title>${escapeHtml(name)}: ${Format.number(values[name])}</title></circle>`;
      offset += length;
      return segment;
    });

    const centerValue = focus ? (byModel[focus] ?? 0) : total;
    const centerLabel = focus ? Format.percent(centerValue, total) + " запросов" : "запросов";
    const list = series.names
      .map((name) => {
        const state = focused ? (name === focused ? "is-focused" : "is-dimmed") : "";
        return `<button class="donut__row ${state}" data-model="${escapeHtml(name)}">
          ${this.colors.swatch(name)}
          <span class="donut__name" title="${escapeHtml(name)}">${escapeHtml(name)}</span>
          <span class="donut__count">${Format.number(values[name])}</span>
          <span class="donut__share">${Format.percent(values[name], total)}</span>
        </button>`;
      })
      .join("");

    this.container.innerHTML = `<div class="donut">
      ${svgElement(210, 210, `${segments.join("")}
        <text class="donut__center-value" x="105" y="106" text-anchor="middle">${Format.compact(centerValue)}</text>
        <text class="donut__center-label" x="105" y="126" text-anchor="middle">${escapeHtml(centerLabel)}</text>`)}
      <div class="donut__list">${list}</div>
    </div>`;
  }
}

class HourlyProfileChart {
  static HEIGHT = 200;

  constructor({ container, tooltip }) {
    Object.assign(this, { container, tooltip });
    this.container.addEventListener("mousemove", (event) => {
      const bar = event.target.closest("[data-hour]");
      if (!bar) return this.tooltip.hide();
      const hour = Number(bar.dataset.hour);
      const count = this.hourly[hour];
      this.tooltip.show(
        `<div class="tooltip__title">${String(hour).padStart(2, "0")}:00–${String((hour + 1) % 24).padStart(2, "0")}:00</div>
         <div>${Format.number(count)} ${Format.plural(count, "запрос", "запроса", "запросов")} · ${Format.percent(count, this.total)}</div>`,
        event,
      );
    });
    this.container.addEventListener("mouseleave", () => this.tooltip.hide());
  }

  render(report) {
    this.hourly = report.hourly;
    this.total = this.hourly.reduce((a, b) => a + b, 0);
    if (!this.total) {
      this.container.innerHTML = emptyState("Нет данных");
      return;
    }
    const width = Math.max(300, this.container.clientWidth);
    const height = HourlyProfileChart.HEIGHT;
    const padding = { top: 12, bottom: 26, left: 6, right: 6 };
    const innerHeight = height - padding.top - padding.bottom;
    const slot = (width - padding.left - padding.right) / 24;
    const peak = Math.max(...this.hourly);

    const bars = this.hourly.map((count, hour) => {
      const barHeight = Math.max(count ? 2 : 0, (count / peak) * innerHeight);
      const x = padding.left + hour * slot;
      const color = count === peak ? "#7c5cff" : "rgba(124, 92, 255, 0.45)";
      const label = hour % 3 === 0
        ? `<g class="axis"><text x="${x + slot / 2}" y="${height - 8}" text-anchor="middle">${String(hour).padStart(2, "0")}</text></g>`
        : "";
      return `<rect x="${x + slot * 0.15}" y="${padding.top + innerHeight - barHeight}" width="${slot * 0.7}" height="${barHeight}" rx="3" fill="${color}"/>
        <rect class="hover-column" data-hour="${hour}" x="${x}" y="${padding.top}" width="${slot}" height="${innerHeight}"/>${label}`;
    });
    this.container.innerHTML = svgElement(width, height, bars.join(""));
  }
}

/* ============================== Таблицы и списки ============================== */

class Delta {
  /** direction: 1 — рост хорош, -1 — рост плох, 0 — нейтрально. */
  static badge(current, previous, direction) {
    if (previous == null || (current === 0 && previous === 0)) return "";
    if (previous === 0) return `<span class="delta delta--neutral">было 0</span>`;
    const change = (current - previous) / previous;
    const text = `${change > 0 ? "+" : ""}${percentFormat.format(change)}`;
    let tone = "neutral";
    if (Math.abs(change) >= 0.02 && direction !== 0) tone = change * direction > 0 ? "good" : "bad";
    return `<span class="delta delta--${tone}">${change > 0 ? "↑" : change < 0 ? "↓" : ""} ${text}</span>`;
  }
}

class KpiPanel {
  constructor({ container, caption, names }) {
    Object.assign(this, { container, caption, names });
  }

  render(report) {
    const previous = report.previous;
    this.caption.textContent = report.comparison_label
      ? `Стрелки ↑↓ — изменение ${report.comparison_label}`
      : "Сравнение с прошлым периодом недоступно: логов за него нет";
    const totalLoads = report.loads.reduce((sum, l) => sum + l.loads, 0);
    const errors = report.inference_errors;
    const topClient = report.clients.find((c) => c.inference_requests > 0);
    const cards = [
      {
        label: "Запросы к моделям",
        color: "#7c5cff",
        value: Format.compact(report.inference_requests),
        delta: Delta.badge(report.inference_requests, previous?.inference_requests, 0),
        sub: `служебных: ${Format.compact(report.service_requests)}`,
      },
      {
        label: "Ошибки",
        color: errors ? "#f87171" : "#34d399",
        value: `${Format.number(errors)}<small>${Format.percent(errors, report.inference_requests)}</small>`,
        delta: Delta.badge(errors, previous?.inference_errors, -1),
        sub: errors ? "ответы 4xx/5xx на запросы к моделям" : "все запросы успешны",
      },
      {
        label: "Медиана ответа",
        color: "#22d3ee",
        value: Format.duration(report.latency.median),
        delta: Delta.badge(report.latency.median, previous?.median_latency, -1),
        sub: `p95 ${Format.duration(report.latency.p95)} · макс ${Format.duration(report.latency.maximum)}`,
      },
      {
        label: "Сгенерировано токенов",
        color: "#fbbf24",
        value: Format.compact(report.generation.generated_tokens),
        delta: Delta.badge(report.generation.generated_tokens, previous?.generated_tokens, 0),
        sub: report.generation.tasks
          ? `${report.generation.average_generation_speed.toFixed(1).replace(".", ",")} ток/с · на входе ${Format.compact(report.generation.prompt_tokens)}`
          : "генераций не было",
      },
      {
        label: "Загрузки моделей",
        color: "#f472b6",
        value: Format.number(totalLoads),
        delta: Delta.badge(totalLoads, previous?.model_loads, -1),
        sub: report.server_starts.length ? `перезапусков ollama: ${report.server_starts.length}` : "без перезапусков ollama",
      },
      {
        label: "Активные клиенты",
        color: "#34d399",
        value: Format.number(report.active_clients),
        delta: Delta.badge(report.active_clients, previous?.active_clients, 0),
        sub: topClient ? `чаще всех: ${this.names.label(topClient.client_ip)}` : "—",
      },
    ];
    this.container.innerHTML = cards
      .map((card) => `<article class="card kpi" style="--kpi-color:${card.color}">
        <div class="kpi__label">${card.label}</div>
        <div class="kpi__value">${card.value}</div>
        <div class="kpi__sub">${card.delta || "&nbsp;"}</div>
        <div class="kpi__sub">${card.sub}</div>
      </article>`)
      .join("");
  }
}

class ModelsTable {
  constructor({ container, coverage, colors, onFocus }) {
    Object.assign(this, { container, coverage, colors, onFocus });
    this.container.addEventListener("click", (event) => {
      const row = event.target.closest("[data-model]");
      if (row) this.onFocus(row.dataset.model);
    });
  }

  render(report, focus) {
    this.#renderCoverage(report);
    if (!report.models.length) {
      this.container.innerHTML = emptyState("За выбранный период запросов к моделям нет");
      return;
    }
    const maxRequests = Math.max(...report.models.map((m) => m.requests));
    const rows = report.models.map((usage) => {
      const state = focus ? (usage.model === focus ? "is-focused" : "is-dimmed") : "";
      const errorRate = usage.errors / usage.requests;
      const endpoints = Object.entries(usage.endpoints)
        .map(([path, count]) => `<span class="chip">${escapeHtml(path)} ${Format.compact(count)}</span>`)
        .join("");
      return `<tr class="is-clickable ${state}" data-model="${escapeHtml(usage.model)}">
        <td><div class="model-name model-name--wide">${this.colors.swatch(usage.model)}
          <span class="model-name__text" title="${escapeHtml(usage.model)}">${escapeHtml(usage.model)}</span>
          ${this.#attributionBadge(usage)}</div></td>
        <td><div class="share">
          <span class="num"><strong>${Format.number(usage.requests)}</strong></span>
          <div class="share__bar"><div class="share__fill" style="width:${(usage.requests / maxRequests) * 100}%;background:${this.colors.colorFor(usage.model)}"></div></div>
          <span class="share__value">${Format.percent(usage.requests, report.inference_requests)}</span>
        </div></td>
        <td class="num ${usage.errors ? (errorRate > 0.05 ? "text-bad" : "text-warn") : "faint"}">
          ${Format.number(usage.errors)}${usage.errors ? ` <span class="faint">(${Format.percent(usage.errors, usage.requests)})</span>` : ""}</td>
        <td class="num">${usage.clients}</td>
        <td class="num">${Format.duration(usage.latency.median)}</td>
        <td class="num">${Format.duration(usage.latency.p95)}</td>
        <td class="num">${Format.duration(usage.latency.maximum)}</td>
        <td class="num muted" title="${escapeHtml(usage.last_request)}">${Format.relative(usage.last_request)}</td>
        <td><div class="chips">${endpoints}</div></td>
      </tr>`;
    });
    this.container.innerHTML = `<table>
      <thead><tr><th>Модель</th><th>Запросы</th><th class="num">Ошибки</th><th class="num">Клиенты</th>
        <th class="num">Медиана</th><th class="num">p95</th><th class="num">Макс</th><th class="num">Последний</th><th>Эндпоинты</th></tr></thead>
      <tbody>${rows.join("")}</tbody></table>`;
  }

  #attributionBadge(usage) {
    if (usage.model === UNKNOWN_MODEL) {
      return `<span class="badge badge--muted" title="Из обычного лога ollama нельзя понять, к какой модели был запрос">?</span>`;
    }
    if (usage.exact === usage.requests) return `<span class="badge badge--good" title="Модель определена точно">точно</span>`;
    return `<span class="badge badge--info" title="Модель определена по косвенным признакам">≈ оценка</span>`;
  }

  #renderCoverage(report) {
    const { exact, approximate, unknown } = report.coverage;
    const total = report.inference_requests;
    if (!total) {
      this.coverage.innerHTML = "";
      return;
    }
    this.coverage.innerHTML = `<div class="chips" title="Насколько точно запросы привязаны к моделям">
      <span class="badge badge--good">точно ${Format.percent(exact, total)}</span>
      <span class="badge badge--info">≈ ${Format.percent(approximate, total)}</span>
      <span class="badge badge--muted">? ${Format.percent(unknown, total)}</span></div>`;
  }
}

class LiveModelsPanel {
  constructor({ container, hint, colors }) {
    Object.assign(this, { container, hint, colors });
  }

  render(status) {
    const ollama = status?.ollama;
    if (!ollama) {
      this.hint.textContent = "";
      this.container.innerHTML = emptyState("Live-статус выключен: задайте OLLAMA_URL, чтобы видеть модели в памяти");
      return;
    }
    if (!ollama.reachable) {
      this.hint.textContent = status.ollama_url;
      this.container.innerHTML = `<div class="notice notice--bad">ollama недоступна по адресу <code>${escapeHtml(status.ollama_url)}</code>: ${escapeHtml(ollama.error)}</div>`;
      return;
    }
    const models = ollama.loaded_models;
    const totalVram = models.reduce((sum, m) => sum + m.vram_bytes, 0);
    this.hint.textContent = `Данные /api/ps · обновлено ${Format.time(ollama.checked_at)}`;
    const meta = `<div class="live-meta">
      <span class="pill"><span class="dot dot--good"></span>ollama <strong>${escapeHtml(ollama.version ?? "?")}</strong></span>
      <span class="pill">загружено <strong>${models.length}</strong></span>
      <span class="pill">VRAM <strong>${Format.bytes(totalVram)}</strong></span>
      <span class="pill">установлено моделей <strong>${ollama.installed_models ?? "?"}</strong></span></div>`;
    if (!models.length) {
      this.container.innerHTML = meta + emptyState("Сейчас ни одна модель не загружена в память");
      return;
    }
    const items = models.map((model) => {
      const onGpu = model.size_bytes ? model.vram_bytes / model.size_bytes : 0;
      const details = [
        model.parameter_size && `${escapeHtml(model.parameter_size)} параметров`,
        model.quantization && escapeHtml(model.quantization),
        model.context_length && `контекст ${Format.number(model.context_length)}`,
        `на GPU ${Format.percent(model.vram_bytes, model.size_bytes)}`,
        model.expires_at ? `выгрузится ${Format.dateTime(model.expires_at)}` : "держится бессрочно",
      ].filter(Boolean);
      return `<div class="live-model">
        <div class="live-model__name">${this.colors.swatch(model.name)}<span title="${escapeHtml(model.name)}">${escapeHtml(model.name)}</span></div>
        <div class="live-model__size">${Format.bytes(model.size_bytes)}</div>
        <div class="vram" title="Доля модели в видеопамяти"><div class="vram__fill" style="width:${onGpu * 100}%"></div></div>
        <div class="live-model__meta">${details.map((d) => `<span>${d}</span>`).join("")}</div>
      </div>`;
    });
    this.container.innerHTML = `${meta}<div class="live-list">${items.join("")}</div>`;
  }
}

class SimpleTable {
  static render(container, headers, rows, emptyText) {
    if (!rows.length) {
      container.innerHTML = emptyState(emptyText);
      return;
    }
    const head = headers.map(([title, numeric]) => `<th class="${numeric ? "num" : ""}">${title}</th>`).join("");
    container.innerHTML = `<table><thead><tr>${head}</tr></thead><tbody>${rows.join("")}</tbody></table>`;
  }
}

class LoadsTable {
  constructor({ container, colors }) {
    Object.assign(this, { container, colors });
  }

  render(report) {
    const rows = report.loads.map((load) => `<tr>
      <td><div class="model-name">${this.colors.swatch(load.model)}<span class="model-name__text">${escapeHtml(load.model)}</span></div></td>
      <td class="num"><strong>${Format.number(load.loads)}</strong></td>
      <td class="num">${load.average_startup_seconds != null ? Format.duration(load.average_startup_seconds) : "—"}</td>
      <td class="num muted" title="${escapeHtml(load.last_loaded)}">${Format.relative(load.last_loaded)}</td>
    </tr>`);
    SimpleTable.render(this.container, [["Модель"], ["Загрузок", true], ["Старт в среднем", true], ["Последняя", true]], rows,
      "За период модели не загружались");
  }
}

class ClientsTable {
  constructor({ container, colors, names, onRename }) {
    Object.assign(this, { container, colors, names, onRename });
    this.editing = null;
    this.container.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-edit-ip]");
      if (button) this.#startEditing(button.dataset.editIp);
    });
  }

  render(report) {
    this.report = report;
    if (this.editing) return;
    const rows = report.clients.slice(0, 15).map((client) => `<tr>
      <td><div class="client-cell" data-ip="${escapeHtml(client.client_ip)}">${this.names.label(client.client_ip)}${this.#editButton(client.client_ip)}</div>
        <div class="faint">служебных: ${Format.compact(client.service_requests)}</div></td>
      <td class="num"><strong>${Format.number(client.inference_requests)}</strong></td>
      <td class="num ${client.errors ? "text-bad" : "faint"}">${Format.number(client.errors)}</td>
      <td>${client.favourite_model
        ? `<div class="model-name">${this.colors.swatch(client.favourite_model)}<span class="model-name__text model-name__text--short" title="${escapeHtml(client.favourite_model)}">${escapeHtml(client.favourite_model)}</span></div>`
        : '<span class="faint">—</span>'}</td>
      <td class="num muted" title="${escapeHtml(client.last_seen)}">${Format.relative(client.last_seen)}</td>
    </tr>`);
    SimpleTable.render(this.container,
      [["Клиент"], ["К моделям", true], ["Ошибок", true], ["Чаще всего"], ["Был", true]], rows, "Клиентов нет");
  }

  #editButton(ip) {
    if (this.names.isFixed(ip)) return "";
    const title = this.names.alias(ip) ? "Изменить имя" : "Задать имя";
    return `<button class="client-edit" data-edit-ip="${escapeHtml(ip)}" title="${title}" aria-label="${title}">
      <svg viewBox="0 0 24 24"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg></button>`;
  }

  #startEditing(ip) {
    const cell = [...this.container.querySelectorAll(".client-cell")].find((element) => element.dataset.ip === ip);
    if (!cell) return;
    this.editing = ip;
    cell.innerHTML = `<input class="client-input" maxlength="64" placeholder="${escapeHtml(ip)}" aria-label="Имя для ${escapeHtml(ip)}">`;
    const input = cell.querySelector("input");
    input.value = this.names.alias(ip);
    input.focus();
    input.select();

    let done = false;
    const finish = async (save) => {
      if (done) return;
      done = true;
      if (save && input.value.trim() !== this.names.alias(ip)) {
        try {
          await this.onRename(ip, input.value);
        } catch (error) {
          if (document.activeElement === input) {
            done = false;
            input.classList.add("is-invalid");
            input.title = error.message;
            return;
          }
        }
      }
      this.editing = null;
      this.render(this.report);
    };
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") finish(true);
      if (event.key === "Escape") {
        event.stopPropagation();
        finish(false);
      }
    });
    input.addEventListener("blur", () => finish(true));
  }
}

class EndpointsTable {
  constructor(container) {
    this.container = container;
  }

  render(report) {
    const rows = report.endpoints.map((endpoint) => `<tr class="${endpoint.is_inference ? "" : "is-dimmed"}">
      <td><span class="chip">${escapeHtml(endpoint.endpoint)}</span></td>
      <td>${endpoint.is_inference ? '<span class="badge badge--info">модель</span>' : '<span class="badge badge--muted">служебный</span>'}</td>
      <td class="num"><strong>${Format.number(endpoint.requests)}</strong></td>
      <td class="num ${endpoint.errors ? "text-bad" : "faint"}">${Format.number(endpoint.errors)}</td>
      <td class="num">${Format.duration(endpoint.latency.median)}</td>
      <td class="num">${Format.duration(endpoint.latency.p95)}</td>
    </tr>`);
    SimpleTable.render(this.container,
      [["Эндпоинт"], ["Тип"], ["Запросов", true], ["Ошибок", true], ["Медиана", true], ["p95", true]], rows, "Запросов нет");
  }
}

class RecentRequestsTable {
  constructor({ container, hint, colors, names }) {
    Object.assign(this, { container, hint, colors, names });
  }

  render(report, focus) {
    const requests = report.recent_requests.filter((r) => !focus || r.model === focus);
    this.hint.textContent = focus
      ? `Только «${focus}» среди последних ${RECENT_LIMIT} запросов к моделям`
      : `Последние ${RECENT_LIMIT} запросов к моделям за период`;
    const rows = requests.map((request) => {
      const statusTone = request.status >= 500 ? "bad" : request.status >= 400 ? "warn" : "good";
      return `<tr>
        <td class="mono nowrap" title="${escapeHtml(request.timestamp)}">${Format.dateTime(request.timestamp)}:${request.timestamp.slice(17, 19)}</td>
        <td><span class="badge badge--${statusTone}">${request.status}</span></td>
        <td><div class="model-name">${this.colors.swatch(request.model)}<span class="model-name__text">${escapeHtml(request.model)}</span></div></td>
        <td><span class="chip">${escapeHtml(request.endpoint)}</span></td>
        <td class="muted nowrap">${this.names.label(request.client_ip)}</td>
        <td class="num">${Format.duration(request.duration)}</td>
      </tr>`;
    });
    SimpleTable.render(this.container, [["Время"], ["Код"], ["Модель"], ["Эндпоинт"], ["Клиент"], ["Длительность", true]], rows,
      focus ? "Среди последних запросов этой модели нет" : "Запросов нет");
  }
}

class ProblemsPanel {
  constructor({ container, names }) {
    Object.assign(this, { container, names });
  }

  render(report) {
    const failed = [...report.failed_requests].reverse().slice(0, 30);
    const incidents = this.#groupIncidents(report.incidents);
    if (!failed.length && !incidents.length) {
      this.container.innerHTML = emptyState("Проблем не обнаружено 🎉");
      return;
    }
    const failedHtml = failed.map((f) => `<div class="problem ${f.status >= 500 ? "problem--bad" : ""}">
      <div class="problem__head"><span>${Format.dateTime(f.timestamp)} · ${this.names.label(f.client_ip)}</span><span>${Format.duration(f.duration)}</span></div>
      <div class="problem__text"><strong>${f.status}</strong> ${escapeHtml(f.endpoint)} → ${escapeHtml(f.model)}</div></div>`).join("");
    const incidentsHtml = incidents.map((i) => `<div class="problem ${i.level === "ERROR" ? "problem--bad" : ""}">
      <div class="problem__head"><span>${i.level} · ×${i.count}</span><span>последний раз ${Format.dateTime(i.last)}</span></div>
      <div class="problem__text" title="${escapeHtml(i.details)}">${escapeHtml(i.message)}</div></div>`).join("");
    this.container.innerHTML = `
      ${failed.length ? `<div class="problems-section"><h3>Неудачные запросы · ${report.failed_requests.length}</h3><div class="problem-list">${failedHtml}</div></div>` : ""}
      ${incidents.length ? `<div class="problems-section"><h3>Сообщения сервера</h3><div class="problem-list">${incidentsHtml}</div></div>` : ""}`;
  }

  #groupIncidents(incidents) {
    const groups = new Map();
    for (const incident of incidents) {
      const key = `${incident.level}|${incident.message}`;
      const group = groups.get(key) ?? { level: incident.level, message: incident.message, count: 0 };
      group.count += 1;
      group.last = incident.timestamp;
      group.details = incident.details;
      groups.set(key, group);
    }
    return [...groups.values()].sort((a, b) => (a.level === b.level ? b.count - a.count : a.level === "ERROR" ? -1 : 1));
  }
}

/* ============================== Наблюдения ============================== */

/** Превращает цифры отчёта в короткие выводы: что важно заметить. */
class InsightsBuilder {
  constructor(names) {
    this.names = names;
  }

  build(report) {
    if (!report.inference_requests) {
      return [{ level: "info", title: "Запросов к моделям нет", text: "За выбранный период ollama не получала запросов на генерацию или эмбеддинги." }];
    }
    const insights = [
      ...this.#errors(report),
      ...this.#restarts(report),
      ...this.#serverErrors(report),
      ...this.#reloads(report),
      ...this.#slowModels(report),
      ...this.#trend(report),
      ...this.#busiest(report),
      ...this.#peakHour(report),
      ...this.#serviceTraffic(report),
    ];
    const problems = insights.filter((i) => i.level === "bad" || i.level === "warn");
    if (!problems.length) {
      insights.unshift({ level: "good", title: "Всё стабильно", text: "Ошибок, перезапусков и частых перезагрузок моделей не обнаружено." });
    }
    return insights.slice(0, 8);
  }

  #busiest(report) {
    const top = report.models.find((m) => m.model !== UNKNOWN_MODEL);
    if (!top) return [];
    return [{
      level: "info",
      title: `Основная нагрузка — ${top.model}`,
      text: `${Format.number(top.requests)} ${Format.plural(top.requests, "запрос", "запроса", "запросов")} (${Format.percent(top.requests, report.inference_requests)} всех), медиана ответа ${Format.duration(top.latency.median)}.`,
    }];
  }

  #peakHour(report) {
    const peak = Math.max(...report.hourly);
    const hour = report.hourly.indexOf(peak);
    const quiet = report.hourly.filter((count) => count === 0).length;
    const pad = (h) => String(h).padStart(2, "0");
    return [{
      level: "info",
      title: `Пик нагрузки — ${pad(hour)}:00–${pad((hour + 1) % 24)}:00`,
      text: `${Format.number(peak)} ${Format.plural(peak, "запрос", "запроса", "запросов")} в этот час` +
        (quiet ? `; ${quiet} ${Format.plural(quiet, "час", "часа", "часов")} суток без запросов.` : "."),
    }];
  }

  #errors(report) {
    const result = [];
    const rate = report.inference_errors / report.inference_requests;
    if (rate >= 0.02) {
      result.push({
        level: "bad",
        title: `Высокая доля ошибок: ${Format.percent(report.inference_errors, report.inference_requests)}`,
        text: `${Format.number(report.inference_errors)} неудачных запросов к моделям. Подробности — в блоке «Проблемы».`,
      });
    }
    report.models
      .filter((m) => m.errors >= 2 && m.errors / m.requests >= 0.05 && m.model !== UNKNOWN_MODEL)
      .slice(0, 2)
      .forEach((m) => result.push({
        level: "bad",
        title: `${m.model}: ${Format.percent(m.errors, m.requests)} запросов с ошибкой`,
        text: `${m.errors} из ${m.requests}. Проверьте сообщения сервера — возможны падения llama-server или нехватка памяти.`,
      }));
    return result;
  }

  #restarts(report) {
    const count = report.server_starts.length;
    if (!count) return [];
    return [{
      level: "warn",
      title: `ollama ${count === 1 ? "перезапускалась" : `перезапускалась ${count} ${Format.plural(count, "раз", "раза", "раз")}`}`,
      text: `Время запуска: ${report.server_starts.map(Format.dateTime).join(", ")}. После перезапуска модели загружаются заново.`,
    }];
  }

  #serverErrors(report) {
    const crashes = report.incidents.filter((i) => i.level === "ERROR");
    if (!crashes.length) return [];
    const messages = [...new Set(crashes.map((i) => i.message))].slice(0, 2).join("; ");
    return [{
      level: "bad",
      title: `Ошибки сервера: ${crashes.length}`,
      text: `${messages}. Последняя — ${Format.dateTime(crashes[crashes.length - 1].timestamp)}.`,
    }];
  }

  #reloads(report) {
    const days = Math.max(1, (Date.parse(report.period.end) - Date.parse(report.period.start)) / 86_400_000);
    const frequent = report.loads.filter((l) => l.loads / days >= 5);
    if (!frequent.length) return [];
    const list = frequent.slice(0, 3).map((l) => `${l.model} — ${l.loads}`).join(", ");
    return [{
      level: "warn",
      title: "Модели часто перезагружаются в память",
      text: `${list}. Скорее всего, модели вытесняют друг друга: проверьте OLLAMA_MAX_LOADED_MODELS, ` +
        "размер контекста и не запрашивают ли клиенты одну модель с разными параметрами (num_ctx).",
    }];
  }

  #slowModels(report) {
    return report.models
      .filter((m) => m.model !== UNKNOWN_MODEL && m.requests >= 3 && m.latency.p95 >= 60)
      .slice(0, 2)
      .map((m) => ({
        level: "info",
        title: `Долгие ответы у ${m.model}`,
        text: `p95 — ${Format.duration(m.latency.p95)}, максимум — ${Format.duration(m.latency.maximum)}. Обычно это длинные генерации или ожидание в очереди.`,
      }));
  }

  #trend(report) {
    const previous = report.previous?.inference_requests;
    if (!previous || previous < 20 || !report.comparison_label) return [];
    const change = (report.inference_requests - previous) / previous;
    if (Math.abs(change) < 0.5) return [];
    return [{
      level: "info",
      title: `Нагрузка ${change > 0 ? "выросла" : "упала"} на ${percentFormat.format(Math.abs(change))}`,
      text: `${Format.number(report.inference_requests)} запросов против ${Format.number(previous)} (${report.comparison_label}).`,
    }];
  }

  #serviceTraffic(report) {
    if (report.service_requests < report.inference_requests * 3) return [];
    const poller = [...report.clients].sort((a, b) => b.service_requests - a.service_requests)[0];
    return [{
      level: "info",
      title: `${Format.percent(report.service_requests, report.total_requests)} трафика — служебные запросы`,
      text: `В основном опросы /api/ps и проверки доступности; больше всех — ${this.names.name(poller.client_ip)} (${Format.compact(poller.service_requests)}). На нагрузку на GPU это не влияет.`,
    }];
  }
}

class InsightsPanel {
  constructor({ container, names }) {
    this.container = container;
    this.builder = new InsightsBuilder(names);
  }

  render(report) {
    const icons = { good: "✓", warn: "!", bad: "×", info: "i" };
    this.container.innerHTML = `<div class="insights">${this.builder.build(report)
      .map((insight) => `<div class="insight insight--${insight.level}">
        <div class="insight__icon">${icons[insight.level]}</div>
        <div><div class="insight__title">${escapeHtml(insight.title)}</div>
        <div class="insight__text">${escapeHtml(insight.text)}</div></div></div>`)
      .join("")}</div>`;
  }
}

/* ============================== Шапка, уведомления, подвал ============================== */

class StatusBar {
  constructor({ pills, subtitle, footer, notices }) {
    Object.assign(this, { pills, subtitle, footer, notices });
  }

  render(report, status, error) {
    this.#renderPills(status);
    this.#renderNotices(report, status, error);
    if (report) {
      this.subtitle.textContent = `Статистика ${report.period.title} · ${report.timezone}`;
    }
    this.#renderFooter(report, status);
  }

  #renderPills(status) {
    if (!status) {
      this.pills.innerHTML = "";
      return;
    }
    const pills = [];
    const ollama = status.ollama;
    if (ollama) {
      pills.push(ollama.reachable
        ? `<span class="pill"><span class="dot dot--good"></span>ollama <strong>${escapeHtml(ollama.version ?? "")}</strong> онлайн</span>`
        : `<span class="pill" title="${escapeHtml(ollama.error)}"><span class="dot dot--bad"></span>ollama недоступна</span>`);
    }
    const collector = status.collector;
    const states = {
      streaming: ["good dot--pulse", "логи в реальном времени"],
      replaying: ["warn", "чтение архива…"],
      starting: ["warn", "подключение…"],
      reconnecting: ["bad", "логи: переподключение"],
      finished: ["good", "логи из файлов"],
    };
    const [tone, text] = states[collector.state] ?? ["warn", collector.state];
    pills.push(`<span class="pill" title="${escapeHtml(collector.last_error ?? collector.source)}"><span class="dot dot--${tone}"></span>${text}</span>`);
    this.pills.innerHTML = pills.join("");
  }

  #renderNotices(report, status, error) {
    const notices = [];
    if (error) {
      notices.push(`<div class="notice notice--bad">Не удалось получить данные: ${escapeHtml(error.message)}</div>`);
    }
    const collector = status?.collector;
    if (collector?.state === "reconnecting" && collector.last_error) {
      notices.push(`<div class="notice notice--bad">Не удаётся читать логи ollama: ${escapeHtml(collector.last_error)}.
        Проверьте <code>OLLAMA_CONTAINER</code> и доступ к Docker API.</div>`);
    }
    if (report && report.inference_requests && !report.debug_logging_detected && report.coverage.exact < report.inference_requests) {
      notices.push(`<div class="notice">В обычном логе ollama у запроса нет имени модели, поэтому часть запросов привязана к моделям
        приблизительно (≈) или не определена (?). Для точной статистики запустите контейнер ollama с <code>OLLAMA_DEBUG=1</code>.</div>`);
    }
    this.notices.innerHTML = notices.join("");
  }

  #renderFooter(report, status) {
    const items = [];
    if (status) {
      items.push(`Источник: ${escapeHtml(status.collector.source)}`);
      items.push(`Обработано строк: ${Format.number(status.collector.lines_processed)}`);
      if (status.collector.archived_lines_replayed) items.push(`из архива: ${Format.number(status.collector.archived_lines_replayed)}`);
      if (status.collector.last_log_at) items.push(`Последняя строка лога: ${Format.dateTime(status.collector.last_log_at)}`);
    }
    if (report?.first_event) items.push(`Данные: ${Format.dateTime(report.first_event)} — ${Format.dateTime(report.last_event)}`);
    if (report) items.push(`Обновлено: ${Format.time(report.generated_at)}`);
    this.footer.innerHTML = items.map((item) => `<span>${item}</span>`).join("");
  }
}

/* ============================== Дашборд ============================== */

class PeriodControls {
  constructor({ buttons, dateInput, onChange }) {
    Object.assign(this, { buttons, dateInput, onChange });
    this.buttons.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-period]");
      if (!button) return;
      const query = { period: button.dataset.period };
      if (button.dataset.hours) query.hours = button.dataset.hours;
      this.onChange(query);
    });
    this.dateInput.addEventListener("change", () => {
      if (this.dateInput.value) this.onChange({ period: "day", date: this.dateInput.value });
    });
  }

  render(query, today) {
    for (const button of this.buttons.querySelectorAll("button")) {
      const active = button.dataset.period === query.period && (button.dataset.hours ?? null) === (query.hours ?? null);
      button.classList.toggle("is-active", active);
    }
    this.dateInput.max = today ?? "";
    this.dateInput.value = query.period === "day" ? query.date : "";
    this.dateInput.parentElement.classList.toggle("is-active", query.period === "day");
  }
}

class Dashboard {
  constructor() {
    const byId = (id) => document.getElementById(id);
    this.api = new ApiClient();
    this.colors = new ColorRegistry();
    this.tooltip = new Tooltip(byId("tooltip"));
    this.names = new ClientNames();
    this.query = Dashboard.#queryFromHash();
    this.focus = null;
    this.report = null;
    this.status = null;

    const onFocus = (model) => this.setFocus(model);
    this.period = new PeriodControls({ buttons: byId("period-buttons"), dateInput: byId("date-input"), onChange: (q) => this.setQuery(q) });
    this.statusBar = new StatusBar({ pills: byId("status-pills"), subtitle: byId("subtitle"), footer: byId("footer"), notices: byId("notices") });
    this.kpis = new KpiPanel({ container: byId("kpis"), caption: byId("kpis-caption"), names: this.names });
    this.timeline = new TimelineChart({
      container: byId("timeline"), legend: byId("timeline-legend"), hint: byId("timeline-hint"),
      colors: this.colors, tooltip: this.tooltip, onFocus,
    });
    this.donut = new DonutChart({ container: byId("donut"), colors: this.colors, onFocus });
    this.models = new ModelsTable({ container: byId("models"), coverage: byId("coverage"), colors: this.colors, onFocus });
    this.live = new LiveModelsPanel({ container: byId("live"), hint: byId("live-hint"), colors: this.colors });
    this.insights = new InsightsPanel({ container: byId("insights"), names: this.names });
    this.loads = new LoadsTable({ container: byId("loads"), colors: this.colors });
    this.hourly = new HourlyProfileChart({ container: byId("hourly"), tooltip: this.tooltip });
    this.clients = new ClientsTable({
      container: byId("clients"), colors: this.colors, names: this.names, onRename: (ip, name) => this.renameClient(ip, name),
    });
    this.endpoints = new EndpointsTable(byId("endpoints"));
    this.recent = new RecentRequestsTable({ container: byId("recent"), hint: byId("recent-hint"), colors: this.colors, names: this.names });
    this.problems = new ProblemsPanel({ container: byId("problems"), names: this.names });

    this.refreshButton = byId("refresh-button");
    this.autoRefresh = byId("auto-refresh");
    this.refreshButton.addEventListener("click", () => this.refresh());
    this.autoRefresh.addEventListener("change", () => this.#scheduleRefresh());
    window.addEventListener("hashchange", () => {
      this.query = Dashboard.#queryFromHash();
      this.refresh();
    });
    let resizeTimer;
    window.addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => this.#renderCharts(), 150);
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && this.focus) this.setFocus(this.focus);
    });
    document.addEventListener("mouseover", (event) => {
      const alias = event.target.closest("[data-client-ip]");
      if (alias) this.tooltip.showAbove(`<span class="mono">${escapeHtml(alias.dataset.clientIp)}</span>`, alias);
    });
    document.addEventListener("mouseout", (event) => {
      const alias = event.target.closest("[data-client-ip]");
      if (alias && !alias.contains(event.relatedTarget)) this.tooltip.hide();
    });
  }

  async renameClient(ip, name) {
    this.names.update(await this.api.saveAlias(ip, name));
    this.#render();
  }

  static #queryFromHash() {
    const params = new URLSearchParams(location.hash.slice(1));
    const query = { period: params.get("period") || "today" };
    if (params.get("hours")) query.hours = params.get("hours");
    if (params.get("date")) query.date = params.get("date");
    return query;
  }

  setQuery(query) {
    const hash = `#${new URLSearchParams(query)}`;
    if (location.hash === hash) this.refresh();
    else location.hash = hash;
  }

  setFocus(model) {
    this.focus = this.focus === model || model === OTHER_MODELS ? null : model;
    this.#render();
  }

  async refresh() {
    this.refreshButton.classList.add("is-spinning");
    this.period.render(this.query, this.status?.now?.slice(0, 10));
    let error = null;
    try {
      let aliases;
      [this.report, this.status, aliases] = await Promise.all([this.api.report(this.query), this.api.status(), this.api.aliases()]);
      this.names.update(aliases);
      if (this.focus && !this.report.models.some((m) => m.model === this.focus)) this.focus = null;
    } catch (caught) {
      error = caught;
    } finally {
      this.refreshButton.classList.remove("is-spinning");
    }
    this.#render(error);
    this.#scheduleRefresh();
  }

  #scheduleRefresh() {
    clearTimeout(this.refreshTimer);
    if (this.autoRefresh.checked) {
      this.refreshTimer = setTimeout(() => this.refresh(), REFRESH_INTERVAL_MS);
    }
  }

  #render(error = null) {
    this.period.render(this.query, this.status?.now?.slice(0, 10));
    this.statusBar.render(this.report, this.status, error);
    this.live.render(this.status);
    if (!this.report) return;
    this.kpis.render(this.report);
    this.#renderCharts();
    this.models.render(this.report, this.focus);
    this.insights.render(this.report);
    this.loads.render(this.report);
    this.clients.render(this.report);
    this.endpoints.render(this.report);
    this.recent.render(this.report, this.focus);
    this.problems.render(this.report);
  }

  #renderCharts() {
    if (!this.report) return;
    this.timeline.render(this.report, this.focus);
    this.donut.render(this.report, this.focus);
    this.hourly.render(this.report);
  }
}

new Dashboard().refresh();
