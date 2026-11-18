<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'

const current = ref(null)
const versions = ref([])
const err = ref('')
const busy = ref(false)

const newCode = ref('')
const newProj = ref('')

const proofBatch = ref('')
const proof = ref(null)
const proofErr = ref('')

async function reload() {
  err.value = ''
  try {
    const data = await api.contract()
    current.value = data.current
    versions.value = data.versions
  } catch (e) {
    err.value = String(e.message || e)
  }
}

async function add() {
  if (!newCode.value.trim() || !newProj.value.trim()) return
  busy.value = true
  err.value = ''
  try {
    await api.contractAdd(newCode.value.trim(), newProj.value.trim())
    newCode.value = ''
    newProj.value = ''
    await reload()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

async function remove(code) {
  busy.value = true
  err.value = ''
  try {
    await api.contractRemove(code)
    await reload()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

async function queryProof() {
  proof.value = null
  proofErr.value = ''
  if (!proofBatch.value.trim()) return
  try {
    proof.value = await api.contractProof(proofBatch.value.trim())
  } catch (e) {
    proofErr.value = String(e.message || e)
  }
}

onMounted(reload)
</script>

<template>
  <div class="page-card">
    <h1>事件码契约</h1>
    <p class="lead">
      允许 event_code 集合与投影文案映射，落库按版本保存；装载入口按当前版本逐点分流：
      契约内进正式时间线，契约外进隔离。改契约只产生新版本，不回溯改写历史正式点。
    </p>
    <p v-if="err" class="muted">{{ err }}</p>

    <template v-if="current">
      <h2 class="sect">当前契约 <span class="ver">v{{ current.version }}</span></h2>
      <table class="table">
        <thead>
          <tr><th>event_code</th><th>投影文案</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="(proj, code) in current.items" :key="code">
            <td>{{ code }}</td>
            <td>{{ proj }}</td>
            <td>
              <button class="btn danger" :disabled="busy" @click="remove(code)">删除</button>
            </td>
          </tr>
          <tr v-if="!Object.keys(current.items).length">
            <td colspan="3" class="muted">空契约：全部 event_code 按违规隔离（失败闭）</td>
          </tr>
        </tbody>
      </table>

      <h2 class="sect">新增 / 更新允许码</h2>
      <div class="form-row">
        <input v-model="newCode" class="inp" placeholder="EVENT_CODE（如 LOST_PACKAGE）" />
        <input v-model="newProj" class="inp" placeholder="投影文案（如 遗失）" />
        <button class="btn" :disabled="busy" @click="add">保存为新版本</button>
      </div>

      <h2 class="sect">版本历史</h2>
      <table class="table">
        <thead>
          <tr><th>版本</th><th>码数</th><th>说明</th><th>时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="v in versions" :key="v.version">
            <td>v{{ v.version }}</td>
            <td>{{ v.codes }}</td>
            <td>{{ v.note }}</td>
            <td>{{ v.created_at }}</td>
          </tr>
        </tbody>
      </table>
    </template>

    <h2 class="sect">批次契约证明（只读）</h2>
    <div class="form-row">
      <input v-model="proofBatch" class="inp" placeholder="批次号（如 B-DEMO-01）" />
      <button class="btn" @click="queryProof">查询证明</button>
    </div>
    <p v-if="proofErr" class="muted">{{ proofErr }}</p>
    <template v-if="proof">
      <div class="stat-row">
        <div class="stat"><b>v{{ proof.contract_version }}</b><span>契约版本</span></div>
        <div class="stat"><b>{{ proof.report.input }}</b><span>输入点</span></div>
        <div class="stat"><b>{{ proof.live_counts.written }}</b><span>正式写入</span></div>
        <div class="stat"><b>{{ proof.live_counts.violations }}</b><span>契约违规</span></div>
        <div class="stat"><b>{{ proof.live_counts.quarantined }}</b><span>隔离数</span></div>
      </div>
      <h2 class="sect">隔离列表（契约违规）</h2>
      <table class="table">
        <thead>
          <tr><th>id</th><th>运单</th><th>原因</th><th>成员</th></tr>
        </thead>
        <tbody>
          <tr v-for="r in proof.quarantine" :key="r.id">
            <td>{{ r.id }}</td>
            <td>{{ r.waybill_id }}</td>
            <td>{{ r.reason }}</td>
            <td>{{ r.member_json }}</td>
          </tr>
          <tr v-if="!proof.quarantine.length"><td colspan="4" class="muted">本批无契约违规</td></tr>
        </tbody>
      </table>
      <h2 class="sect">正式时间线写入</h2>
      <table class="table">
        <thead>
          <tr><th>hop</th><th>运单</th><th>event_code</th><th>时间</th><th>地点</th></tr>
        </thead>
        <tbody>
          <tr v-for="h in proof.hops" :key="h.id">
            <td>{{ h.id }}</td>
            <td>{{ h.waybill_id }}</td>
            <td>{{ h.event_code }}</td>
            <td>{{ h.event_ts }}</td>
            <td>{{ h.location_code }}</td>
          </tr>
          <tr v-if="!proof.hops.length"><td colspan="5" class="muted">本批无正式写入</td></tr>
        </tbody>
      </table>
    </template>
  </div>
</template>

<style scoped>
.sect {
  margin: 22px 0 10px;
  font-family: var(--display);
  font-size: 14px;
  letter-spacing: 0.12em;
}
.ver {
  font-family: var(--mono);
  color: var(--mint);
}
.form-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 8px;
}
.inp {
  flex: 1;
  min-width: 180px;
  padding: 9px 10px;
  border: 1px solid var(--line);
  background: #fff;
  font: inherit;
  font-family: var(--mono);
  font-size: 13px;
}
.btn.danger {
  background: var(--danger);
  padding: 6px 10px;
  font-size: 12px;
}
</style>
