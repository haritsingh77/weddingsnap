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

export default api