import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { verifyInvite } from '../services/api'

export default function Landing() {
    const navigate = useNavigate()
    const [form, setForm] = useState({ code: '', name: '', phone: '' })
    const [error, setError] = useState('')
    const [loading, setLoading] = useState(false)

    const handle = (e) => setForm({ ...form, [e.target.name]: e.target.value })

    useEffect(() => {
        const params = new URLSearchParams(window.location.search)
        const code = params.get('code')
        const name = params.get('name')
        const phone = params.get('phone') || ''

        if (code && name) {
            setForm({ code: code.toUpperCase().trim(), name, phone })
            
            const autoLogin = async () => {
                setLoading(true)
                setError('')
                try {
                    const res = await verifyInvite(code.toUpperCase().trim(), name, phone)
                    const { guest_id, event_name, has_selfie } = res.data
                    localStorage.setItem('guest_id', guest_id)
                    localStorage.setItem('guest_name', name)
                    localStorage.setItem('event_name', event_name)
                    localStorage.setItem('invite_code', code.toUpperCase().trim())
                    
                    if (has_selfie) {
                        navigate('/gallery')
                    } else {
                        navigate('/register')
                    }
                } catch (err) {
                    console.error("Auto login failed:", err)
                    setError(err.response?.data?.detail || 'Auto-login failed. Please verify credentials manually.')
                } finally {
                    setLoading(false)
                }
            }
            autoLogin()
        }
    }, [navigate])

    const submit = async () => {
        if (!form.code || !form.name) {
            setError('Please enter your invite code and name.')
            return
        }
        setLoading(true)
        setError('')
        try {
            const res = await verifyInvite(form.code, form.name, form.phone)
            const { guest_id, event_name, has_selfie } = res.data
            localStorage.setItem('guest_id', guest_id)
            localStorage.setItem('guest_name', form.name)
            localStorage.setItem('event_name', event_name)
            localStorage.setItem('invite_code', form.code.toUpperCase().trim())
            
            if (has_selfie) {
                navigate('/gallery')
            } else {
                navigate('/register')
            }
        } catch (err) {
            setError(err.response?.data?.detail || 'Invalid code or something went wrong.')
        } finally {
            setLoading(false)
        }
    }

    return (
        <div className="min-h-screen bg-[radial-gradient(ellipse_at_top,_var(--tw-gradient-stops))] from-amber-50/30 via-stone-50 to-stone-100/50 flex flex-col items-center justify-center px-4 py-12 select-none">
            {/* Header with Fade-In Animation */}
            <div className="text-center mb-10 animate-fade-in-up">
                <img
                    src="/logo.png"
                    alt="Mahima & Saurav Wedding Logo"
                    className="w-32 h-32 mx-auto mb-6 object-contain rounded-full shadow-lg shadow-gold-100/50 border border-gold-200/20 bg-white/40 backdrop-blur-xs p-1"
                />
                <p className="text-xs font-medium tracking-[0.25em] text-gold-600 uppercase mb-3">You are invite-only guest</p>
                <h1 className="text-5xl md:text-6xl font-serif text-stone-900 tracking-tight leading-tight">
                    Mahima <span className="font-sans font-light text-stone-300 mx-2">&</span> Saurav
                </h1>
                <div className="w-12 h-[1px] bg-gold-400 mx-auto my-5"></div>
                <p className="text-stone-500 font-light text-sm tracking-wide">Find & download your wedding moments</p>
            </div>

            {/* Premium Card */}
            <div className="w-full max-w-md bg-white/70 backdrop-blur-md border border-white/60 rounded-3xl shadow-xl shadow-stone-200/40 p-8 md:p-10 flex flex-col gap-6 animate-fade-in-up [animation-delay:150ms]">
                <div className="flex flex-col gap-1">
                    <label className="text-[10px] font-semibold text-stone-400 uppercase tracking-widest">Invite Code</label>
                    <input
                        name="code"
                        value={form.code}
                        onChange={handle}
                        placeholder="e.g. MAHIMA2024"
                        className="mt-1 w-full border border-stone-200/80 bg-white/50 rounded-xl px-4 py-3.5 text-stone-800 placeholder-stone-300 focus:outline-none focus:border-gold-500 focus:ring-1 focus:ring-gold-500 uppercase tracking-widest transition-all duration-300 text-center text-sm font-semibold"
                    />
                </div>

                <div className="flex flex-col gap-1">
                    <label className="text-[10px] font-semibold text-stone-400 uppercase tracking-widest">Your Name</label>
                    <input
                        name="name"
                        value={form.name}
                        onChange={handle}
                        placeholder="Full Name"
                        className="mt-1 w-full border border-stone-200/80 bg-white/50 rounded-xl px-4 py-3.5 text-stone-800 placeholder-stone-300 focus:outline-none focus:border-gold-500 focus:ring-1 focus:ring-gold-500 transition-all duration-300 text-sm font-medium"
                    />
                </div>

                <div className="flex flex-col gap-1">
                    <div className="flex justify-between items-center">
                        <label className="text-[10px] font-semibold text-stone-400 uppercase tracking-widest">Phone Number</label>
                        <span className="text-[9px] font-medium text-stone-300 uppercase tracking-wider">Optional</span>
                    </div>
                    <input
                        name="phone"
                        value={form.phone}
                        onChange={handle}
                        placeholder="+91 98765 43210"
                        className="mt-1 w-full border border-stone-200/80 bg-white/50 rounded-xl px-4 py-3.5 text-stone-800 placeholder-stone-300 focus:outline-none focus:border-gold-500 focus:ring-1 focus:ring-gold-500 transition-all duration-300 text-sm font-medium"
                    />
                </div>

                {error && (
                    <div className="bg-red-50/50 border border-red-100 rounded-xl py-3 px-4 text-red-500 text-xs text-center font-medium animate-fade-in-up">
                        {error}
                    </div>
                )}

                <button
                    onClick={submit}
                    disabled={loading}
                    className="w-full bg-stone-900 text-white rounded-xl py-4 font-semibold text-sm tracking-widest uppercase hover:bg-gold-600 hover:shadow-lg hover:shadow-gold-500/25 active:scale-[0.98] transition-all duration-300 disabled:opacity-50 mt-2 cursor-pointer"
                >
                    {loading ? (
                        <span className="flex items-center justify-center gap-2">
                            <span className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin"></span>
                            Checking code...
                        </span>
                    ) : 'Enter Gallery'}
                </button>
            </div>

            <p className="text-stone-400 text-[10px] tracking-wider uppercase mt-8 animate-fade-in-up [animation-delay:300ms]">
                🔒 Secure Guest Access • Private Gallery
            </p>
        </div>
    )
}
