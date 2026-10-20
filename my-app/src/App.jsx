import { useEffect, useRef, useState } from 'react'
import './App.css'

const KEY = 'historykey'
const prompts = [
  ['LEAGUE LEADERS', 'Who led the NBA in assists in 2025?'],
  ['PLAYER COMPARISONS', 'Compare LeBron and Curry since 2015'],
  ['SHOOTING STATS', 'Who are the best 3-point shooters?'],
]

// Basketball graphic used as logo
function Ball({ size = 28 }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      aria-hidden="true"
    >
      <circle cx="16" cy="16" r="13" />
      <path d="M3 16h26M16 3v26M7 6c11 6 11 14 0 20M25 6c-11 6-11 14 0 20" />
    </svg>
  )
}

// Arrow used in the "Ask" enter button
function Arrow() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      aria-hidden="true"
    >
      <path d="M4 12h15m-6-6 6 6-6 6" />
    </svg>
  )
}

// Speech bubble used in question history
function SpeechBubble({ size = 16 }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      aria-hidden="true"
    >
      <path d="M20 4H4v13h4l-4 4V4m4 13h12V4" />
    </svg>
  )
}

// Load question history from json in localstorage
function load() {
  try {
    const h = JSON.parse(localStorage.getItem(KEY) || '[]')
    return Array.isArray(h)
      ? h
          .filter(
            (x) =>
              typeof x.id === 'string' &&
              typeof x.question === 'string' &&
              typeof x.answer === 'string',
          )
          .slice(0, 100)
      : []
  } catch {
    return []
  }
}

export default function App() {
  const [history, setHistory] = useState(load)
  const [activeId, setActiveId] = useState(null)
  const [question, setQuestion] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const [menu, setMenu] = useState(false)
  const [storageError, setStorageError] = useState(false)

  const input = useRef(null)
  const active = history.find((x) => x.id === activeId)

  useEffect(() => {
    try {
      localStorage.setItem(KEY, JSON.stringify(history))
      setStorageError(false)
    } catch {
      setStorageError(true)
    }
  }, [history])

  // Reset UI to default new question UI
  function fresh() {
    setActiveId(null)
    setQuestion('')
    setError('')
    setMenu(false)
    input.current?.focus()
  }

  // Ask question
  async function ask(text) {
    text = text.trim()
    if (!text || pending) return
    setPending(true)
    setError('')
    setMenu(false)
    try {
      const response = await fetch('http://127.0.0.1:8000/ask', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: text }),
      })
      const data = await response.json()
      if (!response.ok) {
        throw new Error(typeof data.detail === 'string' ? data.detail : 'Unable to answer this question. Please try again.')
      }
      if (typeof data.answer !== 'string' || !data.answer.trim()) {
        throw new Error('The backend returned an empty answer. Please try again.')
      }
      const entry = { id: crypto.randomUUID(), question: text, answer: data.answer }
      setHistory((h) => [entry, ...h].slice(0, 100))
      setActiveId(entry.id)
      setQuestion('')
    } catch (e) {
      setError(
        e instanceof TypeError
          ? 'Cannot reach the backend. Make sure it is running on port 8000.'
          : e.message,
      )
    } finally {
      setPending(false)
    }
  }

  return (
    <div className="app-shell">
      {menu && (
        <button
          className="overlay"
          aria-label="Close history"
          onClick={() => setMenu(false)}
        />
      )}
      <aside
        className={menu ? 'sidebar open' : 'sidebar'}
        aria-label="Question history"
      >
        <a
          href="#"
          className="brand"
          onClick={(e) => {
            e.preventDefault()
            fresh()
          }}
        >
          <Ball />
          <span>AskNBA</span>
        </a>
        <button className="new-chat" onClick={fresh} disabled={pending}>
          <span className="plus">+</span> New question
        </button>
        <div className="history-heading">
          RECENT QUESTIONS {history.length > 0 && <span>{history.length}</span>}
        </div>
        <nav className="history-list" aria-label="Past questions">
          {history.length ? (
            history.map((x) => (
              <button
                key={x.id}
                className={
                  'history-item ' + (activeId === x.id ? 'selected' : '')
                }
                onClick={() => {
                  setActiveId(x.id)
                  setMenu(false)
                  setError('')
                }}
              >
                <SpeechBubble />
                <span>{x.question}</span>
              </button>
            ))
          ) : (
            <div className="history-empty">
              <SpeechBubble size={24} />
              <p>No questions yet.</p>
              <span>Your past questions will appear here.</span>
            </div>
          )}
        </nav>
        <div className="sidebar-footer">
          {history.length > 0 && (
            <button
              onClick={() => {
                // Set history to empty []
                setHistory([])
                fresh()
              }}
              disabled={pending}
            >
              Clear history
            </button>
          )}
          <div>
            <span className="dot" />
            {storageError
              ? 'History available for this visit only'
              : 'History saved on this device'}
          </div>
        </div>
      </aside>
      <main>
        <header>
          <div>
            <button
              className="menu"
              aria-label="Open question history"
              aria-expanded={menu}
              onClick={() => setMenu(true)}
            >
              ☰
            </button>
          </div>
        </header>
        <div className={`workspace ${active || pending ? 'has-answer' : ''}`}>
          {!active && !pending && (
            <section className="welcome">
              <div className="welcome-ball">
                <Ball size={39} />
              </div>
              <h1>
                Ask a question about
                <br /> NBA stats!
              </h1>
            </section>
          )}
          <form
            className="question-form"
            onSubmit={(e) => {
              e.preventDefault()
              ask(question)
            }}
          >
            <label className="sr-only" htmlFor="question">
              Ask a question about NBA statistics
            </label>
            <input
              id="question"
              ref={input}
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              placeholder={
                active
                  ? 'Ask another question…'
                  : 'What do you want to know about the NBA?'
              }
              maxLength={2000}
              disabled={pending}
              autoComplete="off"
            />
            <button disabled={!question.trim() || pending}>
              {pending ? 'Asking…' : 'Ask'}
              {!pending && <Arrow />}
            </button>
          </form>
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
          <p className="input-hint" role="status">
            {pending ? 'Finding your answer…' : ''}
          </p>
          {active && !pending && (
            <section className="conversation" aria-live="polite">
              <h1>{active.question}</h1>
              <article className="answer">
                <div className="answer-heading">
                  <Ball size={21} />
                  <strong>AskNBA</strong>
                  {active.sample && <span>Sample response</span>}
                </div>
                <p>{active.answer}</p>
              </article>
            </section>
          )}
          {!active && !pending && (
            <section className="suggestions" aria-label="Suggested questions">
              <div className="suggestions-heading">
                <span />
                Try asking
                <span />
              </div>
              <div className="suggestion-list">
                {prompts.map(([, text]) => (
                  <button
                    className="suggestion"
                    key={text}
                    onClick={() => ask(text)}
                    disabled={pending}
                  >
                    {text}
                  </button>
                ))}
              </div>
            </section>
          )}
        </div>
      </main>
    </div>
  )
}
