import { useEffect, useState, useCallback, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  withToken,
  getDownloadAllUrl,
  removeFromGroup,
  removeFromAlbum,
  getHouseholds,
  setHouseholdName,
  getAllClusters,
  addHouseholdMember,
  removeHouseholdMember,
  getPhotos,
  getAllPhotos,
  getHighlights,
  markAsCommon,
  markAsCommonBatch,
  getPhotoPeople,
  removePersonFromPhoto,
  addPersonToPhoto,
  getFaceClusters,
  getClusterPhotos,
  renameCluster,
  deletePhoto,
  sharePhoto,
  getGuestsList,
  getCategories,
  createCategory,
  getCategoryPhotos,
  uploadCategoryPhoto,
  mergeClusters,
  unmergeCluster,
  setClusterProfilePic,
  deletePhotosBatch,
  downloadPhotosBatch,
  uploadClusterProfilePic,
  notMePhoto
} from '../services/api'

const API_BASE = import.meta.env.VITE_API_URL ||
  (typeof window !== 'undefined' && window.location ? `http://${window.location.hostname}:8000` : 'http://localhost:8000')

// Initials for the fallback avatar when a face thumbnail is missing/slow —
// "Sunita Singh" -> "SS", "Aafrin" -> "A", "Person #18" -> "#".
const personInitials = (name = '') => {
  const parts = String(name).trim().split(/\s+/).filter(Boolean)
  if (!parts.length) return '?'
  if (parts[0].startsWith('#') || /^Person$/i.test(parts[0])) return '#'
  return (parts[0][0] + (parts.length > 1 ? parts[parts.length - 1][0] : '')).toUpperCase()
}

// Face avatar with an always-visible initials fallback: the initials sit
// underneath, the photo covers them once it loads, and if it 404s/500s the
// initials simply remain — so a person is never a blank circle.
function FaceAvatar({ name, thumbnailUrl, size = 24 }) {
  const px = { width: size, height: size }
  return (
    <span
      className="relative rounded-full overflow-hidden bg-gradient-to-br from-gold-500/40 to-taupe-500/40 border border-white/20 flex items-center justify-center shrink-0"
      style={px}
    >
      <span className="text-white/75 font-bold" style={{ fontSize: Math.round(size * 0.38) }}>
        {personInitials(name)}
      </span>
      {thumbnailUrl && (
        <img
          src={withToken(`${API_BASE}${thumbnailUrl}`)}
          alt={name}
          className="absolute inset-0 w-full h-full object-cover"
          onError={e => { e.target.style.display = 'none' }}
        />
      )}
    </span>
  )
}

function GalleryPhotoCard({
    photo,
    index,
    isSelected,
    isMultiSelectMode,
    togglePhotoSelection,
    setLightboxIndex,
    downloadSinglePhoto,
    downloadingPhoto,
    API_BASE
}) {
    const [hoverProgress, setHoverProgress] = useState(0);
    const hoverTimerRef = useRef(null);

    const handleMouseEnter = () => {
        if (!isMultiSelectMode) return;
        
        // Clear any existing timer
        if (hoverTimerRef.current) clearInterval(hoverTimerRef.current);
        
        const startTime = Date.now();
        const duration = 3000; // 3 seconds
        
        hoverTimerRef.current = setInterval(() => {
            const elapsed = Date.now() - startTime;
            const progress = Math.min((elapsed / duration) * 100, 100);
            
            setHoverProgress(progress);
            
            if (progress >= 100) {
                clearInterval(hoverTimerRef.current);
                hoverTimerRef.current = null;
                setHoverProgress(0);
                togglePhotoSelection(photo.drive_id);
            }
        }, 30);
    };

    const handleMouseLeave = () => {
        if (hoverTimerRef.current) {
            clearInterval(hoverTimerRef.current);
            hoverTimerRef.current = null;
        }
        setHoverProgress(0);
    };

    // Clean up timer on unmount
    useEffect(() => {
        return () => {
            if (hoverTimerRef.current) clearInterval(hoverTimerRef.current);
        };
    }, []);

    // Also reset progress if multi-select mode is toggled off
    useEffect(() => {
        if (!isMultiSelectMode) {
            handleMouseLeave();
        }
    }, [isMultiSelectMode]);

    // Describes the tile for screen readers; photo content itself is unknown,
    // so convey position and whether it's a personal match or a group shot.
    const mediaKind = photo.is_video ? 'Video' : 'Photo';
    const mediaScope = photo.is_common ? 'group moment' : 'moment of you';
    const mediaLabel = `${mediaKind} ${index + 1} — ${mediaScope}`;

    return (
        <div
            role="button"
            tabIndex={0}
            aria-label={isMultiSelectMode
                ? `${isSelected ? 'Deselect' : 'Select'} ${mediaLabel}`
                : `Open ${mediaLabel}`}
            aria-pressed={isMultiSelectMode ? isSelected : undefined}
            onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    if (isMultiSelectMode) {
                        togglePhotoSelection(photo.drive_id);
                    } else {
                        setLightboxIndex(index);
                    }
                }
            }}
            onClick={() => {
                if (isMultiSelectMode) {
                    togglePhotoSelection(photo.drive_id);
                } else {
                    setLightboxIndex(index);
                }
            }}
            onMouseEnter={handleMouseEnter}
            onMouseLeave={handleMouseLeave}
            className={`aspect-square relative group overflow-hidden bg-ivory-200 border rounded-2xl shadow-sm hover:shadow-md cursor-pointer transition-all duration-300 animate-fade-in-up focus:outline-none focus-visible:ring-2 focus-visible:ring-gold-500 focus-visible:ring-offset-2 focus-visible:ring-offset-ivory-100 ${
                isSelected 
                    ? 'border-gold-500 ring-2 ring-gold-550/30 scale-[0.98]' 
                    : 'border-gold-200/40 hover:border-gold-200'
            } ${hoverProgress > 0 ? 'scale-[1.01] ring-1 ring-gold-400/40' : ''}`}
            style={{ animationDelay: `${(index % 6) * 50}ms` }}
        >
            {/* Top progress bar for 3-second hover countdown */}
            {isMultiSelectMode && hoverProgress > 0 && (
                <div className="absolute top-0 left-0 right-0 h-1.5 bg-black/20 z-20">
                    <div 
                        className="h-full bg-gold-500 transition-all duration-75 ease-out shadow-[0_0_8px_rgba(180,133,59,0.8)]"
                        style={{ width: `${hoverProgress}%` }}
                    />
                </div>
            )}

            {/* Sweep overlay for extra luxury visual effect */}
            {isMultiSelectMode && hoverProgress > 0 && (
                <div 
                    className="absolute inset-0 bg-gold-500/5 pointer-events-none z-10 transition-opacity duration-300"
                    style={{ opacity: hoverProgress / 100 }}
                />
            )}

            {/* Checkbox overlay for multi-select */}
            {isMultiSelectMode && (
                <div className="absolute top-3 left-3 z-10 flex items-center justify-center">
                    <input 
                        type="checkbox"
                        checked={isSelected}
                        onChange={() => {}} // handled by parent div onClick
                        className="w-5 h-5 accent-gold-550 rounded-lg cursor-pointer border-stone-350 focus:ring-gold-500"
                    />
                </div>
            )}

            {photo.is_video ? (
                <div className="w-full h-full relative bg-taupe-900 flex flex-col items-center justify-center overflow-hidden">
                    {/* Video first-frame thumbnail background */}
                    <img
                        src={withToken(`${API_BASE}${photo.thumb_url}`)}
                        alt=""
                        loading="lazy"
                        className="absolute inset-0 w-full h-full object-cover opacity-60 transition-transform duration-700 ease-out group-hover:scale-105"
                        onError={(e) => {
                            e.target.style.display = 'none';
                        }}
                    />
                    
                    {/* Elegant video symbol design */}
                    <div className="relative z-10 flex flex-col items-center gap-2 transition-transform duration-500 group-hover:scale-105">
                        <div className="w-11 h-11 rounded-full bg-gold-400/10 border border-gold-400/30 flex items-center justify-center text-gold-300 text-base shadow-inner">
                            🎬
                        </div>
                        <span className="text-[9px] text-taupe-400 font-semibold tracking-widest uppercase">Play video</span>
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
                    src={withToken(`${API_BASE}${photo.thumb_url}`)}
                    alt={mediaLabel}
                    loading="lazy"
                    decoding="async"
                    className="w-full h-full object-cover transition-transform duration-700 ease-out group-hover:scale-105"
                />
            )}
            
            {/* Subtle Hover Overlay */}
            {!isMultiSelectMode && (
                <div className="absolute inset-0 bg-gradient-to-t from-taupe-900/40 via-transparent to-transparent opacity-0 group-hover:opacity-100 transition-opacity duration-300 flex items-end justify-between p-3.5">
                    <span className="text-[10px] font-semibold text-white/95 uppercase tracking-wider">
                        {photo.is_video ? '🎥 Video' : photo.is_common ? '👥 Group Shot' : '👤 Personal'}
                    </span>
                    <button
                        onClick={(e) => downloadSinglePhoto(photo, e)}
                        className="w-8 h-8 rounded-full bg-white/90 hover:bg-white text-taupe-800 flex items-center justify-center shadow transition-all duration-200 hover:scale-105 active:scale-95 cursor-pointer"
                        disabled={downloadingPhoto === photo.drive_id}
                    >
                        {downloadingPhoto === photo.drive_id ? (
                            <span className="w-3.5 h-3.5 border-2 border-taupe-800/30 border-t-stone-800 rounded-full animate-spin"></span>
                        ) : '⬇'}
                    </button>
                </div>
            )}

            {/* Badges for mobile */}
            {photo.is_video ? (
                <div className="absolute top-2 right-2 md:hidden bg-taupe-800/60 backdrop-blur-sm text-white text-[9px] px-2 py-0.5 rounded-full font-semibold uppercase tracking-wider">
                    Video
                </div>
            ) : photo.is_common ? (
                <div className="absolute top-2 right-2 md:hidden bg-taupe-800/60 backdrop-blur-sm text-white text-[9px] px-2 py-0.5 rounded-full font-semibold uppercase tracking-wider">
                    Group
                </div>
            ) : null}
        </div>
    );
}

export default function Gallery() {
    const navigate = useNavigate()
    const guestId = localStorage.getItem('guest_id')
    const guestName = localStorage.getItem('guest_name')
    const eventName = localStorage.getItem('event_name')
    const inviteCode = localStorage.getItem('invite_code') || ''
    // Admin is either: holding the admin password (set on the Admin page), or
    // being an admin guest — the couple / family, whose own link the backend
    // authorises as admin (is_admin from /auth/link, stored at link-open). The
    // old heuristic keyed off the typed name, which wrongly flipped any guest
    // named like an admin into admin mode; the backend now enforces admin on
    // every route, so these two explicit signals are the only ones.
    const isAdmin = !!localStorage.getItem('admin_password') ||
                    localStorage.getItem('is_admin_guest') === '1'

    const [photos, setPhotos] = useState([])
    const [loading, setLoading] = useState(true)
    const [error, setError] = useState('')
    const [page, setPage] = useState(1)
    const [hasMore, setHasMore] = useState(false)
    const [total, setTotal] = useState(0)
    // "All Moments" is the whole-wedding view and is admin-only, so guests land
    // on their own "Just Me". Admins keep All Moments as the default.
    const [tab, setTab] = useState(isAdmin ? 'all' : 'mine')  // all | mine | common | people
    // Photos vs Videos within a tab. Default to photos so the newest clips never
    // lead the gallery; the switch appears on the Just Me / Group Moments tabs.
    const [mediaFilter, setMediaFilter] = useState('photos')  // photos | videos
    // Families tab (admin): every household, and the name the admin gave it.
    const [households, setHouseholds] = useState([])
    const [loadingHouseholds, setLoadingHouseholds] = useState(false)
    const [savingFamily, setSavingFamily] = useState(null)
    // Editing a family's members (reassigning clusters).
    const [editingFamilyId, setEditingFamilyId] = useState(null)   // guest_id being edited
    const [allClusters, setAllClusters] = useState([])             // [{cluster_id, name, assigned_to}]
    const [loadingAllClusters, setLoadingAllClusters] = useState(false)
    const [memberQuery, setMemberQuery] = useState('')
    const [familyMemberBusy, setFamilyMemberBusy] = useState(null) // cluster_id mid-update
    // The guest's OWN matched count, independent of the active tab. The header
    // must never show the "All Moments" total (that's every file in the Drive,
    // not photos matched to this guest).
    const [myMatchCount, setMyMatchCount] = useState(null)
    
    // Family Profile states
    const [familyMembers, setFamilyMembers] = useState([])
    const [activeFamilyMemberId, setActiveFamilyMemberId] = useState(null) // null = Master Family Album
    
    // Face Clustering states
    const [clusters, setClusters] = useState([])
    const [loadingClusters, setLoadingClusters] = useState(false)
    const [clustersError, setClustersError] = useState(false)
    const [selectedCluster, setSelectedCluster] = useState(null)
    const [editingClusterId, setEditingClusterId] = useState(null)
    const [newName, setNewName] = useState('')
    const [clusterPhotos, setClusterPhotos] = useState([])
    const [loadingClusterPhotos, setLoadingClusterPhotos] = useState(false)
    
    // Lightbox State
    const [lightboxIndex, setLightboxIndex] = useState(null)
    const [downloadingPhoto, setDownloadingPhoto] = useState(null)
    const [deletingPhoto, setDeletingPhoto] = useState(null)   // drive_id being deleted
    const [batchDeleting, setBatchDeleting] = useState(false)
    // Toasts (success/error feedback) + a reusable confirm modal that replaces
    // window.confirm / alert for destructive actions.
    const [toasts, setToasts] = useState([])            // [{ id, msg, type }]
    const [confirmDialog, setConfirmDialog] = useState(null)  // { title, message, confirmLabel, danger, onConfirm } | null
    const [mediaLoading, setMediaLoading] = useState(true)
    const [photoPeople, setPhotoPeople] = useState([])   // people in current lightbox photo
    const [loadingPeople, setLoadingPeople] = useState(false)
    // Admin correction of the people list: which chip is mid-update, whether the
    // "add person" picker is open, and its search text.
    const [peopleBusyId, setPeopleBusyId] = useState(null)
    const [showAddPerson, setShowAddPerson] = useState(false)
    const [addPersonQuery, setAddPersonQuery] = useState('')

    // Dynamic Categories & Albums states
    const [categories, setCategories] = useState([])
    const [loadingCategories, setLoadingCategories] = useState(false)
    const [selectedCategory, setSelectedCategory] = useState(null)
    const [categoryPhotos, setCategoryPhotos] = useState([])
    const [loadingCategoryPhotos, setLoadingCategoryPhotos] = useState(false)
    const [showCreateCategoryModal, setShowCreateCategoryModal] = useState(false)
    const [newCategoryName, setNewCategoryName] = useState('')
    const [uploadingFiles, setUploadingFiles] = useState([])

    // Sharing states
    const [guestsList, setGuestsList] = useState([])
    const [showShareDropdown, setShowShareDropdown] = useState(false)
    const [shareSearchQuery, setShareSearchQuery] = useState('')
    // Multi-assign: a group photo can hold several people the detector missed,
    // so the modal collects a set of guests and assigns them all in one action.
    const [selectedShareGuests, setSelectedShareGuests] = useState([])
    const [sharing, setSharing] = useState(false)

    // Face Merging states
    const [selectedClusterIds, setSelectedClusterIds] = useState([])
    const [isMergeMode, setIsMergeMode] = useState(false)

    // Multi-Select and Person Details edit states
    const [isMultiSelectMode, setIsMultiSelectMode] = useState(false)
    const [selectedPhotos, setSelectedPhotos] = useState([])
    const [isEditingName, setIsEditingName] = useState(false)
    const [newNameInput, setNewNameInput] = useState('')
    const [showChangePhotoModal, setShowChangePhotoModal] = useState(false)
    const [clusterCacheBuster, setClusterCacheBuster] = useState(Date.now())
    const fetchingPageRef = useRef(0)

    // Transient toast (auto-dismisses). type: 'success' | 'error' | 'info'.
    const showToast = useCallback((msg, type = 'success') => {
        const id = Date.now() + Math.random()
        setToasts(prev => [...prev, { id, msg, type }])
        setTimeout(() => setToasts(prev => prev.filter(t => t.id !== id)), 3400)
    }, [])

    // Open the reusable confirm modal. `onConfirm` may be async; the modal shows a
    // busy spinner until it resolves, then closes.
    const askConfirm = useCallback((opts) => setConfirmDialog(opts), [])
    // Close the modal immediately, then fire the action. The action owns its own
    // progress feedback (the toolbar's "Deleting…", the button spinner, and a
    // completion toast), so the modal never sits open for the length of a long
    // batch delete — which looked frozen/broken.
    const runConfirm = useCallback(() => {
        const fn = confirmDialog?.onConfirm
        setConfirmDialog(null)
        if (fn) Promise.resolve().then(fn)
    }, [confirmDialog])

    // Reset media loading + clear people on lightbox index change
    useEffect(() => {
        setMediaLoading(true)
        setPhotoPeople([])
        setShowAddPerson(false)
        setAddPersonQuery('')
        setPeopleBusyId(null)
        if (lightboxIndex !== null && filtered[lightboxIndex]) {
            const driveId = filtered[lightboxIndex].drive_id
            setLoadingPeople(true)
            getPhotoPeople(driveId)
                .then(res => setPhotoPeople(res.data || []))
                .catch(() => setPhotoPeople([]))
                .finally(() => setLoadingPeople(false))
        }
    }, [lightboxIndex]) // eslint-disable-line react-hooks/exhaustive-deps

    // "All Moments" uses getAllPhotos; personal tabs use getPhotos
    const fetchPhotos = useCallback(async (p) => {
        if (fetchingPageRef.current === p) return
        fetchingPageRef.current = p
        setLoading(true)
        setError('')
        try {
            // /photos/all is every photo in the wedding, so it is admin-only.
            // A guest's "All Moments" is their own album — their matched photos
            // plus everything flagged common — which is what /photos/{id}
            // already returns.
            const mediaTabs = tab === 'mine' || tab === 'common' || tab === 'highlights'
            const res = (tab === 'all' && isAdmin)
                ? await getAllPhotos(p)
                : tab === 'highlights'
                    ? await getHighlights(p, mediaFilter)
                    : await getPhotos(guestId, p, mediaTabs ? tab : 'all', mediaTabs ? mediaFilter : 'all')
            const { photos: newPhotos, has_more, total: totalPhotos, family_members: fetchedFamilyMembers } = res.data
            if (fetchedFamilyMembers) {
                setFamilyMembers(fetchedFamilyMembers)
            } else {
                setFamilyMembers([])
            }
            setPhotos(prev => {
                const combined = p === 1 ? newPhotos : [...prev, ...newPhotos]
                const seen = new Set()
                return combined.filter(item => {
                    if (seen.has(item.drive_id)) return false
                    seen.add(item.drive_id)
                    return true
                })
            })
            setHasMore(has_more)
            setTotal(totalPhotos)
            // Only the guest's own feeds report their real matched total — capture
            // it for the header. Highlights is everyone's shared set, not "yours".
            if (tab === 'mine' || tab === 'common') setMyMatchCount(totalPhotos)
            setPage(p)
        } catch (err) {
            console.error('Failed to fetch photos:', err)
            fetchingPageRef.current = 0 // reset on error to allow retry
            if (err.response?.status === 404) {
                localStorage.clear()
                navigate('/')
            } else {
                setError('Could not load gallery photos. Please check your connection.')
            }
        } finally {
            setLoading(false)
        }
    }, [guestId, navigate, tab, mediaFilter])

    // Fetch the guest's own matched count once on mount, so the header is
    // accurate even when they land on the "All Moments" tab. Admins browse the
    // whole gallery, so their personal count is meaningless — skip the fetch.
    useEffect(() => {
        if (!guestId || isAdmin) return
        let cancelled = false
        getPhotos(guestId, 1)
            .then(res => { if (!cancelled) setMyMatchCount(res.data?.total ?? 0) })
            .catch(() => { if (!cancelled) setMyMatchCount(null) })
        return () => { cancelled = true }
    }, [guestId, isAdmin])

    // Re-fetch when tab changes between 'all' and personal tabs
    useEffect(() => {
        // A pure-admin session (admin password, opened from the Admin page) has
        // no guest link, so there is no guest_id — but it is still a valid way to
        // browse the whole gallery. Only bounce when there is neither a guest nor
        // admin: that's a real "not logged in".
        if (!guestId && !isAdmin) {
            navigate('/')
            return
        }
        fetchingPageRef.current = 0
        setPhotos([])
        setPage(1)
        setError('')
        // Only All Moments / Just Me / Group Moments are photo feeds. People,
        // Families and Albums are landing views (their own detail fetch runs when
        // you open a cluster/album). Firing the main photo fetch here was wasted
        // work — and on People it fired the heavy filter=all merge (~25s for a
        // big album) which competed with the clusters call, showing "Could not
        // load gallery photos" and starving the People list.
        if (tab !== 'all' && tab !== 'mine' && tab !== 'common' && tab !== 'highlights') return
        // Just Me / Group Moments are personal feeds keyed to a guest; a pure
        // admin has no "me", so there is nothing to fetch for those tabs.
        // (Highlights is everyone's shared set, so it needs no guest.)
        if ((tab === 'mine' || tab === 'common') && !guestId) return
        fetchPhotos(1)
    }, [guestId, tab, mediaFilter, navigate]) // eslint-disable-line react-hooks/exhaustive-deps

    // Families tab: load the households on demand (admin only).
    useEffect(() => {
        if (tab !== 'families' || !isAdmin) return
        let cancelled = false
        setLoadingHouseholds(true)
        getHouseholds()
            .then(res => { if (!cancelled) setHouseholds(res.data?.households || []) })
            .catch(err => { if (!cancelled) { console.error('Failed to load households:', err); setHouseholds([]) } })
            .finally(() => { if (!cancelled) setLoadingHouseholds(false) })
        return () => { cancelled = true }
    }, [tab, isAdmin])

    // Rename a family. Blank falls back to the link owner's name.
    const handleRenameFamily = async (guestId_, currentName, owner) => {
        const next = window.prompt(`Family name for ${owner}'s household:`, currentName || '')
        if (next === null) return
        setSavingFamily(guestId_)
        try {
            await setHouseholdName(guestId_, next.trim())
            setHouseholds(prev => prev.map(h =>
                h.guest_id === guestId_ ? { ...h, family_name: next.trim() } : h))
            showToast(next.trim() ? 'Family renamed' : 'Family name cleared')
        } catch (err) {
            console.error('Rename family failed:', err)
            showToast('Could not save that family name', 'error')
        } finally {
            setSavingFamily(null)
        }
    }

    // Open/close the member editor for one family; lazily load the full cluster
    // list the first time so the "add member" picker has options.
    const toggleFamilyEdit = (guestId_) => {
        setMemberQuery('')
        setEditingFamilyId(prev => (prev === guestId_ ? null : guestId_))
        if (allClusters.length === 0 && !loadingAllClusters) {
            setLoadingAllClusters(true)
            getAllClusters()
                .then(res => setAllClusters(res.data?.clusters || []))
                .catch(err => console.error('Failed to load clusters:', err))
                .finally(() => setLoadingAllClusters(false))
        }
    }

    // Add a person (cluster) to a family — reassigns the cluster: the family album
    // is the union of its members (read from guest_clusters at query time).
    const handleAddFamilyMember = async (guestId_, cluster) => {
        setFamilyMemberBusy(cluster.cluster_id)
        try {
            const res = await addHouseholdMember(guestId_, cluster.cluster_id, cluster.name)
            setHouseholds(prev => prev.map(h => h.guest_id === guestId_
                ? {
                    ...h,
                    members: [...h.members, { cluster_id: cluster.cluster_id, label: cluster.name }]
                        .sort((a, b) => a.label.localeCompare(b.label)),
                    member_count: res.data?.member_count ?? h.member_count + 1,
                }
                : h))
            setAllClusters(prev => prev.map(c => c.cluster_id === cluster.cluster_id
                ? { ...c, assigned_to: [...(c.assigned_to || []), guestId_] } : c))
            setMemberQuery('')
            showToast(`Added ${cluster.name} to the family`)
        } catch (err) {
            console.error('Add family member failed:', err)
            showToast('Could not add that person', 'error')
        } finally {
            setFamilyMemberBusy(null)
        }
    }

    const handleRemoveFamilyMember = async (guestId_, member) => {
        setFamilyMemberBusy(member.cluster_id)
        try {
            const res = await removeHouseholdMember(guestId_, member.cluster_id)
            setHouseholds(prev => prev.map(h => h.guest_id === guestId_
                ? {
                    ...h,
                    members: h.members.filter(m => m.cluster_id !== member.cluster_id),
                    member_count: res.data?.member_count ?? Math.max(0, h.member_count - 1),
                }
                : h))
            setAllClusters(prev => prev.map(c => c.cluster_id === member.cluster_id
                ? { ...c, assigned_to: (c.assigned_to || []).filter(g => g !== guestId_) } : c))
            showToast(`Removed ${member.label} from the family`)
        } catch (err) {
            console.error('Remove family member failed:', err)
            showToast('Could not remove that person', 'error')
        } finally {
            setFamilyMemberBusy(null)
        }
    }

    // Infinite Scroll helper
    useEffect(() => {
        const handleScroll = () => {
            if (!hasMore || loading) return
            const threshold = 400 // px from bottom
            const isNearBottom = window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - threshold
            if (isNearBottom) {
                fetchPhotos(page + 1)
            }
        }
        window.addEventListener('scroll', handleScroll)
        return () => window.removeEventListener('scroll', handleScroll)
    }, [hasMore, loading, page, fetchPhotos])

    // Tabs filter or selected face cluster photos
    // Tabs filter or selected face cluster/category photos
    const filtered = selectedCluster
        ? clusterPhotos
        : selectedCategory
        ? categoryPhotos
        : photos.filter(p => {
            if (activeFamilyMemberId) {
                return p.member_ids && p.member_ids.includes(activeFamilyMemberId)
            }
            // The server already applied the tab filter, so re-applying it
            // here would only hide rows it deliberately returned.
            return true
        })

    // Fetch clusters dynamically when switching to people tab
    const loadClusters = useCallback(async () => {
        setLoadingClusters(true)
        setClustersError(false)
        try {
            const res = await getFaceClusters()
            setClusters(res.data || [])
        } catch (err) {
            // The first cold request rebuilds ~27k faces (~20s) and can time out.
            // Show a retry, NOT "no recognized faces" — the data is there.
            console.error("Failed to fetch recognized faces:", err)
            setClustersError(true)
        } finally {
            setLoadingClusters(false)
        }
    }, [])

    useEffect(() => {
        if (tab === 'people' && clusters.length === 0 && !loadingClusters) {
            loadClusters()
        }
    }, [tab]) // eslint-disable-line react-hooks/exhaustive-deps

    // Fetch categories dynamically when switching to categories tab
    useEffect(() => {
        if (tab === 'categories') {
            const fetchCategories = async () => {
                setLoadingCategories(true)
                try {
                    const res = await getCategories()
                    setCategories(res.data)
                } catch (err) {
                    console.error("Failed to fetch categories:", err)
                } finally {
                    setLoadingCategories(false)
                }
            }
            fetchCategories()
        }
    }, [tab])

    // Admin only. The list of every guest is admin data — it powers "share this
    // photo with…", which guests cannot do — and the endpoint now enforces that,
    // so calling it unconditionally just produced a 403 in every guest's console.
    useEffect(() => {
        if (!isAdmin) return
        const fetchGuests = async () => {
            try {
                const res = await getGuestsList()
                setGuestsList(res.data)
            } catch (err) {
                console.error("Failed to fetch guests list:", err)
            }
        }
        fetchGuests()
    }, [isAdmin])

    const handleClusterClick = async (clusterId) => {
        setSelectedCluster(clusterId)
        setIsMultiSelectMode(false)
        setSelectedPhotos([])
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

    const handleCategoryClick = async (name) => {
        setSelectedCategory(name)
        setIsMultiSelectMode(false)
        setSelectedPhotos([])
        setLoadingCategoryPhotos(true)
        try {
            const res = await getCategoryPhotos(name)
            setCategoryPhotos(res.data)
        } catch (err) {
            console.error("Failed to fetch category photos:", err)
        } finally {
            setLoadingCategoryPhotos(false)
        }
    }

    const handleCreateCategorySubmit = async (e) => {
        e.preventDefault()
        const name = newCategoryName.trim()
        if (!name) return
        try {
            await createCategory(name)
            setNewCategoryName('')
            setShowCreateCategoryModal(false)
            const res = await getCategories()
            setCategories(res.data)
        } catch (err) {
            console.error("Failed to create category:", err)
            alert("Failed to create album. Please try again.")
        }
    }

    const handleFileUpload = async (filesList, targetCategoryName) => {
        const filesArray = Array.from(filesList)
        const queue = filesArray.map(f => ({ name: f.name, status: 'pending' }))
        setUploadingFiles(queue)
        
        for (let i = 0; i < filesArray.length; i++) {
            const file = filesArray[i]
            setUploadingFiles(prev => prev.map((item, idx) => idx === i ? { ...item, status: 'uploading' } : item))
            try {
                await uploadCategoryPhoto(targetCategoryName, file)
                setUploadingFiles(prev => prev.map((item, idx) => idx === i ? { ...item, status: 'success' } : item))
            } catch (err) {
                console.error(`Failed to upload ${file.name}:`, err)
                setUploadingFiles(prev => prev.map((item, idx) => idx === i ? { ...item, status: 'error' } : item))
            }
        }
        
        setTimeout(() => setUploadingFiles([]), 3000)
        handleCategoryClick(targetCategoryName)
    }

    const handleDrop = async (e, targetCategoryName) => {
        e.preventDefault()
        if (!isAdmin) return
        
        const items = e.dataTransfer.items
        if (!items) return
        
        const filesToUpload = []
        const traverseFileTree = async (item, path = "") => {
            if (item.isFile) {
                const file = await new Promise((resolve) => item.file(resolve))
                filesToUpload.push({ file, category: targetCategoryName || path.split('/')[0] || "Uploads" })
            } else if (item.isDirectory) {
                const subDirName = item.name
                const newCatName = targetCategoryName || subDirName
                
                try {
                    await createCategory(newCatName)
                } catch (err) {
                    console.error("Failed to create category on folder drop:", err)
                }
                
                const dirReader = item.createReader()
                const entries = await new Promise((resolve) => {
                    dirReader.readEntries(resolve)
                })
                for (const entry of entries) {
                    await traverseFileTree(entry, path + subDirName + "/")
                }
            }
        }
        
        for (let i = 0; i < items.length; i++) {
            const item = items[i].webkitGetAsEntry ? items[i].webkitGetAsEntry() : null
            if (item) {
                await traverseFileTree(item)
            }
        }
        
        if (filesToUpload.length > 0) {
            const grouped = {}
            filesToUpload.forEach(item => {
                const cat = item.category || "Uploads"
                if (!grouped[cat]) grouped[cat] = []
                grouped[cat].push(item.file)
            })
            
            for (const catName of Object.keys(grouped)) {
                await handleFileUpload(grouped[catName], catName)
            }
            
            const res = await getCategories()
            setCategories(res.data)
        }
    }

    const handleSelectCluster = (clusterId, e) => {
        e.stopPropagation()
        setSelectedClusterIds(prev => {
            if (prev.includes(clusterId)) {
                return prev.filter(id => id !== clusterId)
            } else {
                return [...prev, clusterId]
            }
        })
    }

    const handleMergeSubmit = async (targetId) => {
        const sources = selectedClusterIds.filter(id => id !== targetId)
        if (sources.length === 0) {
            alert("Please select at least one other person card to merge.")
            return
        }
        
        const targetName = clusters.find(c => c.id === targetId)?.name || `Person #${targetId}`
        if (!window.confirm(`Merge ${sources.length} selected face folder(s) into ${targetName}?`)) {
            return
        }
        
        try {
            await mergeClusters(targetId, sources)
            setSelectedClusterIds([])
            setIsMergeMode(false)
            const res = await getFaceClusters()
            setClusters(res.data)
        } catch (err) {
            console.error("Failed to merge clusters:", err)
            alert("Failed to merge folders. Please try again.")
        }
    }

    const handleUnmergeSubmit = async (clusterId, e) => {
        e.stopPropagation()
        if (!window.confirm("Are you sure you want to restore this merged person's original face folders?")) {
            return
        }
        try {
            await unmergeCluster(clusterId)
            const res = await getFaceClusters()
            setClusters(res.data)
        } catch (err) {
            console.error("Failed to unmerge cluster:", err)
            alert("Failed to restore face folders. Please try again.")
        }
    }

    const toggleShareGuest = (guestId) => {
        setSelectedShareGuests(prev =>
            prev.includes(guestId) ? prev.filter(g => g !== guestId) : [...prev, guestId]
        )
    }

    const handleAssignPhoto = async (photoObj) => {
        if (selectedShareGuests.length === 0) return
        setSharing(true)
        try {
            // One call per guest against the existing share endpoint. A group
            // photo assigned to several people lands in each of their "Just Me".
            await Promise.all(selectedShareGuests.map(gid => sharePhoto(photoObj.drive_id, gid)))
            const names = guestsList
                .filter(g => selectedShareGuests.includes(g.id))
                .map(g => g.name)
            alert(`Added to ${names.length === 1 ? names[0] : names.length + " people"}'s photos.`)
            setShowShareDropdown(false)
            setSelectedShareGuests([])
            setShareSearchQuery('')
        } catch (err) {
            console.error("Failed to assign photo:", err)
            alert("Something went wrong assigning the photo. Please try again.")
        } finally {
            setSharing(false)
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
            const downloadUrl = withToken(`${API_BASE}/photos/stream/${driveId}?download=true`)
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

    // Handle photo delete — confirm via the modal, feedback via toast.
    const handleDeletePhoto = (driveId) => {
        askConfirm({
            title: 'Delete this photo?',
            message: 'It will be moved to a recoverable temp_delete folder and removed from the gallery, albums and face folders.',
            confirmLabel: 'Delete',
            danger: true,
            onConfirm: async () => {
                setDeletingPhoto(driveId)
                try {
                    await deletePhoto(driveId)
                    setPhotos(prev => prev.filter(p => p.drive_id !== driveId))
                    setClusterPhotos(prev => prev.filter(p => p.drive_id !== driveId))
                    setCategoryPhotos(prev => prev.filter(p => p.drive_id !== driveId))
                    setLightboxIndex(null)
                    showToast('Photo deleted')
                } catch (err) {
                    console.error("Failed to delete photo:", err)
                    showToast('Could not delete — check the service account has edit access', 'error')
                } finally {
                    setDeletingPhoto(null)
                }
            },
        })
    }

    // Handle guest-level "Not Me" disassociation action
    // Admin: drop a photo out of Group Moments (clears is_common). The photo is
    // NOT deleted — it stays in the gallery and in the album of whoever is in it.
    const handleRemoveFromGroup = (driveId) => {
        askConfirm({
            title: 'Remove from Group Moments?',
            message: 'It stays in the gallery and in the personal albums of whoever is in it.',
            confirmLabel: 'Remove',
            onConfirm: async () => {
                try {
                    await removeFromGroup(driveId)
                    setPhotos(prev => prev.filter(p => p.drive_id !== driveId))
                    setClusterPhotos(prev => prev.filter(p => p.drive_id !== driveId))
                    setLightboxIndex(null)
                    showToast('Removed from Group Moments')
                } catch (err) {
                    console.error('Remove from group failed:', err)
                    showToast('Could not remove from Group Moments', 'error')
                }
            },
        })
    }

    // Admin: add a photo to everyone's album (Highlights + every download).
    // Additive and reversible, so no confirm — just flip is_common in place.
    const handleMarkCommon = async (driveId) => {
        try {
            await markAsCommon(driveId)
            setPhotos(prev => prev.map(p => p.drive_id === driveId ? { ...p, is_common: true } : p))
            setClusterPhotos(prev => prev.map(p => p.drive_id === driveId ? { ...p, is_common: true } : p))
            showToast("Added to everyone's album ✨")
        } catch (err) {
            console.error('Mark common failed:', err)
            showToast("Could not add to everyone's album", 'error')
        }
    }

    const handleBatchMarkCommon = async () => {
        if (selectedPhotos.length === 0) return
        const ids = selectedPhotos
        try {
            await markAsCommonBatch(ids)
            setPhotos(prev => prev.map(p => ids.includes(p.drive_id) ? { ...p, is_common: true } : p))
            setIsMultiSelectMode(false)
            setSelectedPhotos([])
            showToast(`${ids.length} added to everyone's album ✨`)
        } catch (err) {
            console.error('Batch mark common failed:', err)
            showToast('Could not add the selected photos', 'error')
        }
    }

    // Admin: remove a photo from the album currently being viewed.
    const handleRemoveFromAlbum = (driveId, album) => {
        if (!album) return
        askConfirm({
            title: `Remove from "${album}"?`,
            message: 'The photo itself is kept — it just leaves this album.',
            confirmLabel: 'Remove',
            onConfirm: async () => {
                try {
                    await removeFromAlbum(driveId, album)
                    setCategoryPhotos(prev => prev.filter(p => p.drive_id !== driveId))
                    setPhotos(prev => prev.filter(p => p.drive_id !== driveId))
                    setLightboxIndex(null)
                    showToast(`Removed from ${album}`)
                } catch (err) {
                    console.error('Remove from album failed:', err)
                    showToast('Could not remove from the album', 'error')
                }
            },
        })
    }

    const handleNotMePhoto = (driveId) => {
        if (!guestId) return
        askConfirm({
            title: 'Not you?',
            message: "We'll remove this photo from your personal folder and stop matching it to you.",
            confirmLabel: "It's not me",
            onConfirm: async () => {
                try {
                    await notMePhoto(driveId, guestId)
                    setPhotos(prev => prev.filter(p => p.drive_id !== driveId))
                    setClusterPhotos(prev => prev.filter(p => p.drive_id !== driveId))
                    setCategoryPhotos(prev => prev.filter(p => p.drive_id !== driveId))
                    if (filtered.length <= 1) {
                        setLightboxIndex(null)
                    } else if (lightboxIndex >= filtered.length - 1) {
                        setLightboxIndex(filtered.length - 2)
                    }
                    showToast('Removed from your photos')
                } catch (err) {
                    console.error("Failed to disassociate photo:", err)
                    showToast('Could not process that — please try again', 'error')
                }
            },
        })
    }

    // --- Admin: correct who is in a photo (lightbox chips) ---

    // The "add person" picker is the single tool for "who is in this photo" — it
    // lists recognized faces AND every registered guest, so it also covers guests
    // whose face was never detected (what the old separate "Assign" button did).
    const openAddPerson = () => {
        setShowAddPerson(true)
        setAddPersonQuery('')
        // Load both in parallel and DON'T block the picker on either. The guest
        // list is small and fast, so it shows almost immediately; the recognized
        // faces (a heavier call) merge in when they arrive. Previously the picker
        // awaited the slow clusters call first, so it sat empty for seconds.
        if (guestsList.length === 0) {
            getGuestsList()
                .then(res => setGuestsList(res.data || []))
                .catch(err => console.error('Failed to load guests for add:', err))
        }
        if (clusters.length === 0 && !loadingClusters) {
            setLoadingClusters(true)
            getFaceClusters()
                .then(res => setClusters(res.data || []))
                .catch(err => console.error('Failed to load people for add:', err))
                .finally(() => setLoadingClusters(false))
        }
    }

    const handleRemovePersonFromPhoto = async (personId) => {
        const photo = filtered[lightboxIndex]
        if (!photo) return
        setPeopleBusyId(personId)
        try {
            const removed = photoPeople.find(p => p.id === personId)
            await removePersonFromPhoto(photo.drive_id, personId)
            setPhotoPeople(prev => prev.filter(p => p.id !== personId))
            showToast(removed ? `Removed ${removed.name}` : 'Person removed')
        } catch (err) {
            console.error('Remove person failed:', err)
            showToast('Could not remove that person', 'error')
        } finally {
            setPeopleBusyId(null)
        }
    }

    const handleAddPersonToPhoto = async (person) => {
        const photo = filtered[lightboxIndex]
        if (!photo) return
        if (photoPeople.some(p => p.id === person.id)) {
            setShowAddPerson(false)
            return
        }
        setPeopleBusyId(person.id)
        try {
            await addPersonToPhoto(photo.drive_id, person)
            setPhotoPeople(prev => [...prev, {
                id: person.id,
                name: person.name,
                thumbnail_url: person.thumbnail_url,
                is_guest: !!person.is_guest,
            }])
            setShowAddPerson(false)
            setAddPersonQuery('')
            showToast(`Added ${person.name}`)
        } catch (err) {
            console.error('Add person failed:', err)
            showToast('Could not add that person', 'error')
        } finally {
            setPeopleBusyId(null)
        }
    }

    const togglePhotoSelection = (driveId) => {
        setSelectedPhotos(prev => {
            if (prev.includes(driveId)) {
                return prev.filter(id => id !== driveId)
            } else {
                return [...prev, driveId]
            }
        })
    }

    const handleBatchDownload = async () => {
        if (selectedPhotos.length === 0) return
        try {
            const response = await downloadPhotosBatch(selectedPhotos)
            const blob = new Blob([response.data], { type: 'application/zip' })
            const link = document.createElement('a')
            link.href = window.URL.createObjectURL(blob)
            link.download = 'wedding_photos.zip'
            document.body.appendChild(link)
            link.click()
            document.body.removeChild(link)
            setIsMultiSelectMode(false)
            setSelectedPhotos([])
        } catch (err) {
            console.error('Failed to download batch:', err)
            alert('Could not download batch ZIP. Please try again.')
        }
    }

    const handleBatchDelete = () => {
        if (selectedPhotos.length === 0 || batchDeleting) return
        const n = selectedPhotos.length
        askConfirm({
            title: `Delete ${n} ${n === 1 ? 'item' : 'items'}?`,
            message: 'They will be moved to a recoverable temp_delete folder and removed from the gallery, albums and face folders.',
            confirmLabel: `Delete ${n}`,
            danger: true,
            onConfirm: async () => {
                setBatchDeleting(true)
                const ids = selectedPhotos
                try {
                    await deletePhotosBatch(ids)
                    setPhotos(prev => prev.filter(p => !ids.includes(p.drive_id)))
                    setClusterPhotos(prev => prev.filter(p => !ids.includes(p.drive_id)))
                    setCategoryPhotos(prev => prev.filter(p => !ids.includes(p.drive_id)))
                    setIsMultiSelectMode(false)
                    setSelectedPhotos([])
                    showToast(`${n} ${n === 1 ? 'item' : 'items'} deleted`)
                } catch (err) {
                    console.error('Failed to delete batch:', err)
                    showToast('Could not delete the selected items', 'error')
                } finally {
                    setBatchDeleting(false)
                }
            },
        })
    }

    const handleSetProfilePic = async (driveId) => {
        try {
            await setClusterProfilePic(selectedCluster, driveId)
            // Refresh clusters list so the UI updates
            const res = await getFaceClusters()
            setClusters(res.data)
            setClusterCacheBuster(Date.now())
            setShowChangePhotoModal(false)
        } catch (err) {
            console.error("Failed to set profile picture:", err)
            alert("Failed to set profile picture. Please try again.")
        }
    }

    const handleManualAvatarUpload = async (e) => {
        const file = e.target.files?.[0]
        if (!file) return
        try {
            await uploadClusterProfilePic(selectedCluster, file)
            // Refresh clusters list so the UI updates
            const res = await getFaceClusters()
            setClusters(res.data)
            setClusterCacheBuster(Date.now())
            setShowChangePhotoModal(false)
        } catch (err) {
            console.error("Failed to upload profile picture:", err)
            alert("Failed to upload profile picture. Please try again.")
        }
    }


    const handleRenameClusterPage = async (name) => {
        if (!name.trim()) return
        try {
            await renameCluster(selectedCluster, name.trim())
            setClusters(prev => prev.map(c => c.id === selectedCluster ? { ...c, name: name.trim() } : c))
            setIsEditingName(false)
        } catch (err) {
            console.error('Failed to rename cluster:', err)
            alert('Failed to rename. Please try again.')
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

    return (
        <>
            <div className="min-h-screen bg-ivory-100/70 select-none pb-12 animate-fade-in-up">
            
            {/* Elegant Header */}
            <div className="bg-white/80 backdrop-blur-md border-b border-gold-200/50 px-4 sm:px-6 py-4 sm:py-6 sticky top-0 z-20">
                <div className="max-w-4xl mx-auto flex flex-wrap items-center justify-between gap-y-3">
                    <div className="flex items-center gap-3 sm:gap-4 min-w-0 flex-1">
                        <img
                            src="/logo.png"
                            alt="Logo"
                            className="w-11 h-11 sm:w-14 sm:h-14 shrink-0 object-contain rounded-full shadow-md shadow-gold-100 border border-gold-200/10 bg-white p-0.5"
                        />
                        <div className="min-w-0">
                            <h1 className="font-serif text-taupe-900 text-lg sm:text-xl tracking-tight leading-tight mb-1 truncate">{eventName || 'Wedding Gallery'}</h1>
                            <p className="text-taupe-400 text-xs tracking-wide truncate">
                                {isAdmin
                                    ? 'Browsing all moments · admin'
                                    : myMatchCount === null
                                        ? 'Finding your moments…'
                                        : myMatchCount > 0
                                            ? `${myMatchCount} ${myMatchCount === 1 ? 'moment' : 'moments'} of ${guestName?.split(' ')[0]}`
                                            : `Browsing all moments · none matched to ${guestName?.split(' ')[0]} yet`}
                            </p>
                        </div>
                    </div>
                    
                    <div className="flex gap-2 shrink-0">
                        {/* The "Re-Scan Face" button is gone. Photos are matched
                            by the clustering, which a human has already named, so
                            there is nothing for a guest to re-scan — the button
                            only led to a screen that could fail to find a face. */}
                        {/* Download All zips a specific guest's album. A pure
                            admin (no guest link) has no album to bundle, so the
                            button would point at /download/null/all — hide it. */}
                        {guestId && (
                            <button
                                onClick={() => { window.location.href = getDownloadAllUrl(guestId) }}
                                className="bg-taupe-800 text-white text-xs font-semibold px-3 sm:px-4 py-2.5 rounded-xl whitespace-nowrap hover:bg-gold-600 hover:shadow-lg hover:shadow-gold-500/20 transition-all duration-300 cursor-pointer"
                            >
                                Download<span className="hidden sm:inline"> All</span>
                            </button>
                        )}
                    </div>
                </div>

                {/* Aesthetic Navigation Tabs */}
                <div className="max-w-4xl mx-auto flex gap-5 sm:gap-6 mt-4 sm:mt-6 border-t border-gold-100 pt-4 overflow-x-auto whitespace-nowrap scrollbar-none -mx-4 px-4 sm:mx-0 sm:px-0">
                    {['all', 'mine', 'highlights', 'people', 'families', 'categories'].filter(t => isAdmin
                        // Admin manages the whole gallery: All Moments already contains every
                        // photo, so the guest's personal feed is just noise here — hide it.
                        ? (t !== 'mine')
                        // A guest gets their own album ("My Photos" = every photo they're in,
                        // solo AND group) plus the shared Highlights + Albums. There's no
                        // separate "Group Moments": it was only ever a filtered subset of the
                        // personal album, so it doubled the same photos across two tabs.
                        : (t !== 'all' && t !== 'people' && t !== 'families')
                    ).map(t => (
                        <button
                            key={t}
                            onClick={() => {
                                setTab(t)
                                setSelectedCluster(null)
                                setSelectedCategory(null)
                                setIsMultiSelectMode(false)
                                setSelectedPhotos([])
                                setActiveFamilyMemberId(null)
                            }}
                            className={`shrink-0 text-xs uppercase tracking-widest font-semibold pb-1.5 border-b-2 transition-all duration-300 cursor-pointer ${tab === t
                                    ? 'border-gold-500 text-taupe-900'
                                    : 'border-transparent text-taupe-300 hover:text-taupe-500'
                                }`}
                        >
                            {t === 'all' ? 'All Moments' : t === 'mine' ? 'My Photos' : t === 'highlights' ? 'Highlights' : t === 'people' ? 'People' : t === 'families' ? 'Families' : 'Albums'}
                        </button>
                    ))}
                </div>
            </div>

            {/* Error Message */}
            {error && (
                <div className="max-w-4xl mx-auto px-4 mt-6">
                    <div className="bg-red-50/60 border border-red-100 rounded-xl p-4 text-red-500 text-sm text-center font-medium flex flex-col items-center gap-3">
                        {error}
                        <button
                            onClick={() => { setError(''); fetchingPageRef.current = 0; fetchPhotos(1) }}
                            className="bg-taupe-800 text-white text-xs font-semibold px-4 py-2 rounded-lg hover:bg-gold-600 transition cursor-pointer"
                        >
                            Try again
                        </button>
                    </div>
                </div>
            )}

            {/* Photo Grid Section */}
            <div className="max-w-4xl mx-auto px-4 py-8">
                
                {/* 1. PEOPLE / FACES TAB GRID */}
                {tab === 'people' && !selectedCluster && (
                    loadingClusters ? (
                        <div className="flex flex-col items-center gap-6">
                            <div className="flex items-center gap-2.5 text-taupe-400 text-xs font-medium">
                                <span className="w-3.5 h-3.5 border-2 border-gold-300 border-t-transparent rounded-full animate-spin" />
                                Gathering everyone's faces…
                            </div>
                            <div className="w-full grid grid-cols-3 sm:grid-cols-4 md:grid-cols-5 gap-6 md:gap-8">
                                {[...Array(10)].map((_, i) => (
                                    <div key={i} className="flex flex-col items-center gap-3 animate-pulse">
                                        <div className="w-24 h-24 sm:w-28 sm:h-28 rounded-full bg-gold-100/60" />
                                        <div className="h-3 w-16 bg-gold-100/60 rounded" />
                                        <div className="h-2.5 w-12 bg-stone-150/60 rounded" />
                                    </div>
                                ))}
                            </div>
                        </div>
                    ) : clustersError ? (
                        <div className="text-center py-20 flex flex-col items-center gap-4 animate-fade-in-up">
                            <div className="text-4xl">🌐</div>
                            <h3 className="font-serif text-taupe-800 text-lg">Taking a moment to load</h3>
                            <p className="text-taupe-400 font-light text-sm max-w-xs leading-relaxed">
                                The face folders are waking up (this can take a few seconds the first time). Give it another try.
                            </p>
                            <button
                                onClick={loadClusters}
                                className="mt-1 bg-taupe-800 text-white text-xs font-semibold px-5 py-2.5 rounded-xl hover:bg-gold-600 transition cursor-pointer"
                            >
                                Try again
                            </button>
                        </div>
                    ) : clusters.length === 0 ? (
                        <div className="text-center py-20 flex flex-col items-center gap-4 animate-fade-in-up">
                            <div className="text-4xl">👥</div>
                            <h3 className="font-serif text-taupe-800 text-lg">No recognized faces yet</h3>
                            <p className="text-taupe-400 font-light text-sm max-w-xs leading-relaxed">
                                Once face preprocessing is complete, clusters of all guests will appear here.
                            </p>
                        </div>
                    ) : (
                        <div className="space-y-6">
                            {isAdmin && (
                                <div className="flex justify-between items-center mb-6 pb-2 border-b border-gold-200/50">
                                    <div>
                                        <h2 className="font-serif text-taupe-900 text-base">Group Face Folders</h2>
                                        <p className="text-taupe-400 text-xs mt-0.5">Combine raw face clusters together.</p>
                                    </div>
                                    <div className="flex gap-2">
                                        {isMergeMode ? (
                                            <>
                                                <button
                                                    onClick={() => {
                                                        setIsMergeMode(false)
                                                        setSelectedClusterIds([])
                                                    }}
                                                    className="bg-white border border-gold-200/60 text-taupe-600 text-xs font-semibold px-3 py-1.5 rounded-xl hover:bg-ivory-100 cursor-pointer"
                                                >
                                                    Cancel
                                                </button>
                                                <button
                                                    onClick={() => {
                                                        if (selectedClusterIds.length < 2) {
                                                            alert("Please select at least 2 people to merge.")
                                                            return
                                                        }
                                                        handleMergeSubmit(selectedClusterIds[0])
                                                    }}
                                                    className="bg-taupe-800 text-white text-xs font-semibold px-3 py-1.5 rounded-xl hover:bg-gold-650 cursor-pointer shadow-sm disabled:opacity-50"
                                                    disabled={selectedClusterIds.length < 2}
                                                >
                                                    Merge Selected ({selectedClusterIds.length})
                                                </button>
                                            </>
                                        ) : (
                                            <button
                                                onClick={() => setIsMergeMode(true)}
                                                className="bg-white border border-gold-200/60 text-taupe-700 text-xs font-semibold px-3.5 py-2 rounded-xl hover:bg-ivory-100 cursor-pointer"
                                            >
                                                👥 Merge Faces
                                            </button>
                                        )}
                                    </div>
                                </div>
                            )}

                            <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-5 gap-6 md:gap-8">
                                {clusters.map((cluster) => (
                                    <div
                                        key={cluster.id}
                                        onClick={(e) => {
                                            if (isMergeMode) {
                                                handleSelectCluster(cluster.id, e)
                                            } else {
                                                handleClusterClick(cluster.id)
                                            }
                                        }}
                                        className="flex flex-col items-center gap-3 cursor-pointer group relative animate-fade-in-up"
                                    >
                                        <div className="relative w-24 h-24 sm:w-28 sm:h-28 rounded-full overflow-hidden border-2 border-transparent group-hover:border-gold-500 shadow-md transition-all duration-300 transform group-hover:scale-105">
                                            <img
                                                src={withToken(`${API_BASE}${cluster.thumbnail_url}`)}
                                                alt=""
                                                className="w-full h-full object-cover bg-ivory-200"
                                                loading="lazy"
                                            />
                                            {isMergeMode && (
                                                <div className="absolute inset-0 bg-black/40 flex items-center justify-center">
                                                    <input
                                                        type="checkbox"
                                                        checked={selectedClusterIds.includes(cluster.id)}
                                                        onChange={(e) => handleSelectCluster(cluster.id, e)}
                                                        className="w-5 h-5 accent-gold-500 cursor-pointer"
                                                        onClick={(e) => e.stopPropagation()}
                                                    />
                                                </div>
                                            )}
                                        </div>
                                        
                                        {cluster.is_merged && !isMergeMode && isAdmin && (
                                            <button
                                                onClick={(e) => handleUnmergeSubmit(cluster.id, e)}
                                                className="absolute top-0 right-1 bg-red-650 hover:bg-red-750 text-white rounded-full px-2 py-0.5 shadow-md transition duration-300 text-[9px] font-semibold cursor-pointer"
                                                title="Unmerge folders"
                                            >
                                                Dissolve
                                            </button>
                                        )}

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
                                                        className="w-20 text-center text-xs border border-gold-200 rounded px-1 py-0.5 bg-white text-taupe-800 focus:outline-none focus:border-gold-500"
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
                                                        className="text-xs font-semibold text-taupe-800 hover:text-gold-600 transition-colors cursor-pointer"
                                                    >
                                                        {cluster.name || `Person #${cluster.id}`}
                                                    </span>
                                                    {isAdmin && (
                                                        <button
                                                            onClick={() => {
                                                                setEditingClusterId(cluster.id)
                                                                setNewName(cluster.name || `Person #${cluster.id}`)
                                                            }}
                                                            className="text-taupe-400 hover:text-gold-600 transition-colors text-[10px] opacity-0 group-hover/name:opacity-100 group-hover:opacity-100 focus:opacity-100 cursor-pointer"
                                                            title="Rename person"
                                                        >
                                                            ✏️
                                                        </button>
                                                    )}
                                                </div>
                                            )}
                                            <p className="text-[10px] text-taupe-400 uppercase tracking-wider mt-0.5">
                                                {cluster.count} {cluster.count === 1 ? 'Moment' : 'Moments'}
                                            </p>
                                        </div>
                                    </div>
                                ))}
                            </div>
                        </div>
                    )
                )}

                {/* 2. DYNAMIC CATEGORIES / ALBUMS GRID */}
                {tab === 'categories' && !selectedCategory && (
                    <div 
                        className="space-y-8"
                        onDragOver={(e) => e.preventDefault()}
                        onDrop={(e) => handleDrop(e, null)}
                    >
                        <div className="flex justify-between items-center border-b border-gold-200/50 pb-4">
                            <div>
                                <h2 className="font-serif text-taupe-900 text-lg">Dynamic Albums</h2>
                                <p className="text-taupe-400 text-xs mt-0.5">Drag & drop files or folders anywhere to upload.</p>
                            </div>
                            {isAdmin && (
                                <button
                                    onClick={() => setShowCreateCategoryModal(true)}
                                    className="bg-taupe-800 text-white text-xs font-semibold px-4 py-2.5 rounded-xl hover:bg-gold-600 transition-all duration-300 cursor-pointer"
                                >
                                    + Create Album
                                </button>
                            )}
                        </div>

                        {/* Uploading Status */}
                        {uploadingFiles.length > 0 && (
                            <div className="bg-white border border-gold-200/80 rounded-2xl p-4 shadow-lg flex flex-col gap-2 animate-fade-in-up">
                                <h4 className="text-xs font-bold text-taupe-500 uppercase tracking-wider">Uploading Files...</h4>
                                <div className="max-h-32 overflow-y-auto space-y-1.5 font-mono text-[10px]">
                                    {uploadingFiles.map((file, idx) => (
                                        <div key={idx} className="flex justify-between items-center text-taupe-700">
                                            <span className="truncate max-w-[70%]">{file.name}</span>
                                            <span className={`font-semibold uppercase tracking-wider ${
                                                file.status === 'success' ? 'text-green-600' :
                                                file.status === 'error' ? 'text-red-500' :
                                                file.status === 'uploading' ? 'text-gold-650 animate-pulse' : 'text-stone-450'
                                            }`}>
                                                {file.status}
                                            </span>
                                        </div>
                                    ))}
                                </div>
                            </div>
                        )}

                        {loadingCategories ? (
                            <div className="grid grid-cols-2 sm:grid-cols-3 gap-4 md:gap-6">
                                {[...Array(3)].map((_, i) => (
                                    <div key={i} className="aspect-[4/3] bg-gold-100/50 border border-gold-100/50 rounded-2xl animate-pulse" />
                                ))}
                            </div>
                        ) : categories.length === 0 ? (
                            <div className="text-center py-16 flex flex-col items-center gap-4 border-2 border-dashed border-gold-200/60 rounded-3xl bg-white/40 p-8">
                                <div className="text-4xl">📁</div>
                                <h3 className="font-serif text-taupe-800 text-lg">No albums created yet</h3>
                                {isAdmin ? (
                                    <>
                                        <p className="text-taupe-400 font-light text-xs max-w-xs leading-relaxed">
                                            Create an album or drag & drop files/folders directly onto this page to start.
                                        </p>
                                        <button
                                            onClick={() => setShowCreateCategoryModal(true)}
                                            className="mt-2 bg-taupe-800 text-white text-xs font-semibold px-4 py-2.5 rounded-xl hover:bg-gold-600 transition duration-300 cursor-pointer"
                                        >
                                            Create Your First Album
                                        </button>
                                    </>
                                ) : (
                                    <p className="text-taupe-400 font-light text-xs max-w-xs leading-relaxed">
                                        Once albums are added by the admin, they will appear here.
                                    </p>
                                )}
                            </div>
                        ) : (
                            <div className="grid grid-cols-2 sm:grid-cols-3 gap-4 md:gap-6">
                                {categories.map((cat) => (
                                    <div
                                        key={cat.name}
                                        onClick={() => handleCategoryClick(cat.name)}
                                        className="group flex flex-col gap-3 cursor-pointer bg-white/70 border border-gold-200/50 rounded-2xl p-4 shadow-sm hover:shadow-md hover:border-gold-300 transition-all duration-300 animate-fade-in-up"
                                    >
                                        <div className="aspect-[4/3] relative rounded-xl overflow-hidden bg-ivory-200 border border-gold-100 shadow-inner">
                                            {cat.thumbnail_url ? (
                                                <img
                                                    src={withToken(`${API_BASE}${cat.thumbnail_url}`)}
                                                    alt=""
                                                    className="w-full h-full object-cover transition-transform duration-700 group-hover:scale-105"
                                                    loading="lazy"
                                                />
                                            ) : (
                                                <div className="absolute inset-0 flex items-center justify-center text-3xl opacity-35 bg-ivory-100">
                                                    📁
                                                </div>
                                            )}
                                        </div>
                                        <div className="px-1">
                                            <h3 className="font-semibold text-taupe-800 text-sm truncate group-hover:text-gold-600 transition-colors">
                                                {cat.name}
                                            </h3>
                                            <p className="text-[10px] text-taupe-400 uppercase tracking-wider mt-0.5">
                                                {cat.count} {cat.count === 1 ? 'Moments' : 'Moments'}
                                            </p>
                                        </div>
                                    </div>
                                ))}
                            </div>
                        )}
                    </div>
                )}

                {/* FAMILIES (admin): every household that shares a link */}
                {tab === 'families' && isAdmin && (
                    <div className="mb-8">
                        <div className="mb-6">
                            <h2 className="font-serif text-taupe-900 text-xl sm:text-2xl tracking-tight leading-none">Families</h2>
                            <p className="text-taupe-400 text-xs font-medium mt-1.5">
                                {loadingHouseholds
                                    ? 'Loading…'
                                    : `${households.length} ${households.length === 1 ? 'family shares' : 'families share'} a link — everyone on a link sees the whole family's photos`}
                            </p>
                        </div>

                        {!loadingHouseholds && households.length === 0 && (
                            <p className="text-taupe-400 text-sm">No families yet.</p>
                        )}

                        <div className="grid gap-3 sm:grid-cols-2">
                            {households.map(h => (
                                <div key={h.guest_id} className="bg-white border border-gold-200/60 rounded-2xl p-4 shadow-xs">
                                    <div className="flex items-start justify-between gap-3">
                                        <div className="min-w-0">
                                            <p className="font-serif text-taupe-900 text-base truncate">
                                                {h.family_name || `${h.owner}'s family`}
                                            </p>
                                            <p className="text-[11px] text-taupe-400 mt-0.5">
                                                link owner: {h.owner} · {h.member_count} {h.member_count === 1 ? 'person' : 'people'}
                                            </p>
                                        </div>
                                        <div className="flex gap-1.5 shrink-0">
                                            <button
                                                onClick={() => handleRenameFamily(h.guest_id, h.family_name, h.owner)}
                                                disabled={savingFamily === h.guest_id}
                                                className="bg-white border border-gold-200/60 text-taupe-700 text-[11px] font-semibold px-3 py-1.5 rounded-lg hover:bg-ivory-100 cursor-pointer transition-all"
                                            >
                                                {savingFamily === h.guest_id ? 'Saving…' : (h.family_name ? 'Rename' : 'Name it')}
                                            </button>
                                            <button
                                                onClick={() => toggleFamilyEdit(h.guest_id)}
                                                className={`text-[11px] font-semibold px-3 py-1.5 rounded-lg cursor-pointer transition-all border ${editingFamilyId === h.guest_id ? 'bg-taupe-800 text-white border-taupe-800' : 'bg-white text-taupe-700 border-gold-200/60 hover:bg-ivory-100'}`}
                                            >
                                                {editingFamilyId === h.guest_id ? 'Done' : 'Edit members'}
                                            </button>
                                        </div>
                                    </div>

                                    {/* Member chips — with a ✕ to remove while editing. */}
                                    <div className="flex flex-wrap gap-1.5 mt-3">
                                        {h.members.length === 0 && (
                                            <span className="text-[11px] text-taupe-400 italic">No members yet</span>
                                        )}
                                        {h.members.map(m => (
                                            <span key={m.cluster_id} className={`inline-flex items-center gap-1.5 text-[11px] bg-ivory-100 border border-gold-200/50 text-taupe-600 rounded-full pl-2.5 ${editingFamilyId === h.guest_id ? 'pr-1' : 'pr-2.5'} py-1 ${familyMemberBusy === m.cluster_id ? 'opacity-50' : ''}`}>
                                                {m.label}
                                                {editingFamilyId === h.guest_id && (
                                                    <button
                                                        onClick={() => handleRemoveFamilyMember(h.guest_id, m)}
                                                        disabled={familyMemberBusy === m.cluster_id}
                                                        title="Remove from this family"
                                                        className="w-4 h-4 flex items-center justify-center rounded-full bg-taupe-200/60 hover:bg-red-500 hover:text-white text-taupe-500 text-[10px] leading-none cursor-pointer transition-colors"
                                                    >
                                                        {familyMemberBusy === m.cluster_id
                                                            ? <span className="inline-block w-2 h-2 border border-taupe-400 border-t-transparent rounded-full animate-spin" />
                                                            : '×'}
                                                    </button>
                                                )}
                                            </span>
                                        ))}
                                    </div>

                                    {/* Add-member picker (edit mode). */}
                                    {editingFamilyId === h.guest_id && (
                                        <div className="mt-3 pt-3 border-t border-gold-100">
                                            <input
                                                autoFocus
                                                value={memberQuery}
                                                onChange={e => setMemberQuery(e.target.value)}
                                                placeholder="Add a person to this family…"
                                                className="w-full bg-ivory-100/70 border border-gold-200/50 rounded-lg px-3 py-2 text-xs text-taupe-800 outline-none focus:border-taupe-400 placeholder:text-taupe-400"
                                            />
                                            <div className="max-h-48 overflow-y-auto mt-1.5 flex flex-col gap-0.5">
                                                {loadingAllClusters ? (
                                                    <div className="flex items-center gap-2 px-2 py-3 text-taupe-400 text-xs">
                                                        <span className="inline-block w-3 h-3 border border-taupe-300 border-t-transparent rounded-full animate-spin" />
                                                        Loading people…
                                                    </div>
                                                ) : (() => {
                                                    const already = new Set(h.members.map(m => m.cluster_id))
                                                    const q = memberQuery.trim().toLowerCase()
                                                    const opts = allClusters
                                                        .filter(c => !already.has(c.cluster_id))
                                                        .filter(c => !c.name.startsWith('Person #') || q)  // hide unnamed unless searched
                                                        .filter(c => !q || c.name.toLowerCase().includes(q))
                                                        .slice(0, 30)
                                                    if (opts.length === 0) {
                                                        return <p className="text-taupe-400 text-xs px-2 py-2 text-center">{q ? 'No matches' : 'Start typing a name…'}</p>
                                                    }
                                                    return opts.map(c => {
                                                        const inOther = (c.assigned_to || []).length > 0
                                                        return (
                                                            <button
                                                                key={c.cluster_id}
                                                                onClick={() => handleAddFamilyMember(h.guest_id, c)}
                                                                disabled={familyMemberBusy === c.cluster_id}
                                                                className="flex items-center gap-2 px-2 py-1.5 rounded-lg hover:bg-ivory-100 text-left transition-colors cursor-pointer disabled:opacity-50"
                                                            >
                                                                <span className="text-xs font-medium text-taupe-800 truncate">{c.name}</span>
                                                                {familyMemberBusy === c.cluster_id
                                                                    ? <span className="ml-auto shrink-0 inline-block w-3 h-3 border border-taupe-400 border-t-transparent rounded-full animate-spin" />
                                                                    : inOther && <span className="ml-auto shrink-0 text-[9px] text-gold-600 uppercase tracking-wide">in another family</span>}
                                                            </button>
                                                        )
                                                    })
                                                })()}
                                            </div>
                                        </div>
                                    )}
                                </div>
                            ))}
                        </div>
                    </div>
                )}

                {/* 3. STANDARD FEED OR SELECTED COLLECTION DETAIL FEED */}
                {((tab !== 'people' && tab !== 'categories' && tab !== 'families') || selectedCluster || selectedCategory) && (
                    <>
                        {/* Standard Tabs Header / Action Bar */}
                        {!selectedCluster && !selectedCategory && (tab === 'all' || tab === 'mine' || tab === 'common' || tab === 'highlights') && (
                            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-6 mb-8 pb-6 border-b border-gold-200/50">
                                <div>
                                    <h2 className="font-serif text-taupe-900 text-xl sm:text-2xl tracking-tight leading-none">
                                        {tab === 'all' ? 'All Moments' : tab === 'mine' ? 'My Photos' : 'Highlights'}
                                    </h2>
                                    <p className="text-taupe-400 text-xs font-medium mt-1.5">
                                        {tab === 'highlights'
                                            ? `${total > 0 ? total.toLocaleString() : filtered.length} shots everyone shares — the couple, the venue, the big moments`
                                            : tab === 'mine'
                                                ? `${total > 0 ? total.toLocaleString() : filtered.length} ${total === 1 ? 'photo' : 'photos'} of you — solo and group`
                                                : tab === 'all' && total > 0
                                                    ? `${total.toLocaleString()} moments in the full gallery`
                                                    : `${filtered.length} ${filtered.length === 1 ? 'moment' : 'moments'} shown`}
                                    </p>
                                    {(tab === 'mine' || tab === 'common' || tab === 'highlights') && (
                                        <div className="mt-3 inline-flex items-center gap-0.5 rounded-full bg-ivory-100 border border-gold-200/60 p-0.5 shadow-xs">
                                            {['photos', 'videos'].map(m => (
                                                <button
                                                    key={m}
                                                    onClick={() => setMediaFilter(m)}
                                                    className={`px-5 py-1.5 text-xs font-semibold uppercase tracking-wide rounded-full transition-all duration-300 cursor-pointer ${
                                                        mediaFilter === m
                                                            ? 'bg-taupe-800 text-white shadow-sm'
                                                            : 'text-taupe-400 hover:text-taupe-700'
                                                    }`}
                                                >
                                                    {m === 'photos' ? 'Photos' : 'Videos'}
                                                </button>
                                            ))}
                                        </div>
                                    )}
                                </div>
                                
                                {/* Action Buttons: Multi-Select Mode toggle */}
                                <div className="flex items-center gap-2">
                                    {isMultiSelectMode ? (
                                        <>
                                            <button
                                                onClick={() => {
                                                    setIsMultiSelectMode(false);
                                                    setSelectedPhotos([]);
                                                }}
                                                className="bg-white border border-gold-200/60 text-taupe-600 text-xs font-semibold px-4 py-2.5 rounded-xl hover:bg-ivory-100 cursor-pointer transition-all shadow-xs"
                                            >
                                                Cancel Selection
                                            </button>
                                            {selectedPhotos.length > 0 && (
                                                <>
                                                    <button
                                                        onClick={handleBatchDownload}
                                                        className="bg-taupe-800 text-white text-xs font-semibold px-4 py-2.5 rounded-xl hover:bg-gold-650 cursor-pointer transition-all flex items-center gap-1.5 shadow-sm"
                                                    >
                                                        ⬇️ Download ({selectedPhotos.length})
                                                    </button>
                                                    {isAdmin && (
                                                        <button
                                                            onClick={handleBatchDelete}
                                                            className="bg-red-650 hover:bg-red-750 text-white text-xs font-semibold px-4 py-2.5 rounded-xl cursor-pointer transition-all flex items-center gap-1.5 shadow-sm"
                                                        >
                                                            {batchDeleting ? '⏳ Deleting…' : `🗑️ Delete (${selectedPhotos.length})`}
                                                        </button>
                                                    )}
                                                </>
                                            )}
                                        </>
                                    ) : (
                                        <button
                                            onClick={() => setIsMultiSelectMode(true)}
                                            className="bg-white border border-gold-200/60 text-taupe-700 text-xs font-semibold px-4 py-2.5 rounded-xl hover:bg-ivory-100 cursor-pointer transition-all flex items-center gap-1.5 shadow-xs"
                                        >
                                            ☑️ Select Photos
                                        </button>
                                    )}
                                </div>
                            </div>
                        )}

                        {selectedCluster && (() => {
                            const clusterObj = clusters.find(c => c.id === selectedCluster);
                            const name = clusterObj?.name || (selectedCluster.startsWith('guest_') ? 'Loading...' : `Person #${selectedCluster}`);
                            const thumbnail = clusterObj?.thumbnail_url ? withToken(`${API_BASE}${clusterObj.thumbnail_url}?cb=${clusterCacheBuster}`) : null;

                            return (
                                <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-6 mb-8 pb-6 border-b border-gold-200/50">
                                    <div className="flex items-center gap-4">
                                        <button
                                            onClick={() => {
                                                setSelectedCluster(null);
                                                setIsMultiSelectMode(false);
                                                setSelectedPhotos([]);
                                            }}
                                            className="bg-white border border-gold-200/60 text-taupe-700 text-xs font-semibold px-3.5 py-2 rounded-xl hover:bg-ivory-100 hover:border-gold-200 transition-all cursor-pointer shadow-xs"
                                        >
                                            ← Back
                                        </button>
                                        
                                        {/* Avatar with Change Photo option */}
                                        <div className="relative cursor-pointer w-20 h-20 sm:w-24 sm:h-24 flex-shrink-0">
                                            <div className="w-full h-full rounded-full overflow-hidden border border-gold-200/50 shadow-md">
                                                {thumbnail ? (
                                                    <img 
                                                        src={thumbnail} 
                                                        alt={name} 
                                                        className="w-full h-full object-cover"
                                                    />
                                                ) : (
                                                    <div className="w-full h-full bg-ivory-200 flex items-center justify-center text-taupe-400 text-2xl font-serif">
                                                        {name.charAt(0).toUpperCase()}
                                                    </div>
                                                )}
                                            </div>
                                            {/* Change Photo Badge */}
                                            <div 
                                                onClick={(e) => {
                                                    e.stopPropagation();
                                                    setShowChangePhotoModal(true);
                                                }}
                                                className="absolute bottom-0 right-0 w-7 h-7 bg-white hover:bg-ivory-100 border border-gold-200/60 rounded-full flex items-center justify-center shadow-md cursor-pointer hover:scale-105 transition-all text-xs z-10"
                                                title="Change profile picture"
                                            >
                                                📷
                                            </div>
                                        </div>

                                        {/* Name with Rename options */}
                                        <div className="flex flex-col gap-1.5">
                                            {isEditingName ? (
                                                <form 
                                                    onSubmit={(e) => {
                                                        e.preventDefault();
                                                        handleRenameClusterPage(newNameInput);
                                                    }}
                                                    className="flex items-center gap-2"
                                                >
                                                    <input 
                                                        type="text"
                                                        value={newNameInput}
                                                        onChange={(e) => setNewNameInput(e.target.value)}
                                                        className="font-serif text-taupe-900 text-base sm:text-lg border border-gold-200 rounded-xl px-3 py-1.5 bg-white focus:outline-none focus:border-gold-500 shadow-sm"
                                                        autoFocus
                                                    />
                                                    <button 
                                                        type="submit"
                                                        className="bg-taupe-800 text-white text-xs font-semibold px-3 py-2 rounded-xl hover:bg-gold-600 transition-all cursor-pointer shadow-sm"
                                                    >
                                                        Save
                                                    </button>
                                                    <button 
                                                        type="button"
                                                        onClick={() => setIsEditingName(false)}
                                                        className="bg-white border border-gold-200/60 text-taupe-500 text-xs px-3 py-2 rounded-xl hover:bg-ivory-100 cursor-pointer"
                                                    >
                                                        Cancel
                                                    </button>
                                                </form>
                                            ) : (
                                                <div className="flex items-center gap-2">
                                                    <h2 className="font-serif text-taupe-900 text-xl sm:text-2xl tracking-tight leading-none">
                                                        {name}
                                                    </h2>
                                                    <button
                                                        onClick={() => {
                                                            setIsEditingName(true);
                                                            setNewNameInput(name);
                                                        }}
                                                        className="text-taupe-400 hover:text-gold-600 transition-colors text-xs cursor-pointer bg-transparent border-0 p-1 flex items-center justify-center"
                                                        title="Rename person"
                                                    >
                                                        ✏️
                                                    </button>
                                                </div>
                                            )}
                                            <p className="text-taupe-400 text-xs font-medium">
                                                {filtered.length} moments found
                                            </p>
                                        </div>

                                    </div>

                                    {/* Action Buttons: Multi-Select Mode toggle */}
                                    <div className="flex items-center gap-2">
                                        {isMultiSelectMode ? (
                                            <>
                                                <button
                                                    onClick={() => {
                                                        setIsMultiSelectMode(false);
                                                        setSelectedPhotos([]);
                                                    }}
                                                    className="bg-white border border-gold-200/60 text-taupe-600 text-xs font-semibold px-4 py-2.5 rounded-xl hover:bg-ivory-100 cursor-pointer transition-all shadow-xs"
                                                >
                                                    Cancel Selection
                                                </button>
                                                {selectedPhotos.length > 0 && (
                                                    <>
                                                        <button
                                                            onClick={handleBatchDownload}
                                                            className="bg-taupe-800 text-white text-xs font-semibold px-4 py-2.5 rounded-xl hover:bg-gold-650 cursor-pointer transition-all flex items-center gap-1.5 shadow-sm"
                                                        >
                                                            ⬇️ Download ({selectedPhotos.length})
                                                        </button>
                                                        {isAdmin && (
                                                            <button
                                                                onClick={handleBatchDelete}
                                                                className="bg-red-650 hover:bg-red-750 text-white text-xs font-semibold px-4 py-2.5 rounded-xl cursor-pointer transition-all flex items-center gap-1.5 shadow-sm"
                                                            >
                                                                {batchDeleting ? '⏳ Deleting…' : `🗑️ Delete (${selectedPhotos.length})`}
                                                            </button>
                                                        )}
                                                    </>
                                                )}
                                            </>
                                        ) : (
                                            <button
                                                onClick={() => setIsMultiSelectMode(true)}
                                                className="bg-white border border-gold-200/60 text-taupe-700 text-xs font-semibold px-4 py-2.5 rounded-xl hover:bg-ivory-100 cursor-pointer transition-all flex items-center gap-1.5 shadow-xs"
                                            >
                                                ☑️ Select Photos
                                            </button>
                                        )}
                                    </div>
                                </div>
                            );
                        })()}

                        {selectedCategory && tab === 'categories' && (
                            <div 
                                className="space-y-6 mb-8"
                                onDragOver={(e) => e.preventDefault()}
                                onDrop={(e) => handleDrop(e, selectedCategory)}
                            >
                                <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-6 pb-4 border-b border-gold-200/50">
                                    <div className="flex items-center gap-4">
                                        <button
                                            onClick={() => {
                                                setSelectedCategory(null);
                                                setIsMultiSelectMode(false);
                                                setSelectedPhotos([]);
                                            }}
                                            className="bg-white border border-gold-200/60 text-taupe-700 text-xs font-semibold px-3.5 py-2 rounded-xl hover:bg-ivory-100 hover:border-gold-200 transition-all cursor-pointer shadow-xs"
                                        >
                                            ← Back
                                        </button>
                                        <div>
                                            <h2 className="font-serif text-taupe-900 text-xl tracking-tight leading-none">
                                                {selectedCategory}
                                            </h2>
                                            <p className="text-taupe-400 text-xs font-medium mt-1.5">
                                                {filtered.length} moments found
                                            </p>
                                        </div>
                                    </div>
                                    
                                    {/* Action Buttons: Multi-Select Mode toggle */}
                                    <div className="flex items-center gap-2">
                                        {isMultiSelectMode ? (
                                            <>
                                                <button
                                                    onClick={() => {
                                                        setIsMultiSelectMode(false);
                                                        setSelectedPhotos([]);
                                                    }}
                                                    className="bg-white border border-gold-200/60 text-taupe-600 text-xs font-semibold px-4 py-2.5 rounded-xl hover:bg-ivory-100 cursor-pointer transition-all shadow-xs"
                                                >
                                                    Cancel Selection
                                                </button>
                                                {selectedPhotos.length > 0 && (
                                                    <>
                                                        <button
                                                            onClick={handleBatchDownload}
                                                            className="bg-taupe-800 text-white text-xs font-semibold px-4 py-2.5 rounded-xl hover:bg-gold-650 cursor-pointer transition-all flex items-center gap-1.5 shadow-sm"
                                                        >
                                                            ⬇️ Download ({selectedPhotos.length})
                                                        </button>
                                                        {isAdmin && (
                                                            <button
                                                                onClick={handleBatchDelete}
                                                                className="bg-red-650 hover:bg-red-750 text-white text-xs font-semibold px-4 py-2.5 rounded-xl cursor-pointer transition-all flex items-center gap-1.5 shadow-sm"
                                                            >
                                                                {batchDeleting ? '⏳ Deleting…' : `🗑️ Delete (${selectedPhotos.length})`}
                                                            </button>
                                                        )}
                                                    </>
                                                )}
                                            </>
                                        ) : (
                                            <button
                                                onClick={() => setIsMultiSelectMode(true)}
                                                className="bg-white border border-gold-200/60 text-taupe-700 text-xs font-semibold px-4 py-2.5 rounded-xl hover:bg-ivory-100 cursor-pointer transition-all flex items-center gap-1.5 shadow-xs"
                                            >
                                                ☑️ Select Photos
                                            </button>
                                        )}
                                    </div>
                                </div>

                                {isAdmin && (
                                    <div className="relative border-2 border-dashed border-gold-200/60 hover:border-gold-450 rounded-2xl p-6 text-center bg-white/40 cursor-pointer transition duration-300">
                                        <input
                                            type="file"
                                            multiple
                                            onChange={(e) => handleFileUpload(e.target.files, selectedCategory)}
                                            className="absolute inset-0 opacity-0 cursor-pointer w-full h-full"
                                        />
                                        <div className="space-y-1.5 text-taupe-500">
                                            <p className="text-xs font-semibold">Drag & drop files/folders here or Click to select</p>
                                            <p className="text-[9px] text-taupe-400 uppercase tracking-wider">Supports photos & videos up to 100MB</p>
                                        </div>
                                    </div>
                                )}

                                {uploadingFiles.length > 0 && (
                                    <div className="bg-white border border-gold-200/80 rounded-2xl p-4 shadow-lg flex flex-col gap-2">
                                        <h4 className="text-xs font-bold text-taupe-500 uppercase tracking-wider">Uploading Files...</h4>
                                        <div className="max-h-32 overflow-y-auto space-y-1.5 font-mono text-[10px]">
                                            {uploadingFiles.map((file, idx) => (
                                                <div key={idx} className="flex justify-between items-center text-taupe-700">
                                                    <span className="truncate max-w-[70%]">{file.name}</span>
                                                    <span className={`font-semibold uppercase tracking-wider ${
                                                        file.status === 'success' ? 'text-green-600' :
                                                        file.status === 'error' ? 'text-red-500' :
                                                        file.status === 'uploading' ? 'text-gold-650 animate-pulse' : 'text-stone-450'
                                                    }`}>
                                                        {file.status}
                                                    </span>
                                                </div>
                                            ))}
                                        </div>
                                    </div>
                                )}
                            </div>
                        )}

                        {/* Family Navigation Pill Bar */}
                        {familyMembers.length > 0 && tab === 'mine' && !selectedCluster && !selectedCategory && (
                            <div className="mb-6 p-4 bg-white/40 backdrop-blur-md border border-gold-200/40 rounded-2xl shadow-sm">
                                <h3 className="text-[10px] font-bold text-taupe-400 uppercase tracking-widest mb-3 px-1 flex items-center gap-1.5">
                                    <span>👨‍👩‍👧‍👦</span> Household Members
                                </h3>
                                <div className="flex gap-2.5 overflow-x-auto py-1 scrollbar-none snap-x snap-mandatory">
                                    {/* Master Family Album Pill */}
                                    <button
                                        onClick={() => setActiveFamilyMemberId(null)}
                                        className={`snap-start flex items-center gap-2 px-4 py-2.5 rounded-xl text-xs font-semibold tracking-wide transition-all duration-300 shadow-xs cursor-pointer ${
                                            activeFamilyMemberId === null
                                                ? 'bg-taupe-800 text-white shadow-md shadow-taupe-900/10'
                                                : 'bg-white/70 text-taupe-600 hover:bg-white hover:text-taupe-900 border border-gold-200/40'
                                        }`}
                                    >
                                        <span className="text-sm">👨‍👩‍👧‍👦</span>
                                        <span>Family Album</span>
                                    </button>

                                    {/* Individual Member Pills */}
                                    {familyMembers.map((member) => {
                                        const isActive = activeFamilyMemberId === member.id;
                                        return (
                                            <button
                                                key={member.id}
                                                onClick={() => setActiveFamilyMemberId(member.id)}
                                                className={`snap-start flex items-center gap-2.5 px-3.5 py-2 rounded-xl text-xs font-semibold tracking-wide transition-all duration-300 shadow-xs cursor-pointer ${
                                                    isActive
                                                        ? 'bg-gold-500 text-white shadow-md shadow-gold-500/15'
                                                        : 'bg-white/70 text-taupe-600 hover:bg-white hover:text-taupe-900 border border-gold-200/40'
                                                }`}
                                            >
                                                <div className="w-5 h-5 rounded-full overflow-hidden border border-gold-200/60 bg-ivory-200 flex-shrink-0 flex items-center justify-center">
                                                    <img
                                                        src={withToken(`${API_BASE}/faces/members/${member.id}/selfie`)}
                                                        alt={member.name}
                                                        className="w-full h-full object-cover"
                                                        onError={(e) => {
                                                            e.target.style.display = 'none';
                                                        }}
                                                    />
                                                </div>
                                                <span>{member.name}</span>
                                            </button>
                                        );
                                    })}
                                </div>
                            </div>
                        )}
                        
                        {(loadingClusterPhotos || loadingCategoryPhotos) ? (
                            <div className="grid grid-cols-2 md:grid-cols-3 gap-3 md:gap-4">
                                {[...Array(6)].map((_, i) => (
                                    <div key={i} className="aspect-square bg-gold-100/50 border border-gold-100/50 rounded-2xl animate-pulse" />
                                ))}
                            </div>
                        ) : loading && photos.length === 0 ? (
                            /* Elegant Shimmer Loading Grid */
                            <div className="grid grid-cols-2 md:grid-cols-3 gap-3 md:gap-4">
                                {[...Array(9)].map((_, i) => (
                                    <div key={i} className="aspect-square bg-gold-100/50 border border-gold-100/50 rounded-2xl animate-pulse" />
                                ))}
                            </div>
                        ) : (
                            <>
                                <div className="grid grid-cols-2 md:grid-cols-3 gap-3 md:gap-4">
                                    {filtered.map((photo, i) => {
                                        const isSelected = selectedPhotos.includes(photo.drive_id);
                                        return (
                                            <GalleryPhotoCard
                                                key={photo.drive_id}
                                                photo={photo}
                                                index={i}
                                                isSelected={isSelected}
                                                isMultiSelectMode={isMultiSelectMode}
                                                togglePhotoSelection={togglePhotoSelection}
                                                setLightboxIndex={setLightboxIndex}
                                                downloadSinglePhoto={downloadSinglePhoto}
                                                downloadingPhoto={downloadingPhoto}
                                                API_BASE={API_BASE}
                                            />
                                        );
                                    })}
                                </div>

                                {/* Scroll to Load More Indicator */}
                                {hasMore && !loading && (
                                    <div className="flex justify-center items-center gap-1.5 mt-8 text-[10px] font-bold text-taupe-400 uppercase tracking-widest animate-pulse">
                                        <span>↓</span> Scroll for more photos
                                    </div>
                                )}

                                {/* Infinite Scroll Loader */}
                                {loading && (
                                    <div className="flex justify-center mt-8">
                                        <div className="w-8 h-8 border-3 border-gold-300/20 border-t-gold-550 rounded-full animate-spin"></div>
                                    </div>
                                )}

                                {/* Empty States */}
                                {filtered.length === 0 && !loading && (
                                    <div className="text-center py-20 flex flex-col items-center gap-4 animate-fade-in-up">
                                        <div className="text-4xl">🌾</div>
                                        <h3 className="font-serif text-taupe-800 text-lg">No moments found here</h3>
                                        <p className="text-taupe-400 font-light text-sm max-w-xs leading-relaxed">
                                            {tab === 'mine'
                                                ? "We haven't matched any photos to you yet. Have a look at Highlights for the shared moments."
                                                : tab === 'highlights'
                                                ? "No shared highlights here yet."
                                                : "We haven't found any photos of you yet."
                                            }
                                        </p>
                                        {total === 0 && (
                                            // Nothing for a guest to retry — matching happens
                                            // during preprocessing, not on demand. Point them
                                            // at someone who can actually fix it.
                                            <p className="mt-2 text-taupe-400 text-xs">
                                                If you think some are missing, let the couple know
                                                and they can add you.
                                            </p>
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
                    className="fixed inset-0 z-50 bg-taupe-900/95 backdrop-blur-sm flex items-center justify-center p-4 md:p-10"
                    onClick={() => setLightboxIndex(null)}
                >
                    {/* Close Button */}
                    <button
                        onClick={() => setLightboxIndex(null)}
                        className="absolute top-4 right-4 z-50 w-10 h-10 rounded-full bg-white/10 text-white hover:bg-white/20 flex items-center justify-center transition-all duration-300 cursor-pointer text-lg font-light"
                    >
                        ✕
                    </button>

                    {/* Media badge — moved to the top-left corner so it no longer sits on
                        top of the people chips the way the old bottom caption did. */}
                    <div
                        onClick={(e) => e.stopPropagation()}
                        className="absolute top-4 left-4 z-50 flex items-center gap-2 bg-black/35 backdrop-blur-md border border-white/10 rounded-full pl-3 pr-3.5 py-1.5 text-xs text-white/70"
                    >
                        <span className="w-1.5 h-1.5 rounded-full bg-gold-400"></span>
                        <span className="font-semibold text-white/90">
                            {activePhoto.is_video ? 'Video' : activePhoto.is_common ? 'Group' : 'Personal'}
                        </span>
                        <span className="text-white/40">·</span>
                        <span className="tabular-nums text-white/60">{lightboxIndex + 1} / {filtered.length}</span>
                    </div>

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
                        {/* Centered loading indicator — the preview and its controls
                            only appear once fully loaded, so nothing else is on screen. */}
                        {mediaLoading && (
                            <div className="absolute inset-0 z-20 flex items-center justify-center">
                                <div className="flex items-center gap-2 bg-taupe-900/55 text-white/90 text-[11px] font-medium px-3.5 py-1.5 rounded-full backdrop-blur-sm">
                                    <div className="w-3.5 h-3.5 border-2 border-white/30 border-t-white rounded-full animate-spin"></div>
                                    {activePhoto.is_video ? 'Loading video…' : 'Loading full quality…'}
                                </div>
                            </div>
                        )}

                        {activePhoto.is_video ? (
                            <video
                                src={withToken(`${API_BASE}/photos/stream/${activePhoto.drive_id}`)}
                                controls
                                autoPlay
                                onLoadedData={() => setMediaLoading(false)}
                                onError={(e) => console.error("Video load error:", e)}
                                className="object-contain max-w-full max-h-[75vh] md:max-h-[80vh] rounded-lg shadow-2xl"
                            />
                        ) : (
                            // Screen-sized preview at full lightbox size. It stays invisible
                            // (opacity 0) and hidden behind the loader until it has fully
                            // loaded, then fades in — so there's no blurred placeholder or
                            // halo, just a clean load. Falls back to the thumbnail if the
                            // preview can't be produced.
                            <img
                                key={activePhoto.drive_id}
                                src={withToken(`${API_BASE}/photos/preview/${activePhoto.drive_id}`)}
                                alt=""
                                onLoad={(e) => { e.currentTarget.style.opacity = 1; setMediaLoading(false) }}
                                onError={(e) => { e.currentTarget.onerror = null; e.currentTarget.src = withToken(`${API_BASE}${activePhoto.thumb_url}`); e.currentTarget.style.opacity = 1; setMediaLoading(false) }}
                                style={{ opacity: 0, transition: 'opacity 0.35s ease' }}
                                className="object-contain max-w-full max-h-[82vh] rounded-lg shadow-2xl"
                            />
                        )}
                        
                        {/* Bottom dock — people and actions in ONE anchored panel. The old
                            layout floated the controls bottom-right while the people chips
                            ran in-flow underneath them, so on a big group photo the two
                            overlapped. Here people sit on top, actions below a hairline, and
                            the media badge has moved to the top-left of the overlay. */}
                        {!mediaLoading && (
                        <div
                            onClick={(e) => e.stopPropagation()}
                            className="fixed bottom-0 left-0 right-0 z-50 px-3 sm:px-5 pt-8 pb-4 bg-gradient-to-t from-taupe-900/96 via-taupe-900/85 to-transparent"
                        >
                          <div className="max-w-5xl mx-auto flex flex-col gap-2.5 text-white">

                            {/* People row */}
                            {(loadingPeople || photoPeople.length > 0 || isAdmin) && (
                            <div className="relative">
                                <p className="text-[9px] uppercase tracking-widest text-white/30 font-semibold mb-2">
                                    People in this photo
                                    {isAdmin && <span className="text-white/20 normal-case tracking-normal"> · tap ✕ to correct</span>}
                                </p>
                                {/* One horizontally-scrolling row so a big group photo's chips
                                    stay on a single tidy line instead of wrapping into the dock. */}
                                <div className="flex flex-nowrap gap-2 items-center overflow-x-auto pb-1 [scrollbar-width:thin]">
                                    {loadingPeople ? (
                                        [...Array(3)].map((_, i) => (
                                            <div key={i} className="flex items-center gap-1.5 bg-white/5 rounded-full px-3 py-1.5 animate-pulse shrink-0">
                                                <div className="w-6 h-6 rounded-full bg-white/10" />
                                                <div className="w-14 h-2.5 bg-white/10 rounded" />
                                            </div>
                                        ))
                                    ) : (
                                        <>
                                            {photoPeople.map(person => (
                                                <div
                                                    key={person.id}
                                                    className={`flex items-center gap-1.5 bg-white/8 hover:bg-white/15 border border-white/10 hover:border-gold-400/40 rounded-full pl-1.5 ${isAdmin ? 'pr-1.5' : 'pr-3'} py-1 shrink-0 transition-all duration-200 group ${peopleBusyId === person.id ? 'opacity-50' : ''}`}
                                                >
                                                    <button
                                                        onClick={() => {
                                                            setLightboxIndex(null)
                                                            setTab('people')
                                                            handleClusterClick(person.id)
                                                        }}
                                                        className="flex items-center gap-1.5 cursor-pointer"
                                                    >
                                                        <FaceAvatar name={person.name} thumbnailUrl={person.thumbnail_url} size={26} />
                                                        <span className="text-[11px] font-semibold text-white/80 group-hover:text-gold-300 transition-colors whitespace-nowrap">
                                                            {person.name}
                                                        </span>
                                                    </button>
                                                    {isAdmin && (
                                                        <button
                                                            onClick={() => handleRemovePersonFromPhoto(person.id)}
                                                            disabled={peopleBusyId === person.id}
                                                            title="Not in this photo — remove"
                                                            className="w-5 h-5 flex items-center justify-center rounded-full bg-white/10 hover:bg-red-500/80 text-white/60 hover:text-white text-xs leading-none transition-colors cursor-pointer"
                                                        >
                                                            {peopleBusyId === person.id ? (
                                                                <span className="inline-block w-2.5 h-2.5 border border-white/50 border-t-transparent rounded-full animate-spin" />
                                                            ) : '×'}
                                                        </button>
                                                    )}
                                                </div>
                                            ))}
                                            {!loadingPeople && photoPeople.length === 0 && !isAdmin && null}
                                            {isAdmin && (
                                                <button
                                                    onClick={openAddPerson}
                                                    className="flex items-center gap-1 bg-gold-500/15 hover:bg-gold-500/30 border border-gold-400/30 hover:border-gold-400/60 text-gold-200 rounded-full px-3 py-1.5 text-[11px] font-semibold transition-all duration-200 cursor-pointer whitespace-nowrap shrink-0"
                                                >
                                                    <span className="text-sm leading-none">＋</span> Add person
                                                </button>
                                            )}
                                        </>
                                    )}
                                </div>

                                {/* Admin add-person picker — floats ABOVE the row (the row sits
                                    at the bottom edge of the screen, so a dropdown would be off-screen). */}
                                {isAdmin && showAddPerson && (
                                    <div className="absolute bottom-full left-2 mb-2 z-10 w-72 bg-taupe-900/95 backdrop-blur-md border border-white/15 rounded-xl p-2 shadow-2xl">
                                        <input
                                            autoFocus
                                            value={addPersonQuery}
                                            onChange={e => setAddPersonQuery(e.target.value)}
                                            placeholder="Search people to add…"
                                            className="w-full bg-white/10 text-white text-xs rounded-lg px-3 py-2 mb-2 outline-none placeholder:text-white/30 focus:bg-white/15"
                                        />
                                        <div className="max-h-52 overflow-y-auto flex flex-col gap-0.5">
                                            {(() => {
                                                const shownNames = new Set(photoPeople.map(p => (p.name || '').toLowerCase()))
                                                const q = addPersonQuery.trim().toLowerCase()
                                                // Recognized faces first…
                                                const clusterOpts = clusters
                                                    .filter(c => c.name && !c.name.startsWith('Person #'))
                                                    .map(c => ({ id: c.id, name: c.name, thumbnail_url: c.thumbnail_url, is_guest: c.is_guest, count: c.count }))
                                                const clusterNames = new Set(clusterOpts.map(c => c.name.toLowerCase()))
                                                // …then any registered guest not already covered by a face —
                                                // this is what the old "Assign" button reached (undetected faces).
                                                const guestOpts = guestsList
                                                    .filter(g => g.name && !clusterNames.has(g.name.toLowerCase()))
                                                    .map(g => ({ id: `guest_${g.id}`, name: g.name, thumbnail_url: `/faces/guests/${g.id}/selfie`, is_guest: true }))
                                                const options = [...clusterOpts, ...guestOpts]
                                                    .filter(o => !shownNames.has(o.name.toLowerCase()))
                                                    .filter(o => !q || o.name.toLowerCase().includes(q))
                                                    .sort((a, b) => a.name.localeCompare(b.name))
                                                    .slice(0, 40)
                                                // Nothing loaded yet — show a spinner only when there is
                                                // genuinely nothing to show, so a fast guest list isn't
                                                // held back by the slower recognized-faces call.
                                                if (options.length === 0) {
                                                    return (loadingClusters || guestsList.length === 0)
                                                        ? <div className="flex items-center justify-center gap-2 px-2 py-4 text-white/40 text-xs"><span className="inline-block w-3 h-3 border border-white/40 border-t-transparent rounded-full animate-spin" />Loading people…</div>
                                                        : <p className="text-white/40 text-xs px-2 py-3 text-center">No matches</p>
                                                }
                                                return (
                                                    <>
                                                        {options.map(c => (
                                                            <button
                                                                key={c.id}
                                                                onClick={() => handleAddPersonToPhoto(c)}
                                                                disabled={peopleBusyId === c.id}
                                                                className="flex items-center gap-2 px-2 py-1.5 rounded-lg hover:bg-white/10 text-left transition-colors cursor-pointer disabled:opacity-50"
                                                            >
                                                                <FaceAvatar name={c.name} thumbnailUrl={c.thumbnail_url} size={28} />
                                                                <span className="text-white/85 text-xs font-medium truncate">{c.name}</span>
                                                                {peopleBusyId === c.id
                                                                    ? <span className="ml-auto shrink-0 inline-block w-3 h-3 border border-white/40 border-t-transparent rounded-full animate-spin" />
                                                                    : typeof c.count === 'number'
                                                                        ? <span className="text-white/30 text-[10px] ml-auto shrink-0">{c.count}</span>
                                                                        : <span className="text-white/25 text-[9px] ml-auto shrink-0 uppercase tracking-wide">guest</span>}
                                                            </button>
                                                        ))}
                                                        {loadingClusters && (
                                                            <div className="flex items-center gap-1.5 px-2 py-1.5 text-white/25 text-[10px]">
                                                                <span className="inline-block w-2.5 h-2.5 border border-white/30 border-t-transparent rounded-full animate-spin" />
                                                                finding recognized faces…
                                                            </div>
                                                        )}
                                                    </>
                                                )
                                            })()}
                                        </div>
                                        <button
                                            onClick={() => { setShowAddPerson(false); setAddPersonQuery('') }}
                                            className="w-full mt-1.5 text-white/40 hover:text-white/70 text-[11px] py-1 transition-colors cursor-pointer"
                                        >
                                            Close
                                        </button>
                                    </div>
                                )}
                            </div>
                            )}

                            {/* Action bar — edit actions on the left, primary Download on the right. */}
                            <div className="flex items-center justify-between gap-2 flex-wrap pt-2.5 border-t border-white/10">
                                <div className="flex items-center gap-2 flex-wrap">
                                    {isAdmin && (
                                        <>
                                            {/* "Assign" was retired — adding someone to a photo now
                                                happens through "People in this photo · ＋ Add person"
                                                above, which also fixes the recognition and is reversible. */}
                                            {activePhoto.is_common ? (
                                                <button
                                                    onClick={() => handleRemoveFromGroup(activePhoto.drive_id)}
                                                    title="Takes it out of Highlights / everyone's album — the photo is kept"
                                                    className="bg-taupe-800/90 hover:bg-stone-750 text-white px-4 py-2.5 rounded-xl text-xs font-semibold transition-all duration-300 flex items-center gap-1.5 cursor-pointer shadow-lg border border-white/10"
                                                >
                                                    👥 Not a group photo
                                                </button>
                                            ) : (
                                                <button
                                                    onClick={() => handleMarkCommon(activePhoto.drive_id)}
                                                    title="Adds it to Highlights and every guest's download"
                                                    className="bg-gold-600/90 hover:bg-gold-600 text-white px-4 py-2.5 rounded-xl text-xs font-semibold transition-all duration-300 flex items-center gap-1.5 cursor-pointer shadow-lg border border-white/10"
                                                >
                                                    ✨ Add to everyone's album
                                                </button>
                                            )}
                                            {selectedCategory && (
                                                <button
                                                    onClick={() => handleRemoveFromAlbum(activePhoto.drive_id, selectedCategory)}
                                                    title={`Remove from the "${selectedCategory}" album — the photo is kept`}
                                                    className="bg-taupe-800/90 hover:bg-stone-750 text-white px-4 py-2.5 rounded-xl text-xs font-semibold transition-all duration-300 flex items-center gap-1.5 cursor-pointer shadow-lg border border-white/10"
                                                >
                                                    🗂️ Remove from album
                                                </button>
                                            )}
                                            <button
                                                onClick={() => handleDeletePhoto(activePhoto.drive_id)}
                                                disabled={deletingPhoto === activePhoto.drive_id}
                                                className="bg-red-650 hover:bg-red-750 text-white px-4 py-2.5 rounded-xl text-xs font-semibold transition-all duration-300 flex items-center gap-1.5 cursor-pointer shadow-lg disabled:opacity-70 disabled:cursor-wait"
                                            >
                                                {deletingPhoto === activePhoto.drive_id ? (
                                                    <>
                                                        <span className="w-3.5 h-3.5 border-2 border-white/40 border-t-white rounded-full animate-spin"></span>
                                                        Deleting…
                                                    </>
                                                ) : (
                                                    <>🗑️ Delete</>
                                                )}
                                            </button>
                                        </>
                                    )}
                                </div>
                                <div className="flex items-center gap-2 flex-wrap justify-end">
                                    {/* "Not Me" removes a photo from the viewer's OWN album, so it
                                        only makes sense on personal feeds. All Moments is the whole
                                        wedding (and an admin view), so hide it there. */}
                                    {guestId && !activePhoto.is_common && tab !== 'all' && (
                                        <button
                                            onClick={() => handleNotMePhoto(activePhoto.drive_id)}
                                            className="bg-taupe-800/90 hover:bg-red-950/85 hover:text-red-200 text-taupe-300 border border-taupe-800 hover:border-red-900/60 px-4 py-2.5 rounded-xl text-xs font-semibold transition-all duration-300 flex items-center gap-1.5 cursor-pointer shadow-lg active:scale-95 group/notme"
                                        >
                                            <span className="transition-transform duration-200 group-hover/notme:scale-110">🙅‍♂️</span> Not Me
                                        </button>
                                    )}
                                    <button
                                        onClick={() => downloadSinglePhoto(activePhoto)}
                                        className="bg-white text-taupe-900 px-4 py-2.5 rounded-xl text-xs font-semibold hover:bg-gold-500 hover:text-white transition-all duration-300 flex items-center gap-1.5 cursor-pointer shadow-lg"
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
                        </div>
                        )}
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


            {/* CREATE ALBUM MODAL */}
            {showCreateCategoryModal && (
                <div className="fixed inset-0 z-55 bg-taupe-900/60 backdrop-blur-xs flex items-center justify-center p-4">
                    <div className="bg-white rounded-2xl max-w-md w-full overflow-hidden shadow-2xl border border-gold-200/80 animate-fade-in-up">
                        <div className="px-6 py-4 border-b border-stone-150 flex items-center justify-between">
                            <div>
                                <h3 className="font-serif text-lg text-taupe-900 leading-none mb-1">Create New Album</h3>
                                <p className="text-taupe-400 text-[11px] leading-tight">Organize special wedding moments into a custom collection.</p>
                            </div>
                            <button
                                onClick={() => setShowCreateCategoryModal(false)}
                                className="text-taupe-400 hover:text-stone-705 font-bold text-xl cursor-pointer"
                            >
                                &times;
                            </button>
                        </div>

                        <form onSubmit={handleCreateCategorySubmit} className="p-6 space-y-4">
                            <div>
                                <label className="block text-[10px] font-bold text-taupe-500 uppercase tracking-widest mb-1.5">Album Name</label>
                                <input
                                    type="text"
                                    placeholder="e.g. Drone Shots, Venue Decor, Reception"
                                    value={newCategoryName}
                                    onChange={(e) => setNewCategoryName(e.target.value)}
                                    className="w-full px-4 py-2.5 rounded-xl border border-gold-200/60 focus:outline-none focus:border-taupe-400 text-stone-855 text-sm font-medium"
                                    required
                                    autoFocus
                                />
                            </div>

                            <div className="flex gap-3 pt-2">
                                <button
                                    type="button"
                                    onClick={() => setShowCreateCategoryModal(false)}
                                    className="flex-1 border border-gold-200/60 text-taupe-600 font-semibold py-2.5 rounded-xl hover:bg-ivory-100 transition cursor-pointer text-xs uppercase tracking-wider text-center"
                                >
                                    Cancel
                                </button>
                                <button
                                    type="submit"
                                    className="flex-2 bg-taupe-800 text-white font-semibold py-2.5 rounded-xl hover:bg-taupe-800 transition duration-300 cursor-pointer text-xs uppercase tracking-wider"
                                >
                                    Create Album
                                </button>
                            </div>
                        </form>
                    </div>
                </div>
            )}

            {/* CHOOSE PROFILE PICTURE MODAL */}
            {showChangePhotoModal && (
                <div 
                    className="fixed inset-0 z-55 bg-taupe-900/80 backdrop-blur-sm flex items-center justify-center p-4"
                    onClick={() => setShowChangePhotoModal(false)}
                >
                    <div 
                        className="bg-white rounded-3xl w-full max-w-2xl overflow-hidden shadow-2xl flex flex-col max-h-[85vh] animate-fade-in-up"
                        onClick={(e) => e.stopPropagation()}
                    >
                        {/* Header */}
                        <div className="p-6 border-b border-gold-100 flex justify-between items-center bg-ivory-100">
                            <div>
                                <h3 className="font-serif text-taupe-900 text-lg font-semibold">Choose Profile Picture</h3>
                                <p className="text-taupe-400 text-xs mt-0.5">Select a photo below or upload from your device.</p>
                            </div>
                            <div className="flex items-center gap-3">
                                <label className="bg-taupe-800 text-white hover:bg-gold-600 transition-all text-xs font-semibold px-4 py-2.5 rounded-xl cursor-pointer shadow-sm flex items-center gap-1.5">
                                    📤 Upload Photo
                                    <input 
                                        type="file" 
                                        accept="image/*" 
                                        className="hidden" 
                                        onChange={handleManualAvatarUpload}
                                    />
                                </label>
                                <button 
                                    onClick={() => setShowChangePhotoModal(false)}
                                    className="w-8 h-8 rounded-full bg-gold-100/60 text-taupe-600 hover:bg-gold-100 flex items-center justify-center text-sm transition-all"
                                >
                                    ✕
                                </button>
                            </div>
                        </div>


                        {/* Photo Grid */}
                        <div className="p-6 overflow-y-auto flex-1">
                            {filtered.length === 0 ? (
                                <p className="text-taupe-400 text-center text-sm py-12">No photos available.</p>
                            ) : (
                                <div className="grid grid-cols-3 sm:grid-cols-4 gap-3">
                                    {filtered.filter(p => !p.is_video).map((photo, i) => (
                                        <div 
                                            key={i}
                                            onClick={() => handleSetProfilePic(photo.drive_id)}
                                            className="aspect-square relative group overflow-hidden bg-ivory-200 border border-gold-200/50 rounded-xl cursor-pointer hover:border-gold-500 hover:shadow transition-all duration-300"
                                        >
                                            <img 
                                                src={withToken(`${API_BASE}${photo.thumb_url}`)} 
                                                alt=""
                                                className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-550"
                                            />
                                        </div>
                                    ))}
                                </div>
                            )}
                        </div>
                    </div>
                </div>
            )}


            {/* The "Share with Guest" modal was retired along with the Assign button:
                "People in this photo · ＋ Add person" now covers assigning a photo to
                anyone (recognized face or plain guest) in one place. */}

            {/* FLOATING SELECTION CONTROLS AND BATCH TOOLBAR */}
            {/* Floating circular Select Mode FAB — hidden while the lightbox is open
                so it doesn't overlap the preview's bottom-right controls. */}
            {!isMultiSelectMode && lightboxIndex === null && (tab === 'all' || tab === 'mine' || tab === 'common' || selectedCluster || selectedCategory) && (
                <button
                    onClick={() => setIsMultiSelectMode(true)}
                    className="fixed bottom-6 right-6 bg-taupe-800 text-white shadow-xl hover:shadow-2xl rounded-full w-14 h-14 flex items-center justify-center cursor-pointer transition-all duration-300 hover:scale-110 active:scale-95 group hover:bg-gold-550"
                    style={{ zIndex: 9999 }}
                    title="Select multiple photos"
                >
                    <span className="text-xl group-hover:scale-110 transition-transform">☑️</span>
                </button>
            )}

            {/* Floating Glassmorphic Batch Actions Toolbar */}
            {isMultiSelectMode && (
                <div 
                    className="fixed bottom-6 left-0 right-0 flex justify-center px-4" 
                    style={{ zIndex: 9999 }}
                >
                    <div className="bg-taupe-800/95 backdrop-blur-md border border-white/10 px-5 py-3.5 rounded-2xl shadow-2xl flex items-center gap-5 text-white animate-fade-in-up max-w-[95vw] sm:max-w-md">
                        <div className="flex flex-col">
                            <span className="text-[10px] font-bold text-gold-400 tracking-wider uppercase">Select Mode</span>
                            <span className="text-xs text-white/90 font-medium mt-0.5 whitespace-nowrap">{selectedPhotos.length} selected</span>
                        </div>
                        <div className="h-8 w-[1px] bg-white/10" />
                        <div className="flex items-center gap-2">
                            <button
                                onClick={() => {
                                    setIsMultiSelectMode(false);
                                    setSelectedPhotos([]);
                                }}
                                className="bg-white/10 hover:bg-white/15 text-white text-xs font-semibold px-3 py-2 rounded-xl transition-all cursor-pointer whitespace-nowrap"
                            >
                                Cancel
                            </button>
                            {selectedPhotos.length > 0 && (
                                <>
                                    <button
                                        onClick={handleBatchDownload}
                                        className="bg-gold-500 hover:bg-gold-600 text-taupe-900 text-xs font-bold px-3 py-2 rounded-xl transition-all flex items-center gap-1 cursor-pointer shadow-md whitespace-nowrap"
                                    >
                                        ⬇️ Download
                                    </button>
                                    {isAdmin && (
                                        <button
                                            onClick={handleBatchMarkCommon}
                                            title="Add the selected photos to Highlights and every guest's download"
                                            className="bg-gold-600 hover:bg-gold-700 text-white text-xs font-bold px-3 py-2 rounded-xl transition-all flex items-center gap-1 cursor-pointer shadow-md whitespace-nowrap"
                                        >
                                            ✨ Everyone's album
                                        </button>
                                    )}
                                    {isAdmin && (
                                        <button
                                            onClick={handleBatchDelete}
                                            disabled={batchDeleting}
                                            className="bg-red-650 hover:bg-red-750 text-white text-xs font-bold px-3 py-2 rounded-xl transition-all flex items-center gap-1 cursor-pointer shadow-md whitespace-nowrap disabled:opacity-70 disabled:cursor-wait"
                                        >
                                            {batchDeleting ? '⏳ Deleting…' : '🗑️ Delete'}
                                        </button>
                                    )}
                                </>
                            )}
                        </div>
                    </div>
                </div>
            )}

            {/* Confirm modal — replaces window.confirm for destructive actions. */}
            {confirmDialog && (
                <div
                    className="fixed inset-0 z-[60] bg-taupe-900/60 backdrop-blur-sm flex items-center justify-center p-4 animate-fade-in-up"
                    onClick={() => { if (!confirmBusy) setConfirmDialog(null) }}
                >
                    <div
                        onClick={e => e.stopPropagation()}
                        className="bg-white rounded-2xl max-w-sm w-full shadow-2xl border border-gold-200/70 overflow-hidden"
                    >
                        <div className="p-6">
                            <div className="flex items-start gap-3">
                                <div className={`w-10 h-10 shrink-0 rounded-full flex items-center justify-center text-lg ${confirmDialog.danger ? 'bg-red-50 text-red-600' : 'bg-gold-50 text-gold-600'}`}>
                                    {confirmDialog.danger ? '🗑️' : '❓'}
                                </div>
                                <div className="min-w-0">
                                    <h3 className="font-serif text-lg text-taupe-900 leading-snug">{confirmDialog.title}</h3>
                                    {confirmDialog.message && (
                                        <p className="text-taupe-500 text-[13px] leading-relaxed mt-1.5">{confirmDialog.message}</p>
                                    )}
                                </div>
                            </div>
                        </div>
                        <div className="px-6 py-4 bg-ivory-100/50 border-t border-stone-150 flex items-center justify-end gap-2.5">
                            <button
                                onClick={() => setConfirmDialog(null)}
                                className="text-xs font-semibold text-taupe-600 px-4 py-2.5 rounded-xl hover:bg-white cursor-pointer transition"
                            >
                                {confirmDialog.cancelLabel || 'Cancel'}
                            </button>
                            <button
                                onClick={runConfirm}
                                style={confirmDialog.danger ? { backgroundColor: '#c0392b' } : { backgroundColor: '#4a4038' }}
                                className="text-xs font-semibold text-white px-5 py-2.5 rounded-xl cursor-pointer transition hover:opacity-90"
                            >
                                {confirmDialog.confirmLabel || 'Confirm'}
                            </button>
                        </div>
                    </div>
                </div>
            )}

            {/* Toast stack — transient success/error feedback. */}
            {toasts.length > 0 && (
                <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-[70] flex flex-col items-center gap-2 pointer-events-none">
                    {toasts.map(t => (
                        <div
                            key={t.id}
                            className={`pointer-events-auto flex items-center gap-2.5 px-4 py-2.5 rounded-xl shadow-2xl border text-sm font-medium animate-fade-in-up ${
                                t.type === 'error'
                                    ? 'bg-red-650 border-red-750 text-white'
                                    : t.type === 'info'
                                        ? 'bg-taupe-800 border-taupe-700 text-white'
                                        : 'bg-emerald-600 border-emerald-700 text-white'
                            }`}
                        >
                            <span className="text-base leading-none">{t.type === 'error' ? '⚠️' : t.type === 'info' ? 'ℹ️' : '✓'}</span>
                            {t.msg}
                        </div>
                    ))}
                </div>
            )}
        </>
    )
}