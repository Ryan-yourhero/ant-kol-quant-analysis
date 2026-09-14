import axios from 'axios'

const api = axios.create({ baseURL: '/api', timeout: 120000 })

export function startRun() { return api.post('/runs/start') }
export function getCurrentRun() { return api.get('/runs/current') }
export function getTodayOps(page = 1, pageSize = 20) { return api.get(`/operations/today?page=${page}&page_size=${pageSize}`) }
export function getHistoryOps(params) { return api.get('/operations/history', { params }) }
export function getOpsByDate(date) { return api.get('/operations/history', { params: { date_from: date, date_to: date, page: 1, page_size: 100 } }) }
export function getKols() { return api.get('/kols') }
export function getKolOps(id, page = 1) { return api.get(`/kols/${id}/operations?page=${page}&page_size=20`) }
export function downloadExcel() { return `/api/excel/today` }

// ===== 基金方向库 =====
export function listFunds(params) { return api.get('/funds', { params }) }
export function getFundOptions() { return api.get('/funds/options') }
export function getFundDetail(id) { return api.get(`/funds/${id}`) }
export function createFund(body) { return api.post('/funds', body) }
export function updateFund(id, body) { return api.put(`/funds/${id}`, body) }
export function confirmFund(id, body) { return api.post(`/funds/${id}/confirm`, body) }
export function reanalyzeFund(id) { return api.post(`/funds/${id}/reanalyze`) }
export function getFundEvidence(id) { return api.get(`/funds/${id}/evidence`) }
export function getReports() { return api.get('/reports') }
export function generateReports(date) { return api.post('/reports/generate', { date: date || null }) }
export function getReportContent(date) { return api.get(`/reports/${date}/content`) }
