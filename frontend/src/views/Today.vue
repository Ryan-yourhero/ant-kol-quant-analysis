<template>
  <div>
    <div class="card">
      <div class="card-title">每日采集</div>
      <div style="display: flex; align-items: center; gap: 16px; margin-bottom: 16px;">
        <button class="btn btn-primary" :disabled="isRunning" @click="startRun">
          {{ isRunning ? '任务进行中...' : '开始今日采集' }}
        </button>
        <span v-if="status.status !== 'idle'" class="status-tag" :class="statusClass">{{ statusText }}</span>
        <span v-if="status.message" style="color: #909399; font-size: 13px;">{{ status.message }}</span>
        <span v-if="status.error" style="color: #f56c6c; font-size: 13px;">{{ status.error }}</span>
      </div>
      <div v-if="status.logs && status.logs.length" class="log-box">
      <div class="log-head">
        <span>采集日志</span>
        <span class="log-tip">滚动查看最新进度（共 {{ status.logs.length }} 条）</span>
      </div>
      <div class="log-body" ref="logBodyRef">
        <div v-for="(line, i) in status.logs" :key="i" class="log-line">{{ line }}</div>
      </div>
    </div>
      </div>

    <div class="card">
      <div class="card-title" style="display: flex; justify-content: space-between; align-items: center;">
        <span>任务记录</span>
        <div style="display: flex; gap: 8px;">
          <button class="btn btn-default btn-sm" @click="refreshReports">刷新</button>
          <button class="btn btn-primary btn-sm" :disabled="reportStatus.status === 'generating'" @click="generateAllReports">
            {{ reportStatus.status === 'generating' ? '生成中...' : 'AI报告生成全部历史报告' }}
          </button>
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
                <span v-if="r.has_report" style="color: #52c41a;">已生成</span>
                <span v-else style="color: #909399;">未生成</span>
              </td>
              <td>
                <button v-if="r.has_report" class="btn btn-default btn-sm with-gap" @click="viewReport(r)">查看AI报告</button>
                <button class="btn btn-default btn-sm with-gap" @click="viewRawOps(r)">查看原始交易记录</button>
                <button v-if="r.has_report" class="btn btn-primary btn-sm with-gap" :disabled="reportStatus.status === 'generating'" @click="generateOne(r.date)">重新生成报告</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div v-if="viewing" class="modal-mask" @click.self="viewing = null">
      <div class="modal">
        <div class="modal-head">
          <span>{{ viewing.date }} 每日分析报告</span>
          <button class="btn btn-default btn-sm" @click="viewing = null">关闭</button>
        </div>
        <div class="report-content" v-html="viewingHtml"></div>
      </div>
    </div>

    <div v-if="viewingOps" class="modal-mask" @click.self="viewingOps = null">
      <div class="modal" style="max-width: 1400px;">
        <div class="modal-head">
          <span>{{ viewingOps.date }} 原始交易记录（共 {{ viewingOps.items.length }} 条）</span>
          <button class="btn btn-default btn-sm" @click="viewingOps = null">关闭</button>
        </div>
        <div class="report-content" style="padding: 0;">
          <div v-if="!viewingOps.items.length" style="color: #909399; padding: 16px;">该日期暂无交易记录</div>
          <table v-else style="width: 100%; font-size: 13px;">
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
import { startRun as apiStart, getCurrentRun, getTodayOps, downloadExcel, getReports, generateReports, getReportContent, getOpsByDate } from '../utils/api.js'

marked.setOptions({ breaks: true, gfm: true })

export default {
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
    const logBodyRef = ref(null)
    let timer = null
    let reportTimer = null

    const viewingHtml = computed(() => {
      if (!viewing.value) return ''
      return marked.parse(viewing.value.content || '')
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
        // 日志增长时，自动滚动到底部（最新一行可见）
        if (newLogs && newLogs.length > prevLen && logBodyRef.value) {
          nextTick(() => {
            if (logBodyRef.value) {
              logBodyRef.value.scrollTop = logBodyRef.value.scrollHeight
            }
          })
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

    function generateAllReports() {
      if (reportStatus.value.status === 'generating') return
      generateReports(null).then(r => {
        if (r.data.ok) {
          reportStatus.value = { status: 'generating' }
          pollReports()
        } else {
          alert(r.data.message)
        }
      }).catch(() => {})
    }

    function generateOne(date) {
      if (reportStatus.value.status === 'generating') return
      generateReports(date).then(r => {
        if (r.data.ok) {
          reportStatus.value = { status: 'generating' }
          pollReports()
        } else {
          alert(r.data.message)
        }
      }).catch(() => {})
    }

    function viewReport(item) {
      getReportContent(item.date).then(r => {
        if (r.data.ok) {
          viewing.value = { date: item.date, content: r.data.content }
        } else {
          alert(r.data.message)
        }
      }).catch(() => {})
    }

    function viewRawOps(item) {
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

    return { status, ops, page, oTotal, summary, expanded, reports, reportStatus, viewing, viewingOps, viewingHtml, isRunning, statusText, statusClass, maxPage, logBodyRef, directionStyle, startRun, loadOps, downloadExcel, refreshReports, generateAllReports, generateOne, viewReport, viewRawOps }
  }
}
</script>
