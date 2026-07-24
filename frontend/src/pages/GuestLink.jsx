import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { openGuestLink } from '../services/api'

/**
 * Opening a personal link IS the login.
 *
 * The old flow asked for a shared invite code and a name, then a selfie. The
 * code identified the wedding rather than the person, so two guests with the
 * same name collided; and the selfie could simply fail to find a face. The
 * clustering already knows who is in which photo, so the link just says which
 * cluster is yours.
 *
 * The token is stored and sent on every later request — it is the credential
 * the API checks, so it is treated like a password, never shown in the UI and
 * cleared on failure.
 */
export default function GuestLink() {
    const { token } = useParams()
    const navigate = useNavigate()
    const [error, setError] = useState('')

    useEffect(() => {
        let cancelled = false

        const open = async () => {
            try {
                // Store first: openGuestLink is unauthenticated, but everything
                // the gallery does next needs the header already in place.
                localStorage.setItem('guest_token', token)
                const { data } = await openGuestLink(token)
                if (cancelled) return

                localStorage.setItem('guest_id', data.guest_id)
                localStorage.setItem('guest_name', data.name)
                localStorage.setItem('event_name', data.event_name || '')
                localStorage.setItem('is_household', data.is_household ? '1' : '')
                // Admin guests (the couple / family) get the admin tools from
                // their own link — the backend authorises their token as admin.
                localStorage.setItem('is_admin_guest', data.is_admin ? '1' : '')
                localStorage.setItem('household_members', JSON.stringify(data.members || []))

                navigate('/gallery', { replace: true })
            } catch (err) {
                if (cancelled) return
                // Never leave a bad token behind — it would fail every later call.
                localStorage.removeItem('guest_token')
                localStorage.removeItem('guest_id')
                setError(
                    err.response?.data?.detail ||
                    "We couldn't open this link. Please check the message you were sent."
                )
            }
        }

        if (token) open()
        return () => { cancelled = true }
    }, [token, navigate])

    return (
        <div className="min-h-screen flex items-center justify-center bg-ivory-50 px-6">
            <div className="text-center max-w-sm">
                {!error ? (
                    <>
                        <div className="w-10 h-10 mx-auto mb-5 border-2 border-gold-300 border-t-transparent rounded-full animate-spin" />
                        <p className="font-serif text-taupe-800 text-lg">Opening your photos…</p>
                    </>
                ) : (
                    <>
                        <h1 className="font-serif text-taupe-900 text-xl mb-3">
                            This link didn't work
                        </h1>
                        <p className="text-taupe-500 text-sm leading-relaxed mb-6">{error}</p>
                        <button
                            onClick={() => navigate('/')}
                            className="bg-taupe-800 text-white text-sm font-semibold px-5 py-2.5 rounded-xl hover:bg-gold-650 cursor-pointer"
                        >
                            Go to the home page
                        </button>
                    </>
                )}
            </div>
        </div>
    )
}
