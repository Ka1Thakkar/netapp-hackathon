# Data in Motion UI (React + Vite)

Minimal frontend to visualize datasets, recommendations, and migration jobs
from the Python (policy/analytics) and Go (mover) services.

This README guides you through:
1. Goals and Features
2. Tech Stack & Rationale
3. Project Structure (proposed)
4. API Integration Strategy
5. Environment & Configuration
6. Local Development Workflow
7. Production Build & Deployment (Static Hosting / Cloud)
8. Extensibility Roadmap
9. Sample Code Snippets
10. Troubleshooting

---

## 1. Goals and Features (MVP)

- List datasets with tier, size, last access time
- Display recommendations (tier + confidence) and allow manual migration trigger
- Show migration jobs with status lifecycle
- Auto-refresh panels (polling or WebSocket in future)
- Provide simple filters and search (by name, tier, status)
- Light/dark theme toggle (optional)
- Zero-auth for hackathon demo; pluggable auth later

---

## 2. Tech Stack & Rationale

| Layer        | Choice            | Reason                                         |
|--------------|-------------------|-----------------------------------------------|
| Build Tool   | Vite              | Fast dev server, simple config, ESBuild/Rollup |
| Framework    | React             | Familiar ecosystem, component model           |
| Language     | TypeScript        | Type-safety for API responses                 |
| Styling      | Tailwind (optional) or CSS Modules | Speed + consistency  |
| Data Fetch   | fetch + SWR/React Query (optional) | Cache & revalidate  |
| State Mgmt   | Local component state (MVP)        | Minimal complexity  |
| Charts (optional) | Recharts or Chart.js         | Visual metrics      |

---

## 3. Project Structure (Proposed)

```
ui/
  src/
    api/
      client.ts          # base fetch with error handling
      datasets.ts        # typed dataset calls
      jobs.ts            # job endpoints
      reco.ts            # recommendations
    components/
      DatasetList.tsx
      RecommendationPanel.tsx
      JobTable.tsx
      TierBadge.tsx
      LoadingSpinner.tsx
      ErrorMessage.tsx
      Layout/
        AppLayout.tsx
        Header.tsx
        Footer.tsx
    hooks/
      useDatasets.ts
      useJobs.ts
      useRecommendations.ts
    pages/
      Dashboard.tsx
      Datasets.tsx
      Jobs.tsx
    utils/
      formatters.ts
      polling.ts
    styles/
      index.css
    main.tsx
    App.tsx
  index.html
  tsconfig.json
  vite.config.ts
  package.json
  .env.development
  .env.production
```

---

## 4. API Integration Strategy

Backend endpoints (default ports):
- Python API (policy): http://localhost:8080
  - GET /datasets
  - POST /datasets
  - GET /recommendations?dataset_id=...
  - POST /plan-migration
  - GET /jobs (Python-tracked jobs)
- Go mover: http://localhost:8090
  - GET /jobs
  - POST /jobs/retry/{id}

UI Approach:
- Read-only merging of job lists (Python + Go) optionally
- Trigger migration: POST /plan-migration
- After migration trigger: optimistic add job to table, then poll
- Recommendations polling interval (e.g., 15s) or manual refresh button

Error Handling:
- Unified `api/client.ts` with:
  - automatic JSON parsing
  - HTTP status check converting non-2xx to errors
  - typed response generics for TS

---

## 5. Environment & Configuration

`.env.development`:
```
VITE_API_PYTHON=http://localhost:8080
VITE_API_GO=http://localhost:8090
VITE_POLL_INTERVAL_MS=10000
VITE_ENABLE_DEBUG_LOGS=true
```

`.env.production` (example):
```
VITE_API_PYTHON=https://python.example.com
VITE_API_GO=https://go.example.com
VITE_POLL_INTERVAL_MS=15000
```

Access in code via:
```ts
const API_PY = import.meta.env.VITE_API_PYTHON;
const API_GO = import.meta.env.VITE_API_GO;
```

---

## 6. Local Development Workflow

1. Initialize:
   ```
   npm create vite@latest data-motion-ui -- --template react-ts
   cd data-motion-ui
   npm install
   npm install --save-dev tailwindcss postcss autoprefixer
   npx tailwindcss init -p
   ```
2. Configure Tailwind (optional):
   - `tailwind.config.cjs` content sources: `./index.html`, `./src/**/*.{ts,tsx}`
   - Add directives to `src/styles/index.css`: `@tailwind base; @tailwind components; @tailwind utilities;`
3. Add environment variables (`.env.development`).
4. Run dev server:
   ```
   npm run dev
   ```
5. Open http://localhost:5173 (default Vite port).

---

## 7. Production Build & Deployment

Build:
```
npm run build
```

Output:
```
dist/
```

Deploy Options:
- Static hosting (Netlify, Vercel, GitHub Pages)
- Container:
  - Multi-stage Dockerfile to copy `dist/` into nginx or caddy
- GCP:
  - Cloud Storage (static website + Cloud CDN)
  - Cloud Run (serving static through minimal server)

Example Dockerfile snippet:
```
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
```

---

## 8. Extensibility Roadmap

Short-term:
- Sort & filter datasets by tier, last access, name
- Pagination for large lists
- Inline migration trigger button on each dataset row

Medium-term:
- Real-time updates (WebSocket or SSE) for job status
- Charts (access frequency, latency distribution)
- Recommendation explanation popover (confidence, rule or feature importance)
- Auth (JWT / OIDC) once backend supports it

Long-term:
- Multi-project / tenant selector
- Policy editor UI (YAML or form)
- Historical migrations timeline view
- Cost projection overlay

---

## 9. Sample Code Snippets

### API client (`src/api/client.ts`)
```ts
export async function apiGet<T>(base: string, path: string): Promise<T> {
  const url = `${base}${path}`;
  const res = await fetch(url);
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`GET ${url} failed: ${res.status} ${text}`);
  }
  return res.json() as Promise<T>;
}

export async function apiPost<T>(base: string, path: string, body: unknown): Promise<T> {
  const url = `${base}${path}`;
  const res = await fetch(url, {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify(body)
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`POST ${url} failed: ${res.status} ${text}`);
  }
  return res.json() as Promise<T>;
}
```

### Datasets Hook (`src/hooks/useDatasets.ts`)
```ts
import { useEffect, useState } from 'react';
const API_PY = import.meta.env.VITE_API_PYTHON;

export interface Dataset {
  id: number;
  name: string;
  path_uri: string;
  current_tier: string;
  last_access_ts?: number;
  size_bytes?: number;
  latency_slo_ms?: number;
}

export function useDatasets(pollMs = Number(import.meta.env.VITE_POLL_INTERVAL_MS || 10000)) {
  const [data, setData] = useState<Dataset[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(true);

  async function fetchData() {
    try {
      setError(null);
      const res = await fetch(`${API_PY}/datasets`);
      if (!res.ok) throw new Error(`Failed: ${res.status}`);
      const json = await res.json();
      setData(json);
    } catch (e:any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    fetchData();
    const id = setInterval(fetchData, pollMs);
    return () => clearInterval(id);
  }, [pollMs]);

  return { data, error, loading, refresh: fetchData };
}
```

### Trigger Migration (`src/api/migrations.ts`)
```ts
const API_PY = import.meta.env.VITE_API_PYTHON;

export interface MigrationJob {
  job_id: string;
  dataset_id: number;
  status: string;
  submitted_at: number;
}

export async function planMigration(datasetId: number, destUri?: string, storageClass?: string): Promise<MigrationJob> {
  const body: Record<string, unknown> = { dataset_id: datasetId };
  if (destUri) body.target_location = destUri;
  if (storageClass) body.storage_class = storageClass;
  const res = await fetch(`${API_PY}/plan-migration`, {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify(body)
  });
  if (!res.ok) throw new Error(`Plan migration failed: ${res.status}`);
  return res.json();
}
```

### Formatting Helpers (`src/utils/formatters.ts`)
```ts
export function formatBytes(b?: number): string {
  if (!b) return '0 B';
  const units = ['B','KB','MB','GB','TB'];
  let i = 0;
  let val = b;
  while (val >= 1024 && i < units.length-1) {
    val /= 1024;
    i++;
  }
  return `${val.toFixed(1)} ${units[i]}`;
}

export function formatAge(ts?: number): string {
  if (!ts) return 'N/A';
  const now = Date.now()/1000;
  const age = now - ts;
  if (age < 60) return `${Math.round(age)}s ago`;
  if (age < 3600) return `${Math.round(age/60)}m ago`;
  if (age < 86400) return `${Math.round(age/3600)}h ago`;
  return `${Math.round(age/86400)}d ago`;
}
```

### Dataset List Component (`src/components/DatasetList.tsx`)
```tsx
import React from 'react';
import { useDatasets } from '../hooks/useDatasets';
import { planMigration } from '../api/migrations';
import { formatBytes, formatAge } from '../utils/formatters';

export const DatasetList: React.FC = () => {
  const { data, loading, error, refresh } = useDatasets();

  async function handleMigrate(id: number) {
    try {
      await planMigration(id);
      alert('Migration job queued.');
      refresh();
    } catch (e:any) {
      alert(`Migration failed: ${e.message}`);
    }
  }

  if (loading) return <div>Loading datasets...</div>;
  if (error) return <div>Error: {error}</div>;

  return (
    <table className="min-w-full text-sm">
      <thead>
        <tr>
          <th className="text-left">ID</th>
          <th className="text-left">Name</th>
          <th className="text-left">Tier</th>
          <th className="text-left">Size</th>
          <th className="text-left">Last Access</th>
          <th className="text-left">Actions</th>
        </tr>
      </thead>
      <tbody>
        {data.map(ds => (
          <tr key={ds.id} className="border-b">
            <td>{ds.id}</td>
            <td>{ds.name}</td>
            <td><span className={`inline-block px-2 py-1 rounded bg-slate-200`}>{ds.current_tier}</span></td>
            <td>{formatBytes(ds.size_bytes)}</td>
            <td>{formatAge(ds.last_access_ts)}</td>
            <td>
              <button
                className="px-2 py-1 bg-blue-600 text-white rounded"
                onClick={() => handleMigrate(ds.id)}
              >
                Migrate
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
};
```

---

## 10. Troubleshooting

| Issue | Cause | Resolution |
|-------|-------|------------|
| CORS errors | Backend not configured or port mismatch | Ensure FastAPI CORS allow_origins="*" in dev |
| Empty dataset list | Backend not running or wrong env var | Check `/health` endpoint and VITE_API_PYTHON value |
| Migration button fails | Kafka or API failure | Check backend logs; verify `plan-migration` returns 200 |
| build fails | Missing dependencies | `npm install`; check Node version (>=16 recommended) |
| Mixed job states | Python vs Go job sources diverge | Merge logic or display both separately with labels |

Add logging:
```ts
if (import.meta.env.VITE_ENABLE_DEBUG_LOGS === 'true') {
  console.debug('Datasets fetched', data);
}
```

---

## Next Enhancements

- Add Recommendations fetch hook and panel
- Consolidate jobs from both services (tag source)
- Add skeleton loading states
- Introduce React Query for automatic revalidation
- Add global error boundary

---

Happy building!