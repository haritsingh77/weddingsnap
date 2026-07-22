import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import Landing from './pages/Landing'
import GuestLink from './pages/GuestLink'
import Gallery from './pages/Gallery'
import Download from './pages/Download'
import Admin from './pages/Admin'

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Landing />} />
        {/* A guest's personal link. Opening it is the entire login — the token
            identifies them and is the credential the API checks. Replaces
            /register, where a shared code plus a typed name could not tell two
            guests with the same name apart, and the selfie step could fail to
            find a face at all. */}
        <Route path="/g/:token" element={<GuestLink />} />
        <Route path="/gallery" element={<Gallery />} />
        <Route path="/download" element={<Download />} />
        <Route path="/admin" element={<Admin />} />
        <Route path="*" element={<Navigate to="/" />} />
      </Routes>
    </BrowserRouter>
  )
}

export default App