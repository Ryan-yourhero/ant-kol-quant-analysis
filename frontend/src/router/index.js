import { createRouter, createWebHistory } from 'vue-router'
import Today from '../views/Today.vue'
import History from '../views/History.vue'
import KolDetail from '../views/KolDetail.vue'

const routes = [
  { path: '/', component: Today },
  { path: '/history', component: History },
  { path: '/kol/:id', component: KolDetail, props: true },
]

export default createRouter({ history: createWebHistory(), routes })
