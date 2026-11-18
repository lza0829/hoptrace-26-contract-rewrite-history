<script setup>
import { ref } from 'vue'
import { api } from '../api.js'

const result = ref(null)
const busy = ref(false)
const err = ref('')

async function run() {
  busy.value = true
  err.value = ''
  try {
    result.value = await api.ingest()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="page-card">
    <h1>装载</h1>
    <p class="lead">
      从本地夹具跑一批轨迹点装载。装载入口按当前事件码契约版本逐点分流：
      契约内进正式时间线，契约外进隔离（写明契约违规）。
    </p>
    <button class="btn" :disabled="busy" @click="run">{{ busy ? '装载中…' : '跑装载批次' }}</button>
    <p v-if="err" class="muted" style="margin-top:12px">{{ err }}</p>

    <template v-if="result?.ok">
      <div class="stat-row" style="margin-top:16px">
        <div class="stat"><b>v{{ result.report.contract_version }}</b><span>契约版本</span></div>
        <div class="stat"><b>{{ result.report.input }}</b><span>输入点</span></div>
        <div class="stat"><b>{{ result.report.written }}</b><span>正式写入</span></div>
        <div class="stat"><b>{{ result.report.violations }}</b><span>契约违规</span></div>
        <div class="stat"><b>{{ result.report.quarantined }}</b><span>隔离写入</span></div>
      </div>
      <p v-if="result.report.violation_codes?.length" class="muted">
        被拦截的 event_code：{{ result.report.violation_codes.join('、') }}（详见隔离页 / 契约页批次证明）
      </p>
    </template>
    <p v-else-if="result && !result.ok" class="muted" style="margin-top:12px">装载失败：{{ result.error }}</p>

    <pre v-if="result" style="margin-top:16px;font-family:var(--mono);font-size:12px;white-space:pre-wrap">{{ result }}</pre>
  </div>
</template>
