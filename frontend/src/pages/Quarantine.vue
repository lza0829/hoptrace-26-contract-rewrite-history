<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api.js'

const rows = ref([])
const err = ref('')

function parseMember(text) {
  try {
    return JSON.parse(text)
  } catch {
    return null
  }
}

const enriched = computed(() =>
  rows.value.map((r) => {
    const m = parseMember(r.member_json)
    return {
      ...r,
      event_code: m?.event_code ?? '',
      contract_version: m?.contract_version ?? null,
      hop_id: m?.hop_id ?? '',
      violation: m?.violation ?? '',
    }
  }),
)

onMounted(async () => {
  try {
    rows.value = await api.quarantine()
  } catch (e) {
    err.value = String(e.message || e)
  }
})
</script>

<template>
  <div class="page-card">
    <h1>隔离</h1>
    <p class="lead">
      脏点 / 重复组 / 契约违规隔离区。契约违规由 event_code_contract 门禁写入：仅收未登记码，
      含当次契约版本与违规说明；登记码不得出现在这里（契约页可跑分流对照核证）。
    </p>
    <p v-if="err" class="muted">{{ err }}</p>
    <table class="table">
      <thead>
        <tr>
          <th>id</th><th>运单</th><th>批次</th><th>原因</th>
          <th>event_code</th><th>契约版本</th><th>hop</th><th>违规说明</th><th>时间</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="r in enriched" :key="r.id">
          <td>{{ r.id }}</td>
          <td>{{ r.waybill_id }}</td>
          <td>{{ r.batch_id }}</td>
          <td>{{ r.reason }}</td>
          <td>{{ r.event_code || '—' }}</td>
          <td>{{ r.contract_version != null ? `v${r.contract_version}` : '—' }}</td>
          <td>{{ r.hop_id || '—' }}</td>
          <td>{{ r.violation || r.member_json }}</td>
          <td>{{ r.created_at }}</td>
        </tr>
        <tr v-if="!rows.length"><td colspan="9" class="muted">暂无隔离项</td></tr>
      </tbody>
    </table>
  </div>
  <!-- pad-26 continuity -->
</template>
