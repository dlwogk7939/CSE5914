# AskNBA

React + Vite sports-statistics question interface. Chats are saved in the browser, with up to 100
questions per chat. Clear history removes local browser data.

From `my-app`, run `npm install` the first time, then `npm run dev`.
Open `http://localhost:5173`.

The top-left Sport dropdown lists leagues with imported data from `/api/coverage`.
Switching leagues starts a new chat and updates suggestions, recent results, and question context.
Opening a saved chat restores its league; older unscoped chats use All sports. The selection is saved on this device.

Start the FastAPI backend separately on port 8000 following [backend setup](../backend/README.md).
It loads `API_KEY` from the repository-root `.env` and uses Gemini 3.5 Flash-Lite by default.

## Answers

The Model dropdown loads supported Gemini choices from `/api/models` through the local proxy.
It remembers your selection on this device. If the list cannot load, use Retry after starting the backend.
Questions are sent through `/api/ask` as `{"question":"...","model":"gemini-3.5-flash-lite","league":"nfl"}` with the selected league (or null for All sports).
Each new answer stores and displays the model returned by the backend. Older history still works.
AI-generated follow-up questions appear as buttons under each new answer. Clicking one asks it
in the same chat with the preceding messages and selected league. Suggestions are saved with the answer.
The returned answer is displayed and saved in browser history. Failed requests display an error
without adding a history entry. Earlier turns in the selected chat are sent as follow-up context.
The backend queries the sports data currently loaded in the shared SQLite database; see
[data import and coverage](../backend/README.md#multi-sport-data). Imports can remain partial while running.

Suggested questions follow the selected league. Recent cards show team scores, match participants/results,
or tournament/race-session details as appropriate for the sport.

## Maintain

`src/App.jsx` contains the main UI and localstorage code

`src/App.css` app styles

`src/index.css` global styles
