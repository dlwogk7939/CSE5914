import { useEffect, useState } from 'react'
import './RecentGames.css'

// Shows the most recent games in nba.db with final scores and each game's top scorer.
// Clicking a game asks AskNBA about it.
export default function RecentGames({ apiBase, onAsk, disabled }) {
  const [games, setGames] = useState([])
  const [status, setStatus] = useState('loading') // loading | ready | error
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    async function loadGames() {
      try {
        const response = await fetch(`${apiBase}/recent-games?limit=8`, { signal: controller.signal })
        if (!response.ok) throw new Error('Could not load recent games.')
        const data = await response.json()
        if (!Array.isArray(data.games)) throw new Error('Invalid recent games response.')
        setGames(data.games)
        setStatus('ready')
      } catch {
        if (!controller.signal.aborted) setStatus('error')
      }
    }
    loadGames()
    return () => controller.abort()
  }, [apiBase, attempt])

  if (status === 'ready' && !games.length) return null

  return (
    <section className="recent-games" aria-label="Recent games">
      <div className="recent-games-heading">
        <span />
        Recent games
        <span />
      </div>

      {status === 'loading' && <p className="recent-games-note" role="status">Loading recent games…</p>}

      {status === 'error' && (
        <p className="recent-games-note" role="alert">
          Recent games are unavailable.{' '}
          <button type="button" onClick={() => { setStatus('loading'); setAttempt((n) => n + 1) }}>
            Retry
          </button>
        </p>
      )}

      {status === 'ready' && (
        <ul className="game-list">
          {games.map((game) => (
            <li key={game.game_id}>
              <button
                type="button"
                className="game-card"
                disabled={disabled}
                title={`Ask about ${gameTitle(game)}`}
                onClick={() => onAsk(gameQuestion(game))}
              >
                <span className="game-date">{formatDate(game.date)}</span>
                <TeamRow team={game.away} won={isWinner(game.away, game.home)} />
                <TeamRow team={game.home} won={isWinner(game.home, game.away)} home />
                {game.top_performer && (
                  <span className="game-leader">
                    <strong>{game.top_performer.name}</strong>{' '}
                    {game.top_performer.points} PTS · {game.top_performer.rebounds} REB · {game.top_performer.assists} AST
                  </span>
                )}
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

function TeamRow({ team, won, home = false }) {
  return (
    <span className={won ? 'game-team won' : 'game-team'}>
      <span className="game-team-name" title={team.name}>
        {team.abbreviation ?? '—'}
        {home && <small> home</small>}
      </span>
      <span className="game-score">{team.score ?? '—'}</span>
    </span>
  )
}

function isWinner(team, opponent) {
  return team.score != null && opponent.score != null && team.score > opponent.score
}

function gameTitle(game) {
  return `${game.away.name} at ${game.home.name}`
}

function gameQuestion(game) {
  return `Show the box score for the ${gameTitle(game)} game on ${formatDate(game.date, true)}.`
}

// Dates are stored in UTC ("2026-04-13T00:30Z"); show them in the viewer's time zone.
function formatDate(value, withYear = false) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleDateString(undefined, {
    weekday: withYear ? undefined : 'short',
    month: 'short',
    day: 'numeric',
    year: withYear ? 'numeric' : undefined,
  })
}
