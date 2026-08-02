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
  const guestToken = localStorage.getItem('guest_token')
  if (guestToken) {
    config.headers['X-Guest-Token'] = guestToken
  }
  return config
})

// Every photo endpoint now requires a credential. <img>, <video> and download
// links cannot send headers, so those URLs carry the token as ?tk= instead
// (?t= is already the gallery's cache-buster).
export const withToken = (url) => {
  const token = localStorage.getItem('guest_token')
  const admin = localStorage.getItem('admin_password')
  if (!token && !admin) return url
  const sep = url.includes('?') ? '&' : '?'
  return token
    ? `${url}${sep}tk=${encodeURIComponent(token)}`
    : `${url}${sep}password=${encodeURIComponent(admin)}`
}

// A guest's whole login is opening their link — no code, no name, no selfie.
export const openGuestLink = (token) => api.get(`/auth/link/${token}`)

export const verifyInvite = (code, name, phone) =>
  api.post('/auth/verify-invite', { code, name, phone })

// `filter` is applied server-side. Filtering the returned page in the browser
// showed only whatever matched within those 50 rows — for a guest with 1,316
// personal photos the newest 50 were all group shots, so "Just Me" was empty.
export const getPhotos = (guestId, page = 1, filter = 'all', media = 'all') =>
  api.get(`/photos/${guestId}?page=${page}&limit=50&filter=${filter}&media=${media}`)

export const getAllPhotos = (page = 1) =>
  api.get(`/photos/all?page=${page}&limit=50`)

// Highlights: every "common" photo (venue, décor, group shots + anything an
// admin curated in), for all guests to browse.
export const getHighlights = (page = 1, media = 'all') =>
  api.get(`/photos/highlights?page=${page}&limit=50&media=${media}`)

// Admin: add a photo (or several) to everyone's album — sets is_common, so it
// lands in the Highlights tab and every guest's download.
export const markAsCommon = (driveId) =>
  api.post(`/photos/${driveId}/mark-common`)

export const markAsCommonBatch = (driveIds) =>
  api.post('/photos/mark-common-batch', { drive_ids: driveIds })

export const getPhotoPeople = (driveId) =>
  api.get(`/photos/${driveId}/people`)

// Admin corrections to who is in a photo. Reversible — recorded in
// photo_people.json, never by editing the faces table.
export const removePersonFromPhoto = (driveId, personId) =>
  api.post(`/photos/${driveId}/people/${personId}/remove`)

export const addPersonToPhoto = (driveId, person) =>
  api.post(`/photos/${driveId}/people/add`, {
    id: person.id,
    name: person.name || '',
    is_guest: !!person.is_guest,
  })



// Direct streaming download of the whole album — the browser downloads the ZIP
// as the server generates it (no prepare/poll, no server-side /tmp build).
export const getDownloadAllUrl = (guestId) =>
  withToken(`${api.defaults.baseURL}/download/${guestId}/all`)

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

export const adminCreateGuest = (name, phone, selfieFile, tolerance = null) => {
  const form = new FormData()
  form.append('name', name)
  form.append('phone', phone || '')
  if (selfieFile) {
    form.append('selfie', selfieFile)
  }
  if (tolerance !== null && tolerance !== undefined) {
    form.append('tolerance', tolerance)
  }
  return api.post('/admin/guests', form)
}

export const adminGetGuestPhotos = (guestId) =>
  api.get(`/admin/guests/${guestId}/photos`)

export const adminRemoveGuestPhoto = (guestId, photoId) =>
  api.delete(`/admin/guests/${guestId}/photos/${photoId}`)

export const adminRunGuestMatching = (guestId, tolerance = null) =>
  api.post(`/admin/guests/${guestId}/run-matching${tolerance !== null && tolerance !== undefined ? `?tolerance=${tolerance}` : ''}`)

export const adminRunMatchingAll = (tolerance = null) =>
  api.post(`/admin/run-matching-all${tolerance !== null && tolerance !== undefined ? `?tolerance=${tolerance}` : ''}`)

export const adminDeleteGuest = (guestId) =>
  api.delete(`/admin/guests/${guestId}`)

export const adminUpdateGuest = (guestId, name, phone, selfieFile = null, tolerance = null) => {
  const form = new FormData()
  form.append('name', name)
  form.append('phone', phone || '')
  if (selfieFile) {
    form.append('selfie', selfieFile)
  }
  if (tolerance !== null && tolerance !== undefined) {
    form.append('tolerance', tolerance)
  }
  return api.patch(`/admin/guests/${guestId}`, form)
}

// Households: every family that shares a link, and the name the admin gave it.
export const getHouseholds = () =>
  api.get('/admin/households')

export const setHouseholdName = (guestId, name) =>
  api.post(`/admin/households/${guestId}/name`, { name })

// Every face cluster (numeric id + name) — for the Families "add member" picker.
export const getAllClusters = () =>
  api.get('/admin/clusters')

// Add / remove a person (face cluster) on a family's link. This reassigns the
// cluster: the family album is the union of its members, read from guest_clusters.
export const addHouseholdMember = (guestId, clusterId, label) =>
  api.post(`/admin/households/${guestId}/members`, { cluster_id: clusterId, label })

export const removeHouseholdMember = (guestId, clusterId) =>
  api.delete(`/admin/households/${guestId}/members/${clusterId}`)

// Admin corrections for a single photo. Neither deletes it: the first only
// clears the is_common flag (drops it out of Group Moments), the second only
// removes it from one album.
export const removeFromGroup = (driveId) =>
  api.post(`/photos/${driveId}/remove-from-group`)

export const removeFromAlbum = (driveId, album) =>
  api.post(`/photos/${driveId}/remove-from-album`, { album })

export const getGuestsList = () =>
  api.get('/faces/guests-list')

export const getCategories = () =>
  api.get('/photos/categories')

export const createCategory = (name) =>
  api.post('/photos/categories', { name })

export const getCategoryPhotos = (name) =>
  api.get(`/photos/categories/${encodeURIComponent(name)}/photos`)

export const uploadCategoryPhoto = (name, file) => {
  const form = new FormData()
  form.append('file', file)
  return api.post(`/photos/categories/${encodeURIComponent(name)}/upload`, form)
}

export const mergeClusters = (targetId, sourceIds) =>
  api.post('/faces/clusters/merge', { target_id: targetId, source_ids: sourceIds })

export const unmergeCluster = (clusterId) =>
  api.delete(`/faces/clusters/${clusterId}/unmerge`)

// Admin: hide a whole face folder from the People tab (reversible — photos and
// face data are untouched).
export const deleteCluster = (clusterId) =>
  api.delete(`/faces/clusters/${clusterId}`)

export const setClusterProfilePic = (clusterId, driveId) =>
  api.post(`/faces/clusters/${clusterId}/set-profile-pic`, { drive_id: driveId })

export const deletePhotosBatch = (driveIds) =>
  api.post('/photos/delete-batch', { drive_ids: driveIds })

export const downloadPhotosBatch = (driveIds) =>
  api.post('/photos/download-batch', { drive_ids: driveIds }, { responseType: 'blob' })

export const uploadClusterProfilePic = (clusterId, file) => {
  const form = new FormData()
  form.append('file', file)
  return api.post(`/faces/clusters/${clusterId}/upload-profile-pic`, form)
}

// Admin: set the avatar to a square the admin drew on one of the person's own
// photos. Coordinates are fractions of the full-res image, so the crop lands the
// same regardless of how big the photo was shown in the browser.
export const cropClusterAvatar = (clusterId, driveId, fx, fy, fsize) =>
  api.post(`/faces/clusters/${clusterId}/crop-avatar`, {
    drive_id: driveId, fx, fy, fsize,
  })

export const adminGetFamilyMembers = (guestId) =>
  api.get(`/admin/guests/${guestId}/members`)

export const adminAddFamilyMember = (guestId, name, selfieFile) => {
  const form = new FormData()
  form.append('name', name)
  if (selfieFile) {
    form.append('selfie', selfieFile)
  }
  return api.post(`/admin/guests/${guestId}/members`, form)
}

export const adminDeleteFamilyMember = (memberId) =>
  api.delete(`/admin/members/${memberId}`)

export const notMePhoto = (driveId, guestId) =>
  api.post(`/photos/${driveId}/not-me`, { guest_id: guestId })

export default api