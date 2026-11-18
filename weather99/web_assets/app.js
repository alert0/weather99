'use strict';

const $ = id => document.getElementById(id);
const esc = value => String(value ?? '—').replace(/[&<>"']/g, char => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[char]));
const numeric = value => typeof value === 'number' && Number.isFinite(value);
const number = (value, digits = 2) => numeric(value) ? value.toFixed(digits) : '—';
const percent = value => !numeric(value) ? '—' : value > 0 && value < .001 ? '＜0.1%' : `${(value * 100).toFixed(1)}%`;
const dateFormatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
});
function time(value) {
  const parsed = new Date(value);
  return value && Number.isFinite(parsed.getTime()) ? dateFormatter.format(parsed) : '暂无';
}
const label = value => (value ?? '—').replace(' or below', '及以下').replace(' or higher', '及以上');
const statuses = {
  rejected: '未入场 · 条件未满足', blocked: '数据检查未通过', paper_filled: '已模拟买入',
  pending_confirmation: '等待连续确认', confirmed_research_signal: '信号已确认，尚未成交',
  error: '采集异常',
};
const models = {
  ecmwf_ifs025: ['ECMWF', '欧洲中期天气预报中心'],
  gfs_global: ['GFS', '美国全球预报系统'],
  cma_grapes_global: ['CMA', '中国气象局全球模式'],
  jma_seamless: ['JMA', '日本气象厅模式'],
};
let data = null;
let busy = false;
let refreshTimer;
let dateOptions = '';

function selectedScan() {
  return data?.scans.find(scan => scan.date === $('date-select').value);
}

function renderDecision(scan) {
  if (!scan) {
    $('decision').innerHTML = '<div class="empty">本轮尚未完成评估，等待采集记录。</div>';
    return;
  }
  const market = scan.quotes.find(quote => quote.market_mode);
  const details = scan.details.map(detail => detail.trim());
  const conclusion = details.find(detail => detail.startsWith('整体结论：'))?.replace('整体结论：', '')
    || statuses[scan.status] || '暂无完整结论';
  const checks = details.filter(detail => detail.startsWith('未通过：') || detail.startsWith('模拟成交检查：'));
  const stale = scan.collected_at && Date.now() - new Date(scan.collected_at).getTime()
    > Math.max((data.interval_seconds || 180) * 2, 600) * 1000;
  const probability = scan.probability == null ? '暂无概率记录'
    : `<b>${percent(scan.probability)}</b> / 入场门槛 ${percent(scan.probability_threshold)}`;
  $('decision').innerHTML = `
    <div class="decision-body">
      <div class="decision-topline"><span class="market-day">${esc(scan.date)} · ${esc(scan.layer)} 时间层</span>
        <span class="result-tag${scan.status === 'paper_filled' ? ' success' : ''}">${esc(statuses[scan.status] || '等待评估')}</span></div>
      <div class="forecast-compare">
        <div class="forecast-value"><span>模型最高温预测中心</span><strong>${number(scan.forecast?.center_c, 1)} <small>℃</small></strong></div>
        <span class="versus" aria-hidden="true">VS</span>
        <div class="forecast-value market"><span>市场最看好的温度档</span><strong>${esc(label(market?.label))}</strong></div>
      </div>
      <div class="decision-conclusion">${esc(conclusion)}</div>
      <p class="candidate-label">${scan.candidate_valid ? '通过条件的候选组合' : '被拒候选示例 · 按模型净优势排序首位，并非买入建议'}</p>
      <div class="candidate-line"><strong>${esc(scan.candidate_labels.map(label).join(' + ') || '暂无候选组合')}</strong>
        <span class="probability-text">${probability}</span></div>
      <ul class="checks">${checks.map(detail => `<li>${esc(detail.replace('未通过：', ''))}</li>`).join('')}</ul>
      <div class="decision-footer"><span>${stale ? '旧记录 · ' : ''}评估数据 ${esc(time(scan.collected_at))}</span>
        <span>连续确认 ${scan.confirmation_count ?? '—'} / ${scan.required_confirmations ?? '—'}</span></div>
    </div>`;
}

function probabilityCell(value, market, name) {
  if (!numeric(value)) return '—';
  return `<div class="prob-cell"><meter class="${market ? 'market-meter' : ''}" min="0" max="1" value="${Math.max(0, Math.min(1, value))}"
    aria-label="${esc(name)}${market ? '市场隐含概率' : '天气概率估计'}">${percent(value)}</meter><span>${percent(value)}</span></div>`;
}

function renderMarket() {
  const scan = selectedScan();
  renderDecision(scan);
  renderRealtime(scan);
  if (!scan) {
    $('model-values').innerHTML = '';
    $('quote-rows').innerHTML = '<tr><td colspan="6" class="empty">暂无盘口记录</td></tr>';
    $('quote-time').textContent = '尚无完整采集';
    return;
  }
  $('quote-time').textContent = `${scan.date} 市场 · 采集 ${time(scan.collected_at)}`;
  $('model-values').innerHTML = Object.entries(models).map(([key, [name, description]]) => `
    <div class="model-tile"><span>${name}</span><strong>${number(scan.models[key], 1)} ℃</strong><small>${description}</small></div>`).join('');
  $('quote-rows').innerHTML = scan.quotes.length ? scan.quotes.map(quote => {
    const name = label(quote.label);
    return `<tr${quote.market_mode ? ' class="hot"' : ''}>
      <th scope="row">${esc(name)}</th>
      <td data-label="天气概率估计">${probabilityCell(quote.weather_probability, false, name)}</td>
      <td data-label="市场隐含概率">${probabilityCell(quote.market_probability, true, name)}</td>
      <td data-label="买一价">${quote.bid == null ? '<span class="no-quote">无报价</span>' : number(quote.bid, 3)}</td>
      <td data-label="卖一价">${quote.ask == null ? '<span class="no-quote">无报价</span>' : number(quote.ask, 3)}</td>
      <td data-label="档位标记">${quote.market_mode ? '<span class="tag">市场热门</span>' : ''}${quote.candidate ? '<span class="tag neutral">展示候选</span>' : ''}${!quote.market_mode && !quote.candidate ? '—' : ''}</td>
    </tr>`;
  }).join('') : '<tr><td colspan="6" class="empty">该日期暂无盘口记录</td></tr>';
}

function drawChart(history) {
  const points = history.filter(point => numeric(point.equity) && Number.isFinite(Date.parse(point.time)));
  if (!points.length) {
    $('chart').innerHTML = '<div class="empty">尚无账户采集记录</div>';
    return;
  }
  const width = 1000, height = 180, left = 48, right = 15, top = 12, bottom = 27;
  const values = points.map(point => point.equity);
  const padding = Math.max((Math.max(...values) - Math.min(...values)) * .2, .5);
  const min = Math.min(...values) - padding, max = Math.max(...values) + padding;
  const start = Date.parse(points[0].time), end = Date.parse(points.at(-1).time);
  const x = point => left + (width - left - right) * (Date.parse(point.time) - start) / Math.max(end - start, 1);
  const y = value => top + (height - top - bottom) * (max - value) / (max - min);
  const coordinates = points.map(point => `${x(point).toFixed(2)},${y(point.equity).toFixed(2)}`).join(' ');
  const grid = Array.from({length: 4}, (_, index) => {
    const value = min + (max - min) * index / 3;
    return `<line class="grid" x1="${left}" y1="${y(value)}" x2="${width - right}" y2="${y(value)}"/><text x="0" y="${y(value) + 3}">${value.toFixed(2)}</text>`;
  }).join('');
  $('chart').innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="模拟账户权益曲线，最新 ${number(values.at(-1))} 美元">
    <polygon class="area" points="${left},${height - bottom} ${coordinates} ${x(points.at(-1))},${height - bottom}"/>
    ${grid}<polyline class="curve" points="${coordinates}"/>
    <text x="${left}" y="${height - 3}">${esc(time(points[0].time))}</text>
    <text text-anchor="end" x="${width - right}" y="${height - 3}">${esc(time(points.at(-1).time))}</text></svg>`;
}

function renderRealtime(scan) {
  const statusText = {fresh: '有效', stale: '已过期', future: '时间超前', missing: '暂无数据', incomplete: '五点缺失或时间不一致', source_error: '采集失败', invalid_time: '时间无效'};
  $('realtime-sources').innerHTML = ['metar', 'qweather'].map(key => {
    const row = scan?.realtime?.[key] || {};
    return `<div class="model-tile"><span>${key === 'metar' ? 'METAR · ZSPD 实测' : '和风 · 五点当前均温'}</span><strong>${number(row.temperature_c, 1)} ℃</strong>
      <p>${esc(statusText[row.status] || '暂无数据')} · 有效时间 ${esc(time(row.observed_at))}</p>
      <p>同刻模型 ${number(row.model_current_c, 1)} ℃ · 偏差 ${number(row.residual_c, 2)} ℃</p>
      ${key === 'metar' ? `<p>当日已观测最高 ${number(row.observed_max_c, 1)} ℃</p>` : ''}</div>`;
  }).join('');
  const correction = scan?.correction;
  $('correction-summary').textContent = correction?.status === 'applied'
    ? `当日校正已参与概率计算：${number(correction.before_c, 2)} → ${number(correction.after_c, 2)} ℃（调整 ${number(correction.adjustment_c, 2)} ℃）。偏差权重 METAR 75% / 和风 25%，增益 50%，偏差调整限幅 ±2 ℃，预测中心不低于当日已观测最高；误差尺度保持原值。`
    : correction?.status === 'not_target_day' ? '该市场不是今天：当前观测仅供参考，不校正其他日期。'
    : correction ? '未校正：缺少新鲜且与四模型小时预报对齐的观测。'
    : '该轮没有校正记录；显示原始观测供核对，等待新一轮采集评估。';
  const shadow = scan?.hourly_shadow;
  $('hourly-shadow-summary').textContent = shadow?.status === 'shadow_ready'
    ? `ZSPD 已观测最高 ${number(shadow.observed_high_c, 1)} ℃ · 四模型修正后剩余峰值 ${number(shadow.predicted_remaining_peak_c, 2)} ℃ · 最终最高温参考预测 ${number(shadow.predicted_final_high_c, 2)} ℃。和风网格${shadow.qweather_used ? '已辅助' : '未参与'}；该结果不参与 Paper 下单。`
    : shadow ? `Shadow 暂不计算：${shadow.status}。缺少必要的新鲜机场观测或完整小时曲线，不进行插值补造。`
    : '本轮暂无逐小时 Shadow 计算记录。';
  $('hourly-shadow-rows').innerHTML = shadow?.status === 'shadow_ready' && shadow.hours?.length
    ? shadow.hours.map(hour => `<tr><td>${esc(time(hour.valid_at))}</td><td>${number(hour.model_mean_c, 2)} ℃</td><td>${number(hour.corrected_mean_c, 2)} ℃</td><td>${number(hour.shift_c, 2)} ℃</td></tr>`).join('')
    : '<tr><td colspan="4" class="empty">暂无可用的当日未来小时预报</td></tr>';
}

function renderWeather(weather) {
  const daily = weather.daily_forecast || {};
  $('daily-time').textContent = `预报采集 ${time(daily.collected_at)}${daily.complete === false ? ' · 预报不可用' : ''}`;
  $('daily-rows').innerHTML = daily.days?.length ? daily.days.map(day => `<tr>
    <th scope="row">${esc(day.date)}</th><td>${number(day.max_c, 1)} / ${number(day.min_c, 1)} ℃</td>
    <td>${esc(day.day_condition || '—')} / ${esc(day.night_condition || '—')}</td>
    <td>${esc(time(day.start_at))} — ${esc(time(day.end_at))}</td></tr>`).join('')
    : '<tr><td colspan="4" class="empty">尚无和风单点预报，等待采集；请求失败时保留缺失状态。</td></tr>';
  $('weather-attributions').textContent = `数据来源：和风天气 QWeather。${(weather.attributions || []).join(' · ')}`;
  const names = {center: '中心参考点', north: '北侧采样点', south: '南侧采样点', east: '东侧采样点', west: '西侧采样点'};
  const directions = {center: 'C', north: 'N', south: 'S', east: 'E', west: 'W'};
  $('weather-time').textContent = `最近采集 ${time(weather.collected_at)}`;
  $('weather-points').innerHTML = weather.points.length ? weather.points.map(point => `
    <article class="panel weather-point"><div class="point-head"><h3>${esc(names[point.id] || point.name)}</h3><span class="point-id">${esc(directions[point.id] || '')}</span></div>
      <div class="point-temp">${number(point.temperature, 1)} <small>℃</small></div>
      <p class="point-condition">${point.complete ? '温度已采集' : '温度暂不可用'}</p>
      <p class="point-time">有效时间 ${esc(time(point.observed_at))}<br>${number(point.latitude, 4)} N · ${number(point.longitude, 4)} E</p>
    </article>`).join('') : '<div class="panel empty">尚无和风天气采集记录</div>';
}

function renderLogs() {
  if (!data) return;
  const filter = $('log-filter').value;
  const logs = data.logs.filter(row => filter === 'all' || row.status === filter || (filter === 'blocked' && row.status === 'error'));
  $('log-rows').innerHTML = logs.length ? logs.map(row => `<tr>
    <th scope="row">${esc(time(row.time))}</th><td>${esc(row.date)}</td>
    <td>${esc(statuses[row.status] || '已记录')}</td><td>${esc(row.message)}</td></tr>`).join('')
    : '<tr><td colspan="4" class="empty">没有符合筛选条件的记录</td></tr>';
}

function render(result) {
  data = result;
  const account = data.account || {};
  $('health').textContent = data.healthy ? '采集进程正常' : data.phase === 'not_started' ? '尚未启动' : '采集进程离线';
  $('health').className = `status-pill${data.healthy ? '' : ' warn'}`;
  $('clock').textContent = time(data.server_time);
  $('phase').textContent = ({waiting: '等待下一轮', collecting: '采集评估中', stopped: '已停止', not_started: '未启动'})[data.phase] || '未知';
  const runtime = [
    ['采集周期', data.interval_seconds ? `${data.interval_seconds / 60} 分钟` : '暂无'],
    ['最近心跳', time(data.heartbeat)],
    ['下轮计划', data.phase === 'waiting' ? time(data.next_scan_at) : '—'],
    ['天气采样', `${data.weather.points.filter(point => point.complete).length} / 5 点完整`],
    ['模型状态', data.model_mode === 'equal_weight_baseline' ? '四模型等权基线' : data.model_mode === 'trained' ? '历史训练模型' : '暂无'],
    ['交易执行', '本地模拟账本'],
  ];
  $('runtime').innerHTML = runtime.map(([key, value]) => `<dt>${key}</dt><dd>${esc(value)}</dd>`).join('');
  $('model-note').textContent = data.model_mode === 'trained'
    ? '使用历史训练模型制品。天气概率仍为模型估计。'
    : '四模型等权基线，尚未做历史偏差校准。天气概率仅为模型估计。';
  const signature = data.scans.map(scan => scan.date).join(',');
  if (signature !== dateOptions) {
    const previous = $('date-select').value;
    $('date-select').replaceChildren(...data.scans.map(scan => new Option(scan.date, scan.date)));
    if (data.scans.some(scan => scan.date === previous)) $('date-select').value = previous;
    dateOptions = signature;
  }
  $('date-select').disabled = !data.scans.length;
  renderMarket();
  renderWeather(data.weather);
  $('equity').textContent = number(account.equity_usd);
  $('cash').textContent = number(account.cash_usd);
  $('pnl').textContent = numeric(account.daily_realized_usd)
    ? `${account.daily_realized_usd >= 0 ? '+' : ''}${number(account.daily_realized_usd)}` : '—';
  $('positions-count').textContent = account.open_positions ?? '—';
  $('return').textContent = account.initial_cash_usd && numeric(account.equity_usd)
    ? `初始 $${number(account.initial_cash_usd)} · 累计 ${percent((account.equity_usd - account.initial_cash_usd) / account.initial_cash_usd)}` : '暂无账户数据';
  $('valuation').textContent = account.valuation_complete ? '持仓估值完整'
    : account.valuation_complete === false ? '报价不完整，保守估值' : '等待估值状态';
  drawChart(data.history);
  $('position-rows').innerHTML = data.positions.length ? data.positions.map(position => `<tr>
    <th scope="row">${esc(position.target_date)}</th><td>${esc(time(position.opened_at))}</td>
    <td>${position.shares}</td><td>${position.bucket_count}</td><td>${number(position.cost)}</td>
    <td>${number(position.reserve)}</td><td>${number(position.payout)}</td><td>${position.status === 'open' ? '未结算' : '已结算'}</td></tr>`).join('')
    : '<tr><td colspan="8" class="empty">暂无模拟持仓。组合满足入场条件并通过连续确认后，记录会显示在这里。</td></tr>';
  renderLogs();
}

async function refresh(manual = false) {
  if (busy || (document.hidden && !manual)) return;
  clearTimeout(refreshTimer);
  busy = true;
  $('refresh').disabled = true;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 12000);
  try {
    const response = await fetch('/api/dashboard', {cache: 'no-store', signal: controller.signal});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    render(await response.json());
    $('error').hidden = true;
    // Do not announce every background poll to screen readers.
    $('updated').textContent = `最近读取成功 ${time(data.server_time)} · 每 10 秒更新`;
    if (manual) $('refresh-status').textContent = `数据已刷新，${time(data.server_time)}`;
    $('updated').dataset.loaded = 'true';
  } catch (error) {
    $('error').textContent = `本地数据更新失败（${error.name === 'AbortError' ? '请求超时' : error.message}）。${data ? '下方为上次成功读取的记录，请查看各区数据时间。' : '请确认网页服务已经启动。'}`;
    $('error').hidden = false;
    $('health').textContent = '页面更新失败';
    $('health').className = 'status-pill warn';
  } finally {
    clearTimeout(timeout);
    busy = false;
    $('refresh').disabled = false;
    if (!document.hidden) refreshTimer = setTimeout(refresh, 10000);
  }
}

$('date-select').addEventListener('change', renderMarket);
$('log-filter').addEventListener('change', renderLogs);
$('refresh').addEventListener('click', () => refresh(true));
document.addEventListener('visibilitychange', () => {
  clearTimeout(refreshTimer);
  if (!document.hidden) refresh();
});
const links = [...document.querySelectorAll('.sidebar nav a')];
function markNavigation(hash) {
  for (const link of links) {
    if (link.hash === hash) link.setAttribute('aria-current', 'location');
    else link.removeAttribute('aria-current');
  }
}
for (const link of links) link.addEventListener('click', () => markNavigation(link.hash));
window.addEventListener('hashchange', () => markNavigation(location.hash || '#overview'));
markNavigation(location.hash || '#overview');
refresh();
