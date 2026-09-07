<template>
  <div>
    <div class="card">
      <div class="card-title">历史操作记录</div>
      <div class="filter-row">
        <input type="date" v-model="dateFrom" placeholder="开始日期" />
        <input type="date" v-model="dateTo" placeholder="结束日期" />
        <input type="text" v-model="kolName" placeholder="大V名称" style="width:140px" />
        <select v-model="opType">
          <option value="">全部类型</option>
          <option value="买入">买入</option>
          <option value="卖出">卖出</option>
          <option value="定投">定投</option>
          <option value="撤销">撤销</option>
        </select>
        <input type="text" v-model="fundName" placeholder="基金名称" style="width:200px" />
        <button class="btn btn-primary btn-sm" @click="search">查询</button>
      </div>

      <div style="overflow-x: auto;">
        <table>
          <thead>
            <tr><th>日期</th><th>大V</th><th>发布时间</th><th>操作类型</th><th>基金名称</th><th>买入金额</th><th>卖出份额</th><th>状态</th></tr>
          </thead>
          <tbody>
            <tr v-for="op in ops" :key="op.id">
              <td>{{ op.collect_date }}</td>
              <td><router-link :to="'/kol/' + (op.id)">{{ op.kol_name }}</router-link></td>
              <td>{{ op.publish_time }}</td>
              <td>{{ op.operation_type }}</td>
              <td>{{ op.fund_name }}</td>
              <td>{{ op.buy_amount }}</td>
              <td>{{ op.sell_shares }}</td>
              <td>{{ op.operation_status }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="pagination">
        <button class="btn btn-default btn-sm" :disabled="page <= 1" @click="load(page - 1)">上一页</button>
        <span>第 {{ page }} / {{ maxPage }} 页（共 {{ total }} 条）</span>
        <button class="btn btn-default btn-sm" :disabled="page >= maxPage" @click="load(page + 1)">下一页</button>
        <span class="page-jump">
          跳至
          <input
            type="number"
            v-model.number="jumpPage"
            min="1"
            :max="maxPage"
            @keyup.enter="jumpToPage"
            style="width:60px"
          />
          页
          <button class="btn btn-default btn-sm" @click="jumpToPage">跳转</button>
        </span>
      </div>
    </div>
  </div>
</template>

<script>
import { ref, computed, onMounted } from 'vue'
import { getHistoryOps } from '../utils/api.js'

export default {
  setup() {
    const ops = ref([])
    const page = ref(1)
    const total = ref(0)
    const dateFrom = ref('')
    const dateTo = ref('')
    const kolName = ref('')
    const opType = ref('')
    const fundName = ref('')
    const jumpPage = ref(1)

    const maxPage = computed(() => Math.max(1, Math.ceil(total.value / 20)))

    function load(p) {
      page.value = p || 1
      const params = { page: page.value, page_size: 20 }
      if (dateFrom.value) params.date_from = dateFrom.value
      if (dateTo.value) params.date_to = dateTo.value
      if (kolName.value) params.kol_name = kolName.value
      if (opType.value) params.operation_type = opType.value
      if (fundName.value) params.fund_name = fundName.value

      getHistoryOps(params).then(r => {
        ops.value = r.data.items || []
        total.value = r.data.total || 0
        jumpPage.value = page.value
      })
    }

    function search() { load(1) }

    function jumpToPage() {
      const target = parseInt(jumpPage.value, 10)
      if (isNaN(target) || target < 1 || target > maxPage.value) {
        return
      }
      load(target)
    }

    onMounted(() => { load(1) })

    return { ops, page, total, dateFrom, dateTo, kolName, opType, fundName, jumpPage, maxPage, load, search, jumpToPage }
  }
}
</script>
