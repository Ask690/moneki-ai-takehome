/* 经营看板前端：数据看板（第一关）、问答助手（第三关）、调试面板（第四关）。
 *
 * 全部逻辑都在这个文件里，没有构建步骤、没有依赖包：
 * 图表用 CDN 上的 ECharts，其余是原生 JS。
 * 所有清单（门店、商品、日期区间）都从后端接口取，前端不写死任何编号或数字。
 */
'use strict';

/* ============================ 通用小工具 ============================ */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/** 动态建节点：文本一律走 textContent，文档里的内容不会当成 HTML 执行。 */
function h(tag, attrs, ...kids) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value == null || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'html') node.innerHTML = value;
    else if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2), value);
    } else node.setAttribute(key, value);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return node;
}

const FMT_INT = new Intl.NumberFormat('zh-CN');
const FMT_MONEY = new Intl.NumberFormat('zh-CN', {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const yuan = (v) => (v == null ? '—' : '¥' + FMT_MONEY.format(v));
const num = (v) => (v == null ? '—' : FMT_INT.format(v));
const ms = (v) =>
  v == null ? '—' : v >= 1000 ? (v / 1000).toFixed(2) + ' s' : v.toFixed(1) + ' ms';
const json = (v) => JSON.stringify(v, null, 2);

/** 坐标轴上的金额：上万之后换算成“万”，免得轴上挤满数字。 */
function axisMoney(v) {
  const abs = Math.abs(v);
  if (abs >= 10000) return (v / 10000).toFixed(abs >= 100000 ? 0 : 1) + '万';
  return FMT_INT.format(Math.round(v));
}

function shiftDate(iso, days) {
  const [y, m, d] = iso.split('-').map(Number);
  return new Date(Date.UTC(y, m - 1, d) + days * 86400000).toISOString().slice(0, 10);
}

/** 上一个自然月：[首日, 末日]。 */
function prevMonth(iso) {
  const [y, m] = iso.split('-').map(Number);
  const first = new Date(Date.UTC(y, m - 2, 1));
  const last = new Date(Date.UTC(y, m - 1, 0));
  return [first.toISOString().slice(0, 10), last.toISOString().slice(0, 10)];
}

async function apiGet(path, params) {
  const url = new URL(path, location.origin);
  for (const [key, value] of Object.entries(params || {})) {
    if (value !== '' && value != null) url.searchParams.set(key, value);
  }
  const res = await fetch(url, { headers: { Accept: 'application/json' } });
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    throw new Error((data && (data.error || data.detail)) || 'HTTP ' + res.status);
  }
  return data;
}

async function apiPost(path, body) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    throw new Error((data && (data.error || data.detail)) || 'HTTP ' + res.status);
  }
  return data;
}

/** 可折叠的 JSON 块。 */
function jsonDetails(summary, payload, open) {
  return h(
    'details',
    { class: 'evidence', open: open ? 'open' : null },
    h('summary', { text: summary }),
    h('pre', { class: 'json', text: json(payload) })
  );
}

/* ============================ 全局状态 ============================ */

const state = {
  catalog: null,
  sessionId: null,
  trendChart: null,
  chartBound: false,
  trendDays: [],
  lastTraceId: null,
  answerEngine: 'mock',
  modeSwitchable: false,
};

const ANSWER_TYPES = {
  data: ['数据', 'badge-data'],
  doc: ['文档', 'badge-doc'],
  hybrid: ['数据 + 文档', 'badge-hybrid'],
  refusal: ['拒答', 'badge-refusal'],
  clarify: ['需要澄清', 'badge-clarify'],
};

/* ============================ 标签页 ============================ */

function activateTab(name) {
  $$('.tab').forEach((tab) => {
    const on = tab.dataset.tab === name;
    tab.classList.toggle('is-active', on);
    tab.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  $$('.panel').forEach((panel) => {
    panel.classList.toggle('is-active', panel.id === 'panel-' + name);
  });
  if (name === 'dashboard' && state.trendChart) {
    // 面板从 display:none 切回来时容器才有宽度，图表要重新量一次。
    requestAnimationFrame(() => state.trendChart.resize());
  }
}

/** 深链：#chat、#debug、#debug/t-20260901-0001。方便把一次出错的回答直接发给别人。 */
function applyHash() {
  const raw = (location.hash || '').replace(/^#/, '');
  if (!raw) return;
  const [tab, arg] = raw.split('/');
  if (!['dashboard', 'chat', 'debug'].includes(tab)) return;
  activateTab(tab);
  if (tab === 'debug' && arg) loadTrace(decodeURIComponent(arg));
}

$$('.tab').forEach((tab) =>
  tab.addEventListener('click', () => {
    activateTab(tab.dataset.tab);
    history.replaceState(null, '', '#' + tab.dataset.tab);
  })
);

/* ============================ 第一关：数据看板 ============================ */

const REMOVAL_LABELS = [
  ['1_unparseable_date', '日期无法解析'],
  ['2_empty_amount', '金额为空'],
  ['3_qty_le_zero', '数量 ≤ 0'],
  ['4_store_not_in_stores', '门店外键无效'],
  ['5_product_not_in_products', '商品外键无效'],
  ['6_duplicate_row', '重复行'],
];

function themeColors() {
  const cs = getComputedStyle(document.documentElement);
  const get = (name) => cs.getPropertyValue(name).trim();
  return {
    surface: get('--surface'),
    page: get('--page'),
    ink: get('--ink'),
    ink2: get('--ink-2'),
    muted: get('--muted'),
    grid: get('--grid'),
    axis: get('--axis'),
    series1: get('--series-1'),
  };
}

async function loadHealth() {
  try {
    const health = await apiGet('/api/health');
    $('#today-label').textContent = '今天 ' + health.today;
    // 有 Key 才谈得上切换：没 Key 时 live 这条路根本不存在，按钮不给点。
    state.modeSwitchable = health.llm_mode === 'live';
    renderMode(health);
  } catch (err) {
    $('#today-label').textContent = '服务不可用';
  }
}

/** 徽标报的是**实际会走哪条路**（`answer_engine`），不是配置。
 *
 * 演示时拨到降级之后，"会不会调用模型/花不花额度"才是要看的那件事；
 * 配置是什么，鼠标停上去的说明里写着。
 */
function renderMode(health) {
  const engine = health.answer_engine || health.llm_mode;
  const forced = health.llm_override != null;
  const live = engine === 'live';
  // 记在这里而不是 loadHealth 里：切换之后也要跟着更新，
  // 否则第二次点还是发同一个模式，拨不回去。
  state.answerEngine = engine;

  const badge = $('#llm-mode');
  badge.textContent = live ? '模型已接入' : forced ? '降级模式（手动切换）' : '降级模式（未配置 Key）';
  badge.className = 'badge ' + (live ? 'badge-data' : 'badge-clarify');
  badge.title = live
    ? '问答走模型 + 工具调用，每次提问都会真的调用大模型'
    : forced
      ? '当前手动切到了降级模式：走纯代码的抽取式路径，不调用大模型。' +
        '刷新页面不会恢复，重启服务才回到环境变量决定的模式。'
      : '未配置大模型，问答走模板路径，/api/chat 仍返回结构化回答';

  const btn = $('#llm-toggle');
  btn.hidden = !state.modeSwitchable;
  if (btn.hidden) return;
  btn.disabled = false;
  btn.textContent = live ? '切到降级' : '切回模型';
  btn.title = live
    ? '切到降级模式：不调用大模型，更快也不花额度，但需要多步推理的混合题会退化成模板回答'
    : '切回 live 模式：问答重新走模型 + 工具调用（这次切换只在这个进程里有效，重启服务即复原）';

  const note = $('#chat-note');
  if (note) {
    note.textContent = live
      ? '模型思考加上多轮查数，一次回答可能要几十秒，期间请勿刷新页面。'
      : '当前是降级模式：不调用大模型，回答由代码按工具结果渲染，通常一秒内返回。';
  }
}

async function toggleMode() {
  const btn = $('#llm-toggle');
  const want = state.answerEngine === 'live' ? 'mock' : 'live';
  btn.disabled = true;
  try {
    renderMode(await apiPost('/api/mode', { mode: want }));
  } catch (err) {
    // 这个界面里没有用 alert 的地方，错误一律就地显示（和问答失败同一个路子）。
    // 失败原因放在 title 里：能点到这个按钮，说明 Key 是有的，
    // 真正会失败的情况是服务没在跑，那看徽标就够了。
    const badge = $('#llm-mode');
    badge.textContent = '切换失败';
    badge.className = 'badge badge-refusal';
    badge.title = err.message;
    btn.disabled = false;
  }
}

async function loadCatalog() {
  const catalog = await apiGet('/api/catalog');
  state.catalog = catalog;

  const select = $('#store-select');
  select.innerHTML = '';
  select.append(h('option', { value: '', text: '全部门店' }));
  for (const store of catalog.stores) {
    select.append(
      h('option', {
        value: store.store_id,
        text: store.store_id + ' · ' + store.store_name,
      })
    );
  }

  const start = $('#start-date');
  const end = $('#end-date');
  const period = catalog.data_period || {};
  for (const input of [start, end]) {
    if (period.start) input.min = period.start;
    if (period.end) input.max = period.end;
  }
  if (period.start && period.end) {
    end.value = period.end;
    start.value = shiftDate(period.end, -29);
  }
  state.period = period;

  renderExamples();
  return catalog;
}

function applyPreset(name) {
  const period = state.period || {};
  if (!period.start || !period.end) return;
  let range;
  if (name === 'all') range = [period.start, period.end];
  else if (name === 'month') range = prevMonth(period.end);
  else range = [shiftDate(period.end, -(Number(name) - 1)), period.end];
  $('#start-date').value = range[0];
  $('#end-date').value = range[1];
  loadDashboard();
}

$$('[data-preset]').forEach((btn) =>
  btn.addEventListener('click', () => applyPreset(btn.dataset.preset))
);

function currentRange() {
  return { start: $('#start-date').value, end: $('#end-date').value, storeId: $('#store-select').value };
}

function kpiTile(label, value, unit, note, small) {
  return h(
    'div',
    { class: 'kpi' },
    h('div', { class: 'kpi-label', text: label }),
    h(
      'div',
      { class: 'kpi-value' + (small ? ' small' : '') },
      value,
      unit ? h('span', { class: 'kpi-unit', text: unit }) : null
    ),
    note ? h('div', { class: 'kpi-note', text: note }) : null
  );
}

function renderSummary(summary) {
  const box = $('#summary-cards');
  box.innerHTML = '';
  box.append(
    kpiTile('净营业额', yuan(summary.net_revenue), '', '销售行 + 退款行（KB-001）'),
    kpiTile('退款金额', yuan(summary.refund_amount), '', '区间内退款行金额之和'),
    kpiTile('有效订单数', num(summary.orders), '单', '只数销售行的不同订单号'),
    kpiTile('客单价', summary.aov == null ? '—' : yuan(summary.aov), '', '净营业额 ÷ 有效订单数'),
    kpiTile('销量', num(summary.qty), '份', '退款行按负数量冲减')
  );
}

/** ECharts 是 CDN 上的：取不到时退化成表格，看板其余部分照常可用。 */
function trendTable(days) {
  return h(
    'div',
    { class: 'table-scroll', style: 'max-height:320px' },
    h(
      'table',
      {},
      h(
        'thead',
        {},
        h(
          'tr',
          {},
          h('th', { text: '日期' }),
          h('th', { class: 'num', text: '净营业额' }),
          h('th', { class: 'num', text: '订单数' }),
          h('th', { class: 'num', text: '客单价' })
        )
      ),
      h(
        'tbody',
        {},
        days.map((day) =>
          h(
            'tr',
            {},
            h('td', { text: day.date }),
            h('td', { class: 'num', text: yuan(day.net_revenue) }),
            h('td', { class: 'num', text: num(day.orders) }),
            h('td', { class: 'num', text: day.aov == null ? '—' : yuan(day.aov) })
          )
        )
      )
    )
  );
}

function renderTrend(days) {
  const box = $('#trend-chart');
  state.trendDays = days;
  if (state.trendChart) {
    state.trendChart.dispose();
    state.trendChart = null;
  }
  box.innerHTML = '';
  if (!days.length) {
    $('#trend-sub').textContent = '';
    box.append(h('p', { class: 'muted', text: '区间内没有数据。' }));
    return;
  }
  if (typeof echarts === 'undefined') {
    $('#trend-sub').textContent = '图表库未加载，已改用表格';
    box.append(trendTable(days));
    return;
  }
  state.trendChart = echarts.init(box, null, { renderer: 'canvas' });
  if (!state.chartBound) {
    // 全局只需要绑一次：跟随窗口尺寸，以及跟随系统深浅色切换重画。
    state.chartBound = true;
    window.addEventListener('resize', () => state.trendChart && state.trendChart.resize());
    window
      .matchMedia('(prefers-color-scheme: dark)')
      .addEventListener('change', () => renderTrend(state.trendDays));
  }

  const c = themeColors();
  const dates = days.map((d) => d.date);
  const values = days.map((d) => d.net_revenue);
  $('#trend-sub').textContent =
    days.length + ' 天 · 合计 ' + yuan(values.reduce((a, b) => a + b, 0));

  state.trendChart.setOption(
    {
      // 一个测度、一根轴：订单数与客单价放在悬停提示里，不做双轴。
      grid: { left: 4, right: 12, top: 28, bottom: 4, containLabel: true },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross', crossStyle: { color: c.axis }, label: { backgroundColor: c.ink2 } },
        backgroundColor: c.surface,
        borderColor: c.axis,
        textStyle: { color: c.ink, fontSize: 12 },
        formatter: (params) => {
          const item = params[0];
          const day = state.trendDays[item.dataIndex] || {};
          return [
            '<strong>' + day.date + '</strong>',
            '净营业额　' + yuan(day.net_revenue),
            '订单数　　' + num(day.orders) + ' 单',
            '客单价　　' + (day.aov == null ? '—' : yuan(day.aov)),
          ].join('<br>');
        },
      },
      xAxis: {
        type: 'category',
        boundaryGap: false,
        data: dates,
        axisLine: { lineStyle: { color: c.axis } },
        axisTick: { show: false },
        axisLabel: { color: c.muted, fontSize: 11, hideOverlap: true },
      },
      yAxis: {
        type: 'value',
        splitLine: { lineStyle: { color: c.grid } },
        axisLabel: { color: c.muted, fontSize: 11, formatter: axisMoney },
      },
      series: [
        {
          name: '净营业额',
          type: 'line',
          data: values,
          showSymbol: false,
          symbolSize: 8,
          lineStyle: { width: 2, color: c.series1 },
          itemStyle: { color: c.series1 },
          areaStyle: { color: c.series1, opacity: 0.08 },
          // 只标注最高与最低两天，不是每个点都写数字。
          markPoint: {
            symbol: 'circle',
            symbolSize: 8,
            itemStyle: { color: c.series1 },
            label: { color: c.ink2, fontSize: 11, distance: 8 },
            data: [
              { type: 'max', name: '最高' },
              { type: 'min', name: '最低' },
            ],
          },
        },
      ],
    },
    true
  );
  state.trendChart.resize();
}

function renderTopProducts(products) {
  const box = $('#top-products');
  box.innerHTML = '';
  if (!products.length) {
    box.append(h('p', { class: 'muted', text: '区间内没有商品销售记录。' }));
    return;
  }
  // 占比是“该商品净营业额 ÷ 这些商品的合计”，条形按其中最大的一份定宽。
  const total = products.reduce((sum, p) => sum + p.net_revenue, 0);
  const shares = products.map((p) => (total ? p.net_revenue / total : 0));
  const maxShare = Math.max(...shares) || 1;
  const table = h(
    'table',
    {},
    h(
      'thead',
      {},
      h(
        'tr',
        {},
        h('th', { class: 'num', text: '#' }),
        h('th', { text: '商品' }),
        h('th', { text: '品类' }),
        h('th', { class: 'num', text: '净营业额' }),
        h('th', { text: '占 Top 10 营收' }),
        h('th', { class: 'num', text: '销量' }),
        h('th', { class: 'num', text: '订单数' })
      )
    ),
    h(
      'tbody',
      {},
      products.map((p, index) =>
        h(
          'tr',
          {},
          h('td', { class: 'num rank', text: String(index + 1) }),
          h('td', { text: p.product_name || p.product_id }),
          h('td', { class: 'muted', text: p.product_category || '—' }),
          h('td', { class: 'num', text: yuan(p.net_revenue) }),
          h(
            'td',
            {},
            h(
              'div',
              { class: 'bar-row', style: 'grid-template-columns: 1fr 48px' },
              h(
                'div',
                { class: 'bar-track' },
                h('div', {
                  class: 'bar-fill',
                  style: 'width:' + Math.max(2, (shares[index] / maxShare) * 100).toFixed(1) + '%',
                })
              ),
              h('div', { class: 'bar-value', text: (shares[index] * 100).toFixed(1) + '%' })
            )
          ),
          h('td', { class: 'num', text: num(p.qty) }),
          h('td', { class: 'num', text: num(p.orders) })
        )
      )
    )
  );
  box.append(h('div', { class: 'table-scroll' }, table));
}

function renderQuality(quality) {
  const box = $('#quality-body');
  box.innerHTML = '';
  const report = quality.cleaning_report || {};
  const removed = report.removed || {};
  const raw = report.raw_rows || 0;
  const kept = report.kept_rows || 0;
  const dropped = Math.max(0, raw - kept);

  const period = quality.data_period || {};
  $('#quality-sub').textContent =
    period.start && period.end ? period.start + ' 至 ' + period.end : '';

  box.append(
    h(
      'div',
      { class: 'quality-totals' },
      h(
        'div',
        { class: 'quality-total' },
        h('div', { class: 'kpi-label', text: '原始行数' }),
        h('div', { class: 'n', text: num(raw) })
      ),
      h(
        'div',
        { class: 'quality-total' },
        h('div', { class: 'kpi-label', text: '清洗后保留' }),
        h('div', { class: 'n', text: num(kept) })
      ),
      h(
        'div',
        { class: 'quality-total' },
        h('div', { class: 'kpi-label', text: '剔除合计' }),
        h('div', { class: 'n', text: num(dropped) }),
        h('div', {
          class: 'kpi-note',
          text: raw ? '占 ' + ((dropped / raw) * 100).toFixed(1) + '%' : '',
        })
      )
    ),
    h('div', {
      class: 'muted',
      text: '剔除规则按 KB-001 的执行顺序，一行只记第一条命中的原因：',
    })
  );

  const max = Math.max(1, ...REMOVAL_LABELS.map(([key]) => removed[key] || 0));
  for (const [key, label] of REMOVAL_LABELS) {
    const value = removed[key] || 0;
    box.append(
      h(
        'div',
        { class: 'bar-row' },
        h('div', { class: 'bar-label', text: label }),
        h(
          'div',
          { class: 'bar-track' },
          h('div', {
            class: 'bar-fill',
            style: 'width:' + (value ? Math.max(2, (value / max) * 100).toFixed(1) : 0) + '%',
          })
        ),
        h('div', { class: 'bar-value', text: num(value) })
      )
    );
  }

  if (removed.note_unparseable_amount) {
    box.append(
      h('p', {
        class: 'muted',
        text: '其中 ' + num(removed.note_unparseable_amount) + ' 行的金额非空但无法解析，同样按「金额为空」剔除。',
      })
    );
  }

  const warnings = quality.kb_warnings || [];
  if (warnings.length) {
    box.append(
      h('div', { class: 'muted', style: 'margin-top:12px', text: '知识库载入提示：' }),
      h('ul', { class: 'warn-list' }, warnings.map((text) => h('li', { text })))
    );
  }
}

async function loadDashboard() {
  const { start, end, storeId } = currentRange();
  const error = $('#dash-error');
  error.hidden = true;
  if (!start || !end) {
    error.textContent = '请先选择开始日期与结束日期。';
    error.hidden = false;
    return;
  }
  if (start > end) {
    error.textContent = '开始日期不能晚于结束日期。';
    error.hidden = false;
    return;
  }
  const params = { start, end };
  if (storeId) params.store_id = storeId;
  $('#range-note').textContent =
    '当前口径区间：' + start + ' 至 ' + end + (storeId ? ' · 门店 ' + storeId : ' · 全部门店');

  const button = $('#refresh-btn');
  button.disabled = true;
  try {
    const [summary, daily, top] = await Promise.all([
      apiGet('/api/metrics/summary', params),
      apiGet('/api/metrics/daily', params),
      apiGet('/api/metrics/top_products', Object.assign({ limit: 10 }, params)),
    ]);
    renderSummary(summary);
    renderTrend(daily.days || []);
    renderTopProducts(top.products || []);
  } catch (err) {
    error.textContent = '取数失败：' + err.message;
    error.hidden = false;
  } finally {
    button.disabled = false;
  }
}

$('#refresh-btn').addEventListener('click', loadDashboard);
$('#store-select').addEventListener('change', loadDashboard);
$('#start-date').addEventListener('change', loadDashboard);
$('#end-date').addEventListener('change', loadDashboard);

/* ============================ 第三关：问答助手 ============================ */

function newSessionId() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  return 'sess-' + Date.now().toString(36) + '-' + Math.random().toString(16).slice(2, 10);
}

function startNewSession() {
  state.sessionId = newSessionId();
  $('#session-id').textContent = state.sessionId;
  const log = $('#chat-log');
  log.innerHTML = '';
  log.append(buildChatEmpty());
  bindExamples();
}

function buildChatEmpty() {
  const box = h(
    'div',
    { class: 'chat-empty' },
    h('p', { text: '问几句试试。可以只查数据库、只查公司文档，也可以两样一起问。' }),
    h('div', { class: 'examples', id: 'chat-examples' })
  );
  return box;
}

/** 示例问题从后端拿到的门店与商品名拼出来，换一份数据也不会出现查不到的名字。 */
function renderExamples() {
  const box = $('#chat-examples');
  if (!box || !state.catalog) return;
  const stores = state.catalog.stores || [];
  const products = state.catalog.products || [];
  const period = state.catalog.data_period || {};
  const questions = [];
  if (stores.length) {
    questions.push('最近 30 天 ' + stores[0].store_id + ' 的营业额是多少？');
  }
  if (products.length && period.start) {
    const month = Number(period.start.slice(5, 7));
    questions.push(products[0].product_name + ' 在 ' + month + ' 月卖了多少份？');
  }
  if (stores.length > 1) {
    questions.push('哪家门店的净营业额最高？');
  }
  questions.push('退款金额算不算进营业额？');
  box.innerHTML = '';
  for (const question of questions) {
    box.append(
      h('button', {
        type: 'button',
        class: 'example-chip',
        text: question,
        onclick: () => {
          $('#chat-input').value = question;
          $('#chat-form').requestSubmit();
        },
      })
    );
  }
}

function bindExamples() {
  renderExamples();
}

function citationBlock(citation) {
  return h(
    'div',
    { class: 'citation' },
    h('span', { class: 'doc', text: citation.doc_id || '（未给编号）' }),
    h('blockquote', { text: citation.quote || '' })
  );
}

function evidenceBlock(item, index) {
  const label = item.tool
    ? '工具调用 ' + (index + 1) + '：' + item.tool
    : 'SQL 查询 ' + (index + 1);
  const body = h('div', {});
  if (item.tool) {
    body.append(h('div', { class: 'muted', text: '参数' }), h('pre', { class: 'json', text: json(item.params) }));
  }
  if (item.sql) {
    body.append(h('div', { class: 'muted', text: 'SQL' }), h('pre', { class: 'json', text: item.sql }));
  }
  body.append(h('div', { class: 'muted', text: '结果' }), h('pre', { class: 'json', text: json(item.result) }));
  return h('details', { class: 'evidence' }, h('summary', { text: label }), body);
}

function answerNode(data, elapsedMs) {
  const [label, cls] = ANSWER_TYPES[data.answer_type] || ['未知', 'badge-neutral'];
  const citations = data.citations || [];
  const evidence = data.data_evidence || [];

  const meta = h(
    'div',
    { class: 'msg-meta' },
    h('span', { class: 'badge ' + cls, text: label }),
    h('span', { class: 'muted', text: (elapsedMs / 1000).toFixed(1) + ' 秒' }),
    data.trace_id
      ? h('button', {
          type: 'button',
          class: 'link-btn',
          text: '查看调试',
          title: '打开调试面板，加载 ' + data.trace_id,
          onclick: () => viewTrace(data.trace_id),
        })
      : null,
    data.trace_id ? h('span', { class: 'muted mono', text: data.trace_id }) : null
  );

  const bubble = h('div', { class: 'bubble' }, meta, h('div', { class: 'answer-text', text: data.answer || '' }));

  if (citations.length) {
    bubble.append(
      h('div', { class: 'muted', style: 'margin-top:10px', text: '文档引用（' + citations.length + ' 条）' }),
      h('div', { class: 'citations' }, citations.map(citationBlock))
    );
  }
  if (evidence.length) {
    bubble.append(
      h('div', { class: 'muted', style: 'margin-top:10px', text: '数据证据（' + evidence.length + ' 条）' }),
      h('div', {}, evidence.map(evidenceBlock))
    );
  }
  return h('div', { class: 'msg msg-ai' }, bubble);
}

function thinkingNode() {
  const hint = h('span', { class: 'thinking-hint', text: '模型思考 + 多轮查数，可能要几十秒…' });
  const node = h(
    'div',
    { class: 'msg msg-ai' },
    h(
      'div',
      { class: 'bubble' },
      h(
        'div',
        { class: 'thinking' },
        h('span', { class: 'dots' }, h('i'), h('i'), h('i')),
        h('span', { text: '思考中' }),
        h('span', { class: 'muted mono', text: '0.0 s' }),
        hint
      )
    )
  );
  return node;
}

async function askQuestion(question) {
  const log = $('#chat-log');
  const input = $('#chat-input');
  const send = $('#chat-send');
  const text = (question || '').trim();
  if (!text) return;

  if (!state.sessionId) startNewSession();
  const empty = $('.chat-empty', log);
  if (empty) empty.remove();

  log.append(h('div', { class: 'msg msg-user' }, h('div', { class: 'bubble', text })));
  const thinking = thinkingNode();
  log.append(thinking);
  log.scrollTop = log.scrollHeight;

  input.value = '';
  input.disabled = true;
  send.disabled = true;

  const started = performance.now();
  const clock = $('.mono', thinking);
  const ticker = setInterval(() => {
    const seconds = (performance.now() - started) / 1000;
    clock.textContent = seconds.toFixed(1) + ' s';
    // 等待超过 60 秒时把提示说得更明白一点，避免运营以为页面卡住了。
    if (seconds > 60) $('.thinking-hint', thinking).textContent = '模型仍在思考，请继续等待，不要刷新页面。';
  }, 200);

  try {
    const data = await apiPost('/api/chat', { session_id: state.sessionId, question: text });
    const elapsedMs = performance.now() - started;
    thinking.replaceWith(answerNode(data, elapsedMs));
    state.lastTraceId = data.trace_id || state.lastTraceId;
  } catch (err) {
    thinking.replaceWith(
      h(
        'div',
        { class: 'msg msg-ai' },
        h(
          'div',
          { class: 'bubble' },
          h('div', { class: 'badge badge-refusal', text: '请求失败' }),
          h('p', { style: 'margin-top:8px', text: '这次请求没有成功：' + err.message })
        )
      )
    );
  } finally {
    clearInterval(ticker);
    input.disabled = false;
    send.disabled = false;
    input.focus();
    log.scrollTop = log.scrollHeight;
  }
}

$('#chat-form').addEventListener('submit', (event) => {
  event.preventDefault();
  askQuestion($('#chat-input').value);
});
$('#chat-input').addEventListener('keydown', (event) => {
  // Enter 发送，Shift+Enter 换行。
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    $('#chat-form').requestSubmit();
  }
});
$('#new-session-btn').addEventListener('click', startNewSession);

/* ============================ 第四关：调试面板 ============================ */

const STEP_LABELS = {
  plan: '规划',
  search: '检索',
  tool: '工具调用',
  tool_arguments_invalid: '工具参数解析失败',
  answer_mock: '模板作答（未走模型）',
  answer_live: '模型作答',
  answer_live_failed: '模型作答失败',
  number_check_failed: '数字核对未通过',
  response: '返回结果',
};

const kvList = (pairs) =>
  h(
    'dl',
    { class: 'kv' },
    pairs
      .filter(([, value]) => value != null && value !== '' && !(Array.isArray(value) && !value.length))
      .flatMap(([label, value]) => [
        h('dt', { text: label }),
        h('dd', { text: Array.isArray(value) ? value.join('、') : String(value) }),
      ])
  );

function hitsTable(hits) {
  return h(
    'div',
    { class: 'table-scroll' },
    h(
      'table',
      {},
      h(
        'thead',
        {},
        h(
          'tr',
          {},
          h('th', { text: 'doc_id' }),
          h('th', { text: 'chunk_id' }),
          h('th', { class: 'num', text: '分数' }),
          h('th', { text: '标记' })
        )
      ),
      h(
        'tbody',
        {},
        hits.map((hit) =>
          h(
            'tr',
            {},
            h('td', { text: hit.doc_id }),
            h('td', { class: 'mono', text: hit.chunk_id }),
            h('td', { class: 'num', text: Number(hit.score).toFixed(4) }),
            h(
              'td',
              {},
              hit.padded ? h('span', { class: 'chip chip-warn', text: '凑数补齐，未用于作答' }) : null,
              (hit.dropped_instructions || []).length
                ? h('span', {
                    class: 'chip chip-bad',
                    text: '丢弃文档内指令 ' + hit.dropped_instructions.length + ' 处',
                  })
                : null,
              !hit.padded && !(hit.dropped_instructions || []).length
                ? h('span', { class: 'chip chip-ok', text: '用于作答' })
                : null
            )
          )
        )
      )
    )
  );
}

function filteredTable(filtered) {
  if (!filtered || !filtered.length) {
    return h('p', { class: 'muted', text: '没有被元数据过滤掉的文档。' });
  }
  return h(
    'div',
    { class: 'table-scroll' },
    h(
      'table',
      {},
      h('thead', {}, h('tr', {}, h('th', { text: 'doc_id' }), h('th', { text: '过滤原因' }))),
      h(
        'tbody',
        {},
        filtered.map((item) => h('tr', {}, h('td', { text: item.doc_id }), h('td', { text: item.reason })))
      )
    )
  );
}

function stepDetail(step) {
  const detail = step.detail;
  if (detail == null) return null;

  if (step.step === 'plan') {
    return kvList([
      ['改写后的检索查询', detail.search_query],
      ['完整问题（追问还原）', detail.standalone_question !== detail.question ? detail.standalone_question : null],
      ['原始问题', detail.question],
      ['意图', detail.intent],
      ['问法类型', detail.kind],
      ['时间窗', detail.window ? detail.window.join(' ~ ') : null],
      ['对比时间窗', detail.compare_window ? detail.compare_window.join(' ~ ') : null],
      ['数据基准日 as_of', detail.as_of],
      ['门店', detail.store_id],
      ['商品', detail.product_id],
      ['指标', detail.metric],
      ['需要数据库', detail.needs_data ? '是' : '否'],
      ['需要文档', detail.needs_docs ? '是' : '否'],
      ['拒答原因', detail.refusal],
      ['规划备注', detail.notes],
    ]);
  }

  if (step.step === 'search') {
    return h(
      'div',
      {},
      kvList([
        ['检索用查询', detail.query],
        ['别名扩写', detail.expansions],
        ['覆盖率', detail.coverage],
      ]),
      h('div', { class: 'muted', style: 'margin-top:8px', text: '命中片段（' + (detail.hits || []).length + ' 条）' }),
      hitsTable(detail.hits || []),
      h(
        'div',
        { class: 'muted', style: 'margin-top:12px', text: '被过滤的文档（' + (detail.filtered || []).length + ' 篇）' }
      ),
      filteredTable(detail.filtered)
    );
  }

  if (step.step === 'tool') {
    return h(
      'div',
      {},
      kvList([['工具', detail.tool], ['参数', detail.params ? JSON.stringify(detail.params) : null]]),
      detail.result != null
        ? jsonDetails('工具返回结果', detail.result, false)
        : h('p', { class: 'muted', text: '这一步没有记录结果。' })
    );
  }

  return jsonDetails('步骤详情', detail, false);
}

function renderSteps(steps, totalMs) {
  if (!steps.length) return h('p', { class: 'muted', text: '这次回答没有记录步骤。' });
  const slowest = Math.max(1, ...steps.map((s) => s.took_ms || 0));
  return h(
    'div',
    { class: 'timeline' },
    steps.map((step) => {
      const body = stepDetail(step);
      const took = step.took_ms;
      return h(
        'div',
        { class: 'step' },
        h(
          'div',
          { class: 'step-head' },
          h('span', { class: 'step-name', text: STEP_LABELS[step.step] || step.step }),
          h('span', { class: 'mono muted', text: step.step }),
          h('span', { class: 'step-at', text: '开始于 ' + ms(step.at_ms) }),
          h('span', { class: 'step-took', text: '耗时 ' + ms(took) })
        ),
        took
          ? h(
              'div',
              { class: 'step-bar' },
              h('span', { style: 'width:' + Math.max(2, (took / slowest) * 100).toFixed(1) + '%' })
            )
          : null,
        body ? h('div', { class: 'step-body' }, body) : null
      );
    })
  );
}

function renderLlmCall(call, index) {
  const bad = Boolean(call.error) || (call.finish_reason && !['stop', 'tool_calls'].includes(call.finish_reason));
  const head = h(
    'div',
    { class: 'step-head' },
    h('span', { class: 'step-name', text: '第 ' + (index + 1) + ' 次模型调用' }),
    h('span', { class: 'mono muted', text: call.model || '' }),
    h('span', { class: 'step-took', text: '耗时 ' + ms(call.took_ms) })
  );

  const chips = h(
    'div',
    { style: 'margin-top:8px' },
    call.status ? h('span', { class: `chip ${call.status === 200 ? 'chip-ok' : 'chip-bad'}`, text: 'HTTP ' + call.status }) : null,
    call.finish_reason ? h('span', { class: `chip ${bad ? 'chip-bad' : 'chip-ok'}`, text: 'finish_reason=' + call.finish_reason }) : null,
    call.has_reasoning ? h('span', { class: 'chip', text: '带思考过程' }) : null,
    (call.tool_calls || []).length
      ? h('span', { class: 'chip', text: '工具调用：' + call.tool_calls.join('、') })
      : null,
    call.usage ? h('span', { class: 'chip', text: 'tokens ' + (call.usage.total_tokens || '?') }) : null,
    call.error ? h('span', { class: 'chip chip-bad', text: '错误：' + call.error }) : null
  );

  const blocks = [head, chips];
  if (call.endpoint) blocks.push(h('p', { class: 'muted mono', text: 'POST ' + call.endpoint }));
  if (call.detail) blocks.push(h('p', { class: 'muted', text: call.detail }));

  // 提示词原文：能解析成消息数组就按角色分段显示，否则原样贴出来。
  let messages = null;
  if (typeof call.prompt === 'string') {
    try {
      const parsed = JSON.parse(call.prompt);
      if (Array.isArray(parsed)) messages = parsed;
    } catch (err) {
      messages = null;
    }
  }
  if (messages) {
    blocks.push(
      h(
        'details',
        { class: 'evidence', open: 'open' },
        h('summary', { text: '发给模型的完整提示词（' + messages.length + ' 条消息）' }),
        h(
          'div',
          {},
          messages.map((message) => {
            const text = typeof message.content === 'string' ? message.content : json(message.content);
            return h(
              'div',
              { class: 'msg-block' },
              h('div', { class: 'role', text: message.role + (message.tool_calls ? ' · 工具调用' : '') }),
              text ? h('pre', { class: 'json', text }) : null,
              message.reasoning_content
                ? jsonDetails('思考过程（仅调试可见）', message.reasoning_content, false)
                : null,
              message.tool_calls ? h('pre', { class: 'json', text: json(message.tool_calls) }) : null
            );
          })
        )
      )
    );
  } else if (call.prompt) {
    blocks.push(jsonDetails('发给模型的完整提示词', call.prompt, true));
  }

  if (call.raw_content != null) {
    blocks.push(
      h('div', { class: 'msg-block' }, h('div', { class: 'role', text: '模型原始输出' }),
        h('pre', { class: 'json', text: call.raw_content || '（空）' }))
    );
  }
  if (call.raw_reasoning) {
    blocks.push(jsonDetails('模型的思考过程（只在这里可见，不进 answer）', call.raw_reasoning, false));
  }
  return h('div', { class: 'llm-call' + (bad ? ' is-error' : '') }, blocks);
}

function renderTrace(data) {
  const body = $('#trace-body');
  body.innerHTML = '';

  const steps = data.steps || [];
  const errors = data.errors || [];
  const llmCalls = data.llm_calls || [];
  const stepMs = steps.reduce((sum, step) => sum + (step.took_ms || 0), 0);

  body.append(
    h(
      'div',
      { class: 'card' },
      h('div', { class: 'card-head' }, h('h2', { text: '本次回答' })),
      kvList([
        ['trace_id', data.trace_id],
        ['session_id', data.session_id],
        ['问题', data.question],
        ['开始时间', data.started_at],
        ['总耗时', ms(data.total_ms)],
        ['步骤耗时合计', ms(stepMs) + '（步骤可嵌套，可能大于总耗时）'],
        ['步骤数', steps.length],
        ['模型调用次数', llmCalls.length],
        ['错误数', errors.length],
      ])
    )
  );

  if (errors.length) {
    const box = h(
      'div',
      { class: 'card' },
      h('div', { class: 'card-head' }, h('h2', { text: '错误（' + errors.length + ' 条）' }))
    );
    for (const error of errors) {
      box.append(
        h(
          'div',
          { class: 'error-box' },
          h('span', { class: 'err-where', text: error.where }),
          h('span', { class: 'err-type', text: error.type }),
          h('p', { style: 'margin-top:6px', text: error.message }),
          error.traceback ? jsonDetails('堆栈', error.traceback, false) : null
        )
      );
    }
    body.append(box);
  }

  body.append(
    h(
      'div',
      { class: 'card' },
      h('div', { class: 'card-head' }, h('h2', { text: '处理过程' })),
      renderSteps(steps, data.total_ms)
    )
  );

  const llmCard = h(
    'div',
    { class: 'card' },
    h(
      'div',
      { class: 'card-head' },
      h('h2', { text: '大模型调用（' + llmCalls.length + ' 次）' }),
      h('span', {
        class: 'muted',
        text: llmCalls.length ? '提示词与原始输出原文' : '',
      })
    )
  );
  if (llmCalls.length) {
    llmCalls.forEach((call, index) => llmCard.append(renderLlmCall(call, index)));
  } else {
    llmCard.append(
      h('p', {
        class: 'muted',
        text: '这次回答没有调用模型（未配置 Key 时走模板路径）。配置模型后这里会显示每一次请求的提示词与原始输出。',
      })
    );
  }
  body.append(llmCard);
}

async function loadTrace(traceId) {
  const id = (traceId || '').trim();
  const status = $('#trace-status');
  if (!id) {
    status.textContent = '请输入 trace_id。';
    return;
  }
  $('#trace-input').value = id;
  status.textContent = '加载中…';
  try {
    const data = await apiGet('/api/trace/' + encodeURIComponent(id));
    renderTrace(data);
    status.textContent = '已加载 ' + id;
  } catch (err) {
    status.textContent = '加载失败';
    $('#trace-body').innerHTML = '';
    $('#trace-body').append(
      h(
        'div',
        { class: 'card empty-card' },
        h('p', { text: '取不到这个 trace：' + err.message }),
        h('p', {
          class: 'muted',
          text: 'trace 只保留最近若干次回答，服务重启后会清空；请用刚问过的那一条。',
        })
      )
    );
  }
}

/** 从问答面板跳过来：把 trace_id 填好并立刻加载。 */
function viewTrace(traceId) {
  state.lastTraceId = traceId;
  activateTab('debug');
  history.replaceState(null, '', '#debug/' + encodeURIComponent(traceId));
  loadTrace(traceId);
}

$('#trace-load-btn').addEventListener('click', () => loadTrace($('#trace-input').value));
$('#trace-reload-btn').addEventListener('click', () => loadTrace($('#trace-input').value));
$('#trace-input').addEventListener('keydown', (event) => {
  if (event.key === 'Enter') loadTrace($('#trace-input').value);
});

/* ---------- 检索试跑 ---------- */

async function runRetrieve() {
  const query = $('#retrieve-query').value.trim();
  const topK = Number($('#retrieve-topk').value) || 5;
  const box = $('#retrieve-body');
  box.innerHTML = '';
  if (!query) {
    box.append(h('p', { class: 'muted', text: '先写一句要检索的话。' }));
    return;
  }
  box.append(h('p', { class: 'muted', text: '检索中…' }));
  try {
    const data = await apiPost('/api/retrieve', { query, top_k: topK });
    const results = data.results || [];
    box.innerHTML = '';
    box.append(
      h(
        'div',
        { class: 'table-scroll' },
        h(
          'table',
          {},
          h(
            'thead',
            {},
            h('tr', {}, h('th', { class: 'num', text: '#' }), h('th', { text: 'doc_id' }), h('th', { text: 'chunk_id' }), h('th', { class: 'num', text: '分数' }))
          ),
          h(
            'tbody',
            {},
            results.map((item, index) =>
              h(
                'tr',
                {},
                h('td', { class: 'num rank', text: String(index + 1) }),
                h('td', { text: item.doc_id }),
                h('td', { class: 'mono', text: item.chunk_id }),
                h('td', { class: 'num', text: Number(item.score).toFixed(4) })
              )
            )
          )
        )
      ),
      h('div', { style: 'margin-top:12px' }, results.map((item) =>
        jsonDetails(item.doc_id + ' · ' + item.chunk_id, item.text, false)
      ))
    );
  } catch (err) {
    box.innerHTML = '';
    box.append(h('p', { class: 'muted', text: '检索失败：' + err.message }));
  }
}

$('#retrieve-btn').addEventListener('click', runRetrieve);
$('#retrieve-query').addEventListener('keydown', (event) => {
  if (event.key === 'Enter') runRetrieve();
});

$('#llm-toggle').addEventListener('click', toggleMode);

/* ============================ 启动 ============================ */

async function boot() {
  startNewSession();
  try {
    await loadCatalog();
  } catch (err) {
    const error = $('#dash-error');
    error.textContent = '拿不到门店与商品清单（/api/catalog）：' + err.message;
    error.hidden = false;
  }
  loadHealth();
  loadDashboard();
  apiGet('/api/data_quality')
    .then(renderQuality)
    .catch((err) => {
      $('#quality-body').append(h('p', { class: 'muted', text: '数据质量加载失败：' + err.message }));
    });
  applyHash();
}

boot();
