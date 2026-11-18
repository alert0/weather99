(() => {
  'use strict';

  const $ = id => document.getElementById(id);
  const canvas = $('hourly-chart');
  const context = canvas.getContext('2d');
  const slider = $('hourly-hour');
  const oneHour = 3600000;
  const colors = {original: '#3473d9', adjusted: '#00786a', observation: '#b85d24'};
  const hourlyReasons = {
    not_target_day: '逐小时校正仅在目标日当天计算。',
    verified_station_high_missing: '缺少已核验的机场最高温，校正暂不可用。',
    model_not_available_at_snapshot: '采集时基准模型尚不可用，校正暂不可用。',
    target_day_hourly_path_incomplete: '四模型逐小时数据不完整，校正暂不可用。',
    future_hourly_model_missing: '未来小时数据不完整，校正暂不可用。',
    future_hourly_path_missing: '目标日已无剩余整点，校正暂不可用。',
    metar_unavailable_or_unaligned: '缺少新鲜且能与小时预报对齐的 METAR，校正暂不可用。',
    station_high_inconsistent: '机场最高温与最新实测不一致，校正暂不可用。',
    shadow_computation_failed: '本轮逐小时校正计算失败。',
  };
  function minuteLabel(minutes) {
    return `${String(Math.floor(minutes / 60)).padStart(2, '0')}:${String(minutes % 60).padStart(2, '0')}`;
  }
  const hourLabel = hour => minuteLabel(hour * 60);
  const temperature = value => Number.isFinite(value) ? `${value.toFixed(1)} °C` : '—';
  const clock = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  });
  let selectedDate;
  let original = [];
  let adjusted = [];
  let observed = [];

  function draw() {
    if (!context || canvas.clientWidth === 0) return;
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    const scale = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * scale);
    canvas.height = Math.round(height * scale);
    context.setTransform(scale, 0, 0, scale, 0, 0);
    const left = 41, right = 20, top = 18, bottom = height - 32;
    const x = hour => left + (width - left - right) * hour / 24;
    const values = [...original, ...adjusted, ...observed].map(point => point.value);
    const low = values.length ? Math.min(...values) - 1 : 0;
    const high = values.length ? Math.max(...values) + 1 : 4;
    const y = value => bottom - (bottom - top) * (value - low) / (high - low);

    context.font = '11px "Segoe UI", sans-serif';
    context.lineWidth = 1;
    context.fillStyle = '#53677e';
    context.strokeStyle = '#dce6f0';
    for (let row = 0; row <= 4; row += 1) {
      const value = low + (high - low) * row / 4;
      const position = y(value);
      context.beginPath();
      context.moveTo(left, position);
      context.lineTo(width - right, position);
      context.stroke();
      if (values.length) {
        context.textAlign = 'right';
        context.fillText(value.toFixed(1), left - 9, position + 4);
      }
    }
    context.textAlign = 'center';
    const ticks = width < 460 ? [0, 6, 12, 18, 23] : [0, 4, 8, 12, 16, 20, 23];
    for (const hour of ticks) context.fillText(hourLabel(hour), x(hour), height - 9);
    if (!values.length) return;

    const selected = Number(slider.value);
    context.strokeStyle = '#8ba0b8';
    context.setLineDash([3, 4]);
    context.beginPath();
    context.moveTo(x(selected), top);
    context.lineTo(x(selected), bottom);
    context.stroke();

    function pointMark(point, color, radius) {
      context.beginPath();
      context.arc(x(point.hour), y(point.value), radius, 0, Math.PI * 2);
      context.fillStyle = color;
      context.fill();
    }

    function series(points, color, dashes) {
      context.strokeStyle = color;
      context.lineWidth = 2.5;
      context.setLineDash(dashes);
      context.beginPath();
      points.forEach((point, index) => {
        if (index === 0 || point.hour !== points[index - 1].hour + 1) {
          context.moveTo(x(point.hour), y(point.value));
        } else {
          context.lineTo(x(point.hour), y(point.value));
        }
      });
      context.stroke();
      points.forEach(point => pointMark(point, color, point.hour === selected ? 4.5 : 2));
    }

    series(original, colors.original, []);
    series(adjusted, colors.adjusted, [6, 4]);
    observed.forEach(point => pointMark(point, colors.observation, 3.5));
  }

  function selectHour() {
    const hour = Number(slider.value);
    const label = slider.disabled ? '—' : hourLabel(hour);
    slider.setAttribute('aria-valuetext', label);
    $('hourly-selected').textContent = label;
    $('hourly-inspection-title').textContent = slider.disabled ? '所选小时' : `${label} · 小时预报`;
    const originalValue = temperature(original.find(point => point.hour === hour)?.value);
    const adjustedValue = temperature(adjusted.find(point => point.hour === hour)?.value);
    $('hourly-original-value').textContent = originalValue;
    $('hourly-adjusted-value').textContent = adjustedValue;
    $('hourly-inline-values').textContent = `原始 ${originalValue} · 校正后 ${adjustedValue}`;
    draw();
  }

  function renderRows() {
    const rows = new Map();
    for (const [key, points] of [['original', original], ['adjusted', adjusted], ['observed', observed]]) {
      for (const {minute, value} of points) {
        if (!rows.has(minute)) rows.set(minute, {});
        rows.get(minute)[key] = value;
      }
    }
    const elements = [...rows].sort(([left], [right]) => left - right).map(([minute, row]) => {
      const element = document.createElement('tr');
      const heading = document.createElement('th');
      heading.scope = 'row';
      heading.textContent = minuteLabel(minute);
      element.append(heading);
      for (const value of [row.original, row.adjusted, row.observed]) {
        const cell = element.insertCell();
        cell.textContent = temperature(value);
      }
      return element;
    });
    if (!elements.length) {
      const row = document.createElement('tr');
      const cell = row.insertCell();
      cell.colSpan = 4;
      cell.className = 'empty';
      cell.textContent = '暂无记录';
      elements.push(row);
    }
    $('hourly-rows').replaceChildren(...elements);
  }

  window.renderHourly = function (scan) {
    const dayStart = Date.parse(`${scan?.date}T00:00:00+08:00`);
    const shadow = scan?.hourly_shadow;
    function points(rows, timeKey, valueKey, limit) {
      return (rows || []).map(row => {
        const elapsed = Date.parse(row[timeKey]) - dayStart;
        return {hour: elapsed / oneHour, minute: Math.floor(elapsed / 60000), value: row[valueKey]};
      }).filter(point => point.hour >= 0 && point.hour < limit && Number.isFinite(point.value))
        .sort((left, right) => left.hour - right.hour);
    }

    original = points(scan?.hourly?.hours, 'valid_at', 'model_mean_c', 24);
    adjusted = shadow?.status === 'shadow_ready' ? points(shadow.hours, 'valid_at', 'corrected_mean_c', 24) : [];
    observed = points(scan?.hourly?.observations, 'observed_at', 'temperature_c', 24);
    const peak = records => records.reduce((highest, point) =>
      !highest || point.value > highest.value ? point : highest, null);
    const observation = peak(observed);
    const originalPeak = peak(original);
    slider.disabled = !(original.length || adjusted.length);
    if (scan?.date !== selectedDate || slider.disabled) {
      const captureHour = Math.floor((Date.parse(scan?.collected_at) - dayStart) / oneHour);
      const sameDayHour = captureHour >= 0 && captureHour < 24 ? captureHour : null;
      slider.value = adjusted[0]?.hour ?? sameDayHour ?? originalPeak?.hour ?? 0;
      selectedDate = scan?.date;
    }
    $('hourly-empty').hidden = Boolean(original.length || adjusted.length || observed.length);
    $('hourly-date').textContent = scan?.date || '—';
    $('hourly-observed-peak').textContent = temperature(observation?.value);
    $('hourly-observed-time').textContent = observation ? `${minuteLabel(observation.minute)} 北京时间` : '暂无该日常规 METAR';
    $('hourly-model-peak').textContent = originalPeak
      ? `${temperature(originalPeak.value)} · ${hourLabel(originalPeak.hour)}` : '—';
    $('hourly-final-high').textContent = shadow?.status === 'shadow_ready'
      ? temperature(shadow.predicted_final_high_c) : '—';
    const forecastAt = Date.parse(scan?.hourly?.forecast_at);
    $('hourly-forecast-time').textContent = Number.isFinite(forecastAt)
      ? `Open-Meteo 采集于 ${clock.format(forecastAt)}` : '暂无完整的四模型小时预报';
    $('hourly-shadow-summary').textContent = shadow?.status === 'shadow_ready'
      ? `校正依据为 ZSPD METAR${shadow.qweather_used ? '，辅以和风五点实时气温' : ''}。校正仅覆盖当日未来小时。`
      : hourlyReasons[shadow?.status] || (scan?.market_source_failed
        ? '市场接口采集失败，本轮未生成校正记录。小时预报和机场实测按各自采集结果展示。'
        : '暂无本轮逐小时校正记录。');
    renderRows();
    selectHour();
  };

  slider.addEventListener('input', selectHour);
  canvas.addEventListener('click', event => {
    if (slider.disabled) return;
    const bounds = canvas.getBoundingClientRect();
    const hour = Math.round((event.clientX - bounds.left - 41) / (bounds.width - 61) * 24);
    slider.value = Math.max(0, Math.min(23, hour));
    selectHour();
  });
  new ResizeObserver(draw).observe(canvas);
})();
