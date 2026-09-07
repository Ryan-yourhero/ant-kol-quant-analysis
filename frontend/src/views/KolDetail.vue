<template>
  <div>
    <div class="card" v-if="kol">
      <div class="card-title">{{ kol.name }}</div>
      <div class="stat-box" style="margin-bottom: 12px;">
        <span class="num">{{ total }}</span>
        <span class="label">历史操作数</span>
      </div>
    </div>

    <div class="card">
      <div class="card-title">操作记录</div>
      <div style="overflow-x: auto;">
        <table>
          <thead>
            <tr><th>日期</th><th>发布时间</th><th>操作类型</th><th>操作状态</th><th>基金名称</th><th>买入金额</th><th>卖出份额</th></tr>
          </thead>
          <tbody>
            <tr v-for="op in ops" :key="op.id">
              <td>{{ op.collect_date }}</td>
              <td>{{ op.publish_time }}</td>
              <td>{{ op.operation_type }}</td>
              <td>{{ op.operation_status }}</td>
              <td>{{ op.fund_name }}</td>
              <td>{{ op.buy_amount }}</td>
              <td>{{ op.sell_shares }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="pagination">
        <button class="btn btn-default btn-sm" :disabled="page <= 1" @click="load(page - 1)">上一页</button>
        <span>第 {{ page }} / {{ maxPage }} 页（共 {{ total }} 条）</span>
        <button class="btn btn-default btn-sm" :disabled="page >= maxPage" @click="load(page + 1)">下一页</button>
      </div>
    </div>
  </div>
</template>

<script>
import { ref, computed, onMounted } from 'vue'
import { getKolOps } from '../utils/api.js'

export default {
  props: ['id'],
  setup(props) {
    const kol = ref(null)
    const ops = ref([])
    const page = ref(1)
    const total = ref(0)
    const maxPage = computed(() => Math.max(1, Math.ceil(total.value / 20)))

    function load(p) {
      page.value = p || 1
      getKolOps(Number(props.id), page.value).then(r => {
        kol.value = r.data.kol
        ops.value = r.data.items || []
        total.value = r.data.total || 0
      })
    }

    onMounted(() => { load(1) })

    return { kol, ops, page, total, maxPage, load }
  }
}
</script>
