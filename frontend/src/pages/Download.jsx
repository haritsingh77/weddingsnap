import { useState, useEffect, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { prepareDownload, getDownloadStatus, getStreamUrl } from '../services/api'

export default function Download() {
    const navigate = useNavigate()
    const guestId = localStorage.getItem('guest_id')
    const guestName = localStorage.getItem('guest_name')

    const [status, setStatus] = useState('idle')   // idle | preparing | ready | failed
    const [sessionId, setSessionId] = useState(null)
    const [photoCount, setPhotoCount] = useState(0)
    const [currentStep, setCurrentStep] = useState(0)
    
    const intervalRef = useRef(null)

    // Polling step statements to show progress
    const progressSteps = [
        'Connecting to secure photo server...',
        'Downloading your matched images from Google Drive...',
        'Filtering group shots & personal portraits...',
        'Zipping all wedding memories into a high-res package...',
        'Almost ready, finalizing your download...'
    ]

    // Cycle through progress messages
    useEffect(() => {
        let stepInterval
        if (status === 'preparing') {
            setCurrentStep(0)
            stepInterval = setInterval(() => {
                setCurrentStep(prev => (prev < progressSteps.length - 1 ? prev + 1 : prev))
            }, 6000)
        }
        return () => {
            if (stepInterval) clearInterval(stepInterval)
        }
    }, [status])

    // Cleanup polling interval on unmount
    useEffect(() => {
        return () => {
            if (intervalRef.current) {
                clearInterval(intervalRef.current)
            }
        }
    }, [])

    const start = async () => {
        setStatus('preparing')
        try {
            const res = await prepareDownload(guestId)
            const sid = res.data.session_id
            setSessionId(sid)
            poll(sid)
        } catch (err) {
            console.error('Failed to prepare download:', err)
            setStatus('failed')
        }
    }

    const poll = (sid) => {
        if (intervalRef.current) clearInterval(intervalRef.current)

        intervalRef.current = setInterval(async () => {
            try {
                const res = await getDownloadStatus(sid)
                const { status: s, photo_count } = res.data
                if (s === 'ready') {
                    setPhotoCount(photo_count)
                    setStatus('ready')
                    if (intervalRef.current) {
                        clearInterval(intervalRef.current)
                        intervalRef.current = null
                    }
                } else if (s === 'failed') {
                    setStatus('failed')
                    if (intervalRef.current) {
                        clearInterval(intervalRef.current)
                        intervalRef.current = null
                    }
                }
            } catch (err) {
                console.error('Download status check failed:', err)
                setStatus('failed')
                if (intervalRef.current) {
                    clearInterval(intervalRef.current)
                    intervalRef.current = null
                }
            }
        }, 4000)
    }

    const downloadZip = () => {
        window.location.href = getStreamUrl(guestId, sessionId)
    }

    // Navigation Guard
    useEffect(() => {
        if (!guestId) navigate('/')
    }, [guestId, navigate])

    if (!guestId) return null

    return (
        <div className="min-h-screen bg-[radial-gradient(ellipse_at_top,_var(--tw-gradient-stops))] from-blush-50/40 via-ivory-100 to-ivory-200/60 flex flex-col items-center justify-center px-4 py-12 select-none animate-fade-in-up">
            <div className="w-full max-w-sm bg-ivory-50/80 backdrop-blur-md border border-gold-200/40 rounded-3xl shadow-xl shadow-gold-200/20 p-8 md:p-10 text-center">

                {/* State: Idle / Start */}
                {status === 'idle' && (
                    <div className="flex flex-col items-center">
                        <img
                            src="/logo.png"
                            alt="Logo"
                            className="w-20 h-20 object-contain rounded-full shadow-md shadow-gold-100 border border-gold-200/10 bg-white p-0.5 mb-6"
                        />
                        <h2 className="text-3xl font-serif text-taupe-900 mb-2">
                            Download Gallery
                        </h2>
                        <p className="text-taupe-700/70 font-light text-sm leading-relaxed mb-8">
                            We will bundle all of your matched personal photos and common group photos into a single, high-quality ZIP file.
                        </p>
                        
                        <button
                            onClick={start}
                            className="w-full bg-taupe-800 text-ivory-50 rounded-xl py-4 font-semibold text-sm tracking-[0.2em] uppercase hover:bg-gold-600 hover:shadow-lg hover:shadow-gold-500/25 active:scale-[0.98] transition-all duration-300 cursor-pointer"
                        >
                            Prepare My Bundle
                        </button>
                        
                        <button
                            onClick={() => navigate('/gallery')}
                            className="mt-4 text-xs font-semibold text-taupe-700/50 uppercase tracking-widest hover:text-taupe-800 transition cursor-pointer"
                        >
                            ← Back to Gallery
                        </button>
                    </div>
                )}

                {/* State: Preparing / Polling */}
                {status === 'preparing' && (
                    <div className="flex flex-col items-center py-4">
                        <div className="w-20 h-20 bg-gold-50 rounded-full flex items-center justify-center text-4xl mb-6 shadow-inner relative overflow-hidden animate-pulse-glow">
                            ⏳
                            <div className="absolute inset-0 border border-gold-400 rounded-full animate-ping opacity-30" />
                        </div>
                        <h2 className="text-2xl font-serif text-taupe-900 mb-3">Bundling Memories</h2>
                        
                        <p className="text-taupe-700/60 font-light text-xs max-w-[280px] min-h-[36px] leading-relaxed mb-6">
                            {progressSteps[currentStep]}
                        </p>
                        
                        {/* Animated golden steps indicator */}
                        <div className="w-full flex justify-between gap-1 max-w-[180px]">
                            {progressSteps.map((_, i) => (
                                <div
                                    key={i}
                                    className={`h-1.5 flex-1 rounded-full transition-all duration-500 ${
                                        i <= currentStep ? 'bg-gold-500 shadow-[0_0_4px_#cca157]' : 'bg-gold-100'
                                    }`}
                                />
                            ))}
                        </div>
                    </div>
                )}

                {/* State: Ready */}
                {status === 'ready' && (
                    <div className="flex flex-col items-center">
                        <div className="w-16 h-16 bg-emerald-50 border border-emerald-100 rounded-full flex items-center justify-center text-3xl mb-6">
                            ✅
                        </div>
                        <h2 className="text-2xl font-serif text-taupe-900 mb-2">Package Ready!</h2>
                        <p className="text-taupe-700/70 font-light text-sm mb-8">
                            We've successfully bundled <span className="font-semibold text-taupe-900">{photoCount} high-resolution photos</span> for you, {guestName?.split(' ')[0]}.
                        </p>
                        
                        <button
                            onClick={downloadZip}
                            className="w-full bg-taupe-800 text-ivory-50 rounded-xl py-4 font-semibold text-sm tracking-[0.2em] uppercase hover:bg-gold-600 hover:shadow-lg hover:shadow-gold-500/25 active:scale-[0.98] transition-all duration-300 cursor-pointer shadow-md"
                        >
                            Download ZIP
                        </button>

                        <button
                            onClick={() => navigate('/gallery')}
                            className="mt-4 text-xs font-semibold text-taupe-700/50 uppercase tracking-widest hover:text-taupe-800 transition cursor-pointer"
                        >
                            ← Back to Gallery
                        </button>
                    </div>
                )}

                {/* State: Failed */}
                {status === 'failed' && (
                    <div className="flex flex-col items-center">
                        <div className="w-16 h-16 bg-red-50 border border-red-100 rounded-full flex items-center justify-center text-3xl mb-6">
                            ❌
                        </div>
                        <h2 className="text-2xl font-serif text-taupe-900 mb-2">Bundling Failed</h2>
                        <p className="text-taupe-700/70 font-light text-sm leading-relaxed mb-8">
                            We hit an issue while downloading or zipping your photo archive. Please try again.
                        </p>
                        
                        <button
                            onClick={() => setStatus('idle')}
                            className="w-full bg-taupe-800 text-ivory-50 rounded-xl py-4 font-semibold text-sm tracking-[0.2em] uppercase hover:bg-gold-600 hover:shadow-lg hover:shadow-gold-500/25 active:scale-[0.98] transition-all duration-300 cursor-pointer"
                        >
                            Try Again
                        </button>
                    </div>
                )}

            </div>
        </div>
    )
}