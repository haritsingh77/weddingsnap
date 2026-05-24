import { useEffect, useState, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { getPhotos, getFaceClusters, getClusterPhotos, renameCluster, deletePhoto } from '../services/api'

const API_BASE = import.meta.env.VITE_API_URL || 
  (typeof window !== 'undefined' && window.location ? `http://${window.location.hostname}:8000` : 'http://localhost:8000')

export default function Gallery() {
    const navigate = useNavigate()
    const guestId = localStorage.getItem('guest_id')
    const guestName = localStorage.getItem('guest_name')
    const eventName = localStorage.getItem('event_name')
    const inviteCode = localStorage.getItem('invite_code') || ''
    const isAdmin = inviteCode.toUpperCase().includes('ADMIN') ||
                    guestName?.toLowerCase().includes('saurav') ||
                    guestName?.toLowerCase().includes('mahima')

    const [photos, setPhotos] = useState([])
    const [loading, setLoading] = useState(true)
    const [error, setError] = useState('')
    const [page, setPage] = useState(1)
    const [hasMore, setHasMore] = useState(false)
    const [total, setTotal] = useState(0)
    const [tab, setTab] = useState('all')  // all | mine | common | people
    
    // Face Clustering states
    const [clusters, setClusters] = useState([])
    const [loadingClusters, setLoadingClusters] = useState(false)
    const [selectedCluster, setSelectedCluster] = useState(null)
    const [editingClusterId, setEditingClusterId] = useState(null)
    const [newName, setNewName] = useState('')
    const [clusterPhotos, setClusterPhotos] = useState([])
    const [loadingClusterPhotos, setLoadingClusterPhotos] = useState(false)
    
    // Lightbox State
    const [lightboxIndex, setLightboxIndex] = useState(null)
    const [downloadingPhoto, setDownloadingPhoto] = useState(null)
    const [mediaLoading, setMediaLoading] = useState(true)

    // Reset media loading on lightbox index change
    useEffect(() => {
        setMediaLoading(true)
    }, [lightboxIndex])

    const fetchPhotos = useCallback(async (p) => {
        setLoading(true)
        setError('')
        try {
            const res = await getPhotos(guestId, p)
            const { photos: newPhotos, has_more, total: totalPhotos } = res.data
            setPhotos(p === 1 ? newPhotos : prev => [...prev, ...newPhotos])
            setHasMore(has_more)
            setTotal(totalPhotos)
            setPage(p)
        } catch (err) {
            console.error('Failed to fetch photos:', err)
            if (err.response?.status === 404) {
                localStorage.clear()
                navigate('/')
            } else {
                setError('Could not load gallery photos. Please check your connection.')
            }
        } finally {
            setLoading(false)
        }
    }, [guestId, navigate])

    useEffect(() => {
        if (!guestId) {
            navigate('/')
            return
        }
        fetchPhotos(1)
    }, [guestId, fetchPhotos, navigate])

    // Tabs filter or selected face cluster photos
    const filtered = selectedCluster
        ? clusterPhotos
        : photos.filter(p => {
            if (tab === 'mine') return !p.is_common
            if (tab === 'common') return p.is_common
            return true
        })

    // Fetch clusters dynamically when switching to people tab
    useEffect(() => {
        if (tab === 'people' && clusters.length === 0) {
            const fetchClusters = async () => {
                setLoadingClusters(true)
                try {
                    const res = await getFaceClusters()
                    setClusters(res.data)
                } catch (err) {
                    console.error("Failed to fetch recognized faces:", err)
                } finally {
                    setLoadingClusters(false)
                }
            }
            fetchClusters()
        }
    }, [tab, clusters.length])

    const handleClusterClick = async (clusterId) => {
        setSelectedCluster(clusterId)
        setLoadingClusterPhotos(true)
        try {
            const res = await getClusterPhotos(clusterId)
            setClusterPhotos(res.data)
        } catch (err) {
            console.error("Failed to fetch photos for cluster:", err)
        } finally {
            setLoadingClusterPhotos(false)
        }
    }

    const handleRenameSubmit = async (e, clusterId) => {
        e.stopPropagation()
        if (!newName.trim()) return
        try {
            await renameCluster(clusterId, newName.trim())
            setClusters(prev => prev.map(c => c.id === clusterId ? { ...c, name: newName.trim() } : c))
            setEditingClusterId(null)
            setNewName('')
        } catch (err) {
            console.error('Failed to rename cluster:', err)
        }
    }

    // Single media download helper
    const downloadSinglePhoto = (photoObj, event) => {
        if (event) event.stopPropagation()
        const driveId = photoObj.drive_id
        setDownloadingPhoto(driveId)
        try {
            const downloadUrl = `${API_BASE}/photos/stream/${driveId}?download=true`
            const link = document.createElement('a')
            link.href = downloadUrl
            link.target = '_blank'
            document.body.appendChild(link)
            link.click()
            document.body.removeChild(link)
        } catch (err) {
            console.error('Failed to download media:', err)
            alert('Could not download media. Please try again.')
        } finally {
            // Keep the visual feedback spinner for 1.5s
            setTimeout(() => {
                setDownloadingPhoto(null)
            }, 1500)
        }
    }

    // Handle photo delete
    const handleDeletePhoto = async (driveId) => {
        if (!window.confirm("Are you sure you want to delete this photo/video? This will move it to the temp_delete folder and remove it from the gallery.")) {
            return
        }
        try {
            await deletePhoto(driveId)
            // Remove from local photos list
            setPhotos(prev => prev.filter(p => p.drive_id !== driveId))
            // Also filter in clusterPhotos if active
            setClusterPhotos(prev => prev.filter(p => p.drive_id !== driveId))
            // Close lightbox
            setLightboxIndex(null)
        } catch (err) {
            console.error("Failed to delete photo:", err)
            alert("Failed to delete photo. Please make sure the service account has editor permissions.")
        }
    }

    // Lightbox navigation helpers
    const showPrevPhoto = (e) => {
        if (e) e.stopPropagation()
        if (lightboxIndex > 0) {
            setLightboxIndex(lightboxIndex - 1)
        }
    }

    const showNextPhoto = (e) => {
        if (e) e.stopPropagation()
        if (lightboxIndex < filtered.length - 1) {
            setLightboxIndex(lightboxIndex + 1)
        }
    }

    // Handle arrow keys for lightbox navigation
    useEffect(() => {
        const handleKeyDown = (e) => {
            if (lightboxIndex === null) return
            if (e.key === 'Escape') setLightboxIndex(null)
            if (e.key === 'ArrowLeft') showPrevPhoto()
            if (e.key === 'ArrowRight') showNextPhoto()
        }
        window.addEventListener('keydown', handleKeyDown)
        return () => window.removeEventListener('keydown', handleKeyDown)
    }, [lightboxIndex, filtered])

    const activePhoto = lightboxIndex !== null ? filtered[lightboxIndex] : null
    console.log("Gallery State - lightboxIndex:", lightboxIndex, "filtered.length:", filtered.length, "activePhoto:", activePhoto);

    return (
        <>
            <div className="min-h-screen bg-stone-50/70 select-none pb-12 animate-fade-in-up">
            
            {/* Elegant Header */}
            <div className="bg-white/80 backdrop-blur-md border-b border-stone-200/50 px-6 py-6 sticky top-0 z-20">
                <div className="max-w-4xl mx-auto flex items-center justify-between">
                    <div className="flex items-center gap-4">
                        <img
                            src="/logo.png"
                            alt="Logo"
                            className="w-14 h-14 object-contain rounded-full shadow-md shadow-gold-100 border border-gold-200/10 bg-white p-0.5"
                        />
                        <div>
                            <h1 className="font-serif text-stone-900 text-xl tracking-tight leading-none mb-1.5">{eventName || 'Wedding Gallery'}</h1>
                            <p className="text-stone-400 text-xs tracking-wide">
                                {total > 0 ? `${total} matched moments found for ${guestName?.split(' ')[0]}` : 'Matching moments...'}
                            </p>
                        </div>
                    </div>
                    
                    <div className="flex gap-2">
                        <button
                            onClick={() => navigate('/register')}
                            className="bg-white border border-stone-200 text-stone-700 text-xs font-semibold px-4 py-2.5 rounded-xl hover:bg-stone-50 hover:border-stone-300 transition-all duration-300 cursor-pointer"
                        >
                            📸 Re-Scan Face
                        </button>
                        <button
                            onClick={() => navigate('/download')}
                            className="bg-stone-900 text-white text-xs font-semibold px-4 py-2.5 rounded-xl hover:bg-gold-600 hover:shadow-lg hover:shadow-gold-500/20 transition-all duration-300 cursor-pointer"
                        >
                            Download All
                        </button>
                    </div>
                </div>

                {/* Aesthetic Navigation Tabs */}
                <div className="max-w-4xl mx-auto flex gap-6 mt-6 border-t border-stone-100 pt-4">
                    {['all', 'mine', 'common', 'people'].filter(t => t !== 'people' || isAdmin).map(t => (
                        <button
                            key={t}
                            onClick={() => {
                                setTab(t)
                                setSelectedCluster(null)
                            }}
                            className={`text-xs uppercase tracking-widest font-semibold pb-1.5 border-b-2 transition-all duration-300 cursor-pointer ${tab === t
                                    ? 'border-gold-500 text-stone-900'
                                    : 'border-transparent text-stone-300 hover:text-stone-500'
                                }`}
                        >
                            {t === 'all' ? 'All Moments' : t === 'mine' ? 'Just Me' : t === 'common' ? 'Group Moments' : 'Recognized Faces'}
                        </button>
                    ))}
                </div>
            </div>

            {/* Error Message */}
            {error && (
                <div className="max-w-4xl mx-auto px-4 mt-6">
                    <div className="bg-red-50/60 border border-red-100 rounded-xl p-4 text-red-500 text-sm text-center font-medium">
                        {error}
                    </div>
                </div>
            )}

            {/* Photo Grid Section */}
            <div className="max-w-4xl mx-auto px-4 py-8">
                {tab === 'people' && !selectedCluster ? (
                    // RENDER RECOGNIZED FACES LIST
                    loadingClusters ? (
                        <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-5 gap-6 md:gap-8">
                            {[...Array(5)].map((_, i) => (
                                <div key={i} className="flex flex-col items-center gap-3 animate-pulse">
                                    <div className="w-24 h-24 sm:w-28 sm:h-28 rounded-full bg-stone-200/60" />
                                    <div className="h-3 w-16 bg-stone-200/60 rounded" />
                                    <div className="h-2.5 w-12 bg-stone-150/60 rounded" />
                                </div>
                            ))}
                        </div>
                    ) : clusters.length === 0 ? (
                        <div className="text-center py-20 flex flex-col items-center gap-4 animate-fade-in-up">
                            <div className="text-4xl">👥</div>
                            <h3 className="font-serif text-stone-800 text-lg">No recognized faces yet</h3>
                            <p className="text-stone-400 font-light text-sm max-w-xs leading-relaxed">
                                Once face preprocessing is complete, clusters of all guests will appear here.
                            </p>
                        </div>
                    ) : (
                        <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-5 gap-6 md:gap-8">
                            {clusters.map((cluster) => (
                                <div
                                    key={cluster.id}
                                    onClick={() => handleClusterClick(cluster.id)}
                                    className="flex flex-col items-center gap-3 cursor-pointer group"
                                >
                                    <div className="relative w-24 h-24 sm:w-28 sm:h-28 rounded-full overflow-hidden border-2 border-transparent group-hover:border-gold-500 shadow-md transition-all duration-300 transform group-hover:scale-105">
                                        <img
                                            src={`${API_BASE}${cluster.thumbnail_url}`}
                                            alt=""
                                            className="w-full h-full object-cover bg-stone-100"
                                            loading="lazy"
                                        />
                                    </div>
                                    <div className="text-center w-full px-2" onClick={(e) => e.stopPropagation()}>
                                        {editingClusterId === cluster.id ? (
                                            <form
                                                onSubmit={(e) => handleRenameSubmit(e, cluster.id)}
                                                className="flex items-center gap-1 justify-center mt-1"
                                            >
                                                <input
                                                    type="text"
                                                    value={newName}
                                                    onChange={(e) => setNewName(e.target.value)}
                                                    className="w-20 text-center text-xs border border-stone-300 rounded px-1 py-0.5 bg-white text-stone-800 focus:outline-none focus:border-gold-500"
                                                    autoFocus
                                                />
                                                <button
                                                    type="submit"
                                                    className="text-xs text-green-600 hover:text-green-700 font-bold px-1"
                                                >
                                                    ✓
                                                </button>
                                                <button
                                                    type="button"
                                                    onClick={() => setEditingClusterId(null)}
                                                    className="text-xs text-red-500 hover:text-red-650 font-bold px-1"
                                                >
                                                    ✗
                                                </button>
                                            </form>
                                        ) : (
                                            <div className="flex items-center justify-center gap-1 mt-0.5 group/name">
                                                <span 
                                                    onClick={() => handleClusterClick(cluster.id)}
                                                    className="text-xs font-semibold text-stone-800 hover:text-gold-600 transition-colors cursor-pointer"
                                                >
                                                    {cluster.name || `Person #${cluster.id}`}
                                                </span>
                                                {isAdmin && (
                                                    <button
                                                        onClick={() => {
                                                            setEditingClusterId(cluster.id)
                                                            setNewName(cluster.name || `Person #${cluster.id}`)
                                                        }}
                                                        className="text-stone-400 hover:text-gold-600 transition-colors text-[10px] opacity-0 group-hover/name:opacity-100 group-hover:opacity-100 focus:opacity-100 cursor-pointer"
                                                        title="Rename person"
                                                    >
                                                        ✏️
                                                    </button>
                                                )}
                                            </div>
                                        )}
                                        <p className="text-[10px] text-stone-400 uppercase tracking-wider mt-0.5">
                                            {cluster.count} {cluster.count === 1 ? 'Moment' : 'Moments'}
                                        </p>
                                    </div>
                                </div>
                            ))}
                        </div>
                    )
                ) : (
                    // STANDARD PHOTO GRID (OR SINGLE CLUSTER DETAIL FEED)
                    <>
                        {selectedCluster && (
                            <div className="flex items-center justify-between mb-8 pb-4 border-b border-stone-200/50">
                                <div className="flex items-center gap-4">
                                    <button
                                        onClick={() => setSelectedCluster(null)}
                                        className="bg-white border border-stone-205 text-stone-750 text-xs font-semibold px-3.5 py-2 rounded-xl hover:bg-stone-50 hover:border-stone-300 transition-all cursor-pointer shadow-xs"
                                    >
                                        ← Back
                                    </button>
                                    <h2 className="font-serif text-stone-900 text-xl tracking-tight leading-none">
                                        Moments with {clusters.find(c => c.id === selectedCluster)?.name || `Person #${selectedCluster}`}
                                    </h2>
                                </div>
                                <p className="text-stone-400 text-xs font-medium">
                                    {filtered.length} moments found
                                </p>
                            </div>
                        )}
                        
                        {loadingClusterPhotos ? (
                            <div className="grid grid-cols-2 md:grid-cols-3 gap-3 md:gap-4">
                                {[...Array(6)].map((_, i) => (
                                    <div key={i} className="aspect-square bg-stone-200/50 border border-stone-100/50 rounded-2xl animate-pulse" />
                                ))}
                            </div>
                        ) : loading && photos.length === 0 ? (
                            /* Elegant Shimmer Loading Grid */
                            <div className="grid grid-cols-2 md:grid-cols-3 gap-3 md:gap-4">
                                {[...Array(9)].map((_, i) => (
                                    <div key={i} className="aspect-square bg-stone-200/50 border border-stone-100/50 rounded-2xl animate-pulse" />
                                ))}
                            </div>
                        ) : (
                            <>
                                <div className="grid grid-cols-2 md:grid-cols-3 gap-3 md:gap-4">
                            {filtered.map((photo, i) => (
                                <div 
                                    key={i} 
                                    onClick={() => setLightboxIndex(i)}
                                    className="aspect-square relative group overflow-hidden bg-stone-100 border border-stone-200/40 rounded-2xl shadow-sm hover:shadow-md hover:border-gold-200 cursor-pointer transition-all duration-300 animate-fade-in-up"
                                    style={{ animationDelay: `${(i % 6) * 50}ms` }}
                                >
                                    {photo.is_video ? (
                                        <div className="w-full h-full relative bg-stone-950 flex flex-col items-center justify-center overflow-hidden">
                                            {/* Beautiful dark radial gradient background */}
                                            <div className="absolute inset-0 bg-[radial-gradient(circle_at_center,_var(--tw-gradient-stops))] from-stone-900 to-stone-950 opacity-95" />
                                            
                                            {/* Elegant video symbol design */}
                                            <div className="relative z-10 flex flex-col items-center gap-2 transition-transform duration-500 group-hover:scale-105">
                                                <div className="w-11 h-11 rounded-full bg-gold-400/10 border border-gold-400/30 flex items-center justify-center text-gold-300 text-base shadow-inner">
                                                    🎬
                                                </div>
                                                <span className="text-[9px] text-stone-400 font-semibold tracking-widest uppercase">Play video</span>
                                            </div>
                                            
                                            {/* Film strip luxury side borders */}
                                            <div className="absolute left-1.5 top-0 bottom-0 w-1 flex flex-col justify-between py-2.5 opacity-25">
                                                {[...Array(6)].map((_, idx) => (
                                                    <div key={idx} className="w-1 h-1 bg-white/70 rounded-xs" />
                                                ))}
                                            </div>
                                            <div className="absolute right-1.5 top-0 bottom-0 w-1 flex flex-col justify-between py-2.5 opacity-25">
                                                {[...Array(6)].map((_, idx) => (
                                                    <div key={idx} className="w-1 h-1 bg-white/70 rounded-xs" />
                                                ))}
                                            </div>
                                        </div>
                                    ) : (
                                        <img
                                            src={`${API_BASE}${photo.thumb_url}`}
                                            alt=""
                                            loading="lazy"
                                            className="w-full h-full object-cover transition-transform duration-700 ease-out group-hover:scale-105"
                                        />
                                    )}
                                    
                                    {/* Subtle Gradient Overlay on Hover */}
                                    <div className="absolute inset-0 bg-gradient-to-t from-stone-950/40 via-transparent to-transparent opacity-0 group-hover:opacity-100 transition-opacity duration-300 flex items-end justify-between p-3.5">
                                        <span className="text-[10px] font-semibold text-white/95 uppercase tracking-wider">
                                            {photo.is_video ? '🎥 Video' : photo.is_common ? '👥 Group Shot' : '👤 Personal'}
                                        </span>
                                        <button
                                            onClick={(e) => downloadSinglePhoto(photo, e)}
                                            className="w-8 h-8 rounded-full bg-white/90 hover:bg-white text-stone-800 flex items-center justify-center shadow transition-all duration-200 hover:scale-105 active:scale-95 cursor-pointer"
                                            disabled={downloadingPhoto === photo.drive_id}
                                        >
                                            {downloadingPhoto === photo.drive_id ? (
                                                <span className="w-3.5 h-3.5 border-2 border-stone-800/30 border-t-stone-800 rounded-full animate-spin"></span>
                                            ) : '⬇'}
                                        </button>
                                    </div>

                                    {/* Badges for mobile */}
                                    {photo.is_video ? (
                                        <div className="absolute top-2 right-2 md:hidden bg-stone-900/60 backdrop-blur-sm text-white text-[9px] px-2 py-0.5 rounded-full font-semibold uppercase tracking-wider">
                                            Video
                                        </div>
                                    ) : photo.is_common ? (
                                        <div className="absolute top-2 right-2 md:hidden bg-stone-900/60 backdrop-blur-sm text-white text-[9px] px-2 py-0.5 rounded-full font-semibold uppercase tracking-wider">
                                            Group
                                        </div>
                                    ) : null}
                                </div>
                            ))}
                        </div>

                        {/* Pagination Trigger */}
                        {hasMore && (
                            <div className="flex justify-center mt-10">
                                <button
                                    onClick={() => fetchPhotos(page + 1)}
                                    className="border border-stone-200 text-stone-600 bg-white/50 px-8 py-3.5 rounded-xl font-semibold text-xs uppercase tracking-widest hover:bg-stone-50 hover:border-stone-300 transition-all duration-300 cursor-pointer"
                                >
                                    {loading ? 'Loading more...' : 'Load More Stories'}
                                </button>
                            </div>
                        )}

                        {/* Empty States */}
                        {filtered.length === 0 && !loading && (
                            <div className="text-center py-20 flex flex-col items-center gap-4 animate-fade-in-up">
                                <div className="text-4xl">🌾</div>
                                <h3 className="font-serif text-stone-800 text-lg">No moments found here</h3>
                                <p className="text-stone-400 font-light text-sm max-w-xs leading-relaxed">
                                    {tab === 'mine' 
                                        ? "We couldn't find any individual moments of you. Check Group Moments or scan again."
                                        : tab === 'common' 
                                        ? "No group moments have matched your selfie yet." 
                                        : "We couldn't find any matched moments of you yet."
                                    }
                                </p>
                                {total === 0 && (
                                    <button
                                        onClick={() => navigate('/register')}
                                        className="mt-2 bg-stone-950 text-white text-xs font-semibold px-6 py-3 rounded-xl hover:bg-gold-600 transition duration-300 cursor-pointer"
                                    >
                                        Scan Your Selfie Again
                                    </button>
                                )}
                            </div>
                        )}
                    </>
                )}
                </>
                )}
            </div>
        </div>

        {/* LIGHTBOX MODAL */}
        {activePhoto && (
                <div 
                    className="fixed inset-0 z-50 bg-stone-950/95 backdrop-blur-sm flex items-center justify-center p-4 md:p-10"
                    onClick={() => setLightboxIndex(null)}
                >
                    {/* Close Button */}
                    <button 
                        onClick={() => setLightboxIndex(null)}
                        className="absolute top-4 right-4 z-50 w-10 h-10 rounded-full bg-white/10 text-white hover:bg-white/20 flex items-center justify-center transition-all duration-300 cursor-pointer text-lg font-light"
                    >
                        ✕
                    </button>

                    {/* Left Navigation Arrow */}
                    {lightboxIndex > 0 && (
                        <button 
                            onClick={showPrevPhoto}
                            className="absolute left-4 z-50 w-12 h-12 rounded-full bg-white/5 text-white hover:bg-white/15 flex items-center justify-center transition-all duration-300 cursor-pointer text-xl"
                        >
                            ‹
                        </button>
                    )}

                    {/* Media Panel */}
                    <div 
                        className="relative max-w-full max-h-[80vh] md:max-h-[85vh] flex flex-col items-center min-w-[280px] sm:min-w-[400px] min-h-[300px] justify-center"
                        onClick={(e) => e.stopPropagation()}
                    >
                        {/* Elegant Loader inside Lightbox */}
                        {mediaLoading && (
                            <div className="absolute inset-0 flex items-center justify-center bg-stone-950/10 backdrop-blur-xs rounded-lg z-10 min-h-[300px]">
                                <div className="w-10 h-10 border-3 border-gold-300/20 border-t-gold-400 rounded-full animate-spin"></div>
                            </div>
                        )}

                        {activePhoto.is_video ? (
                            <video
                                src={`${API_BASE}/photos/stream/${activePhoto.drive_id}`}
                                controls
                                autoPlay
                                onLoadedData={() => setMediaLoading(false)}
                                onError={(e) => console.error("Video load error:", e)}
                                className="object-contain max-w-full max-h-[75vh] md:max-h-[80vh] rounded-lg shadow-2xl"
                            />
                        ) : (
                            <img 
                                src={`${API_BASE}/photos/thumb/${activePhoto.drive_id}?size=1600`}
                                alt=""
                                onLoad={() => setMediaLoading(false)}
                                onError={(e) => console.error("Image load error for ID " + activePhoto.drive_id + ":", e)}
                                className="object-contain max-w-full max-h-[75vh] md:max-h-[80vh] rounded-lg shadow-2xl"
                            />
                        )}
                        
                        {/* Caption & Download bar */}
                        <div className="w-full flex items-center justify-between mt-4 text-white px-2 gap-4">
                            <div className="text-xs min-w-0">
                                <span className="font-semibold uppercase tracking-wider text-gold-400 block truncate">
                                    {activePhoto.is_video ? '🎥 Video' : activePhoto.is_common ? '👥 Group Moment' : '👤 Personal Moment'}
                                </span>
                                <p className="text-white/40 mt-1 font-mono text-[10px] truncate">{activePhoto.drive_id}</p>
                            </div>

                            <div className="flex items-center gap-2 flex-shrink-0">
                                {isAdmin && (
                                    <button 
                                        onClick={() => handleDeletePhoto(activePhoto.drive_id)}
                                        className="bg-red-650 hover:bg-red-750 text-white px-4 py-2.5 rounded-xl text-xs font-semibold transition-all duration-300 flex items-center gap-1.5 cursor-pointer shadow-lg"
                                    >
                                        🗑️ Delete
                                    </button>
                                )}
                                <button 
                                    onClick={() => downloadSinglePhoto(activePhoto)}
                                    className="bg-white text-stone-950 px-4 py-2.5 rounded-xl text-xs font-semibold hover:bg-gold-500 hover:text-white transition-all duration-300 flex items-center gap-1.5 cursor-pointer shadow-lg"
                                    disabled={downloadingPhoto === activePhoto.drive_id}
                                >
                                    {downloadingPhoto === activePhoto.drive_id ? (
                                        <>
                                            <span className="w-3.5 h-3.5 border-2 border-stone-850/30 border-t-stone-850 rounded-full animate-spin"></span>
                                            Downloading...
                                        </>
                                    ) : (
                                        <>
                                            <span>⬇</span> Download {activePhoto.is_video ? 'Video' : 'Photo'}
                                        </>
                                    )}
                                </button>
                            </div>
                        </div>
                    </div>

                    {/* Right Navigation Arrow */}
                    {lightboxIndex < filtered.length - 1 && (
                        <button 
                            onClick={showNextPhoto}
                            className="absolute right-4 z-50 w-12 h-12 rounded-full bg-white/5 text-white hover:bg-white/15 flex items-center justify-center transition-all duration-300 cursor-pointer text-xl"
                        >
                            ›
                        </button>
                    )}
                </div>
            )}
        </>
    )
}