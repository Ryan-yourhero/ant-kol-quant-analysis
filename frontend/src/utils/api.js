import axios from 'axios'

const api = axios.create({ baseURL: '/api', timeout: 120000 })

export function startRun() { return api.post('/runs/start') }
export function getCurrentRun() { return api.get('/runs/current') }
export function getTodayOps(page = 1, pageSize = 20) { return api.get(`/operations/today?page=${page}&page_size=${pageSize}`) }
export function getHistoryOps(params) { return api.get('/operations/history', { params }) }
export function getKols() { return api.get('/kols') }
export function getKolOps(id, page = 1) { return api.get(`/kols/${id}/operations?page=${page}&page_size=20`) }
export function downloadExcel() { return `/api/excel/today` }
export function getReports() { return api.get('/reports') }
export function generateReports(date) { return api.post('/reports/generate', { date: date || null }) }
export function getReportContent(date) { return api.get(`/reports/${date}/content`) }
