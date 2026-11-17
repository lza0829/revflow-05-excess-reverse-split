<script setup>
import { computed, onMounted, ref } from 'vue'
import { getJson } from '../api'

const rows = ref([])
const err = ref('')

onMounted(async () => {
  try {
    rows.value = await getJson('/api/quarantine')
  } catch (e) {
    err.value = String(e.message || e)
  }
})

function yuan(cents) {
  return (Number(cents || 0) / 100).toFixed(2)
}

function isSplit(q) {
  return q.reason === '冲正超额' && (q.split_applied_cents || 0) > 0
}

const splitRows = computed(() => rows.value.filter((q) => q.reason === '冲正超额'))
const excessTotal = computed(() =>
  splitRows.value.reduce((s, q) => s + (q.amount_cents || 0), 0),
)
const appliedTotal = computed(() =>
  splitRows.value.reduce((s, q) => s + (q.split_applied_cents || 0), 0),
)
</script>

<template>
  <div>
    <h1>未平隔离</h1>
    <p class="lead">冲正无原单、账户不一致或超出目标剩余敞口的部分，未能配平即在此保留可查。</p>
    <p class="note-strip">
      超额隔离共 {{ splitRows.length }} 行，超额合计 {{ yuan(excessTotal) }}；
      同行可配平前缀合计 {{ yuan(appliedTotal) }}，已计入账户余额。两者共享同一原冲正键。
    </p>
    <p v-if="err" class="msg">{{ err }}</p>
    <div class="panel">
      <table>
        <thead>
          <tr>
            <th>流水键</th><th>账户</th><th>原因</th><th>隔离金额（元）</th><th>其中已配平（元）</th><th>原敞口（元）</th><th>拆分</th><th>归并#</th><th>时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="q in rows" :key="q.id">
            <td>{{ q.entry_key }}</td>
            <td>{{ q.account_code }}</td>
            <td>
              <span class="tag warn">{{ q.reason }}</span>
            </td>
            <td>{{ yuan(q.amount_cents) }}</td>
            <td>{{ isSplit(q) ? yuan(q.split_applied_cents) : '—' }}</td>
            <td>{{ q.reason === '冲正超额' ? yuan(q.exposure_cents) : '—' }}</td>
            <td>
              <span v-if="q.reason === '冲正超额'" class="tag" :class="isSplit(q) ? 'warn' : 'bad'">
                {{ q.split_kind }}
              </span>
              <span v-else>—</span>
            </td>
            <td>{{ q.settle_run_id }}</td>
            <td>{{ q.created_at }}</td>
          </tr>
          <tr v-if="!rows.length"><td colspan="9">暂无隔离项（先跑归并）</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
