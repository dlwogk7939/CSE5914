import { useEffect, useState } from 'react'
import './RecentGames.css'

// Shows loaded results for the selected league; clicking one starts a question.
export default function RecentGames({ apiBase, league, onAsk, disabled }) {
  const [games, setGames] = useState([])
  const [status, setStatus] = useState('loading') // loading | ready | error
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    async function loadGames() {
      try {
        const response = await fetch(`${apiBase}/recent-games?limit=8&league=${encodeURIComponent(league?.league_id || 'nba')}`, { signal: controller.signal })
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
  }, [apiBase, league?.league_id, attempt])

  if (status === 'ready' && !games.length) return null

  return (
    <section className="recent-games" aria-label="Recent results">
      <div className="recent-games-heading">
        <span />
        Recent {league?.name || 'NBA'} results
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
                onClick={() => onAsk(gameQuestion(game, league?.name || 'NBA'))}
              >
                <span className="game-date">{formatDate(game.date)}</span>
                {game.kind === 'event' ? (
                  <>
                    <strong className="game-event">{gameTitle(game)}</strong>
                    {game.participants.find((p) => p.winner === 1) && <span className="game-leader">
                      Winner: {game.participants.find((p) => p.winner === 1).name}
                    </span>}
                  </>
                ) : game.kind === 'match' ? (
                  <>
                    {game.participants.map((p) => <TeamRow key={p.id} team={{ name: p.name, score: p.winner === 1 ? 'Won' : null }} won={p.winner === 1} />)}
                    <span className="game-leader">{game.round_name || game.event_name}</span>
                  </>
                ) : (
                  <>
                    <TeamRow team={game.away} won={isWinner(game.away, game.home)} />
                    <TeamRow team={game.home} won={isWinner(game.home, game.away)} home />
                  </>
                )}
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
        {team.abbreviation || team.name || '—'}
        {home && <small> home</small>}
      </span>
      <span className="game-score">{team.score ?? ''}</span>
    </span>
  )
}

function isWinner(team, opponent) {
  return team.score != null && opponent.score != null && team.score > opponent.score
}

function gameTitle(game) {
  if (game.kind === 'event') return [game.event_name, game.competition_type !== 'total' && game.competition_type?.replaceAll('_', ' ')].filter(Boolean).join(' · ')
  if (game.kind === 'match') return game.participants.map((p) => p.name).join(' vs ')
  return `${game.away.name} at ${game.home.name}`
}

function gameQuestion(game, league) {
  const phase = game.kind === 'team' && game.phase_name ? ` (${game.phase_name})` : ''
  return `Show the ${game.kind === 'event' || game.kind === 'match' ? 'results' : 'box score'} for ${league}${phase}: ${gameTitle(game)} on ${game.date} (UTC).`
}

// Dates are stored in UTC ("2026-04-13T00:30Z"); show them in the viewer's time zone.
function formatDate(value) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleDateString(undefined, {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
  })
}
