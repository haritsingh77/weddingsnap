import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  adminLogin,
  adminGetGuests,
  adminCreateGuest,
  adminGetGuestPhotos,
  adminRemoveGuestPhoto,
  adminRunGuestMatching,
  adminRunMatchingAll,
  adminDeleteGuest
} from '../services/api'

const API_BASE = import.meta.env.VITE_API_URL || 
  (typeof window !== 'undefined' && window.location ? `http://${window.location.hostname}:8000` : 'http://localhost:8000')

export default function Admin() {
  const navigate = useNavigate()
  const [isAuthenticated, setIsAuthenticated] = useState(false)
  const [password, setPassword] = useState('')
  const [loginError, setLoginError] = useState('')

  // State lists
  const [guests, setGuests] = useState([])
  const [loadingGuests, setLoadingGuests] = useState(false)
  const [activeTab, setActiveTab] = useState('registry') // registry | batch

  // Create Guest Form state
  const [newGuestName, setNewGuestName] = useState('')
  const [newGuestPhone, setNewGuestPhone] = useState('+91')
  const [selfieFile, setSelfieFile] = useState(null)
  const [selfiePreview, setSelfiePreview] = useState(null)
  const [creatingGuest, setCreatingGuest] = useState(false)
  const [createMessage, setCreateMessage] = useState('')

  // Batch Matching state
  const [batchMatching, setBatchMatching] = useState(false)
  const [batchResult, setBatchResult] = useState('')

  // Review Modal state
  const [selectedGuest, setSelectedGuest] = useState(null)
  const [reviewPhotos, setReviewPhotos] = useState([])
  const [loadingPhotos, setLoadingPhotos] = useState(false)
  const [lightboxIndex, setLightboxIndex] = useState(null)

  // Auto-authenticate if password already stored
  useEffect(() => {
    const storedPass = localStorage.getItem('admin_password')
    if (storedPass) {
      setIsAuthenticated(true)
      fetchGuestsList()
    }
  }, [])

  const handleLogin = async (e) => {
    e.preventDefault()
    setLoginError('')
    try {
      await adminLogin(password)
      localStorage.setItem('admin_password', password)
      setIsAuthenticated(true)
      fetchGuestsList()
    } catch (err) {
      console.error(err)
      setLoginError('Invalid admin password. Please try again.')
    }
  }

  const handleLogout = () => {
    localStorage.removeItem('admin_password')
    setIsAuthenticated(false)
    setGuests([])
  }

  const fetchGuestsList = async () => {
    setLoadingGuests(true)
    try {
      const res = await adminGetGuests()
      setGuests(res.data)
    } catch (err) {
      console.error("Failed to load guests:", err)
    } finally {
      setLoadingGuests(false)
    }
  }

  const handleCreateSubmit = async (e) => {
    e.preventDefault()
    if (!newGuestName.trim()) return
    setCreatingGuest(true)
    setCreateMessage('')
    try {
      const res = await adminCreateGuest(newGuestName.trim(), newGuestPhone.trim(), selfieFile)
      setCreateMessage(`Guest "${res.data.name}" added successfully! ${res.data.photo_count} matches found.`)
      setNewGuestName('')
      setNewGuestPhone('+91')
      setSelfieFile(null)
      setSelfiePreview(null)
      fetchGuestsList()
    } catch (err) {
      console.error("Failed to add guest:", err)
      setCreateMessage('Failed to create guest or match face. Ensure photo has a clear face.')
    } finally {
      setCreatingGuest(false)
    }
  }

  const handleSelfieChange = (e) => {
    const file = e.target.files[0]
    if (file) {
      setSelfieFile(file)
      setSelfiePreview(URL.createObjectURL(file))
    }
  }

  const handleRunMatching = async (guestId) => {
    if (!window.confirm("Re-run face matching for this guest? This will check all photos again but preserve your manual removals.")) {
      return
    }
    setGuests(prev => prev.map(g => g.id === guestId ? { ...g, photo_count: 'Matching...' } : g))
    try {
      const res = await adminRunGuestMatching(guestId)
      setGuests(prev => prev.map(g => g.id === guestId ? { ...g, photo_count: res.data.photo_count } : g))
      alert(res.data.message || "Matching complete!")
    } catch (err) {
      console.error(err)
      alert("Failed to run matching. Ensure guest has a registered selfie.")
      fetchGuestsList()
    }
  }

  const handleRunBatchMatching = async () => {
    if (!window.confirm("Are you sure you want to run matching for all guests? This processes all 12,000+ photos against all guest selfies in the background.")) {
      return
    }
    setBatchMatching(true)
    setBatchResult('Processing matching pipeline against entire catalog...')
    try {
      const res = await adminRunMatchingAll()
      setBatchResult(res.data.message || `Successfully matched guests!`)
      fetchGuestsList()
    } catch (err) {
      console.error(err)
      setBatchResult('Failed to run batch matching. Please check server logs.')
    } finally {
      setBatchMatching(false)
    }
  }

  const handleDeleteGuest = async (guestId, name) => {
    if (!window.confirm(`Are you sure you want to delete guest "${name}"? This removes their database profile and all their matched albums.`)) {
      return
    }
    try {
      await adminDeleteGuest(guestId)
      setGuests(prev => prev.filter(g => g.id !== guestId))
    } catch (err) {
      console.error(err)
      alert("Failed to delete guest.")
    }
  }

  const handleOpenReview = async (guest) => {
    setSelectedGuest(guest)
    setReviewPhotos([])
    setLoadingPhotos(true)
    try {
      const res = await adminGetGuestPhotos(guest.id)
      setReviewPhotos(res.data)
    } catch (err) {
      console.error(err)
    } finally {
      setLoadingPhotos(false)
    }
  }

  const handleRemovePhoto = async (photoId) => {
    if (!selectedGuest) return
    if (!window.confirm("Remove this match from the guest's album? They will no longer see this photo, and it won't be re-added when re-matching.")) {
      return
    }
    try {
      await adminRemoveGuestPhoto(selectedGuest.id, photoId)
      setReviewPhotos(prev => prev.filter(p => p.id !== photoId))
      setGuests(prev => prev.map(g => g.id === selectedGuest.id ? { ...g, photo_count: g.photo_count - 1 } : g))
    } catch (err) {
      console.error(err)
      alert("Failed to remove match.")
    }
  }

  const getWhatsAppInviteLink = (guest) => {
    const inviteCode = localStorage.getItem('invite_code') || 'WEDDING2026'
    const baseUrl = window.location.origin
    const text = `Hi ${guest.name}! Here is your personalized wedding photo album for Mahima & Saurav's Wedding: ${baseUrl}/?code=${inviteCode}&name=${encodeURIComponent(guest.name)}`
    return `https://api.whatsapp.com/send?phone=${guest.phone.replace('+', '')}&text=${encodeURIComponent(text)}`
  }

  if (!isAuthenticated) {
    return (
      <div className="min-h-screen bg-stone-50/70 flex items-center justify-center px-4">
        <div className="bg-white p-8 rounded-2xl border border-stone-200/60 shadow-xl shadow-stone-100 max-w-md w-full">
          <div className="text-center mb-6">
            <h1 className="font-serif text-3xl text-stone-900 mb-2">WeddingSnap</h1>
            <p className="text-stone-400 text-sm">Enter password to access Admin Control Panel</p>
          </div>

          <form onSubmit={handleLogin} className="space-y-4">
            <div>
              <input
                type="password"
                placeholder="Admin Password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="w-full px-4 py-3 rounded-xl border border-stone-200 focus:outline-none focus:border-stone-400 text-stone-800"
                required
              />
            </div>
            {loginError && <p className="text-red-500 text-xs">{loginError}</p>}
            <button
              type="submit"
              className="w-full bg-stone-900 text-white font-semibold py-3 rounded-xl hover:bg-stone-800 transition duration-300 cursor-pointer"
            >
              Access Admin Panel
            </button>
          </form>
        </div>
      </div>
    )
  }

  return (
    <div className="min-h-screen bg-stone-50/70 pb-16">
      {/* Header */}
      <div className="bg-white border-b border-stone-200/50 px-6 py-6 sticky top-0 z-20 shadow-sm">
        <div className="max-w-6xl mx-auto flex items-center justify-between">
          <div>
            <h1 className="font-serif text-stone-900 text-2xl tracking-tight leading-none mb-1.5">Admin Dashboard</h1>
            <p className="text-stone-400 text-xs tracking-wide">WeddingSnap Guest Registry & Match Manager</p>
          </div>
          <div className="flex gap-3">
            <button
              onClick={() => navigate('/gallery')}
              className="bg-white border border-stone-200 text-stone-700 text-xs font-semibold px-4 py-2.5 rounded-xl hover:bg-stone-50 cursor-pointer"
            >
              🖼 View Gallery
            </button>
            <button
              onClick={handleLogout}
              className="bg-stone-100 text-stone-600 text-xs font-semibold px-4 py-2.5 rounded-xl hover:bg-stone-200 cursor-pointer"
            >
              Sign Out
            </button>
          </div>
        </div>
      </div>

      <div className="max-w-6xl mx-auto px-6 mt-8 grid grid-cols-1 lg:grid-cols-3 gap-8">
        
        {/* Left Side: Create / Register Guest Form */}
        <div className="lg:col-span-1 space-y-6">
          <div className="bg-white p-6 rounded-2xl border border-stone-200/50 shadow-sm">
            <h2 className="font-serif text-lg text-stone-800 mb-4 border-b border-stone-100 pb-2">Add New Guest</h2>
            <form onSubmit={handleCreateSubmit} className="space-y-4">
              <div>
                <label className="block text-xs font-bold text-stone-500 uppercase tracking-wider mb-1.5">Guest Name</label>
                <input
                  type="text"
                  placeholder="e.g. Mark Robinson"
                  value={newGuestName}
                  onChange={(e) => setNewGuestName(e.target.value)}
                  className="w-full px-4 py-2.5 rounded-xl border border-stone-200 focus:outline-none focus:border-stone-400 text-stone-800 text-sm"
                  required
                />
              </div>

              <div>
                <label className="block text-xs font-bold text-stone-500 uppercase tracking-wider mb-1.5">Phone Number (with country code)</label>
                <input
                  type="text"
                  placeholder="e.g. +919876543210"
                  value={newGuestPhone}
                  onChange={(e) => setNewGuestPhone(e.target.value)}
                  className="w-full px-4 py-2.5 rounded-xl border border-stone-200 focus:outline-none focus:border-stone-400 text-stone-800 text-sm"
                />
              </div>

              <div>
                <label className="block text-xs font-bold text-stone-500 uppercase tracking-wider mb-1.5">Reference Photo (Selfie)</label>
                <input
                  type="file"
                  accept="image/*"
                  onChange={handleSelfieChange}
                  className="hidden"
                  id="admin-selfie-upload"
                />
                <label
                  htmlFor="admin-selfie-upload"
                  className="block w-full border-2 border-dashed border-stone-200 hover:border-stone-400 rounded-xl p-4 text-center cursor-pointer transition duration-300"
                >
                  {selfiePreview ? (
                    <img
                      src={selfiePreview}
                      alt="Preview"
                      className="w-24 h-24 object-cover mx-auto rounded-full border border-stone-200"
                    />
                  ) : (
                    <div className="space-y-1 text-stone-400">
                      <p className="text-xs font-semibold">Click to select photo</p>
                      <p className="text-[10px]">JPG, PNG up to 10MB</p>
                    </div>
                  )}
                </label>
              </div>

              {createMessage && <p className="text-stone-600 text-xs font-semibold mt-2">{createMessage}</p>}

              <button
                type="submit"
                disabled={creatingGuest}
                className="w-full bg-stone-900 text-white font-semibold py-2.5 rounded-xl hover:bg-stone-800 transition duration-300 cursor-pointer disabled:bg-stone-300"
              >
                {creatingGuest ? 'Processing Matching...' : 'Register & Run Match'}
              </button>
            </form>
          </div>

          <div className="bg-white p-6 rounded-2xl border border-stone-200/50 shadow-sm">
            <h2 className="font-serif text-lg text-stone-800 mb-2">Global Operations</h2>
            <p className="text-stone-400 text-xs mb-4">Run the matching algorithm for all registered guests concurrently.</p>
            
            {batchResult && (
              <div className="bg-stone-50 border border-stone-150 rounded-xl p-3 mb-4 text-stone-600 text-xs font-mono whitespace-pre-line leading-relaxed">
                {batchResult}
              </div>
            )}

            <button
              onClick={handleRunBatchMatching}
              disabled={batchMatching}
              className="w-full bg-stone-150 hover:bg-stone-200 text-stone-800 font-semibold py-2.5 rounded-xl transition duration-300 cursor-pointer disabled:bg-stone-50 disabled:text-stone-300"
            >
              {batchMatching ? 'Processing Catalog...' : '🔁 Match All Guests'}
            </button>
          </div>
        </div>

        {/* Right Side: Guest Registry List */}
        <div className="lg:col-span-2 space-y-6">
          <div className="bg-white rounded-2xl border border-stone-200/50 shadow-sm overflow-hidden">
            <div className="px-6 py-4 border-b border-stone-100 flex items-center justify-between">
              <h2 className="font-serif text-lg text-stone-800">Guest Directory</h2>
              <button
                onClick={fetchGuestsList}
                className="text-stone-400 hover:text-stone-700 text-xs font-semibold cursor-pointer"
              >
                🔄 Refresh List
              </button>
            </div>

            {loadingGuests ? (
              <div className="p-8 text-center text-stone-400 text-sm">Loading guest profiles...</div>
            ) : guests.length === 0 ? (
              <div className="p-8 text-center text-stone-400 text-sm">No guests registered in system yet. Use form on the left to add guests.</div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full border-collapse">
                  <thead>
                    <tr className="bg-stone-50 border-b border-stone-100 text-[10px] text-stone-400 uppercase tracking-wider font-bold">
                      <th className="px-6 py-3 text-left">Photo</th>
                      <th className="px-6 py-3 text-left">Name</th>
                      <th className="px-6 py-3 text-left">Phone</th>
                      <th className="px-6 py-3 text-center">Matches</th>
                      <th className="px-6 py-3 text-right">Actions</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-stone-100">
                    {guests.map(guest => (
                      <tr key={guest.id} className="hover:bg-stone-50/50 transition-colors">
                        <td className="px-6 py-4">
                          <img
                            src={`${API_BASE}/admin/guests/${guest.id}/selfie?password=${localStorage.getItem('admin_password')}`}
                            alt="Selfie"
                            className="w-10 h-10 rounded-full object-cover border border-stone-200/70"
                            onError={(e) => {
                              // If selfie missing, show generic user icon
                              e.target.src = '/logo.png'
                            }}
                          />
                        </td>
                        <td className="px-6 py-4 text-stone-800 text-sm font-semibold">{guest.name}</td>
                        <td className="px-6 py-4 text-stone-400 text-xs">{guest.phone || '—'}</td>
                        <td className="px-6 py-4 text-center">
                          <span className="bg-stone-100 text-stone-700 text-xs px-2.5 py-1 rounded-full font-bold">
                            {guest.photo_count}
                          </span>
                        </td>
                        <td className="px-6 py-4 text-right space-x-2">
                          <button
                            onClick={() => handleOpenReview(guest)}
                            className="text-stone-700 hover:text-stone-900 text-xs font-semibold bg-stone-100 hover:bg-stone-200 px-3 py-1.5 rounded-lg cursor-pointer"
                            title="Review & Remove wrong matches"
                          >
                            👁 Review
                          </button>
                          <button
                            onClick={() => handleRunMatching(guest.id)}
                            className="text-stone-400 hover:text-stone-700 text-xs p-1"
                            title="Re-run Face Match"
                          >
                            🔁
                          </button>
                          {guest.phone && (
                            <a
                              href={getWhatsAppInviteLink(guest)}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="text-green-500 hover:text-green-600 font-semibold text-xs bg-green-50 hover:bg-green-100 px-3 py-1.5 rounded-lg inline-block"
                              title="Send invitation code via WhatsApp"
                            >
                              💬 Invite
                            </a>
                          )}
                          <button
                            onClick={() => handleDeleteGuest(guest.id, guest.name)}
                            className="text-red-400 hover:text-red-600 text-xs p-1"
                            title="Delete Guest profile"
                          >
                            🗑
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>

      </div>

      {/* Review Album Modal Overlay */}
      {selectedGuest && (
        <div className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl max-w-4xl w-full max-h-[85vh] flex flex-col overflow-hidden shadow-2xl border border-stone-200">
            <div className="px-6 py-4 border-b border-stone-100 flex items-center justify-between">
              <div>
                <h3 className="font-serif text-lg text-stone-900 leading-none mb-1">Album Review: {selectedGuest.name}</h3>
                <p className="text-stone-400 text-xs">Verify matched photos and remove any incorrect ones.</p>
              </div>
              <button
                onClick={() => setSelectedGuest(null)}
                className="text-stone-400 hover:text-stone-700 font-bold text-xl cursor-pointer"
              >
                &times;
              </button>
            </div>

            <div className="flex-1 overflow-y-auto p-6">
              {loadingPhotos ? (
                <div className="p-12 text-center text-stone-400 text-sm">Loading matched album...</div>
              ) : reviewPhotos.length === 0 ? (
                <div className="p-12 text-center text-stone-400 text-sm">No personal matched photos found for this guest.</div>
              ) : (
                <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                  {reviewPhotos.map((photo, index) => (
                    <div
                      key={photo.id}
                      className="group relative aspect-square rounded-xl overflow-hidden bg-stone-100 border border-stone-200/50 cursor-pointer"
                      onClick={() => setLightboxIndex(index)}
                    >
                      {photo.is_video ? (
                        <video
                          src={`${API_BASE}${photo.stream_url}`}
                          className="w-full h-full object-cover"
                          preload="metadata"
                        />
                      ) : (
                        <img
                          src={`${API_BASE}${photo.thumb_url}`}
                          alt="Match"
                          className="w-full h-full object-cover transition duration-300 group-hover:scale-105"
                          loading="lazy"
                        />
                      )}
                      
                      {/* Video indicator badge */}
                      {photo.is_video && (
                        <div className="absolute bottom-2 right-2 bg-black/50 text-white text-[10px] font-bold px-2 py-0.5 rounded-full">
                          ▶ Video
                        </div>
                      )}

                      {/* Remove Button Overlay (visible on hover) */}
                      <button
                        onClick={(e) => {
                          e.stopPropagation()
                          handleRemovePhoto(photo.id)
                        }}
                        className="absolute top-2 right-2 bg-red-600 hover:bg-red-700 text-white rounded-full p-2 shadow-md transition duration-300 opacity-0 group-hover:opacity-100 cursor-pointer"
                        title="Remove wrong match"
                      >
                        🗑
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Lightbox for detailed view */}
      {lightboxIndex !== null && reviewPhotos[lightboxIndex] && (
        <div
          className="fixed inset-0 z-50 bg-black/95 flex items-center justify-center p-4"
          onClick={() => setLightboxIndex(null)}
        >
          <button
            onClick={() => setLightboxIndex(null)}
            className="absolute top-6 right-6 text-white text-3xl font-bold cursor-pointer hover:text-stone-300"
          >
            &times;
          </button>
          
          <div
            className="max-w-4xl max-h-[85vh] flex items-center justify-center"
            onClick={(e) => e.stopPropagation()}
          >
            {reviewPhotos[lightboxIndex].is_video ? (
              <video
                src={`${API_BASE}${reviewPhotos[lightboxIndex].stream_url}`}
                className="max-w-full max-h-[85vh] rounded"
                controls
                autoPlay
              />
            ) : (
              <img
                src={`${API_BASE}${reviewPhotos[lightboxIndex].stream_url}`}
                alt="Enlarged"
                className="max-w-full max-h-[85vh] object-contain rounded"
              />
            )}
          </div>
        </div>
      )}

    </div>
  )
}
