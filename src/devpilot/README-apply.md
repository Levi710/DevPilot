# DevPilot SQLite investigation history update

## Files
- `src/devpilot/investigation_store.py` — new SQLite persistence layer.
- `src/devpilot/api.py` — replacement API with history endpoints.

## Important compatibility check
Before replacing your current `api.py`, open `src/devpilot/repository_registry.py`.
The replacement expects a function named `get_repositories()` returning a list of
repository dictionaries with `repository_id` and `name`. If your registry uses a
different function name, adjust that import/call to match your existing registry.

## Apply
1. Back up your existing `src/devpilot/api.py`.
2. Copy `investigation_store.py` into `src/devpilot/`.
3. Replace `src/devpilot/api.py` with the provided file after the compatibility check.
4. Restart FastAPI:
   `uvicorn api:app --app-dir src/devpilot --reload`
5. Verify `http://127.0.0.1:8000/docs`.
6. Run one test investigation. Then visit `GET /api/investigations`.
   The SQLite database is created at `src/devpilot/data/investigations.sqlite3`.

## Frontend integration
The API now supports:
- `GET /api/investigations?limit=50`
- `GET /api/investigations/{id}`
- `POST /api/investigations` returns the saved record, including `id` and `created_at`.

Update `frontend/src/App.jsx` to load history from the API rather than the hardcoded
`recentInvestigations` array. On app mount, fetch `${API_BASE_URL}/api/investigations`,
then set the result's `investigations` array into state. After a successful POST,
prepend the returned saved record to that state. Use `description` as the history title,
`repository_name` as the repository label, and `created_at` for the timestamp.
Do not display old hardcoded demo records as real investigations.

Suggested helpers:

```jsx
const [recentInvestigations, setRecentInvestigations] = useState([]);
const [historyError, setHistoryError] = useState("");

async function loadInvestigations() {
  try {
    const response = await fetch(`${API_BASE_URL}/api/investigations?limit=50`);
    if (!response.ok) throw new Error(`History API returned HTTP ${response.status}`);
    const data = await response.json();
    setRecentInvestigations(Array.isArray(data.investigations) ? data.investigations : []);
    setHistoryError("");
  } catch (error) {
    setHistoryError(error instanceof Error ? error.message : "Could not load history.");
  }
}
```

Then call `loadInvestigations()` inside the existing initial `useEffect`, and after
`setInvestigationResult(data)` in `handleSubmit`, call `loadInvestigations()` (or
prepend `data` directly). Render each item using:
- title: `item.description`
- repo: `item.repository_name`
- status: `item.status`
- time: `new Date(item.created_at).toLocaleString()`

The history list endpoint persists the data, but the current frontend must be wired
to it before history appears in the UI.
