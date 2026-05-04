import { useState, useEffect, createContext, useContext } from 'react'
import { Routes, Route, Link, useNavigate } from "react-router-dom"
import ChatApp from './components/Chat/ChatApp'
import './App.css'

const UserContext = createContext(null)
export function useUser() { return useContext(UserContext) }

function UserProvider({ children }) {
  const [user, setUser] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    fetch('http://localhost:8000/auth/session/', {
      method: 'GET',
      credentials: 'include',
    })
      .then(r => r.ok ? r.json() : Promise.reject())
      .then(data => {
        // Accept Supabase user object directly
        if (data && data.id) setUser(data)
        setLoading(false)
      })
      .catch(() => setLoading(false))
  }, [])

  async function login(email, password) {
    const res = await fetch('http://localhost:8000/auth/login/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ email, password }),
    })
    if (!res.ok) throw new Error((await res.json()).error || 'Login failed')
    const data = await res.json()
    setUser(data.user)
    return data
  }

  async function signup(email, password, username) {
    const res = await fetch('http://localhost:8000/auth/signup/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ email, password, username }),
    })
    if (!res.ok) throw new Error((await res.json()).error || 'Signup failed')
    const data = await res.json()
    setUser({ email, username, id: data.supabase_uid })
    return data
  }

  async function logout() {
    await fetch('http://localhost:8000/auth/logout/', {
      method: 'POST',
      credentials: 'include',
    })
    setUser(null)
  }

  async function refreshSession() {
    await fetch('http://localhost:8000/auth/session/refresh/', {
      method: 'POST',
      credentials: 'include',
    })
  }

  return (
    <UserContext.Provider value={{ user, loading, login, signup, logout, refreshSession }}>
      {children}
    </UserContext.Provider>
  )
}

function Auth({ type }) {
  const [showWelcome, setShowWelcome] = useState(true)
  const [form, setForm] = useState({ username: '', email: '', password: '' })
  const [error, setError] = useState('')
  const { login, signup } = useUser()
  const navigate = useNavigate()

  useEffect(() => {
    const timer = setTimeout(() => setShowWelcome(false), 2000)
    return () => clearTimeout(timer)
  }, [])

  function handleChange(e) {
    const { name, value } = e.target
    setForm((f) => ({ ...f, [name]: value }))
  }

  async function handleSubmit(e) {
    e.preventDefault()
    setError('')
    try {
      if (type === 'signup') {
        if (!form.username.trim()) return setError('Username required')
        if (!form.email.trim()) return setError('Email required')
        if (!form.password) return setError('Password required')
        await signup(form.email, form.password, form.username)
      } else {
        if (!form.email.trim()) return setError('Email required')
        if (!form.password) return setError('Password required')
        await login(form.email, form.password)
      }
      navigate('/chat')
    } catch (err) {
      setError(err.message)
    }
  }

  return (
    <>
      {showWelcome ? (
        <p className='welcome-msg'>Welcome!</p>
      ) : (
        <div className="menu">
          <div className="body">
            <Link to="/"><p className="logo">WeShare</p></Link>
          </div>
          <div className="form">
            <form onSubmit={handleSubmit}>
              <h3>{type === 'signup' ? 'Sign Up' : 'Login'}</h3>
              {type === 'signup' && (
                <div className="username">
                  <label htmlFor="username">Username:</label>
                  <input autoComplete="off" type="text" name="username" id="username" value={form.username} onChange={handleChange} />
                </div>
              )}
              <div className="email">
                <label htmlFor="email">Email:</label>
                <input autoComplete="off" type="email" name="email" id="email" value={form.email} onChange={handleChange} />
              </div>
              <div className="password">
                <label htmlFor="password">Password:</label>
                <input autoComplete="off" type="password" name="password" id="password" value={form.password} onChange={handleChange} />
              </div>
              <button id="button" type="submit">Submit</button>
              {error && <div className="auth-error" role="alert">{error}</div>}
            </form>
          </div>
        </div>
      )}
    </>
  )
}

function Home() {
  return (
    <div className="home-container">
      <header className="home-header">
        <h1>Welcome to WeShare</h1>
        <p>Connecting people through sharing.</p>
      </header>

      <div className="home-content">
        <Link to="/signup"><button className="primary-btn">Sign Up</button></Link>
        <Link to="/login"><button className="secondary-btn">Login</button></Link>
      </div>
    </div>
  )
}

function ChatWrapper() {
  const { user, logout, loading } = useUser()
  const navigate = useNavigate()
  if (loading) return <div>Loading...</div>
  if (!user) {
    navigate('/login')
    return null
  }
  return <><ChatApp /><button onClick={logout} className="logout-btn">Logout</button></>
}

function App() {
  return (
    <UserProvider>
      <Routes>
        <Route path="/login" element={<Auth type="login" />} />
        <Route path="/signup" element={<Auth type="signup" />} />
        <Route path="/chat" element={<ChatWrapper />} />
        <Route path="/" element={<Home />} />
      </Routes>
    </UserProvider>
  )
}

export default App
