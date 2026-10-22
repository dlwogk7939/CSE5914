# AskNBA

Minimal React + Vite NBA question interface. Questions and answers are saved in browser, capped at 100 entries. Clear history removes local browser data.

From `my-app`, run `npm install` the first time, then `npm run dev`.
Open `http://localhost:5173`.

Start the FastAPI backend separately on port 8000 following [backend setup](../backend/README.md).
It loads `API_KEY` from the repository-root `.env` and uses Gemini 3.5 Flash-Lite by default.

## Answers

The Model dropdown loads supported Gemini choices from `http://127.0.0.1:8000/models`.
It remembers your selection on this device. If the list cannot load, use Retry after starting the backend.
Questions are sent to `http://127.0.0.1:8000/ask` as `{"question":"...","model":"gemini-3.5-flash-lite"}`.
Each new answer stores and displays the model returned by the backend. Older history still works.
The returned answer is displayed and saved in browser history. Failed requests display an error
without adding a history entry. Questions are independent; history is not sent to the backend.

Suggested questions are also hardcoded.

## Maintain

`src/App.jsx` contains the main UI and localstorage code

`src/App.css` app styles

`src/index.css` global styles
