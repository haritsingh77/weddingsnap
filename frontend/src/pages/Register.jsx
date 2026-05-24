import { useRef, useState, useCallback, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { registerFace } from '../services/api'

export default function Register() {
    const navigate = useNavigate()
    const videoRef = useRef(null)
    const canvasRef = useRef(null)
    const streamRef = useRef(null)

    const [step, setStep] = useState('intro')   // intro | camera | preview | loading | error
    const [selfieBlob, setSelfieBlob] = useState(null)
    const [preview, setPreview] = useState(null)
    const [error, setError] = useState('')
    const [matchInfo, setMatchInfo] = useState(null)

    const guestId = localStorage.getItem('guest_id')
    const guestName = localStorage.getItem('guest_name')

    // Stop active camera helper
    const stopCamera = useCallback(() => {
        if (streamRef.current) {
            streamRef.current.getTracks().forEach(track => track.stop())
            streamRef.current = null
        }
        if (videoRef.current) {
            videoRef.current.srcObject = null
        }
    }, [])

    // Ensure camera stops when navigating away or unmounting
    useEffect(() => {
        return () => {
            stopCamera()
        }
    }, [stopCamera])

    // ── Camera ────────────────────────────────────────────────────────────────

    const startCamera = async () => {
        setStep('camera')
        setError('')
        try {
            // Stop any existing stream first
            stopCamera()

            const stream = await navigator.mediaDevices.getUserMedia({
                video: { facingMode: 'user', width: 640, height: 480 }
            })
            streamRef.current = stream

            // Wait for video element to be available in DOM
            setTimeout(() => {
                if (videoRef.current) {
                    videoRef.current.srcObject = stream
                }
            }, 100)
        } catch (err) {
            console.error('Camera Access Error:', err)
            setError('Camera access denied or unavailable. Please allow camera permissions.')
            setStep('error')
        }
    }

    const capture = useCallback(() => {
        const video = videoRef.current
        const canvas = canvasRef.current
        if (!video || !canvas) return

        canvas.width = video.videoWidth
        canvas.height = video.videoHeight
        canvas.getContext('2d').drawImage(video, 0, 0)

        // Stop camera stream immediately
        stopCamera()

        canvas.toBlob(blob => {
            setSelfieBlob(blob)
            setPreview(URL.createObjectURL(blob))
            setStep('preview')
        }, 'image/jpeg', 0.92)
    }, [stopCamera])

    const retake = async () => {
        setPreview(null)
        setSelfieBlob(null)
        await startCamera()
    }

    // ── Skip (bypass selfie) ──────────────────────────────────────────────────

    const skipToGallery = () => {
        stopCamera()
        navigate('/gallery')
    }

    // ── Submit ────────────────────────────────────────────────────────────────

    const submit = async () => {
        setStep('loading')
        try {
            const file = new File([selfieBlob], 'selfie.jpg', { type: 'image/jpeg' })
            const res = await registerFace(guestId, file)
            setMatchInfo(res.data)
            setTimeout(() => navigate('/gallery'), 2500)
        } catch (err) {
            setError(err.response?.data?.detail || err.message || 'Could not recognize a face. Please try again in better lighting.')
            setStep('error')
        }
    }

    // Navigation Guard & Admin skip
    useEffect(() => {
        if (!guestId) {
            navigate('/')
            return
        }

        const inviteCode = localStorage.getItem('invite_code') || ''
        const isAdmin = inviteCode.toUpperCase().includes('ADMIN') ||
                        guestName?.toLowerCase().includes('saurav') ||
                        guestName?.toLowerCase().includes('mahima')

        if (isAdmin) {
            navigate('/gallery')
        }
    }, [guestId, guestName, navigate])

    if (!guestId) return null

    return (
        <div className="min-h-screen bg-[radial-gradient(ellipse_at_top,_var(--tw-gradient-stops))] from-amber-50/30 via-stone-50 to-stone-100/50 flex flex-col items-center justify-center px-4 py-8 select-none">
            <div className="w-full max-w-sm bg-white/70 backdrop-blur-md border border-white/60 rounded-3xl shadow-xl shadow-stone-200/40 p-8 flex flex-col gap-6 text-center animate-fade-in-up">

                {/* Step 1: Intro */}
                {step === 'intro' && (
                    <div className="flex flex-col items-center">
                        <img
                            src="/logo.png"
                            alt="Mahima & Saurav Logo"
                            className="w-20 h-20 object-contain rounded-full shadow-md shadow-gold-100 border border-gold-200/10 bg-white p-0.5 mb-6"
                        />
                        <h2 className="text-3xl font-serif text-stone-900 mb-2">
                            Welcome, {guestName?.split(' ')[0]}
                        </h2>
                        <p className="text-stone-500 font-light text-sm leading-relaxed mb-8">
                            Take a quick selfie, and we'll automatically scan our wedding gallery to find all the photos you are in.
                        </p>

                        <button
                            onClick={startCamera}
                            className="w-full bg-stone-900 text-white rounded-xl py-4 font-semibold text-sm tracking-widest uppercase hover:bg-gold-600 hover:shadow-lg hover:shadow-gold-500/25 active:scale-[0.98] transition-all duration-300 cursor-pointer"
                        >
                            Start Scanner
                        </button>

                        <button
                            onClick={skipToGallery}
                            className="w-full text-stone-400 text-xs font-medium py-3 hover:text-stone-600 transition-colors cursor-pointer underline-offset-2 hover:underline"
                        >
                            Skip selfie, browse all photos →
                        </button>
                    </div>
                )}

                {/* Step 2: Camera Feed */}
                {step === 'camera' && (
                    <div className="flex flex-col items-center w-full">
                        <p className="text-stone-500 font-light text-xs tracking-wider uppercase mb-4">Fit your face inside the frame</p>

                        <div className="relative w-full aspect-[4/3] rounded-2xl overflow-hidden bg-stone-900 border border-stone-200/60 shadow-inner">
                            <video
                                ref={videoRef}
                                autoPlay
                                playsInline
                                muted
                                className="w-full h-full object-cover scale-x-[-1]"
                            />

                            {/* Premium Gold Scanner Overlay */}
                            <div className="absolute inset-0 flex items-center justify-center pointer-events-none">
                                <div className="w-40 h-52 border-2 border-gold-400/80 rounded-full ring-[999px] ring-stone-900/60 flex items-center justify-center">
                                    {/* Subtly animated scanner line */}
                                    <div className="absolute left-0 w-full h-[2px] bg-gradient-to-r from-transparent via-gold-400 to-transparent shadow-[0_0_8px_#cca157] animate-scan" />
                                </div>
                            </div>
                        </div>

                        <canvas ref={canvasRef} className="hidden" />

                        <div className="flex w-full gap-3 mt-6">
                            <button
                                onClick={() => { stopCamera(); setStep('intro'); }}
                                className="flex-1 border border-stone-200 text-stone-500 bg-white/50 rounded-xl py-3.5 font-medium text-xs uppercase tracking-wider hover:bg-stone-50 transition cursor-pointer"
                            >
                                Back
                            </button>
                            <button
                                onClick={capture}
                                className="flex-2 bg-stone-900 text-white rounded-xl py-3.5 font-semibold text-xs tracking-widest uppercase hover:bg-gold-600 hover:shadow-lg hover:shadow-gold-500/25 transition cursor-pointer"
                            >
                                Capture Photo
                            </button>
                        </div>
                    </div>
                )}

                {/* Step 3: Photo Preview */}
                {step === 'preview' && (
                    <div className="flex flex-col items-center w-full">
                        <p className="text-stone-500 font-light text-xs tracking-wider uppercase mb-4">Confirm your selfie</p>

                        <div className="w-full aspect-[4/3] rounded-2xl overflow-hidden bg-stone-900 border border-stone-200/60 shadow-lg">
                            <img
                                src={preview}
                                alt="Selfie preview"
                                className="w-full h-full object-cover scale-x-[-1]"
                            />
                        </div>

                        <div className="flex w-full gap-3 mt-6">
                            <button
                                onClick={retake}
                                className="flex-1 border border-stone-200 text-stone-500 bg-white/50 rounded-xl py-3.5 font-medium text-xs uppercase tracking-wider hover:bg-stone-50 transition cursor-pointer"
                            >
                                Retake
                            </button>
                            <button
                                onClick={submit}
                                className="flex-2 bg-stone-900 text-white rounded-xl py-3.5 font-semibold text-xs tracking-widest uppercase hover:bg-gold-600 hover:shadow-lg hover:shadow-gold-500/25 transition cursor-pointer"
                            >
                                Find My Photos
                            </button>
                        </div>
                    </div>
                )}

                {/* Step 4: Loading & Scanning */}
                {step === 'loading' && (
                    <div className="flex flex-col items-center py-6">
                        <div className="w-20 h-20 bg-gold-50 rounded-full flex items-center justify-center text-4xl mb-6 shadow-inner relative overflow-hidden animate-pulse-glow">
                            🔍
                            <div className="absolute inset-0 border border-gold-400 rounded-full animate-ping opacity-40" />
                        </div>
                        <h2 className="text-2xl font-serif text-stone-950 mb-2">Analyzing Photos</h2>
                        <p className="text-stone-400 font-light text-sm max-w-[280px] leading-relaxed mb-6">
                            Matching your face details across all raw high-res wedding photos...
                        </p>

                        {matchInfo ? (
                            <div className="w-full bg-emerald-50/60 border border-emerald-100 rounded-xl p-4 text-emerald-800 text-xs font-semibold animate-fade-in-up">
                                🌟 Matched: {matchInfo.personal_count} of yours & {matchInfo.common_count} group photos!
                            </div>
                        ) : (
                            <div className="w-32 bg-stone-100 rounded-full h-1.5 overflow-hidden">
                                <div className="bg-gold-500 h-full w-2/3 rounded-full animate-[loading_1.5s_ease-in-out_infinite]" style={{
                                    animationName: 'shimmer',
                                    animationDuration: '1.5s',
                                    animationIterationCount: 'infinite',
                                }} />
                                <style dangerouslySetInnerHTML={{
                                    __html: `
                                    @keyframes shimmer {
                                        0% { transform: translateX(-100%); }
                                        100% { transform: translateX(150%); }
                                    }
                                `}} />
                            </div>
                        )}
                    </div>
                )}

                {/* Step 5: Error / Retry */}
                {step === 'error' && (
                    <div className="flex flex-col items-center">
                        <div className="w-16 h-16 bg-red-50 border border-red-100 rounded-full flex items-center justify-center text-3xl mb-6">
                            😕
                        </div>
                        <h2 className="text-2xl font-serif text-stone-900 mb-2">Let's Try Again</h2>
                        <p className="text-red-400 font-medium text-xs leading-relaxed max-w-[260px] mb-8">
                            {error}
                        </p>

                        <button
                            onClick={() => setStep('intro')}
                            className="w-full bg-stone-900 text-white rounded-xl py-4 font-semibold text-sm tracking-widest uppercase hover:bg-gold-600 hover:shadow-lg hover:shadow-gold-500/25 active:scale-[0.98] transition-all duration-300 cursor-pointer"
                        >
                            Retry Scanner
                        </button>

                        <button
                            onClick={skipToGallery}
                            className="w-full text-stone-400 text-xs font-medium py-3 hover:text-stone-600 transition-colors cursor-pointer underline-offset-2 hover:underline"
                        >
                            Browse without selfie →
                        </button>
                    </div>
                )}

            </div>
        </div>
    )
}