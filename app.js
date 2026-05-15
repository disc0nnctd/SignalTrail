const state = {
  rows: [], sourceSummary: {}, metricsV2: {}, breakdownSamples: [],
  sorts: { leaderboard: { key: 'calls_evaluated', dir: 'desc' }, breakdown: { key: 'net_return_pct', dir: 'desc' } },
  activeTab: 'public',
  publicData: null,
  privateData: null,
};
const fallbackData = {
  generated_at_utc: '2026-05-14T00:00:00Z',
  source_summary: {
    message_count: 7768, parsed_calls_count: 89, outcome_rows_count: 340,
    benchmark_symbol: 'NIFTYBEES.NS', market_data_range: '1y', lookback_days: 240,
    horizons_days: [1, 3, 5, 10], prefer_target_stop: true, same_bar_policy: 'stop_first',
  },
  metrics_v2: {
    call_level: { win_rate: 0.5, resolved_win_rate: 0.5385, bayes_win_rate: 0.5, profit_factor: 24.7313, expectancy: 11.3472, median_return: 0.692 },
    row_level: { win_rate: 0.3393, resolved_win_rate: 0.6552, bayes_win_rate: 0.3594, profit_factor: 46.9436, expectancy: 20.4718, median_return: 1.024 },
    methods: {
      target_stop: { target_stop_win_rate: 1.0, win_rate: 0.6, resolved_win_rate: 1.0 },
      directional_horizon: { benchmark_relative_win_rate: 0.4118, win_rate: 0.1944, resolved_win_rate: 0.4118 },
    },
  },
  rows: [
    { rank: 1, display_name: '******', channel: 'Motilal Oswal Official', tier: 'IS', score: null, calls_evaluated: 14, rows_evaluated: 56, resolved_calls: 13, call_win_rate: 0.5, resolved_win_rate: 0.5385, row_win_rate: 0.3393, row_resolved_win_rate: 0.6552, benchmark_relative_win_rate: 0.4118, target_stop_win_rate: 1, target_hits: 8, stop_hits: 0, target_hit_rate: 1, stop_hit_rate: 0, avg_r: 11.3472, profit_factor: 24.7313, bayes_win_rate: 0.5, confidence: 'insufficient_sample' },
    { rank: 2, display_name: '*****', channel: 'Angel One', tier: 'IS', score: null, calls_evaluated: 9, rows_evaluated: 33, resolved_calls: 9, call_win_rate: 0.3333, resolved_win_rate: 0.3333, row_win_rate: 0.4242, row_resolved_win_rate: 0.6364, benchmark_relative_win_rate: 0.6364, target_stop_win_rate: 0, target_hits: 0, stop_hits: 0, target_hit_rate: null, stop_hit_rate: null, avg_r: -0.7599, profit_factor: 0.5207, bayes_win_rate: 0.4118, confidence: 'insufficient_sample' }
  ],
};

const fmt = (v, digits = 2) => v === null || v === undefined || Number.isNaN(Number(v)) ? '-' : Number(v).toLocaleString(undefined, { maximumFractionDigits: digits });
const pct = (v) => v === null || v === undefined ? '-' : `${fmt(Number(v) * 100, 1)}%`;
const esc = (v) => String(v ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#39;');
const sum = (rows, key) => rows.reduce((acc, row) => acc + (Number(row?.[key]) || 0), 0);
const avg = (rows, key) => {
  const values = rows.map(row => Number(row?.[key])).filter(Number.isFinite);
  return values.length ? values.reduce((a, b) => a + b, 0) / values.length : null;
};
const weighted = (rows, key, weightKey) => {
  let total = 0, weight = 0;
  for (const row of rows) {
    const value = Number(row?.[key]);
    const w = Number(row?.[weightKey]);
    if (!Number.isFinite(value) || !Number.isFinite(w) || w <= 0) continue;
    total += value * w; weight += w;
  }
  return weight ? total / weight : null;
};
const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
const get = (obj, path, fallback = null) => path.split('.').reduce((acc, key) => (acc && acc[key] !== undefined ? acc[key] : undefined), obj) ?? fallback;

const RED_FLAG_LABELS = {
  no_sebi_number_shown: 'No reg#',
  stop_loss_hidden_behind_paywall: 'Stops hidden',
  account_handling_50_50_profit_share: 'Account handling',
  self_declared_not_sebi_registered: 'Self: not SEBI',
  listed_as_sebi_on_source_site: 'Misrepresented',
  not_a_trading_channel: 'Not signals',
  app_promotion_channel: 'App promo',
  no_calls_found: 'No calls',
  no_complete_trade_plans: 'No full plans',
  primary_calls_behind_paywall: 'Paywalled',
  access_restricted_to_customers: 'Customers only',
  paid_promotion_contact_listed: 'Paid promo',
  no_stops: 'No stops',
  potentially_misleading_name: 'Misleading name',
};

const DATA_QUALITY_MAP = {
  full_plan:             { cls: 'dq-full',    label: 'Full plan' },
  partial:               { cls: 'dq-partial', label: 'Partial' },
  no_complete_plans:     { cls: 'dq-warn',    label: 'No full plans' },
  stops_hidden:          { cls: 'dq-warn',    label: 'Stops hidden' },
  no_stops:              { cls: 'dq-warn',    label: 'No stops' },
  no_calls:              { cls: 'dq-none',    label: 'No calls' },
  not_a_signals_channel: { cls: 'dq-none',    label: 'Not signals' },
};

function renderSebiStatus(r) {
  if (r.sebi_claimed === false) {
    return `<span class="sebi-badge sebi-no" title="Channel explicitly states it is not SEBI registered">Not SEBI</span>`;
  }
  if (r.sebi_reg_number) {
    return `<span class="sebi-badge sebi-num" title="Self-disclosed number — not independently verified by SignalTrail">${esc(r.sebi_reg_number)}<span class="sebi-unverified">*</span></span>`;
  }
  return `<span class="sebi-badge sebi-claimed" title="Claims SEBI registration but no registration number visible in public feed">Claimed</span>`;
}

function renderRedFlags(flags) {
  if (!Array.isArray(flags) || !flags.length) return '';
  return flags.map(f => `<span class="red-flag">${esc(RED_FLAG_LABELS[f] || f)}</span>`).join('');
}

function renderDataQuality(r) {
  const q = r.data_quality || '';
  const { cls = 'dq-none', label = q || '—' } = DATA_QUALITY_MAP[q] || {};
  return `<span class="dq-badge ${esc(cls)}">${esc(label)}</span>`;
}

function overallStats(rows) {
  const calls = sum(rows, 'calls_evaluated');
  const evaluatedRows = sum(rows, 'rows_evaluated');
  const resolvedRows = sum(rows, 'resolved_target_stop_rows');
  const targetRows = sum(rows, 'target_stop_rows');
  const targetHits = sum(rows, 'target_hits');
  const stopHits = sum(rows, 'stop_hits');
  return {
    traders: rows.length, calls, rows: evaluatedRows,
    callWin: weighted(rows, 'call_win_rate', 'calls_evaluated'),
    resolvedWin: weighted(rows, 'resolved_win_rate', 'resolved_calls') ?? weighted(rows, 'resolved_win_rate', 'calls_evaluated'),
    rowWin: weighted(rows, 'row_win_rate', 'rows_evaluated'),
    rowResolvedWin: weighted(rows, 'row_resolved_win_rate', 'rows_evaluated'),
    directionalWin: weighted(rows, 'benchmark_relative_win_rate', 'rows_evaluated'),
    bayesWin: weighted(rows, 'bayes_win_rate', 'calls_evaluated'),
    avgR: avg(rows, 'avg_r'),
    profitFactor: avg(rows, 'profit_factor'),
    targetHitRate: resolvedRows ? targetHits / resolvedRows : null,
    stopHitRate: resolvedRows ? stopHits / resolvedRows : null,
    targetCoverage: evaluatedRows ? targetRows / evaluatedRows : null,
    resolvedRows,
  };
}

function statCard(label, value, note) {
  return `<article class="stat"><div class="stat-label">${esc(label)}</div><div class="stat-value">${value}</div><div class="stat-note">${esc(note)}</div></article>`;
}

function sortValue(row, key) {
  const v = row?.[key];
  if (key === 'direction') return String(v || '').toLowerCase();
  if (v === null || v === undefined || v === '') return Number.NEGATIVE_INFINITY;
  const n = Number(v);
  return Number.isFinite(n) ? n : String(v).toLowerCase();
}

function sortRows(rows, tableName) {
  const { key, dir } = state.sorts[tableName] || {};
  const sign = dir === 'asc' ? 1 : -1;
  return [...rows].sort((a, b) => {
    const av = sortValue(a, key), bv = sortValue(b, key);
    if (av < bv) return -1 * sign;
    if (av > bv) return 1 * sign;
    const ac = Number(a.calls_evaluated) || 0, bc = Number(b.calls_evaluated) || 0;
    return (bc - ac) * sign;
  });
}

function sourceMeta(data) { return data.sourceSummary || data.source_summary || {}; }
function metricsMeta(data) { return data.metricsV2 || data.metrics_v2 || {}; }

function renderSummary(data) {
  const source = sourceMeta(data);
  const metrics = metricsMeta(data);
  const call = metrics.call_level || {};
  const row = metrics.row_level || {};
  const target = (metrics.methods || {}).target_stop || {};
  const directional = (metrics.methods || {}).directional_horizon || {};
  const stats = overallStats(data.rows || []);
  const horizons = Array.isArray(source.horizons_days) ? source.horizons_days.join('/') + 'd' : 'multi-horizon';
  document.getElementById('summary').innerHTML = [
    statCard('Channels audited', fmt(stats.traders, 0), `${source.lookback_days ?? '-'} day lookback`),
    statCard('Parsed calls', fmt(source.parsed_calls_count ?? stats.calls, 0), horizons),
    statCard('Outcome rows', fmt(source.outcome_rows_count ?? stats.rows, 0), `Benchmark: ${source.benchmark_symbol || '—'}`),
    statCard('Call win', pct(call.win_rate ?? stats.callWin), `Resolved-only ${pct(call.resolved_win_rate ?? stats.resolvedWin)}`),
    statCard('Row win', pct(row.win_rate ?? stats.rowWin), `Resolved-only ${pct(row.resolved_win_rate ?? stats.rowResolvedWin)}`),
    statCard('Target/stop win', pct(target.target_stop_win_rate ?? stats.targetHitRate), `${fmt(sum((data.rows || []).filter(r => Number(r.target_stop_rows) > 0), 'target_stop_rows'), 0)} target-plan rows`),
    statCard('Directional win', pct(directional.benchmark_relative_win_rate ?? stats.directionalWin), 'Versus benchmark'),
    statCard('Bayes win', pct(call.bayes_win_rate ?? stats.bayesWin), 'Shrinkage-adjusted'),
    statCard('Avg R', `<span class="${Number(stats.avgR || 0) >= 0 ? 'good' : 'bad'}">${fmt(call.expectancy ?? stats.avgR, 2)}</span>`, `Median ${fmt(call.median_return, 2)} R`),
    statCard('Profit factor', fmt(call.profit_factor ?? stats.profitFactor, 2), 'Best sources float to the top'),
  ].join('');
}

function renderInsight(rows, data) {
  const metrics = metricsMeta(data);
  const call = metrics.call_level || {};
  const row = metrics.row_level || {};
  const target = (metrics.methods || {}).target_stop || {};
  const directional = (metrics.methods || {}).directional_horizon || {};
  const top = [...rows].sort((a, b) => {
    const scoreA = (Number(a.profit_factor) || 0) + (Number(a.resolved_win_rate) || 0) * 10 + (Number(a.avg_r) || 0) / 5;
    const scoreB = (Number(b.profit_factor) || 0) + (Number(b.resolved_win_rate) || 0) * 10 + (Number(b.avg_r) || 0) / 5;
    return scoreB - scoreA;
  })[0];
  const standout = top ? `${esc(top.display_name)} looks strongest in this sample: ${pct(top.resolved_win_rate ?? top.call_win_rate)} resolved win, PF ${fmt(top.profit_factor, 2)}, and avg R ${fmt(top.avg_r, 2)}.` : 'No source data available.';
  const moreLikely = top && Number(top.resolved_win_rate) >= 0.5
    ? `${esc(top.display_name)} is the most likely to win more often than lose in the visible sample, based on resolved outcomes and risk-adjusted edge.`
    : 'No source in the current view clears the simple >50% resolved-win heuristic.';
  const sampleNote = `${fmt(rows.length, 0)} visible sources, with ${fmt((data.sourceSummary || data.source_summary || {}).parsed_calls_count ?? rows.reduce((a, r) => a + (Number(r.calls_evaluated) || 0), 0), 0)} parsed calls across the selected view.`;
  document.getElementById('insight').innerHTML = `
    <div class="card-head">
      <h2>Performance summary</h2>
      <span class="muted">Plain-language read of the current sample</span>
    </div>
    <div style="padding:18px 20px 20px">
      <p class="insight-copy">${standout} ${moreLikely}</p>
      <p class="insight-meta">At the method level, call win is ${pct(call.win_rate)}, resolved-only win is ${pct(call.resolved_win_rate)}, row win is ${pct(row.win_rate)}, target/stop win is ${pct(target.target_stop_win_rate)}, and directional win vs benchmark is ${pct(directional.benchmark_relative_win_rate)}.</p>
      <p class="insight-meta">${sampleNote} Treat this as a sample snapshot, not a promise of future returns.</p>
    </div>
  `;
}

function renderBarChart(rows, { title, subtitle, metric, formatValue, foot, filter = () => true }) {
  const items = rows.filter(filter).filter(row => Number.isFinite(Number(row?.[metric]))).sort((a, b) => (Number(b?.[metric]) || 0) - (Number(a?.[metric]) || 0)).slice(0, 6);
  const max = Math.max(...items.map(r => Number(r?.[metric]) || 0), 1);
  const bars = items.length ? items.map((row, idx) => {
    const raw = Number(row?.[metric]) || 0;
    const width = clamp((raw / max) * 100, 8, 100);
    const tone = metric === 'avg_r' || metric === 'benchmark_relative_win_rate' ? (raw >= 0 ? 'good' : 'warn') : '';
    return `
      <div class="bar-row">
        <div class="bar-top">
          <span class="bar-name">${idx + 1}. ${esc(row.display_name)}</span>
          <span class="bar-meta">${formatValue(raw, row)}</span>
        </div>
        <div class="bar-track"><div class="bar-fill ${tone}" style="width:${width}%"></div></div>
        <div class="bar-foot"><span>${esc(row.tier)} · ${fmt(row.calls_evaluated, 0)} calls</span><span>${foot(row)}</span></div>
      </div>
    `;
  }).join('') : '<div class="bar-row"><div class="bar-top"><span class="bar-name muted">No rows with sufficient data</span><span class="bar-meta">-</span></div></div>';
  return `
    <section class="card chart-card">
      <div class="chart-head"><h3>${esc(title)}</h3><p>${esc(subtitle)}</p></div>
      <div class="bars">${bars}</div>
    </section>
  `;
}

function renderCharts(rows, data) {
  document.getElementById('charts').innerHTML = [
    renderBarChart(rows, {
      title: 'Top call volume',
      subtitle: 'Most active channels in the sample.',
      metric: 'calls_evaluated',
      formatValue: (value) => `${fmt(value, 0)} calls`,
      foot: (row) => `Resolved win ${pct(row.resolved_win_rate ?? row.win_rate ?? row.call_win_rate)}`,
    }),
    renderBarChart(rows, {
      title: 'Top profit factor',
      subtitle: 'Strongest risk-adjusted sources.',
      metric: 'profit_factor',
      formatValue: (value) => `PF ${fmt(value, 2)}`,
      foot: (row) => `Avg R ${fmt(row.avg_r, 2)}`,
    }),
    renderBarChart(rows, {
      title: 'Top resolved win rate',
      subtitle: 'Resolved-only win rate after outcomes settle.',
      metric: 'resolved_win_rate',
      formatValue: (value) => pct(value),
      foot: (row) => `${fmt(row.rows_evaluated, 0)} rows · Bayes ${pct(row.bayes_win_rate)}`,
    }),
    renderBarChart(rows, {
      title: 'Top target/stop edge',
      subtitle: 'Only rows with target-plan evidence shown.',
      metric: 'target_stop_win_rate',
      filter: (row) => Number(row.target_stop_rows) > 0,
      formatValue: (value) => pct(value),
      foot: (row) => `${fmt(row.target_stop_rows, 0)} target-plan rows`,
    }),
  ].join('');
}

function renderBreakdown() {
  const cols = [
    ['author_alias', 'Author'],
    ['symbol', 'Stock'],
    ['direction', 'Dir'],
    ['evaluation_window', 'Days'],
    ['evaluation_method', 'Method'],
    ['outcome', 'Outcome'],
    ['reached_target', 'Target?'],
    ['reached_stop', 'Stop?'],
    ['net_return_pct', 'Net R'],
    ['benchmark_excess_return_pct', 'Excess R'],
  ];
  const rows = sortRows((state.breakdownSamples || []).slice(0, 10), 'breakdown');
  const head = `<thead><tr>${cols.map(([key, label]) => sortableTh('breakdown', key, label)).join('')}</tr></thead>`;
  const body = `<tbody>${rows.length ? rows.map((row) => `<tr>${cols.map(([key]) => `<td>${breakdownCell(row, key)}</td>`).join('')}</tr>`).join('') : `<tr><td colspan="${cols.length}" class="muted">No sample breakdown data available.</td></tr>`}</tbody>`;
  document.getElementById('breakdown').innerHTML = head + body;
}

function renderLeaderboard(rows) {
  const cols = [
    ['rank', 'Rank'], ['display_name', 'Trader'], ['tier', 'Tier'],
    ['calls_evaluated', 'Calls'], ['rows_evaluated', 'Rows'],
    ['call_win_rate', 'Call Win %'], ['resolved_win_rate', 'Resolved Win %'], ['row_win_rate', 'Row Win %'],
    ['row_resolved_win_rate', 'Row Resolved Win %'], ['target_stop_win_rate', 'T/S Win %'],
    ['benchmark_relative_win_rate', 'Dir Win %'], ['bayes_win_rate', 'Bayes Win %'],
    ['avg_r', 'Avg R'], ['profit_factor', 'PF'], ['confidence', 'Confidence'],
  ];
  const head = `<thead><tr>${cols.map(([key, label]) => sortableTh('leaderboard', key, label)).join('')}</tr></thead>`;
  const body = `<tbody>${rows.map((r, idx) => {
    const hasFlags = Array.isArray(r.red_flags) && r.red_flags.length > 0;
    const rowClass = hasFlags ? ' class="row-flagged"' : '';
    return `<tr${rowClass}>${cols.map(([key]) => `<td>${cell({ ...r, rank: r.rank ?? idx + 1 }, key)}</td>`).join('')}</tr>`;
  }).join('')}</tbody>`;
  document.getElementById('leaderboard').innerHTML = head + body;
}

function render() {
  const q = document.getElementById('search').value.toLowerCase().trim();
  const tier = document.getElementById('tierFilter').value;
  const rows = sortRows((state.rows || [])
    .filter(r => {
      const text = `${r.display_name} ${r.channel}`.toLowerCase();
      return (!q || text.includes(q)) && (!tier || r.tier === tier);
    })
  , 'leaderboard');
  renderLeaderboard(rows);
  renderInsight(rows, { sourceSummary: state.sourceSummary, source_summary: state.sourceSummary, metricsV2: state.metricsV2, metrics_v2: state.metricsV2 });
  renderSummary({ rows, sourceSummary: state.sourceSummary, metricsV2: state.metricsV2 });
  renderCharts(rows, { sourceSummary: state.sourceSummary, metricsV2: state.metricsV2 });
  renderBreakdown();
}

function cell(r, key) {
  if (key === 'sebi_status') return renderSebiStatus(r);
  if (key === 'data_quality') return renderDataQuality(r);
  if (key === 'display_name') {
    const name = esc(r.display_name);
    const url = r.channel_url ? esc(r.channel_url) : null;
    const handle = esc(r.channel || '');
    const channelLink = url
      ? `<a href="${url}" target="_blank" rel="noopener noreferrer" class="channel-link">${handle}</a>`
      : `<span class="muted">${handle}</span>`;
    const flags = Array.isArray(r.red_flags) && r.red_flags.length
      ? `<div class="flag-row">${renderRedFlags(r.red_flags)}</div>` : '';
    const noteTitle = r.data_note ? ` title="${esc(r.data_note)}"` : '';
    const infoIcon = r.data_note ? `<span class="info-icon"${noteTitle}>ℹ</span>` : '';
    return `<div class="trader-cell"><div class="trader-name">${name}${infoIcon}</div><div class="trader-meta">${channelLink}</div>${flags}</div>`;
  }
  if (key === 'tier') return `<span class="tier ${esc(r.tier)}">${esc(r.tier)}</span>`;
  if (key === 'call_win_rate') return pct(r.call_win_rate ?? r.win_rate);
  if (key === 'resolved_win_rate') return pct(r.resolved_win_rate ?? r.win_rate);
  if (key === 'row_win_rate') return pct(r.row_win_rate ?? r.legacy_win_rate ?? r.win_rate);
  if (key === 'row_resolved_win_rate') return pct(r.row_resolved_win_rate);
  if (key === 'target_stop_win_rate') return pct(r.target_stop_win_rate);
  if (key === 'bayes_win_rate') return pct(r.bayes_win_rate);
  if (key === 'benchmark_relative_win_rate') return pct(r.benchmark_relative_win_rate);
  if (key === 'target_hit_rate') return pct(r.target_hit_rate);
  if (key === 'stop_hit_rate') return pct(r.stop_hit_rate);
  if (key === 'timeout_rate') return pct(r[key]);
  if (key === 'avg_r') {
    const v = r[key];
    if (v === null || v === undefined) return '-';
    return `<span class="${Number(v) >= 0 ? 'good' : 'bad'}">${fmt(v, 2)}</span>`;
  }
  if (key === 'profit_factor' || key === 'score') return fmt(r[key], 2);
  if (key === 'rank') return r.rank ?? '<span class="muted">—</span>';
  if (key === 'calls_evaluated' || key === 'rows_evaluated' || key === 'target_hits' || key === 'stop_hits') return fmt(r[key], 0);
  if (key === 'confidence') {
    const cls = r.confidence === 'eligible' ? 'conf-eligible' : r.confidence === 'no_evaluable_calls' ? 'conf-none' : 'conf-is';
    return `<span class="conf-badge ${cls}">${esc(r.confidence ?? '—')}</span>`;
  }
  return esc(r[key]);
}

function breakdownCell(r, key) {
  if (key === 'direction') return `<span class="tier ${esc(String(r.direction || '').toLowerCase())}">${esc(r.direction || '-')}</span>`;
  if (key === 'reached_target' || key === 'reached_stop') return r[key] ? '✓' : '—';
  if (key === 'net_return_pct' || key === 'benchmark_excess_return_pct') {
    const value = Number(r[key]);
    if (!Number.isFinite(value)) return r[key] === null ? '<span class="muted">pending</span>' : '-';
    return `<span class="${value >= 0 ? 'good' : 'bad'}">${fmt(value, 2)}%</span>`;
  }
  if (key === 'implied_r') {
    const value = Number(r[key]);
    if (!Number.isFinite(value)) return '-';
    return `<span class="good">${fmt(value, 2)}R</span>`;
  }
  if (key === 'stop_price') {
    return r[key] === null ? '<span class="red-flag-inline">hidden</span>' : esc(r[key] ?? '-');
  }
  if (key === 'outcome') {
    const v = String(r[key] || '').toLowerCase();
    const cls = v === 'win' ? 'good' : v === 'loss' ? 'bad' : 'muted';
    return `<span class="${cls}">${esc(r[key] ?? '-')}</span>`;
  }
  if (key === 'observation_note') {
    const note = String(r[key] || '');
    if (!note) return '-';
    return `<span class="obs-note" title="${esc(note)}">${esc(note.length > 40 ? note.slice(0, 40) + '…' : note)}</span>`;
  }
  return esc(r[key] ?? '-');
}

function sortableTh(tableName, key, label) {
  const sort = state.sorts[tableName] || {};
  const active = sort.key === key;
  const indicator = active ? (sort.dir === 'asc' ? '▲' : '▼') : '';
  return `<th class="sortable" data-sort-table="${tableName}" data-sort-key="${key}"><button type="button">${esc(label)}<span class="sort-indicator">${indicator}</span></button></th>`;
}

function toggleSort(tableName, key) {
  const current = state.sorts[tableName] || { key, dir: 'desc' };
  state.sorts[tableName] = { key, dir: current.key === key && current.dir === 'desc' ? 'asc' : 'desc' };
  render();
}

function applyData(data) {
  state.rows = data.rows || [];
  state.sourceSummary = data.source_summary || data.sourceSummary || {};
  state.metricsV2 = data.metrics_v2 || data.metricsV2 || {};
  state.breakdownSamples = data.breakdown_samples || data.breakdownSamples || [];
}

function loadData(data, label) {
  applyData(data);
  document.getElementById('generated').textContent = label || `Generated: ${data.generated_at_utc || '-'}`;
  render();
}

function switchTab(tab) {
  state.activeTab = tab;
  const isPublic = tab === 'public';

  document.getElementById('panel-public').hidden = !isPublic;
  document.getElementById('panel-mine').hidden = isPublic;
  document.getElementById('tab-btn-public').setAttribute('aria-selected', String(isPublic));
  document.getElementById('tab-btn-mine').setAttribute('aria-selected', String(!isPublic));
  document.getElementById('tab-btn-public').classList.toggle('active', isPublic);
  document.getElementById('tab-btn-mine').classList.toggle('active', !isPublic);

  const data = isPublic ? state.publicData : state.privateData;
  if (data) {
    const label = isPublic
      ? `Generated: ${data.generated_at_utc || '-'}`
      : `Your data · Generated: ${data.generated_at_utc || '-'}`;
    loadData(data, label);
  }

  const url = isPublic ? location.pathname : `${location.pathname}?tab=mine`;
  history.pushState({ tab }, '', url);
}

async function main() {
  const params = new URLSearchParams(window.location.search);
  const dataset = params.get('dataset') || 'leaderboard-public.json';
  let data = fallbackData;
  let label = null;
  try {
    const res = await fetch(dataset, { cache: 'no-store' });
    if (!res.ok) throw new Error(`Failed to load ${dataset} (${res.status})`);
    data = await res.json();
  } catch (err) {
    label = `Using fallback example data: ${err.message}`;
  }
  state.publicData = data;
  loadData(data, label);

  if (params.get('tab') === 'mine') switchTab('mine');
}

// --- Tab switching ---
function initTabs() {
  document.getElementById('tab-btn-public').addEventListener('click', () => switchTab('public'));
  document.getElementById('tab-btn-mine').addEventListener('click', () => switchTab('mine'));
  window.addEventListener('popstate', (e) => switchTab(e.state?.tab || 'public'));
}

// --- Upload flow ---
function initUpload() {
  const dialog = document.getElementById('uploadDisclaimer');
  const ack = document.getElementById('disclaimerAck');
  const confirm = document.getElementById('disclaimerConfirm');
  const cancel = document.getElementById('disclaimerCancel');
  const trigger = document.getElementById('uploadTrigger');
  const input = document.getElementById('uploadInput');
  const drop = document.getElementById('uploadDrop');
  const banner = document.getElementById('privateViewBanner');
  const uploadZone = document.getElementById('uploadZone');
  const resetBtn = document.getElementById('resetToPublic');

  ack.addEventListener('change', () => { confirm.disabled = !ack.checked; });

  function openDisclaimer() {
    ack.checked = false;
    confirm.disabled = true;
    dialog.showModal();
  }

  cancel.addEventListener('click', () => dialog.close());
  dialog.addEventListener('click', (e) => { if (e.target === dialog) dialog.close(); });
  confirm.addEventListener('click', () => { dialog.close(); input.click(); });
  trigger.addEventListener('click', (e) => { e.preventDefault(); openDisclaimer(); });

  drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('drag-over'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('drag-over'));
  drop.addEventListener('drop', (e) => {
    e.preventDefault();
    drop.classList.remove('drag-over');
    const file = e.dataTransfer?.files?.[0];
    if (file) openDisclaimer();
  });

  input.addEventListener('change', () => {
    const file = input.files?.[0];
    if (file) readAndLoad(file);
    input.value = '';
  });

  resetBtn.addEventListener('click', () => {
    state.privateData = null;
    banner.hidden = true;
    uploadZone.hidden = false;
    if (state.publicData) loadData(state.publicData, `Generated: ${state.publicData.generated_at_utc || '-'}`);
  });

  function readAndLoad(file) {
    const reader = new FileReader();
    reader.onload = (e) => {
      try {
        const data = JSON.parse(e.target.result);
        if (!Array.isArray(data.rows)) throw new Error('Not a valid leaderboard JSON (missing "rows" array).');
        state.privateData = data;
        loadData(data, `Your data · Generated: ${data.generated_at_utc || '-'}`);
        uploadZone.hidden = true;
        banner.hidden = false;
      } catch (err) {
        alert(`Could not load file: ${err.message}`);
      }
    };
    reader.readAsText(file);
  }
}

document.getElementById('search').addEventListener('input', render);
document.getElementById('tierFilter').addEventListener('change', render);
document.getElementById('leaderboard').addEventListener('click', (event) => {
  const th = event.target.closest('th[data-sort-table][data-sort-key]');
  if (!th || th.dataset.sortTable !== 'leaderboard') return;
  toggleSort(th.dataset.sortTable, th.dataset.sortKey);
});
document.getElementById('breakdown').addEventListener('click', (event) => {
  const th = event.target.closest('th[data-sort-table][data-sort-key]');
  if (!th || th.dataset.sortTable !== 'breakdown') return;
  toggleSort(th.dataset.sortTable, th.dataset.sortKey);
});
initTabs();
initUpload();
main().catch(err => { document.getElementById('generated').textContent = err.message; });
