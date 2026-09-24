<template>
  <div>
    <div class="card">
      <div class="card-title">每日采集</div>
      <div style="display: flex; align-items: center; gap: 16px; margin-bottom: 16px;">
        <button class="btn btn-primary" :disabled="isRunning" @click="startRun">
          {{ isRunning ? '任务进行中...' : '开始今日采集' }}
        </button>
        <span v-if="status.status !== 'idle'" class="status-tag" :class="statusClass">{{ statusText }}</span>
        <span v-if="status.status !== 'success' && status.status !== 'failed' && status.message" style="color: #909399; font-size: 13px;">{{ status.message }}</span>
        <span v-if="status.error" style="color: #f56c6c; font-size: 13px;">{{ status.error }}</span>
      </div>
      <div v-if="status.status === 'crawling'" style="color: #409eff; font-size: 13px; margin-top: 4px;">
        正在采集第 <b>{{ currentPage }}</b> 页的数据…
      </div>
      <div v-if="status.status === 'failed' && status.error"
           style="color: #f56c6c; font-size: 13px; margin-top: 4px;">
        {{ status.error }}
      </div>
    </div>

    <div class="card">
      <div class="card-title" style="display: flex; justify-content: space-between; align-items: center;">
        <span>任务记录</span>
        <div style="display: flex; gap: 8px;">
          <button class="btn btn-default btn-sm" @click="refreshReports">刷新</button>
        </div>
      </div>
      <div v-if="reportStatus.status === 'generating'" style="margin-bottom: 12px; color: #409eff; font-size: 13px;">
        正在生成 {{ reportStatus.current_date || '' }}（{{ reportStatus.done }}/{{ reportStatus.total }}）...
      </div>
      <div v-if="reportStatus.status === 'failed' && reportStatus.failed_dates && reportStatus.failed_dates.length" style="margin-bottom: 12px; color: #f56c6c; font-size: 13px;">
        失败：{{ reportStatus.failed_dates.map(f => f.date).join('、') }}
      </div>
      <div v-if="reports.length === 0" style="color: #909399;">暂无历史数据</div>
      <div v-else style="overflow-x: auto;">
        <table>
          <thead>
            <tr><th>日期</th><th>记录数</th><th>报告状态</th><th>操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="r in reports" :key="r.date">
              <td>{{ r.date }}</td>
              <td>{{ r.record_count }}</td>
              <td>
                <span v-if="r.report_status === 'generating'" style="color: #409eff;">生成中...</span>
                <span v-else-if="r.report_status === 'failed'" style="color: #f56c6c;">生成失败</span>
                <span v-else-if="r.has_report" style="color: #52c41a;">已生成</span>
                <span v-else style="color: #909399;">未生成</span>
              </td>
              <td>
                <button v-if="r.has_report && r.report_status !== 'generating'" class="btn btn-default btn-sm with-gap" @click="viewReport(r)">查看AI报告</button>
                <button class="btn btn-default btn-sm with-gap" @click="viewRawOps(r)">查看原始交易记录</button>
                <button class="btn btn-primary btn-sm with-gap" :disabled="reportStatus.status === 'generating'" @click="generateOne(r.date)">
                  {{ r.report_status === 'generating' ? '生成中...' : '重新生成报告' }}
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div v-if="viewing" class="modal-mask" @click.self="viewing = null">
      <div class="modal" style="max-width: 1400px;">
        <div class="modal-head">
          <span>{{ viewing.date }} 每日分析报告</span>
          <div style="display: flex; gap: 8px;">
            <button class="btn btn-default btn-sm" @click="viewing.showMarkdown = !viewing.showMarkdown">
              {{ viewing.showMarkdown ? '查看仪表盘' : '查看 Markdown' }}
            </button>
            <button class="btn btn-default btn-sm" @click="viewing = null">关闭</button>
          </div>
        </div>
        <div class="report-content">
          <div v-if="!viewing.structured && !viewing.showMarkdown" style="color: #909399; padding: 16px; text-align: center;">
            加载中...
          </div>
          <ReportDashboard v-else-if="viewing.structured && !viewing.showMarkdown" :structured="viewing.structured" />
          <pre v-else-if="viewing.showMarkdown" style="white-space: pre-wrap; font-family: inherit; line-height: 1.6; margin: 0; font-size: 13px;">{{ viewing.markdown }}</pre>
        </div>
      </div>
    </div>

    <div v-if="viewingOps" class="modal-mask" @click.self="viewingOps = null">
      <div class="modal" style="max-width: 1400px;">
        <div class="modal-head">
          <span>{{ viewingOps.date }} 原始交易记录（共 {{ viewingOps.items.length }} 条）</span>
          <div style="display: flex; gap: 8px;">
            <button class="btn btn-sm"
                    :class="groupByDirection ? 'btn-primary' : 'btn-default'"
                    @click="groupByDirection = !groupByDirection">
              {{ groupByDirection ? '✓ 按方向排列' : '按方向排列' }}
            </button>
            <button class="btn btn-default btn-sm" @click="viewingOps = null">关闭</button>
          </div>
        </div>
        <div class="report-content" style="padding: 0;">
          <div v-if="!viewingOps.items.length" style="color: #909399; padding: 16px;">该日期暂无交易记录</div>

          <!-- 默认视图：按采集顺序 -->
          <table v-else-if="!groupByDirection" style="width: 100%; font-size: 13px;">
            <thead>
              <tr style="background: #f5f7fa;">
                <th style="padding: 6px 8px; text-align: center; border-bottom: 1px solid #ebeef5; width: 50px;">序号</th>
                <th style="padding: 6px 8px; text-align: left; border-bottom: 1px solid #ebeef5;">大V</th>
                <th style="padding: 6px 8px; text-align: left; border-bottom: 1px solid #ebeef5; width: 60px;">时间</th>
                <th style="padding: 6px 8px; text-align: left; border-bottom: 1px solid #ebeef5; width: 80px;">操作</th>
                <th style="padding: 6px 8px; text-align: left; border-bottom: 1px solid #ebeef5;">基金</th>
                <th style="padding: 6px 8px; text-align: left; border-bottom: 1px solid #ebeef5;">方向</th>
                <th style="padding: 6px 8px; text-align: right; border-bottom: 1px solid #ebeef5;">买入金额</th>
                <th style="padding: 6px 8px; text-align: right; border-bottom: 1px solid #ebeef5;">卖出份额</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(op, idx) in viewingOps.items" :key="op.id">
                <td style="padding: 4px 8px; border-bottom: 1px solid #ebeef5; text-align: center; color: #909399;">{{ idx + 1 }}</td>
                <td style="padding: 4px 8px; border-bottom: 1px solid #ebeef5;">{{ op.kol_name || '-' }}</td>
                <td style="padding: 4px 8px; border-bottom: 1px solid #ebeef5;">{{ op.publish_time || '' }}</td>
                <td style="padding: 4px 8px; border-bottom: 1px solid #ebeef5;">
                  <span style="color: #52c41a;" v-if="['买入','定投'].includes(op.operation_type)">{{ op.operation_type }}</span>
                  <span style="color: #f56c6c;" v-else-if="op.operation_type === '卖出'">{{ op.operation_type }}{{ op.remark === '转换' ? '(转换)' : '' }}</span>
                  <span v-else>{{ op.operation_type }}</span>
                </td>
                <td style="padding: 4px 8px; border-bottom: 1px solid #ebeef5; max-width: 480px; word-break: break-all;">{{ op.fund_name }}</td>
                <td style="padding: 4px 8px; border-bottom: 1px solid #ebeef5;">
                  <span v-if="op.direction" :style="directionStyle(op.direction)">{{ op.direction }}</span>
                  <span v-else style="color: #c0c4cc;">-</span>
                </td>
                <td style="padding: 4px 8px; text-align: right; border-bottom: 1px solid #ebeef5;">{{ op.buy_amount || '-' }}</td>
                <td style="padding: 4px 8px; text-align: right; border-bottom: 1px solid #ebeef5;">{{ op.sell_shares || '-' }}</td>
              </tr>
            </tbody>
          </table>

          <!-- 按方向排列：同方向放一起 -->
          <div v-else>
            <div v-if="!groupedOps.length" style="color: #909399; padding: 16px;">该日期暂无交易记录</div>
            <table v-for="g in groupedOps" :key="g.direction"
                   style="width: 100%; font-size: 13px; margin-bottom: 12px; border: 1px solid #ebeef5;">
              <thead>
                <tr style="background: #f0f4fa;">
                  <th :colspan="8"
                      style="padding: 8px 12px; text-align: left; border-bottom: 1px solid #ebeef5;">
                    <span :style="directionStyle(g.direction)" style="font-size: 14px;">{{ g.direction || '其他/待分类' }}</span>
                    <span style="color: #909399; font-weight: normal; margin-left: 12px; font-size: 12px;">
                      共 {{ g.items.length }} 条 · 大V {{ g.kolCount }} 人
                    </span>
                  </th>
                </tr>
                <tr style="background: #f5f7fa;">
                  <th style="padding: 4px 8px; text-align: center; border-bottom: 1px solid #ebeef5; width: 50px;">序号</th>
                  <th style="padding: 4px 8px; text-align: left; border-bottom: 1px solid #ebeef5;">大V</th>
                  <th style="padding: 4px 8px; text-align: left; border-bottom: 1px solid #ebeef5; width: 60px;">时间</th>
                  <th style="padding: 4px 8px; text-align: left; border-bottom: 1px solid #ebeef5; width: 80px;">操作</th>
                  <th style="padding: 4px 8px; text-align: left; border-bottom: 1px solid #ebeef5;">基金</th>
                  <th style="padding: 4px 8px; text-align: left; border-bottom: 1px solid #ebeef5;">方向</th>
                  <th style="padding: 4px 8px; text-align: right; border-bottom: 1px solid #ebeef5;">买入金额</th>
                  <th style="padding: 4px 8px; text-align: right; border-bottom: 1px solid #ebeef5;">卖出份额</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(op, idx) in g.items" :key="op.id">
                  <td style="padding: 4px 8px; border-bottom: 1px solid #ebeef5; text-align: center; color: #909399;">{{ idx + 1 }}</td>
                  <td style="padding: 4px 8px; border-bottom: 1px solid #ebeef5;">{{ op.kol_name || '-' }}</td>
                  <td style="padding: 4px 8px; border-bottom: 1px solid #ebeef5;">{{ op.publish_time || '' }}</td>
                  <td style="padding: 4px 8px; border-bottom: 1px solid #ebeef5;">
                    <span style="color: #52c41a;" v-if="['买入','定投'].includes(op.operation_type)">{{ op.operation_type }}</span>
                    <span style="color: #f56c6c;" v-else-if="op.operation_type === '卖出'">{{ op.operation_type }}{{ op.remark === '转换' ? '(转换)' : '' }}</span>
                    <span v-else>{{ op.operation_type }}</span>
                  </td>
                  <td style="padding: 4px 8px; border-bottom: 1px solid #ebeef5; max-width: 480px; word-break: break-all;">{{ op.fund_name }}</td>
                  <td style="padding: 4px 8px; border-bottom: 1px solid #ebeef5;">
                    <span v-if="op.direction" :style="directionStyle(op.direction)">{{ op.direction }}</span>
                    <span v-else style="color: #c0c4cc;">-</span>
                  </td>
                  <td style="padding: 4px 8px; text-align: right; border-bottom: 1px solid #ebeef5;">{{ op.buy_amount || '-' }}</td>
                  <td style="padding: 4px 8px; text-align: right; border-bottom: 1px solid #ebeef5;">{{ op.sell_shares || '-' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>

    <div v-if="summary.total > 0" class="card">
      <div class="card-title">今日概览</div>
      <div style="display: flex;">
        <div class="stat-box"><span class="num">{{ summary.total }}</span><span class="label">今日操作</span></div>
        <div class="stat-box"><span class="num">{{ summary.buy }}</span><span class="label">买入</span></div>
        <div class="stat-box"><span class="num">{{ summary.sell }}</span><span class="label">卖出</span></div>
      </div>
      <div style="margin-top: 12px;">
        <a :href="downloadExcel()" class="btn btn-default btn-sm">下载今日Excel</a>
      </div>
    </div>

    <div v-if="ops.length > 0" class="card">
      <div class="card-title" style="display: flex; justify-content: space-between; align-items: center;">
        <span>今日操作记录</span>
        <button class="btn btn-default btn-sm" @click="expanded = !expanded">
          {{ expanded ? '收起' : '展开全部字段' }}
        </button>
      </div>
      <div style="overflow-x: auto;">
        <!-- 默认视图 -->
        <table v-if="!expanded">
          <thead>
            <tr><th>大V</th><th>发布时间</th><th>操作类型</th><th>操作状态</th><th>基金名称</th><th>买入金额</th><th>卖出份额</th></tr>
          </thead>
          <tbody>
            <tr v-for="op in ops" :key="op.id">
              <td><router-link :to="'/kol/' + (op.kol_id || op.id)">{{ op.kol_name }}</router-link></td>
              <td>{{ op.publish_time }}</td>
              <td>{{ op.operation_type }}</td>
              <td>{{ op.operation_status }}</td>
              <td>{{ op.fund_name }}</td>
              <td>{{ op.buy_amount }}</td>
              <td>{{ op.sell_shares }}</td>
            </tr>
          </tbody>
        </table>
        <!-- 展开全部字段 -->
        <table v-else>
          <thead>
            <tr><th>大V</th><th>收益率</th><th>发布时间</th><th>动态正文</th><th>操作类型</th><th>操作状态</th><th>基金名称</th><th>买入金额</th><th>卖出份额</th><th>采集日期</th><th>备注</th></tr>
          </thead>
          <tbody>
            <tr v-for="op in ops" :key="op.id">
              <td><router-link :to="'/kol/' + (op.kol_id || op.id)">{{ op.kol_name }}</router-link></td>
              <td>{{ op.yield_rate }}</td>
              <td>{{ op.publish_time }}</td>
              <td style="max-width: 240px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" :title="op.opinion_text">{{ op.opinion_text }}</td>
              <td>{{ op.operation_type }}</td>
              <td>{{ op.operation_status }}</td>
              <td>{{ op.fund_name }}</td>
              <td>{{ op.buy_amount }}</td>
              <td>{{ op.sell_shares }}</td>
              <td>{{ op.collect_date }}</td>
              <td>{{ op.remark }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="pagination">
        <button class="btn btn-default btn-sm" :disabled="page <= 1" @click="loadOps(page - 1)">上一页</button>
        <span>第 {{ page }} / {{ maxPage }} 页（共 {{ oTotal }} 条）</span>
        <button class="btn btn-default btn-sm" :disabled="page >= maxPage" @click="loadOps(page + 1)">下一页</button>
      </div>
    </div>
  </div>
</template>

<script>
import { ref, computed, onMounted, onUnmounted, nextTick } from 'vue'
import { marked } from 'marked'
import { startRun as apiStart, getCurrentRun, getTodayOps, downloadExcel, getReports, generateReports, getReportContent, getReportStructured, getOpsByDate } from '../utils/api.js'

marked.setOptions({ breaks: true, gfm: true })

import ReportDashboard from '../components/ReportDashboard.vue'

export default {
  components: { ReportDashboard },
  setup() {
    const status = ref({ status: 'idle', message: '' })
    const ops = ref([])
    const page = ref(1)
    const oTotal = ref(0)
    const summary = ref({ total: 0, buy: 0, sell: 0 })
    const expanded = ref(false)
    const reports = ref([])
    const reportStatus = ref({ status: 'idle' })
    const viewing = ref(null)
    const viewingOps = ref(null)
    const groupByDirection = ref(false)
    const logBodyRef = ref(null)
    const currentPage = ref(0)  // 当前采集的页数（从 logs 解析 "页面N"）
    let timer = null
    let reportTimer = null

    const viewingHtml = computed(() => {
      if (!viewing.value) return ''
      return marked.parse(viewing.value.content || '')
    })

    // 按方向分组：方向为空归到「其他/待分类」；组间按组内大V 数 + 操作数从高到低排序
    const groupedOps = computed(() => {
      if (!viewingOps.value || !viewingOps.value.items) return []
      const groups = new Map()
      for (const op of viewingOps.value.items) {
        const dir = op.direction || '其他/待分类'
        if (!groups.has(dir)) groups.set(dir, [])
        groups.get(dir).push(op)
      }
      const arr = Array.from(groups.entries()).map(([direction, items]) => ({
        direction,
        items,
        kolCount: new Set(items.map(i => i.kol_name).filter(Boolean)).size,
      }))
      arr.sort((a, b) => {
        if (b.items.length !== a.items.length) return b.items.length - a.items.length
        return b.kolCount - a.kolCount
      })
      return arr
    })

    const DIRECTION_COLOR = {
      '港股方向': '#5b8cff',
      '黄金': '#d4a017',
      '债券': '#67c23a',
      'CPO/光模块': '#f56c6c',
      '半导体/科创芯片': '#e6a23c',
      '创新药/医药': '#9b59b6',
      '全球科技/QDII': '#409eff',
      '白酒/消费': '#a87f0a',
      '资源/有色金属': '#909399',
      '量化/全市场': '#13c2c2',
      '固收+/股债混合': '#5b8cff',
      '其他/待分类': '#c0c4cc',
    }
    function directionStyle(d) {
      const c = DIRECTION_COLOR[d] || '#606266'
      return `color: ${c}; font-weight: 500;`
    }

    const isRunning = computed(() => status.value.status !== 'idle' && status.value.status !== 'success' && status.value.status !== 'failed')

    const statusText = computed(() => {
      const map = { crawling: '正在采集', starting_db: '检测/启动数据库', parsing: 'AI解析中', saving: '正在写入', success: '已完成', failed: '失败' }
      return map[status.value.status] || status.value.status
    })

    const statusClass = computed(() => 'status-' + (status.value.status === 'success' ? 'success' : status.value.status === 'failed' ? 'failed' : 'running'))

    const maxPage = computed(() => Math.max(1, Math.ceil(oTotal.value / 20)))

    function loadOps(p) {
      page.value = p || 1
      getTodayOps(page.value).then(r => {
        ops.value = r.data.items || []
        oTotal.value = r.data.total || 0
        summary.value = { total: r.data.total || 0, buy: 0, sell: 0 }
      }).catch(() => {})
    }

    function pollStatus() {
      getCurrentRun().then(r => {
        const newLogs = r.data.logs
        const prevLen = (status.value && status.value.logs) ? status.value.logs.length : 0
        status.value = r.data
        // 从最新日志解析当前页数（main.py 输出 "# 页面N"）
        if (newLogs && newLogs.length) {
          for (let i = newLogs.length - 1; i >= 0; i--) {
            const m = /#\s*页面\s*(\d+)/.exec(newLogs[i])
            if (m) { currentPage.value = parseInt(m[1], 10); break }
          }
        }
        const s = r.data.status
        if (s === 'success') {
          loadOps(1)
        }
        if (s === 'idle' || s === 'success' || s === 'failed') {
          if (timer) { clearInterval(timer); timer = null }
        }
      }).catch(() => {})
    }

    function startRun() {
      if (isRunning.value) return
      apiStart().then(r => {
        if (r.data.ok) {
          timer = setInterval(pollStatus, 1000)
          status.value = { status: 'crawling', message: '正在启动...' }
        } else {
          alert(r.data.message)
        }
      })
    }

    function refreshReports() {
      getReports().then(r => {
        reports.value = r.data.items || []
        reportStatus.value = r.data.status || { status: 'idle' }
      }).catch(() => {})
    }

    function pollReports() {
      getReports().then(r => {
        reports.value = r.data.items || []
        reportStatus.value = r.data.status || { status: 'idle' }
        if (reportStatus.value.status === 'generating') {
          if (!reportTimer) { reportTimer = setInterval(pollReports, 3000) }
        } else {
          if (reportTimer) { clearInterval(reportTimer); reportTimer = null }
        }
      }).catch(() => {})
    }

    function generateOne(date) {
      if (reportStatus.value.status === 'generating') return
      // 乐观更新：立刻把这一行状态切到「生成中」
      const idx = reports.value.findIndex(r => r.date === date)
      if (idx >= 0) {
        reports.value[idx] = { ...reports.value[idx], report_status: 'generating' }
      }
      generateReports(date).then(r => {
        if (r.data.ok) {
          reportStatus.value = { status: 'generating' }
          pollReports()
        } else {
          alert(r.data.message)
          if (idx >= 0) {
            reports.value[idx] = { ...reports.value[idx], report_status: 'failed' }
          }
        }
      }).catch(() => {})
    }

    function viewReport(item) {
      // 打开弹窗，先加载结构化数据；如果结构化接口 404，再回退到 markdown
      viewing.value = { date: item.date, showMarkdown: false, markdown: '', structured: null }
      getReportStructured(item.date).then(r => {
        if (r.data.ok) {
          viewing.value = {
            date: item.date,
            showMarkdown: false,
            markdown: (r.data.format === 'legacy') ? r.data.structured.summary : '',
            structured: r.data.structured,
          }
          // 顺便缓存 markdown 内容（如果结构化接口是 legacy 模式，summary 即为完整 markdown）
          if (r.data.format === 'legacy') {
            getReportContent(item.date).then(mr => {
              if (mr.data.ok && viewing.value && viewing.value.date === item.date) {
                viewing.value.markdown = mr.data.content
              }
            }).catch(() => {})
          }
        } else {
          alert(r.data.message)
          viewing.value = null
        }
      }).catch(() => {
        viewing.value = null
      })
    }

    function viewRawOps(item) {
      groupByDirection.value = false
      getOpsByDate(item.date).then(r => {
        const items = (r.data && r.data.items) || []
        viewingOps.value = { date: item.date, items }
      }).catch(() => {
        viewingOps.value = { date: item.date, items: [] }
      })
    }

    onMounted(() => {
      loadOps(1)
      pollStatus()
      refreshReports()
      if (isRunning.value) { timer = setInterval(pollStatus, 1000) }
    })

    onUnmounted(() => {
      if (timer) clearInterval(timer)
      if (reportTimer) clearInterval(reportTimer)
    })

    return { status, ops, page, oTotal, summary, expanded, reports, reportStatus, viewing, viewingOps, groupByDirection, groupedOps, viewingHtml, isRunning, statusText, statusClass, maxPage, logBodyRef, currentPage, directionStyle, startRun, loadOps, downloadExcel, refreshReports, generateOne, viewReport, viewRawOps }
  }
}
</script>
