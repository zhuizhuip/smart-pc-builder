const state = {
  keyword: "",
  query: "",
  charts: {},
  refreshTimer: null,
  refreshLockedUntil: 0,
  refreshCooldownSeconds: window.__REFRESH_COOLDOWN_SECONDS__ || 60,
  hasManualSynced: false,
};

const dashboardPalette = ["#2563EB", "#14B8A6", "#1D4ED8", "#0EA5E9", "#22C55E", "#6366F1"];

function $(id) {
  return document.getElementById(id);
}

function formatPrice(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "-";
  }
  return `¥ ${Number(value).toFixed(2)}`;
}

function formatPercent(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "-";
  }
  return `${(Number(value) * 100).toFixed(1)}%`;
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok || !payload.ok) {
    throw new Error(payload.error || "接口请求失败");
  }
  return payload.data;
}

function setSyncStatus(text) {
  const node = $("syncStatusText");
  if (node) {
    node.textContent = text;
  }
}

function updateRefreshButton() {
  const button = $("manualRefreshButton");
  if (!button) {
    return;
  }

  const remaining = Math.max(0, Math.ceil((state.refreshLockedUntil - Date.now()) / 1000));
  if (remaining > 0) {
    button.disabled = true;
    button.textContent = `${remaining}s 后可同步`;
    setSyncStatus(`手动同步冷却中，${remaining} 秒后可再次触发`);
    if (!state.refreshTimer) {
      state.refreshTimer = window.setInterval(updateRefreshButton, 1000);
    }
    return;
  }

  button.disabled = false;
  button.textContent = "实时同步";
  if (state.refreshTimer) {
    window.clearInterval(state.refreshTimer);
    state.refreshTimer = null;
  }
}

async function loadKeywords() {
  const data = await fetchJson("/api/keywords");
  const select = $("keywordSelect");
  if (!select) {
    return;
  }
  select.innerHTML = "";

  data.forEach((item, index) => {
    const option = document.createElement("option");
    option.value = item.keyword;
    option.textContent = item.keyword;
    select.appendChild(option);
    if (!state.keyword && index === 0) {
      state.keyword = item.keyword;
    }
  });

  if (state.keyword) {
    select.value = state.keyword;
  }
}

async function loadDashboardData() {
  const params = new URLSearchParams();
  if (state.keyword) {
    params.set("keyword", state.keyword);
  }
  if (state.query) {
    params.set("query", state.query);
  }

  const data = await fetchJson(`/api/dashboard-data?${params.toString()}`);
  state.keyword = data.selected_keyword;
  if ($("keywordSelect")) {
    $("keywordSelect").value = state.keyword;
  }
  renderDashboard(data);
}

async function runManualRefresh() {
  const button = $("manualRefreshButton");
  if (!button || button.disabled) {
    return;
  }

  button.disabled = true;
  button.textContent = "同步中...";

  try {
    const data = await fetchJson("/api/dashboard-refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        keyword: state.keyword,
        query: state.query,
      }),
    });

    state.keyword = data.selected_keyword;
    state.refreshLockedUntil = Date.now() + state.refreshCooldownSeconds * 1000;
    state.hasManualSynced = true;
    renderDashboard(data);
    // 显示当前时间的同步结果，让用户感知到数据是"刚拿到的"
    const now = new Date();
    const timeStr = now.getHours().toString().padStart(2,'0') + ':' +
                    now.getMinutes().toString().padStart(2,'0') + ':' +
                    now.getSeconds().toString().padStart(2,'0');
    setSyncStatus(`刚刚同步 ${timeStr}，共 ${data.snapshot_count || 0} 条商品数据`);
    updateRefreshButton();
  } catch (error) {
    const message = String(error?.message || "");
    const match = message.match(/(\d+)/);
    if (match) {
      state.refreshLockedUntil = Date.now() + Number(match[1]) * 1000;
      updateRefreshButton();
    } else {
      button.disabled = false;
      button.textContent = "实时同步";
    }
    setSyncStatus(message || "同步失败，请稍后重试");
    console.error(error);
  }
}

function ensureChart(id) {
  if (!state.charts[id]) {
    state.charts[id] = echarts.init($(id));
  }
  return state.charts[id];
}

function renderMetrics(overview) {
  $("metricSnapshotCount").textContent = overview.clean_snapshot_count ?? "-";
  $("metricKeyword").textContent = overview.selected_keyword || "-";
  $("metricSubtypeCount").textContent = overview.selected_subtype_count ?? "-";
  $("metricDirtyCount").textContent = overview.dirty_count ?? "-";
  $("metricLatestTime").textContent = overview.latest_crawl_time || "-";
}

function renderTrendChart(payload) {
  const chart = ensureChart("trendChart");
  const trend = payload.subtype_trend;
  const hasSeries = trend.series.some((series) => (series.avg || []).some((value) => value !== null && value !== undefined));

  // 仅手动同步后才追加当前时间数据点
  var times = trend.times || [];
  var enrichedSeries = trend.series;
  if (state.hasManualSynced && times.length > 0) {
    var now = new Date();
    var pad = function(n) { return n.toString().padStart(2, '0'); };
    var currentTimeLabel = now.getFullYear() + '-' + pad(now.getMonth() + 1) + '-' + pad(now.getDate()) +
                           ' ' + pad(now.getHours()) + ':' + pad(now.getMinutes());
    times = [...times, currentTimeLabel];
    enrichedSeries = trend.series.map(function(s) {
      var lastAvg = s.avg && s.avg.length ? s.avg[s.avg.length - 1] : null;
      var lastMin = s.min && s.min.length ? s.min[s.min.length - 1] : null;
      var lastMax = s.max && s.max.length ? s.max[s.max.length - 1] : null;
      return {
        name: s.name,
        avg: [...(s.avg || []), lastAvg],
        min: [...(s.min || []), lastMin],
        max: [...(s.max || []), lastMax],
      };
    });
  }

  chart.setOption(
    {
      backgroundColor: "transparent",
      color: dashboardPalette,
      animationDuration: 500,
      grid: { left: 48, right: 110, top: 24, bottom: 38 },
      tooltip: {
        trigger: "axis",
        backgroundColor: "rgba(24, 20, 50, 0.95)",
        borderColor: "rgba(145, 135, 244, 0.25)",
        textStyle: { color: "#f8f7ff" },
        formatter(params) {
          const lines = [params[0]?.axisValue || ""];
          params.forEach((item) => {
            const series = enrichedSeries.find((entry) => entry.name === item.seriesName);
            const index = item.dataIndex;
            const min = series?.min?.[index];
            const max = series?.max?.[index];
            lines.push(
              `${item.marker}${item.seriesName} 均价 ${formatPrice(item.value)} / 最低 ${formatPrice(min)} / 最高 ${formatPrice(max)}`
            );
          });
          return lines.join("<br/>");
        },
      },
      legend: {
        top: 0,
        textStyle: { color: "#7b7ca1", fontSize: 11 },
      },
      xAxis: {
        type: "category",
        data: times,
        axisLine: { lineStyle: { color: "rgba(145, 135, 244, 0.18)" } },
        axisLabel: { color: "#7b7ca1", rotate: 18, fontSize: 11 },
      },
      yAxis: {
        type: "value",
        axisLine: { show: false },
        splitLine: { lineStyle: { color: "rgba(145, 135, 244, 0.08)" } },
        axisLabel: { color: "#7b7ca1", fontSize: 11 },
      },
      series: enrichedSeries.map((series, index) => ({
        name: series.name,
        type: "line",
        smooth: true,
        data: series.avg,
        showSymbol: true,
        symbol: "circle",
        symbolSize: times.length <= 1 ? 10 : 7,
        lineStyle: { width: 3, color: dashboardPalette[index % dashboardPalette.length] },
        itemStyle: { color: dashboardPalette[index % dashboardPalette.length] },
        endLabel: {
          show: true,
          formatter: "{a}",
          color: dashboardPalette[index % dashboardPalette.length],
          fontSize: 11,
        },
        labelLayout: { moveOverlap: "shiftY" },
        emphasis: { focus: "series" },
      })),
      graphic: hasSeries
        ? []
        : [
            {
              type: "text",
              left: "center",
              top: "middle",
              style: {
                text: "当前批次暂无可展示的趋势数据",
                fill: "#8a8fb0",
                fontSize: 12,
              },
            },
          ],
    },
    true
  );

  $("trendTitle").textContent = `${payload.selected_keyword} 小类价格趋势`;
}

function renderSubtypeChart(payload) {
  const chart = ensureChart("subtypeChart");
  const categories = payload.subtype_stats.map((item) => item.product_subtype);

  chart.setOption(
    {
      backgroundColor: "transparent",
      color: ["#8178eb", "#90b0ff", "#b18cf0"],
      animationDuration: 500,
      grid: { left: 88, right: 18, top: 24, bottom: 18 },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        backgroundColor: "rgba(24, 20, 50, 0.95)",
        borderColor: "rgba(145, 135, 244, 0.25)",
        textStyle: { color: "#f8f7ff" },
      },
      legend: {
        top: 0,
        textStyle: { color: "#7b7ca1", fontSize: 11 },
      },
      xAxis: {
        type: "value",
        axisLabel: { color: "#7b7ca1", fontSize: 11 },
        splitLine: { lineStyle: { color: "rgba(145, 135, 244, 0.08)" } },
      },
      yAxis: {
        type: "category",
        data: categories,
        axisLabel: { color: "#7b7ca1", width: 88, overflow: "truncate", fontSize: 11 },
        axisLine: { lineStyle: { color: "rgba(145, 135, 244, 0.16)" } },
      },
      series: [
        { name: "均价", type: "bar", data: payload.subtype_stats.map((item) => item.avg_effective_price), barWidth: 10 },
        { name: "最低价", type: "scatter", data: payload.subtype_stats.map((item) => item.min_effective_price), symbolSize: 9 },
        { name: "最高价", type: "scatter", data: payload.subtype_stats.map((item) => item.max_effective_price), symbolSize: 9 },
      ],
      graphic: categories.length
        ? []
        : [
            {
              type: "text",
              left: "center",
              top: "middle",
              style: {
                text: "当前批次暂无可展示的小类价格区间",
                fill: "#8a8fb0",
                fontSize: 12,
              },
            },
          ],
    },
    true
  );

  $("subtypeChartTitle").textContent = `${payload.selected_keyword} 小类均价与价格区间`;
}

function renderAlerts(payload) {
  const panel = $("alertPanel");
  const container = $("alertList");

  if (!payload.alerts.length) {
    panel.classList.add("is-empty");
    container.innerHTML = '<div class="empty-state compact">当前没有达到阈值的小类价格波动告警。</div>';
    $("alertTitle").textContent = `${payload.selected_keyword} 异常告警`;
    return;
  }

  panel.classList.remove("is-empty");
  container.className = "scroll-panel alert-list";
  container.innerHTML = payload.alerts
    .map(
      (item) => `
        <article class="alert-item ${item.level}">
          <strong>${item.product_subtype}</strong>
          <div class="alert-type">按小类均值波动阈值触发</div>
          <div class="alert-meta">
            <span>上一时点均价：${formatPrice(item.previous_avg_price)}</span>
            <span>当前均价：${formatPrice(item.latest_avg_price)}</span>
            <span>涨跌幅：${formatPercent(item.delta_rate)}</span>
            <span>变动额：${formatPrice(item.delta_amount)}</span>
            <span>当前最低价：${formatPrice(item.current_min_price)}</span>
            <span>当前最高价：${formatPrice(item.current_max_price)}</span>
            <span>样本数：${item.sample_count}</span>
            <span>时间：${item.previous_crawl_time} → ${item.latest_crawl_time}</span>
          </div>
        </article>
      `
    )
    .join("");

  $("alertTitle").textContent = `${payload.selected_keyword} 异常告警`;
}

function buildProductLink(url, label = "查看商品") {
  if (!url) {
    return "";
  }
  return `
    <span class="jump-link">
      <a href="${url}" target="_blank" rel="noopener noreferrer">
        <span class="jump-link-icon">↗</span>
        <span>${label}</span>
      </a>
    </span>
  `;
}

function renderSnapshot(payload) {
  const tbody = $("snapshotTableBody");
  if (!payload.snapshot_extremes.length) {
    tbody.innerHTML = '<tr><td colspan="4"><div class="empty-state">当前没有可展示的小类价格边界快照。</div></td></tr>';
    $("snapshotTitle").textContent = `${payload.selected_keyword} 价格边界`;
    return;
  }

  tbody.innerHTML = payload.snapshot_extremes
    .map(
      (item) => `
        <tr>
          <td><span class="subtype-label">${item.product_subtype}</span></td>
          <td>
            <div class="sku-line">
              <span class="shop-name">${item.low_shop}</span>
              <span>${item.low_sku_name}</span>
              ${buildProductLink(item.low_source_url)}
              <span class="price-pill">${formatPrice(item.low_price)}</span>
            </div>
          </td>
          <td>
            <div class="sku-line">
              <span class="shop-name">${item.high_shop}</span>
              <span>${item.high_sku_name}</span>
              ${buildProductLink(item.high_source_url)}
              <span class="price-pill">${formatPrice(item.high_price)}</span>
            </div>
          </td>
          <td>${item.latest_crawl_time}</td>
        </tr>
      `
    )
    .join("");

  $("snapshotTitle").textContent = `${payload.selected_keyword} 价格边界`;
}

function renderSearchResults(payload) {
  const tbody = $("searchTableBody");
  if (!payload.search_results.length) {
    tbody.innerHTML = '<tr><td colspan="6"><div class="empty-state">当前没有匹配的搜索结果或最近记录。</div></td></tr>';
    return;
  }

  tbody.innerHTML = payload.search_results
    .map(
      (item) => `
        <tr>
          <td>${item.crawl_time}</td>
          <td>${item.keyword}</td>
          <td>${item.product_subtype}</td>
          <td>${item.shop}</td>
          <td>
            <div class="sku-line">
              <span>${item.sku_name}</span>
              ${buildProductLink(item.source_url)}
            </div>
          </td>
          <td><span class="price-pill">${formatPrice(item.effective_price)}</span></td>
        </tr>
      `
    )
    .join("");
}

function renderDashboard(payload) {
  renderMetrics(payload.overview);
  renderTrendChart(payload);
  renderSubtypeChart(payload);
  renderAlerts(payload);
  renderSnapshot(payload);
  renderSearchResults(payload);

  const downloadLink = $("downloadCleanSnapshot");
  if (downloadLink && payload.download_links?.clean_snapshot) {
    downloadLink.href = payload.download_links.clean_snapshot;
  }
}

function bindEvents() {
  const keywordSelect = $("keywordSelect");
  if (keywordSelect) {
    keywordSelect.addEventListener("change", async (event) => {
      state.keyword = event.target.value;
      await loadDashboardData();
    });
  }

  const searchButton = $("searchButton");
  if (searchButton) {
    searchButton.addEventListener("click", async () => {
      state.query = $("searchInput").value.trim();
      await loadDashboardData();
    });
  }

  const searchInput = $("searchInput");
  if (searchInput) {
    searchInput.addEventListener("keydown", async (event) => {
      if (event.key === "Enter") {
        state.query = searchInput.value.trim();
        await loadDashboardData();
      }
    });
  }

  const manualRefreshButton = $("manualRefreshButton");
  if (manualRefreshButton) {
    manualRefreshButton.addEventListener("click", runManualRefresh);
  }

  window.addEventListener("resize", () => {
    Object.values(state.charts).forEach((chart) => chart.resize());
  });
}

async function bootstrap() {
  bindEvents();
  await loadKeywords();
  await loadDashboardData();
  updateRefreshButton();
}

bootstrap().catch((error) => {
  setSyncStatus("页面初始化失败，请刷新后重试");
  console.error(error);
});
