import axios from 'axios'

const getBaseURL = () => {
  if (import.meta.env.VITE_API_URL) {
    return import.meta.env.VITE_API_URL
  }
  // Fallback to window hostname with port 8000
  if (typeof window !== 'undefined' && window.location) {
    return `http://${window.location.hostname}:8000`
  }
  return 'http://localhost:8000'
}

const api = axios.create({
  baseURL: getBaseURL(),
})

api.interceptors.request.use((config) => {
  const adminPass = localStorage.getItem('admin_password')
  if (adminPass) {
    config.headers['x-admin-password'] = adminPass
  }
  return config
})

export const verifyInvite = (code, name, phone) =>
  api.post('/auth/verify-invite', { code, name, phone })

export const registerFace = (guestId, selfieFile) => {
  const form = new FormData()
  form.append('guest_id', guestId)
  form.append('selfie', selfieFile)
  return api.post('/faces/register', form)
}

export const getPhotos = (guestId, page = 1) =>
  api.get(`/photos/${guestId}?page=${page}&limit=50`)

export const prepareDownload = (guestId) =>
  api.post(`/download/${guestId}/prepare`)

export const getDownloadStatus = (sessionId) =>
  api.get(`/download/status/${sessionId}`)

export const getStreamUrl = (guestId, sessionId) =>
  `${api.defaults.baseURL}/download/${guestId}/stream/${sessionId}`

export const getFaceClusters = () =>
  api.get('/faces/clusters')

export const getClusterPhotos = (clusterId) =>
  api.get(`/faces/clusters/${clusterId}/photos`)

export const renameCluster = (clusterId, name) =>
  api.post(`/faces/clusters/${clusterId}/rename`, { name })

export const deletePhoto = (driveId) =>
  api.delete(`/photos/${driveId}`)

// Admin endpoints
export const adminLogin = (password) =>
  api.post('/admin/login', { password })

export const adminGetGuests = () =>
  api.get('/admin/guests')

export const adminCreateGuest = (name, phone, selfieFile) => {
  const form = new FormData()
  form.append('name', name)
  form.append('phone', phone || '')
  if (selfieFile) {
    form.append('selfie', selfieFile)
  }
  return api.post('/admin/guests', form)
}

export const adminGetGuestPhotos = (guestId) =>
  api.get(`/admin/guests/${guestId}/photos`)

export const adminRemoveGuestPhoto = (guestId, photoId) =>
  api.delete(`/admin/guests/${guestId}/photos/${photoId}`)

export const adminRunGuestMatching = (guestId) =>
  api.post(`/admin/guests/${guestId}/run-matching`)

export const adminRunMatchingAll = () =>
  api.post('/admin/run-matching-all')

export const adminDeleteGuest = (guestId) =>
  api.delete(`/admin/guests/${guestId}`)

export default api