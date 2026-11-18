import { createRouter, createWebHistory } from 'vue-router'
import Board from './pages/Board.vue'
import Ingest from './pages/Ingest.vue'
import Contract from './pages/Contract.vue'
import Quarantine from './pages/Quarantine.vue'
import Sidecar from './pages/Sidecar.vue'
import Proofs from './pages/Proofs.vue'

export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: Board },
    { path: '/ingest', component: Ingest },
    { path: '/contract', component: Contract },
    { path: '/quarantine', component: Quarantine },
    { path: '/sidecar', component: Sidecar },
    { path: '/proofs', component: Proofs },
  ],
})
