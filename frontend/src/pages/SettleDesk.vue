<script setup>
import { onMounted, ref } from 'vue'
import { getJson, postJson } from '../api'

const runs = ref([])
const err = ref('')
const msg = ref('')
const busy = ref(false)

async function load() {
  runs.value = await getJson('/api/settle-runs')
}

onMounted(async () => {
  try {
    await load()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

async function runSettle() {
  busy.value = true
  err.value = ''
  msg.value = ''
  try {
    const r = await postJson('/api/settle-runs')
    msg.value = `归并完成 #${r.id}：净额 ${(r.net_cents / 100).toFixed(2)}，已配平冲正 ${(r.applied_reversal_cents / 100).toFixed(2)}，超额隔离 ${(r.excess_quarantine_cents / 100).toFixed(2)}，剩余敞口 ${(r.remaining_exposure_cents / 100).toFixed(2)}`
    await load()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

function yuan(cents) {
  return (Number(cents || 0) / 100).toFixed(2)
}
</script>

<template>
  <div>
    <h1>归并台</h1>
    <p class="lead">对「待归并」正向与冲正跑一轮净额归并：可配平前缀落余额，超出目标敞口的额度作为超额隔离保留；无原单/账户不符整笔进隔离。</p>
    <p class="note-strip">本轮只处理状态为「待归并」的流水；配平段计入余额，超额隔离段不计入。整轮同事务，失败全部回到操作前。</p>
    <div class="row-actions">
      <button class="btn" :disabled="busy" @click="runSettle">跑净额归并</button>
    </div>
    <p v-if="msg" class="ok">{{ msg }}</p>
    <p v-if="err" class="msg">{{ err }}</p>
    <div class="panel">
      <h2>归并运行</h2>
      <table>
        <thead>
          <tr>
            <th>#</th><th>状态</th><th>正向</th><th>冲正</th><th>已归并</th><th>隔离</th><th>已配平冲正（元）</th><th>超额隔离（元）</th><th>剩余敞口（元）</th><th>净额（元）</th><th>结束</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in runs" :key="r.id">
            <td>{{ r.id }}</td>
            <td>{{ r.status }}</td>
            <td>{{ r.forward_count }}</td>
            <td>{{ r.reversal_count }}</td>
            <td>{{ r.settled_count }}</td>
            <td>{{ r.quarantine_count }}</td>
            <td>{{ yuan(r.applied_reversal_cents) }}</td>
            <td>{{ yuan(r.excess_quarantine_cents) }}</td>
            <td>{{ yuan(r.remaining_exposure_cents) }}</td>
            <td>{{ yuan(r.net_cents) }}</td>
            <td>{{ r.finished_at || '—' }}</td>
          </tr>
          <tr v-if="!runs.length"><td colspan="11">尚无归并运行</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
