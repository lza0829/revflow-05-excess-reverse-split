<script setup>
import { computed, onMounted, ref } from 'vue'
import { getJson } from '../api'

const rows = ref([])
const err = ref('')

onMounted(async () => {
  try {
    rows.value = await getJson('/api/entries?kind=冲正')
  } catch (e) {
    err.value = String(e.message || e)
  }
})

function yuan(cents) {
  return (Number(cents || 0) / 100).toFixed(2)
}

function applied(e) {
  return e.applied_cents ?? e.amount_cents
}

function excess(e) {
  return e.excess_cents ?? 0
}

function tagClass(s) {
  return { on: s === '已归并', warn: s === '部分归并', bad: s === '已隔离' }
}

const splitCount = computed(() => rows.value.filter((e) => excess(e) > 0).length)
</script>

<template>
  <div>
    <h1>冲正流水</h1>
    <p class="lead">
      冲正须关联原正向流水键；归并时不超过目标剩余敞口的部分配平计入余额，超出部分作为超额隔离保留，两项共享同一原冲正键。
    </p>
    <p class="note-strip">
      分栏「已配平 / 超额」写入后不可变；超额 &gt; 0 的冲正共 {{ splitCount }} 笔，可在未平隔离页按原冲正键溯源。
    </p>
    <p v-if="err" class="msg">{{ err }}</p>
    <div class="panel">
      <table>
        <thead>
          <tr>
            <th>流水键</th><th>账户</th><th>冲正金额（元）</th><th>已配平（元）</th><th>超额（元）</th><th>原单键</th><th>业务单</th><th>状态</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="e in rows" :key="e.id">
            <td>{{ e.entry_key }}</td>
            <td>{{ e.account_code }}</td>
            <td>{{ yuan(e.amount_cents) }}</td>
            <td>{{ e.applied_cents == null ? '—' : yuan(e.applied_cents) }}</td>
            <td>
              <span :class="excess(e) > 0 ? 'tag warn' : ''">
                {{ e.excess_cents == null ? '—' : yuan(e.excess_cents) }}
              </span>
            </td>
            <td>{{ e.target_entry_key || '—' }}</td>
            <td>{{ e.biz_ref }}</td>
            <td><span class="tag" :class="tagClass(e.status)">{{ e.status }}</span></td>
          </tr>
          <tr v-if="!rows.length"><td colspan="8">尚无冲正流水</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
