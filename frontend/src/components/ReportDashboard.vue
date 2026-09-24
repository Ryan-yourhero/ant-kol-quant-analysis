<template>
  <div class="report-dashboard">
    <!-- 1. 指标卡 -->
    <div class="cards">
      <div class="card-item" v-for="c in cards" :key="c.title">
        <div class="num">{{ c.value }}</div>
        <div class="label">{{ c.title }} <span v-if="c.unit" class="unit">{{ c.unit }}</span></div>
      </div>
    </div>

    <!-- 2. 今日总体判断 + 风险提示 -->
    <div v-if="structured.summary" class="card-block">
      <h3>今日总体判断</h3>
      <pre class="summary-text">{{ structured.summary }}</pre>
    </div>

    <!-- 3. 图表区 -->
    <div class="chart-grid">
      <div class="chart-cell">
        <h4>今日方向买入金额 TopN</h4>
        <canvas ref="amountCanvas" height="220"></canvas>
        <div v-if="!chartData.direction_buy_amount || chartData.direction_buy_amount.length === 0" class="empty">
          暂无方向数据
        </div>
      </div>
      <div class="chart-cell">
        <h4>今日方向参与人数 TopN</h4>
        <canvas ref="peopleCanvas" height="220"></canvas>
        <div v-if="!chartData.direction_buy_people || chartData.direction_buy_people.length === 0" class="empty">
          暂无方向数据
        </div>
      </div>
      <div class="chart-cell">
        <h4>操作类型分布</h4>
        <canvas ref="opCanvas" height="220"></canvas>
        <div v-if="!chartData.operation_type_distribution || chartData.operation_type_distribution.length === 0" class="empty">
          暂无操作数据
        </div>
      </div>
      <div class="chart-cell">
        <h4>大V买卖金额对比（买入）</h4>
        <canvas ref="kolCanvas" height="220"></canvas>
        <div v-if="!chartData.kol_buy_sell_compare || chartData.kol_buy_sell_compare.length === 0" class="empty">
          暂无大V买卖数据
        </div>
      </div>
    </div>

    <!-- 4. 方向汇总（按人数） -->
    <div class="card-block">
      <h3>方向汇总 · 按参与人数</h3>
      <table v-if="tables.direction_people_table && tables.direction_people_table.length">
        <thead><tr><th>排名</th><th>方向</th><th>参与大V数</th></tr></thead>
        <tbody>
          <tr v-for="r in tables.direction_people_table" :key="r.direction">
            <td>{{ r.rank }}</td>
            <td>{{ r.direction }}</td>
            <td>{{ r.kol_count }}</td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty">今日未形成可用的方向共识（主库命中不足 / 有效方向样本不足）</div>
    </div>

    <!-- 5. 方向汇总（按金额） -->
    <div class="card-block">
      <h3>方向汇总 · 按买入金额</h3>
      <table v-if="tables.direction_amount_table && tables.direction_amount_table.length">
        <thead><tr><th>排名</th><th>方向</th><th>买入总金额</th><th>主要贡献者</th></tr></thead>
        <tbody>
          <tr v-for="r in tables.direction_amount_table" :key="r.direction">
            <td>{{ r.rank }}</td>
            <td>{{ r.direction }}</td>
            <td>{{ formatAmount(r.buy_amount) }}</td>
            <td>{{ r.main_kols }}</td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty">今日无方向买入数据</div>
    </div>

    <!-- 6. 近 7 日趋势 -->
    <div class="card-block">
      <h3>近 7 日趋势变化</h3>
      <table v-if="tables.trend_table && tables.trend_table.length">
        <thead><tr><th>方向</th><th>今日人数</th><th>今日金额</th><th>7 日日均</th><th>变化幅度</th><th>信号类型</th><th>置信度</th></tr></thead>
        <tbody>
          <tr v-for="r in tables.trend_table" :key="r.direction">
            <td>{{ r.direction }}</td>
            <td>{{ r.today_kol_count }}</td>
            <td>{{ formatAmount(r.today_buy_amount) }}</td>
            <td>{{ formatAmount(r.avg_7d) }}</td>
            <td>{{ r.change_pct === null ? '—' : (r.change_pct > 0 ? '+' : '') + r.change_pct.toFixed(1) + '%' }}</td>
            <td>{{ r.signal_type }}</td>
            <td>{{ r.confidence }}</td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty">今日无近 7 日趋势数据</div>
    </div>

    <!-- 7. 买入推荐 -->
    <div class="card-block">
      <h3>买入推荐</h3>
      <table v-if="tables.buy_recommend_table && tables.buy_recommend_table.length">
        <thead><tr><th>排名</th><th>方向</th><th>今日买入人数</th><th>今日买入金额</th><th>近 7 日变化</th><th>信号类型</th><th>置信度</th></tr></thead>
        <tbody>
          <tr v-for="(r, idx) in tables.buy_recommend_table" :key="r.direction">
            <td>{{ idx + 1 }}</td>
            <td>{{ r.direction }}</td>
            <td>{{ r.today_buy_kol_count }}</td>
            <td>{{ formatAmount(r.today_buy_amount) }}</td>
            <td>{{ r.buy_change_pct === null ? '—' : (r.buy_change_pct > 0 ? '+' : '') + r.buy_change_pct.toFixed(1) + '%' }}</td>
            <td>{{ r.signal_type }}</td>
            <td>{{ r.confidence }}</td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty">今日无有效买入推荐</div>
    </div>

    <!-- 8. 卖出推荐 -->
    <div class="card-block">
      <h3>卖出推荐</h3>
      <table v-if="tables.sell_recommend_table && tables.sell_recommend_table.length">
        <thead><tr><th>排名</th><th>方向</th><th>今日卖出人数</th><th>信号类型</th><th>置信度</th></tr></thead>
        <tbody>
          <tr v-for="(r, idx) in tables.sell_recommend_table" :key="r.direction">
            <td>{{ idx + 1 }}</td>
            <td>{{ r.direction }}</td>
            <td>{{ r.today_sell_kol_count }}</td>
            <td>{{ r.signal_type }}</td>
            <td>{{ r.confidence }}</td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty">今日无有效卖出推荐</div>
    </div>

    <!-- 9. 核心大V操作（按大V 表格化） -->
    <div class="card-block">
      <h3>核心大V操作</h3>
      <div v-for="k in tables.kol_ops_table || []" :key="k.kol" class="kol-block">
        <h4>{{ k.kol }} <span class="op-count">（{{ k.rows.length }} 笔）</span></h4>
        <table>
          <thead><tr><th>时间</th><th>操作</th><th>基金名称</th><th>金额/份额</th><th>是否转换</th><th>方向</th><th>备注</th></tr></thead>
          <tbody>
            <tr v-for="(row, i) in k.rows" :key="i">
              <td>{{ row.time }}</td>
              <td>{{ row.operation }}</td>
              <td>{{ row.fund }}</td>
              <td>{{ row.amount_or_shares }}</td>
              <td>{{ row.is_conversion }}</td>
              <td>{{ row.direction }}</td>
              <td>{{ row.remark }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-if="!tables.kol_ops_table || tables.kol_ops_table.length === 0" class="empty">无大V操作数据</div>
    </div>

    <!-- 10. 待确认基金（折叠） -->
    <div v-if="appendix.length" class="card-block pending-block">
      <h3 @click="pendingOpen = !pendingOpen" style="cursor: pointer;">
        附录：待确认基金（{{ appendix.length }} 条）
        <span style="font-size: 12px; color: #909399;">{{ pendingOpen ? '点击折叠' : '点击展开' }}</span>
      </h3>
      <div v-show="pendingOpen">
        <p style="font-size: 12px; color: #909399; margin-bottom: 8px;">
          这些交易保留在原始记录中，不参与方向汇总、推荐与趋势展示。请到「基金方向库」补充主库后即可纳入分析。
        </p>
        <table>
          <thead><tr><th>大V</th><th>基金名称</th><th>操作类型</th><th>未命中原因</th><th>处理状态</th></tr></thead>
          <tbody>
            <tr v-for="(p, i) in appendix" :key="i">
              <td>{{ p.kol }}</td>
              <td>{{ p.fund }}</td>
              <td>{{ p.operation }}</td>
              <td>{{ p.reason }}</td>
              <td>{{ p.status }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>

<script>
import { ref, watch, onMounted, nextTick, computed } from 'vue'

// 简易 Canvas 图表（不依赖第三方库）
function drawBar(canvas, labels, values, opts = {}) {
  if (!canvas) return
  const ctx = canvas.getContext('2d')
  const dpr = window.devicePixelRatio || 1
  const cssW = canvas.clientWidth || 600
  const cssH = canvas.clientHeight || 220
  canvas.width = cssW * dpr
  canvas.height = cssH * dpr
  ctx.scale(dpr, dpr)
  ctx.clearRect(0, 0, cssW, cssH)
  if (!labels || labels.length === 0) return

  const padding = { l: 100, r: 20, t: 16, b: 28 }
  const w = cssW - padding.l - padding.r
  const h = cssH - padding.t - padding.b
  const maxV = Math.max(...values, 1)
  const barH = Math.max(8, Math.min(28, h / labels.length - 6))

  // y 轴
  ctx.strokeStyle = '#e4e7ed'
  ctx.lineWidth = 1
  ctx.beginPath()
  ctx.moveTo(padding.l, padding.t)
  ctx.lineTo(padding.l, padding.t + h)
  ctx.lineTo(padding.l + w, padding.t + h)
  ctx.stroke()

  labels.forEach((label, i) => {
    const v = values[i]
    const y = padding.t + i * (h / labels.length) + 4
    // 标签文字
    ctx.fillStyle = '#606266'
    ctx.font = '12px sans-serif'
    ctx.textAlign = 'right'
    ctx.textBaseline = 'middle'
    const txt = label.length > 14 ? label.slice(0, 13) + '…' : label
    ctx.fillText(txt, padding.l - 8, y + barH / 2)
    // bar
    const barW = (v / maxV) * w
    ctx.fillStyle = opts.color || '#409eff'
    ctx.fillRect(padding.l, y, barW, barH)
    // 数值
    ctx.fillStyle = '#303133'
    ctx.textAlign = 'left'
    const valStr = opts.fmt ? opts.fmt(v) : String(v)
    ctx.fillText(valStr, padding.l + barW + 6, y + barH / 2)
  })
}

function drawVerticalBar(canvas, labels, values, opts = {}) {
  if (!canvas) return
  const ctx = canvas.getContext('2d')
  const dpr = window.devicePixelRatio || 1
  const cssW = canvas.clientWidth || 600
  const cssH = canvas.clientHeight || 220
  canvas.width = cssW * dpr
  canvas.height = cssH * dpr
  ctx.scale(dpr, dpr)
  ctx.clearRect(0, 0, cssW, cssH)
  if (!labels || labels.length === 0) return

  const padding = { l: 40, r: 16, t: 16, b: 60 }
  const w = cssW - padding.l - padding.r
  const h = cssH - padding.t - padding.b
  const maxV = Math.max(...values, 1)
  const slot = w / labels.length
  const barW = Math.max(10, slot - 12)

  ctx.strokeStyle = '#e4e7ed'
  ctx.beginPath()
  ctx.moveTo(padding.l, padding.t + h)
  ctx.lineTo(padding.l + w, padding.t + h)
  ctx.stroke()

  labels.forEach((label, i) => {
    const v = values[i]
    const x = padding.l + i * slot + (slot - barW) / 2
    const barH = (v / maxV) * h
    const y = padding.t + h - barH
    ctx.fillStyle = opts.color || '#67c23a'
    ctx.fillRect(x, y, barW, barH)
    // 标签
    ctx.fillStyle = '#909399'
    ctx.font = '11px sans-serif'
    ctx.textAlign = 'center'
    const txt = label.length > 6 ? label.slice(0, 5) + '…' : label
    ctx.save()
    ctx.translate(x + barW / 2, padding.t + h + 8)
    ctx.rotate(-Math.PI / 6)
    ctx.fillText(txt, 0, 0)
    ctx.restore()
    // 数值
    ctx.fillStyle = '#303133'
    ctx.font = '11px sans-serif'
    ctx.textAlign = 'center'
    const valStr = opts.fmt ? opts.fmt(v) : String(v)
    ctx.fillText(valStr, x + barW / 2, y - 4)
  })
}

const COLORS = [
  '#409eff', '#67c23a', '#e6a23c', '#f56c6c', '#909399',
  '#5b8cff', '#13c2c2', '#a87f0a', '#9b59b6', '#d4a017',
]

function drawPie(canvas, items, opts = {}) {
  if (!canvas) return
  const ctx = canvas.getContext('2d')
  const dpr = window.devicePixelRatio || 1
  const cssW = canvas.clientWidth || 600
  const cssH = canvas.clientHeight || 220
  canvas.width = cssW * dpr
  canvas.height = cssH * dpr
  ctx.scale(dpr, dpr)
  ctx.clearRect(0, 0, cssW, cssH)
  if (!items || items.length === 0) return

  const total = items.reduce((s, x) => s + (x.value || 0), 0)
  if (total === 0) return

  const cx = cssW / 2 - 80
  const cy = cssH / 2
  const r = Math.min(cx, cy) - 16
  let start = -Math.PI / 2

  items.forEach((it, i) => {
    const ang = (it.value / total) * Math.PI * 2
    ctx.beginPath()
    ctx.moveTo(cx, cy)
    ctx.arc(cx, cy, r, start, start + ang)
    ctx.closePath()
    ctx.fillStyle = COLORS[i % COLORS.length]
    ctx.fill()
    start += ang
  })

  // legend
  ctx.font = '12px sans-serif'
  ctx.textAlign = 'left'
  ctx.textBaseline = 'middle'
  items.forEach((it, i) => {
    const y = 24 + i * 20
    ctx.fillStyle = COLORS[i % COLORS.length]
    ctx.fillRect(cssW - 160, y - 6, 12, 12)
    ctx.fillStyle = '#606266'
    const valStr = opts.fmt ? opts.fmt(it.value) : String(it.value)
    ctx.fillText(`${it.name} (${valStr})`, cssW - 142, y)
  })
}

function formatAmount(n) {
  if (n == null || n === '') return '—'
  const num = Number(n)
  if (isNaN(num)) return '—'
  return num.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}

export default {
  name: 'ReportDashboard',
  props: {
    structured: { type: Object, required: true },
  },
  setup(props) {
    const amountCanvas = ref(null)
    const peopleCanvas = ref(null)
    const opCanvas = ref(null)
    const kolCanvas = ref(null)
    const pendingOpen = ref(false)

    const cards = computed(() => props.structured.summary_cards || [])
    const tables = computed(() => props.structured.tables || {})
    const chartData = computed(() => props.structured.chart_data || {})
    const appendix = computed(() => props.structured.appendix_pending_funds || [])

    function render() {
      nextTick(() => {
        const cd = chartData.value
        // 1) 方向买入金额 TopN（横向 bar）
        const dirAmt = (cd.direction_buy_amount || []).slice(0, 8)
        drawBar(amountCanvas.value,
          dirAmt.map(x => x.direction), dirAmt.map(x => x.value),
          { color: '#409eff', fmt: formatAmount })
        // 2) 方向参与人数 TopN（横向 bar）
        const dirPeople = (cd.direction_buy_people || []).slice(0, 8)
        drawBar(peopleCanvas.value,
          dirPeople.map(x => x.direction), dirPeople.map(x => x.value),
          { color: '#67c23a' })
        // 3) 操作类型分布（pie）
        drawPie(opCanvas.value, cd.operation_type_distribution || [])
        // 4) 大V买卖对比（横向 bar，按 buy 排序）
        const kol = (cd.kol_buy_sell_compare || [])
          .slice().sort((a, b) => b.buy - a.buy).slice(0, 8)
        drawBar(kolCanvas.value,
          kol.map(x => x.name), kol.map(x => x.buy),
          { color: '#e6a23c', fmt: formatAmount })
      })
    }

    watch(() => props.structured, render, { deep: true })
    onMounted(render)
    window.addEventListener('resize', render)

    return {
      amountCanvas, peopleCanvas, opCanvas, kolCanvas,
      pendingOpen,
      cards, tables, chartData, appendix,
      formatAmount,
    }
  },
}
</script>

<style scoped>
.report-dashboard { font-size: 13px; color: #303133; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 8px; margin-bottom: 16px; }
.card-item { background: #f5f7fa; border-radius: 6px; padding: 12px 8px; text-align: center; }
.card-item .num { font-size: 22px; font-weight: 700; color: #409eff; }
.card-item .label { font-size: 12px; color: #606266; margin-top: 4px; }
.card-item .unit { color: #909399; font-size: 11px; }

.card-block { background: #fff; border-radius: 6px; padding: 14px 16px; margin-bottom: 14px; box-shadow: 0 1px 2px rgba(0,0,0,0.04); }
.card-block h3 { font-size: 15px; margin-bottom: 10px; color: #303133; }
.card-block h4 { font-size: 13px; margin: 8px 0; color: #606266; }
.summary-text { white-space: pre-wrap; font-family: inherit; line-height: 1.6; margin: 0; font-size: 13px; }

.chart-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 14px; }
.chart-cell { background: #fff; border-radius: 6px; padding: 12px 14px; box-shadow: 0 1px 2px rgba(0,0,0,0.04); }
.chart-cell h4 { font-size: 13px; margin-bottom: 8px; color: #606266; }
.chart-cell canvas { width: 100%; display: block; }
.chart-cell .empty { color: #909399; font-size: 12px; padding: 32px 0; text-align: center; }

table { width: 100%; border-collapse: collapse; font-size: 12px; }
table th, table td { padding: 6px 8px; border-bottom: 1px solid #ebeef5; text-align: left; }
table th { background: #f5f7fa; font-weight: 600; color: #606266; }
table tr:hover { background: #fafbfc; }

.kol-block { margin-bottom: 12px; }
.kol-block h4 { color: #303133; }
.op-count { color: #909399; font-weight: normal; font-size: 12px; }

.pending-block { background: #fdf6ec; }

.empty { color: #909399; padding: 14px; text-align: center; font-size: 13px; }
</style>