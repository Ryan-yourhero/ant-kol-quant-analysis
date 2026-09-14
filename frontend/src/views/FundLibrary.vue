<template>
  <div>
    <div class="card">
      <div class="card-title" style="display: flex; justify-content: space-between; align-items: center;">
        <div>
          <span>基金方向库</span>
          <div style="font-size: 12px; color: #909399; margin-top: 4px; font-weight: normal;">
            维护基金与投资方向映射，人工确认结果将作为 AI 分析的最高优先级依据。
          </div>
        </div>
        <div style="display: flex; gap: 12px;">
          <button class="btn btn-default btn-sm" @click="filterUnconfirmedOnly = !filterUnconfirmedOnly; reload(1)"
                  :style="filterUnconfirmedOnly ? 'color: #409eff; border-color: #409eff;' : ''">
            {{ filterUnconfirmedOnly ? '✓ 仅看待确认' : '仅看待确认' }}
          </button>
          <button class="btn btn-default btn-sm" @click="exportFunds">导出</button>
          <button class="btn btn-default btn-sm" @click="triggerImport">导入</button>
          <input ref="fileInputRef" type="file" accept=".json" @change="onImportFile" style="display: none;" />
          <button class="btn btn-primary btn-sm" @click="openCreate()">+ 新增基金</button>
        </div>
      </div>

      <!-- 表头下方筛选行 -->
      <div style="display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 12px;">
        <input v-model="filters.keyword" @keyup.enter="reload(1)" placeholder="基金名称"
               style="width: 180px; padding: 4px 8px; border: 1px solid #dcdfe6; border-radius: 4px;" />
        <input v-model="filters.fund_code" @keyup.enter="reload(1)" placeholder="基金代码"
               style="width: 100px; padding: 4px 8px; border: 1px solid #dcdfe6; border-radius: 4px;" />
        <select v-model="filters.direction" @change="reload(1)" style="padding: 4px 8px; border: 1px solid #dcdfe6; border-radius: 4px;">
          <option value="">投资方向（全部）</option>
          <option v-for="d in options.direction" :key="d.value" :value="d.value">{{ d.label }}</option>
        </select>
        <select v-model="filters.fund_type" @change="reload(1)" style="padding: 4px 8px; border: 1px solid #dcdfe6; border-radius: 4px;">
          <option value="">基金类型（全部）</option>
          <option v-for="d in options.fund_type" :key="d.value" :value="d.value">{{ d.label }}</option>
        </select>
        <select v-model="filters.source" @change="reload(1)" style="padding: 4px 8px; border: 1px solid #dcdfe6; border-radius: 4px;">
          <option value="">来源（全部）</option>
          <option v-for="d in options.source" :key="d.value" :value="d.value">{{ d.label }}</option>
        </select>
        <select v-model="filters.confidence" @change="reload(1)" style="padding: 4px 8px; border: 1px solid #dcdfe6; border-radius: 4px;">
          <option value="">置信度（全部）</option>
          <option v-for="d in options.confidence" :key="d.value" :value="d.value">{{ d.label }}</option>
        </select>
        <select v-model="filters.status" @change="reload(1)" style="padding: 4px 8px; border: 1px solid #dcdfe6; border-radius: 4px;">
          <option value="">状态（全部）</option>
          <option v-for="d in options.status" :key="d.value" :value="d.value">{{ d.label }}</option>
        </select>
        <input v-model="filters.evidence_period" @keyup.enter="reload(1)" placeholder="证据周期"
               style="width: 90px; padding: 4px 8px; border: 1px solid #dcdfe6; border-radius: 4px;" />
        <input v-model="filters.updated_date" @change="reload(1)" type="date" title="最后更新日期"
               style="width: 140px; padding: 4px 8px; border: 1px solid #dcdfe6; border-radius: 4px;" />
        <button class="btn btn-primary btn-sm" @click="reload(1)">查询</button>
        <button class="btn btn-default btn-sm" @click="resetFilters()">重置</button>
      </div>

      <div style="overflow-x: auto;">
        <table>
          <thead>
            <tr>
              <th style="width: 40px;"><input type="checkbox" :checked="isAllSelected" @change="toggleSelectAll" /></th>
              <th>序号</th>
              <th>基金名称</th>
              <th>投资方向</th>
              <th>基金类型</th>
              <th>状态</th>
              <th>证据周期</th>
              <th style="min-width: 220px;">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(it, idx) in items" :key="it.id">
              <td><input type="checkbox" :checked="selectedIds.has(it.id)" @change="toggleSelect(it.id)" /></td>
              <td>{{ (page - 1) * pageSize + idx + 1 }}</td>
              <td>
                <span :style="it.classification_source === 'manual' ? 'font-weight: 600; color: #409eff;' : ''">{{ it.fund_name }}</span>
              </td>
              <td>{{ it.direction_label }}</td>
              <td>{{ it.fund_type_label }}</td>
              <td>
                <span :style="statusStyle(it.status_label)">{{ it.status_label }}</span>
              </td>
              <td>{{ it.evidence_period || '-' }}</td>
              <td>
                <button v-if="it.status_label === '已确认'" class="btn btn-default btn-sm btn-row" @click="openEdit(it)">编辑</button>
                <button v-if="it.status_label === '已确认'" class="btn btn-default btn-sm btn-row" @click="openEvidence(it)">查看依据</button>
                <!-- manual verified → 不显示重新分析 -->
                <button v-if="it.status_label === '临时判断'" class="btn btn-primary btn-sm btn-row" @click="quickConfirm(it)">确认</button>
                <button v-if="it.status_label === '临时判断'" class="btn btn-default btn-sm btn-row" @click="openEdit(it)">编辑</button>
                <button v-if="it.status_label === '临时判断'" class="btn btn-default btn-sm btn-row" @click="openEvidence(it)">查看依据</button>
                <button v-if="it.status_label === '临时判断' && it.classification_source !== 'manual'" class="btn btn-default btn-sm btn-row" @click="doReanalyze(it)">重新分析</button>

                <button v-if="it.status_label === '待确认'" class="btn btn-primary btn-sm btn-row" @click="openEdit(it)">确认方向</button>
                <button v-if="it.status_label === '待确认'" class="btn btn-default btn-sm btn-row" @click="openEdit(it)">编辑</button>
                <button v-if="it.status_label === '待确认'" class="btn btn-default btn-sm btn-row" @click="openEvidence(it)">查看依据</button>
                <button v-if="it.status_label === '待确认' && it.classification_source !== 'manual'" class="btn btn-default btn-sm btn-row" @click="doReanalyze(it)">重新分析</button>
              </td>
            </tr>
            <tr v-if="!items.length">
              <td colspan="7" style="color: #909399; text-align: center; padding: 24px;">暂无数据</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="pagination">
        <button class="btn btn-default btn-sm" :disabled="page <= 1" @click="reload(page - 1)">上一页</button>
        <span>第 {{ page }} / {{ maxPage }} 页（共 {{ total }} 条）</span>
        <button class="btn btn-default btn-sm" :disabled="page >= maxPage" @click="reload(page + 1)">下一页</button>
      </div>
    </div>

    <!-- 新增 / 编辑 弹窗 -->
    <div v-if="editing" class="modal-mask" @click.self="editing = null">
      <div class="modal" style="max-width: 600px;">
        <div class="modal-head">
          <span>{{ editing.id ? '编辑基金' : '新增基金' }}</span>
          <button class="btn btn-default btn-sm" @click="editing = null">关闭</button>
        </div>
        <div class="report-content">
          <div style="display: flex; flex-direction: column; gap: 12px;">
            <div>
              <label style="display: block; font-size: 13px; color: #606266; margin-bottom: 4px;">基金名称 *</label>
              <input v-model="editing.fund_name" :disabled="!!editing.id"
                     style="width: 100%; padding: 6px 8px; border: 1px solid #dcdfe6; border-radius: 4px;" />
              <div v-if="editing.id" style="font-size: 12px; color: #909399; margin-top: 4px;">基金名称不可修改</div>
            </div>
            <div>
              <label style="display: block; font-size: 13px; color: #606266; margin-bottom: 4px;">基金代码</label>
              <input v-model="editing.fund_code" :disabled="!!editing.id"
                     style="width: 100%; padding: 6px 8px; border: 1px solid #dcdfe6; border-radius: 4px;" />
            </div>
            <div>
              <label style="display: block; font-size: 13px; color: #606266; margin-bottom: 4px;">投资方向 *</label>
              <select v-model="editing.direction"
                      style="width: 100%; padding: 6px 8px; border: 1px solid #dcdfe6; border-radius: 4px;">
                <option v-for="d in options.direction" :key="d.value" :value="d.value">{{ d.label }}</option>
              </select>
            </div>
            <div>
              <label style="display: block; font-size: 13px; color: #606266; margin-bottom: 4px;">基金类型</label>
              <select v-model="editing.fund_type"
                      style="width: 100%; padding: 6px 8px; border: 1px solid #dcdfe6; border-radius: 4px;">
                <option value="">（不指定）</option>
                <option v-for="d in options.fund_type" :key="d.value" :value="d.value">{{ d.label }}</option>
              </select>
            </div>
            <div>
              <label style="display: block; font-size: 13px; color: #606266; margin-bottom: 4px;">备注</label>
              <textarea v-model="editing.evidence" rows="3"
                        style="width: 100%; padding: 6px 8px; border: 1px solid #dcdfe6; border-radius: 4px;"></textarea>
            </div>
          </div>
          <div style="margin-top: 16px; text-align: right; display: flex; gap: 8px; justify-content: flex-end;">
            <button class="btn btn-default btn-sm" @click="editing = null">取消</button>
            <button class="btn btn-primary btn-sm" @click="saveEdit" :disabled="!editing.fund_name || !editing.direction">保存</button>
          </div>
        </div>
      </div>
    </div>

    <!-- AI 依据抽屉 -->
    <div v-if="evidence" class="modal-mask" @click.self="evidence = null">
      <div class="modal" style="max-width: 900px;">
        <div class="modal-head">
          <span>判断依据 — {{ evidence.fund_name }}（{{ evidence.fund_code || '-' }}）</span>
          <button class="btn btn-default btn-sm" @click="evidence = null">关闭</button>
        </div>
        <div class="report-content">
          <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px;">
            <div><b>投资方向：</b>{{ evidence.direction_label }}</div>
            <div><b>基金类型：</b>{{ evidence.fund_type_label }}</div>
            <div><b>来源：</b>{{ evidence.source_label }}</div>
            <div><b>置信度：</b>{{ evidence.confidence_label }}</div>
            <div><b>状态：</b>{{ evidence.status_label }}</div>
            <div><b>证据周期：</b>{{ evidence.evidence_period || '-' }}</div>
            <div><b>最近联网：</b>{{ formatTime(evidence.last_search_at) }}</div>
            <div><b>下次允许重验证：</b>{{ formatTime(evidence.next_reverify_at) }}</div>
          </div>
          <div style="margin-top: 12px;">
            <b>证据文本：</b>
            <div style="background: #f5f7fa; padding: 8px; border-radius: 4px; white-space: pre-wrap; max-height: 200px; overflow-y: auto; font-size: 13px;">
              {{ evidence.evidence || '（无）' }}
            </div>
          </div>
          <div v-if="evidence.source_url" style="margin-top: 12px;">
            <b>主来源 URL：</b>
            <a :href="evidence.source_url" target="_blank" style="color: #409eff;">{{ evidence.source_url }}</a>
          </div>
          <div v-if="evidence.evidence_items && evidence.evidence_items.length" style="margin-top: 12px;">
            <b>证据明细（{{ evidence.evidence_items.length }} 条）：</b>
            <table style="width: 100%; font-size: 12px; margin-top: 6px;">
              <thead>
                <tr style="background: #f5f7fa;">
                  <th style="padding: 4px 8px; text-align: left;">来源类型</th>
                  <th style="padding: 4px 8px; text-align: left;">身份校验</th>
                  <th style="padding: 4px 8px; text-align: left;">匹配基金代码</th>
                  <th style="padding: 4px 8px; text-align: left;">URL</th>
                  <th style="padding: 4px 8px; text-align: left;">摘要</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(it, i) in evidence.evidence_items" :key="i" style="border-bottom: 1px solid #ebeef5;">
                  <td style="padding: 4px 8px;">{{ it.source_type || '-' }}</td>
                  <td style="padding: 4px 8px;">{{ it.identity_confidence || '-' }}</td>
                  <td style="padding: 4px 8px;">{{ it.matched_fund_code || '-' }}</td>
                  <td style="padding: 4px 8px; max-width: 240px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">
                    <a v-if="it.url" :href="it.url" target="_blank" style="color: #409eff;">{{ it.url }}</a>
                  </td>
                  <td style="padding: 4px 8px; max-width: 280px; word-break: break-all;">{{ it.snippet_excerpt || '-' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
import { ref, computed, onMounted } from 'vue'
import axios from 'axios'
import {
  listFunds, getFundOptions,
  createFund, updateFund, confirmFund, reanalyzeFund, getFundEvidence
} from '../utils/api.js'

export default {
  setup() {
    const items = ref([])
    const total = ref(0)
    const page = ref(1)
    const pageSize = ref(20)
    const options = ref({ direction: [], fund_type: [], source: [], confidence: [], status: [] })
    const filterUnconfirmedOnly = ref(false)

    const filters = ref({
      keyword: '', fund_code: '', direction: '', fund_type: '',
      source: '', confidence: '', status: '', evidence_period: '', updated_date: '',
    })

    const editing = ref(null)
    const evidence = ref(null)
    const selectedIds = ref(new Set())
    const fileInputRef = ref(null)

    const maxPage = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)))
    const isAllSelected = computed(() => {
      if (!items.value.length) return false
      return items.value.every(it => selectedIds.value.has(it.id))
    })

    function formatTime(s) {
      if (!s) return '-'
      try {
        const d = new Date(s)
        if (isNaN(d.getTime())) return s
        const pad = n => String(n).padStart(2, '0')
        return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
      } catch { return s }
    }

    function statusStyle(s) {
      if (s === '已确认') return 'color: #52c41a; font-weight: 600;'
      if (s === '待确认') return 'color: #f56c6c;'
      return 'color: #e6a23c;'
    }

    function reload(p) {
      page.value = p || 1
      const params = { page: page.value, page_size: pageSize.value }
      if (filterUnconfirmedOnly.value) params.status = 'unconfirmed'
      if (filters.value.keyword) params.keyword = filters.value.keyword
      if (filters.value.fund_code) params.fund_code = filters.value.fund_code
      if (filters.value.direction) params.direction = filters.value.direction
      if (filters.value.fund_type) params.fund_type = filters.value.fund_type
      if (filters.value.source) params.source = filters.value.source
      if (filters.value.confidence) params.confidence = filters.value.confidence
      if (filters.value.status && !filterUnconfirmedOnly.value) params.status = filters.value.status
      if (filters.value.evidence_period) params.evidence_period = filters.value.evidence_period
      if (filters.value.updated_date) params.updated_date = filters.value.updated_date

      listFunds(params).then(r => {
        items.value = r.data.items || []
        total.value = r.data.total || 0
        // 不再覆盖 options：options 由 onMounted 调 getFundOptions() 填好 [{value,label}]
      }).catch(e => {
        console.error('list funds error', e)
      })
    }

    function resetFilters() {
      filters.value = {
        keyword: '', fund_code: '', direction: '', fund_type: '',
        source: '', confidence: '', status: '', evidence_period: '', updated_date: '',
      }
      filterUnconfirmedOnly.value = false
      reload(1)
    }

    function openCreate() {
      editing.value = {
        id: null,
        fund_name: '', fund_code: '', direction: options.value.direction[0]?.value || '',
        fund_type: '', evidence: '',
      }
    }

    function openEdit(it) {
      editing.value = {
        id: it.id,
        fund_name: it.fund_name,
        fund_code: it.fund_code || '',
        direction: it.direction,
        fund_type: it.fund_type_detail || it.fund_type || '',
        evidence: it.evidence || '',
      }
    }

    function openEvidence(it) {
      getFundEvidence(it.id).then(r => {
        evidence.value = r.data
      }).catch(e => {
        alert('获取依据失败：' + (e.response?.data?.detail || e.message))
      })
    }

    function quickConfirm(it) {
      if (!confirm('确认把该基金方向标为「已确认」？')) return
      confirmFund(it.id, { fund_id: it.id, direction: it.direction }).then(r => {
        if (r.data.ok) {
          reload(page.value)
        } else {
          alert('确认失败')
        }
      })
    }

    function doReanalyze(it) {
      if (!confirm('重新 AI 分析会调用 Tavily + LLM（约 5-10 秒），确定？')) return
      reanalyzeFund(it.id).then(r => {
        if (r.data.ok) {
          alert(`已重新分析：direction=${r.data.direction}  conf=${r.data.confidence}  source=${r.data.source}`)
          reload(page.value)
        } else {
          alert('重分析失败：' + (r.data.detail || '未知错误'))
        }
      }).catch(e => {
        const detail = e.response?.data?.detail
        alert('重分析失败：' + (typeof detail === 'string' ? detail : JSON.stringify(detail || e.message)))
      })
    }

    function toggleSelect(id) {
      const s = new Set(selectedIds.value)
      if (s.has(id)) s.delete(id)
      else s.add(id)
      selectedIds.value = s
    }
    function toggleSelectAll(e) {
      const s = new Set(selectedIds.value)
      if (e.target.checked) {
        items.value.forEach(it => s.add(it.id))
      } else {
        items.value.forEach(it => s.delete(it.id))
      }
      selectedIds.value = s
    }

    function exportFunds() {
      // 导出当前筛选结果的所有基金（不只选中项）
      const params = {}
      if (filterUnconfirmedOnly.value) params.status = 'unconfirmed'
      if (filters.value.keyword) params.keyword = filters.value.keyword
      if (filters.value.fund_code) params.fund_code = filters.value.fund_code
      if (filters.value.direction) params.direction = filters.value.direction
      if (filters.value.fund_type) params.fund_type = filters.value.fund_type
      if (filters.value.source) params.source = filters.value.source
      if (filters.value.confidence) params.confidence = filters.value.confidence
      if (filters.value.status && !filterUnconfirmedOnly.value) params.status = filters.value.status
      if (filters.value.evidence_period) params.evidence_period = filters.value.evidence_period
      params.page = 1
      params.page_size = 100
      axios.get('/api/funds/export', { params, responseType: 'blob' }).then(r => {
        const blob = new Blob([r.data], { type: 'application/json' })
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url
        const ts = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14)
        a.download = `fund_direction_master_${ts}.json`
        document.body.appendChild(a)
        a.click()
        document.body.removeChild(a)
        URL.revokeObjectURL(url)
      }).catch(e => {
        alert('导出失败：' + (e.response?.data?.detail || e.message))
      })
    }

    function triggerImport() {
      fileInputRef.value && fileInputRef.value.click()
    }
    function onImportFile(e) {
      const file = e.target.files && e.target.files[0]
      if (!file) return
      if (!confirm(`确认导入 ${file.name}？\n将按 fund_code / fund_name 匹配并覆盖现有记录。`)) {
        e.target.value = ''
        return
      }
      const reader = new FileReader()
      reader.onload = () => {
        let payload
        try { payload = JSON.parse(reader.result) }
        catch { alert('文件不是合法 JSON'); e.target.value = ''; return }
        const items = Array.isArray(payload) ? payload : (payload.items || [])
        if (!items.length) { alert('文件中没有 fund 记录'); e.target.value = ''; return }
        axios.post('/api/funds/import', { items }, { timeout: 60000 }).then(r => {
          const d = r.data || {}
          alert(`导入完成：新增 ${d.added || 0} 条 / 更新 ${d.updated || 0} 条 / 失败 ${d.failed || 0} 条`)
          reload(page.value)
        }).catch(err => {
          alert('导入失败：' + (err.response?.data?.detail || err.message))
        })
        e.target.value = ''
      }
      reader.readAsText(file)
    }

    function saveEdit() {
      const body = {
        fund_name: editing.value.fund_name.trim(),
        fund_code: editing.value.fund_code.trim() || null,
        direction: editing.value.direction,
        fund_type: editing.value.fund_type || null,
        evidence: editing.value.evidence || null,
      }
      if (editing.value.id) {
        // 编辑
        updateFund(editing.value.id, body).then(r => {
          if (r.data.ok) {
            editing.value = null
            reload(page.value)
          } else {
            alert('保存失败：' + (r.data.detail || '未知'))
          }
        }).catch(e => {
          const detail = e.response?.data?.detail
          alert('保存失败：' + (typeof detail === 'string' ? detail : JSON.stringify(detail || e.message)))
        })
      } else {
        // 新增
        createFund(body).then(r => {
          if (r.data.ok) {
            editing.value = null
            reload(1)
          } else {
            alert('新增失败：' + (r.data.detail || '未知'))
          }
        }).catch(e => {
          const detail = e.response?.data?.detail
          if (e.response?.status === 409) {
            if (confirm('该基金已存在，是否进入编辑？')) {
              const existingId = detail?.existing_id
              if (existingId) {
                getFundDetail(existingId).then(r => {
                  editing.value = {
                    id: r.data.id,
                    fund_name: r.data.fund_name,
                    fund_code: r.data.fund_code || '',
                    direction: r.data.direction,
                    fund_type: r.data.fund_type_detail || r.data.fund_type || '',
                    evidence: r.data.evidence || '',
                  }
                })
              }
            }
          } else {
            alert('新增失败：' + (typeof detail === 'string' ? detail : JSON.stringify(detail || e.message)))
          }
        })
      }
    }

    onMounted(() => {
      getFundOptions().then(r => {
        options.value = r.data
      })
      reload(1)
    })

    return {
      items, total, page, pageSize, maxPage, options, filters, filterUnconfirmedOnly,
      editing, evidence, selectedIds, isAllSelected, fileInputRef,
      formatTime, statusStyle,
      reload, resetFilters,
      openCreate, openEdit, openEvidence, quickConfirm, doReanalyze, saveEdit,
      toggleSelect, toggleSelectAll,
      exportFunds, triggerImport, onImportFile,
    }
  }
}
</script>

<style scoped>
.btn-row { margin-right: 6px; margin-bottom: 4px; }
</style>