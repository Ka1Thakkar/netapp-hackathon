/* eslint-disable @typescript-eslint/ban-ts-comment */
import React, { useEffect, useState } from "react";
import ReactDOM from "react-dom/client";

// Provided by Vite define() in vite.config.ts (optional)
declare const __APP_BUILD_TIME__: string | undefined;

const buildInfo =
  typeof __APP_BUILD_TIME__ !== "undefined"
    ? __APP_BUILD_TIME__
    : new Date().toISOString();

const API_PY = import.meta.env.VITE_API_PYTHON ?? "/py";
const API_GO = import.meta.env.VITE_API_GO ?? "/go";

type JobEvent = {
  job_id: string;
  dataset_id?: number;
  status: string;
  error?: string | null;
  ts?: number;
};

/* Hooks moved inside App component */

function App() {
  const [events, setEvents] = useState<JobEvent[]>([]);
  const [sseStatus, setSseStatus] = useState<"connecting" | "open" | "error">(
    "connecting",
  );
  // Details drawer selection (click a row to view more)
  const [selectedJob, setSelectedJob] = useState<JobEvent | null>(null);
  const [metrics, setMetrics] = useState<string>("loading...");
  const [nginx, setNginx] = useState<{
    active: number;
    accepts: number;
    handled: number;
    requests: number;
    reading: number;
    writing: number;
    waiting: number;
  } | null>(null);
  const [reqSeries, setReqSeries] = useState<number[]>([]);
  const [nginxSeries, setNginxSeries] = useState<number[]>([]);

  // Jobs dashboard state
  const [jobs, setJobs] = useState<any[]>([]);
  const [jobsSource, setJobsSource] = useState<"python" | "go">("python");
  const [jobStatusFilter, setJobStatusFilter] = useState<string>("all");
  const [jobSearch, setJobSearch] = useState<string>("");
  const [jobsMsg, setJobsMsg] = useState<string>("");

  // Dataset creation state
  const [dsName, setDsName] = useState("demo-dataset");
  const [dsPath, setDsPath] = useState("file:///shared_storage/demo-dataset");
  const [dsSize, setDsSize] = useState<number>(1048576);
  const [dsOwner, setDsOwner] = useState<string>("");
  const [dsAutoSeed, setDsAutoSeed] = useState<boolean>(true);
  const [dsMsg, setDsMsg] = useState("");

  // Dataset list panel state
  const [datasets, setDatasets] = useState<any[]>([]);
  const [dsListMsg, setDsListMsg] = useState("");

  // Migration planning state
  const [migDatasetId, setMigDatasetId] = useState<number>(1);
  const [migTarget, setMigTarget] = useState(
    "file:///shared_storage/migrated/demo",
  );
  const [migClass, setMigClass] = useState<"hot" | "warm" | "cold">("hot");
  const [migMsg, setMigMsg] = useState("");

  // Training & recommendations
  const [trainingMsg, setTrainingMsg] = useState("");
  const [recoDatasetId, setRecoDatasetId] = useState<number | "">("");
  const [recs, setRecs] = useState<
    Array<{
      dataset_id: number;
      recommended_tier: string;
      confidence: number;
      reason?: any;
    }>
  >([]);

  // Toast (inline banner) state
  const [toast, setToast] = useState<{
    type: "success" | "error";
    msg: string;
  } | null>(null);
  const showToast = (type: "success" | "error", msg: string) => {
    setToast({ type, msg });
    setTimeout(() => setToast(null), 4000);
  };

  const pyBase = API_PY.replace(/\/$/, "");

  const createDataset = async (ev: React.FormEvent) => {
    ev.preventDefault();
    setDsMsg("Creating...");
    try {
      const resp = await fetch(`${pyBase}/datasets`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: dsName,
          path_uri: dsPath,
          size_bytes: dsSize,
          owner: dsOwner || undefined,
          auto_seed: dsAutoSeed,
        }),
      });
      const body = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        setDsMsg(
          `Error (${resp.status}): ${
            body.detail || JSON.stringify(body).slice(0, 120)
          }`,
        );
      } else {
        const seedInfo =
          body && typeof body === "object" && body.seed_attempted !== undefined
            ? ` [seed: ${
                body.seed_created
                  ? "created"
                  : body.seed_error
                    ? `error: ${String(body.seed_error).slice(0, 80)}`
                    : "unknown"
              }]`
            : "";
        setDsMsg(`Created dataset id=${body.id}${seedInfo}`);
      }
    } catch (e: any) {
      setDsMsg(`Error: ${String(e)}`);
    }
  };

  const planMigration = async (ev: React.FormEvent) => {
    ev.preventDefault();
    setMigMsg("Planning...");
    try {
      const resp = await fetch(`${pyBase}/plan-migration`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          dataset_id: migDatasetId,
          target_location: migTarget,
          storage_class: migClass,
        }),
      });
      const body = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        setMigMsg(
          `Error (${resp.status}): ${
            body.detail || JSON.stringify(body).slice(0, 120)
          }`,
        );
      } else {
        setMigMsg(`Enqueued job ${body.job_id} (status=${body.status})`);
      }
    } catch (e: any) {
      setMigMsg(`Error: ${String(e)}`);
    }
  };

  const trainModel = async () => {
    setTrainingMsg("Training...");
    try {
      const resp = await fetch(`${pyBase}/train`, { method: "POST" });
      const body = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        setTrainingMsg(
          `Error (${resp.status}): ${
            body.detail || JSON.stringify(body).slice(0, 120)
          }`,
        );
      } else {
        setTrainingMsg(`Trained model version=${body.model_version}`);
      }
    } catch (e: any) {
      setTrainingMsg(`Error: ${String(e)}`);
    }
  };

  const listRecommendations = async () => {
    try {
      const q = recoDatasetId === "" ? "" : `?dataset_id=${recoDatasetId}`;
      const resp = await fetch(`${pyBase}/recommendations${q}`);
      const body = await resp.json();
      if (Array.isArray(body)) {
        setRecs(body);
      } else {
        setRecs([]);
      }
    } catch {
      setRecs([]);
    }
  };

  useEffect(() => {
    // SSE stream for live updates with heartbeat and auto-reconnect
    const url = `${API_PY.replace(/\/$/, "")}/events`;
    let es: EventSource | null = null;
    let reconnectTimer: number | null = null;
    let heartbeatTimer: number | null = null;
    let lastBeat = Date.now();
    let cancelled = false;

    const connect = () => {
      if (cancelled) return;
      setSseStatus("connecting");
      es = new EventSource(url);

      es.onopen = () => {
        setSseStatus("open");
        lastBeat = Date.now();
        if (heartbeatTimer) {
          window.clearInterval(heartbeatTimer);
        }
        // Heartbeat checker: if no events for 30s, reconnect
        heartbeatTimer = window.setInterval(() => {
          if (Date.now() - lastBeat > 30000) {
            try {
              es?.close();
            } catch {
              /* ignore */
            }
            es = null;
            connect();
          }
        }, 10000);
      };

      const jobHandler = (ev: MessageEvent) => {
        try {
          lastBeat = Date.now();
          const data: JobEvent & { type?: string } = JSON.parse(ev.data);
          if (data && data.type === "job_status") {
            setEvents((prev: JobEvent[]) => [data, ...prev].slice(0, 50));
          }
        } catch {
          /* ignore */
        }
      };

      es.addEventListener("job_status", jobHandler as any);

      es.onerror = () => {
        setSseStatus("error");
        try {
          es?.close();
        } catch {
          /* ignore */
        }
        es = null;
        if (!reconnectTimer) {
          reconnectTimer = window.setTimeout(() => {
            reconnectTimer = null;
            connect();
          }, 2000);
        }
      };
    };

    connect();

    return () => {
      cancelled = true;
      if (reconnectTimer) window.clearTimeout(reconnectTimer);
      if (heartbeatTimer) window.clearInterval(heartbeatTimer);
      try {
        es?.close();
      } catch {
        /* ignore */
      }
      es = null;
    };
  }, []);
  useEffect(() => {
    let cancelled = false;

    const fetchMetrics = async () => {
      try {
        const resp = await fetch(`${API_PY.replace(/\/$/, "")}/metrics`);
        const text = await resp.text();
        if (!cancelled) {
          setMetrics(text);
          try {
            const sum = text
              .split("\n")
              .filter((l) => l.startsWith("request_count_total"))
              .map((l) => {
                const parts = l.trim().split(" ");
                return Number(parts[parts.length - 1]) || 0;
              })
              .reduce((a, b) => a + b, 0);
            setReqSeries((prev) => [...prev.slice(-29), sum]);
          } catch (e) {
            /* ignore */
          }
        }
      } catch {
        if (!cancelled) setMetrics("error");
      }
    };

    const fetchNginx = async () => {
      try {
        const resp = await fetch(`/nginx_status`);
        const raw = await resp.text();
        const lines = raw.trim().split("\n");
        let active = 0,
          accepts = 0,
          handled = 0,
          requests = 0,
          reading = 0,
          writing = 0,
          waiting = 0;
        for (const line of lines) {
          if (line.startsWith("Active connections:")) {
            const parts = line.split(":");
            active = Number((parts[1] || "").trim()) || 0;
          } else if (/^\d+\s+\d+\s+\d+$/.test(line.trim())) {
            const parts = line.trim().split(/\s+/);
            if (parts.length >= 3) {
              accepts = Number(parts[0]) || 0;
              handled = Number(parts[1]) || 0;
              requests = Number(parts[2]) || 0;
            }
          } else if (line.startsWith("Reading:")) {
            const m = line.match(
              /Reading:\s*(\d+)\s*Writing:\s*(\d+)\s*Waiting:\s*(\d+)/,
            );
            if (m) {
              reading = Number(m[1]) || 0;
              writing = Number(m[2]) || 0;
              waiting = Number(m[3]) || 0;
            }
          }
        }
        if (!cancelled) {
          setNginx({
            active,
            accepts,
            handled,
            requests,
            reading,
            writing,
            waiting,
          });
          try {
            setNginxSeries((prev) => [...prev.slice(-29), active]);
          } catch (e) {
            /* ignore */
          }
        }
      } catch {
        if (!cancelled) setNginx(null);
      }
    };

    fetchMetrics();
    fetchNginx();
    const t = window.setInterval(() => {
      fetchMetrics();
      fetchNginx();
    }, 10000);

    return () => {
      cancelled = true;
      window.clearInterval(t);
    };
  }, []);

  return (
    <React.StrictMode>
      <div style={containerStyle}>
        <header style={headerStyle}>
          <h1 style={{ margin: 0, fontSize: "1.15rem" }}>Data in Motion UI</h1>
          <div style={{ opacity: 0.75, fontSize: "0.8rem" }}>
            Build: {buildInfo}
          </div>
          {toast && (
            <div
              style={{
                marginLeft: "auto",
                padding: "0.35rem 0.6rem",
                borderRadius: "6px",
                background: toast.type === "success" ? "#1b5e20" : "#7f1d1d",
                color: "#fff",
                fontSize: "0.75rem",
                boxShadow: "0 0 0 1px rgba(0,0,0,0.4)",
              }}
            >
              {toast.msg}
            </div>
          )}
        </header>

        <main style={mainStyle}>
          <section style={cardStyle}>
            <h2 style={h2Style}>Create Dataset</h2>
            <form
              onSubmit={createDataset}
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit,minmax(220px,1fr))",
                gap: "0.6rem",
              }}
            >
              <label style={{ display: "grid", gap: "0.25rem" }}>
                <span>Name</span>
                <input
                  value={dsName}
                  onChange={(e) => setDsName(e.target.value)}
                  required
                />
              </label>
              <label style={{ display: "grid", gap: "0.25rem" }}>
                <span>Path URI</span>
                <input
                  value={dsPath}
                  onChange={(e) => setDsPath(e.target.value)}
                  required
                />
              </label>
              <label style={{ display: "grid", gap: "0.25rem" }}>
                <span>Size (bytes)</span>
                <input
                  type="number"
                  value={dsSize}
                  onChange={(e) => setDsSize(Number(e.target.value || 0))}
                  min={0}
                />
              </label>
              <label style={{ display: "grid", gap: "0.25rem" }}>
                <span>Owner (optional)</span>
                <input
                  value={dsOwner}
                  onChange={(e) => setDsOwner(e.target.value)}
                />
              </label>
              <label
                style={{
                  display: "flex",
                  gap: "0.5rem",
                  alignItems: "center",
                  paddingTop: "0.2rem",
                }}
              >
                <input
                  type="checkbox"
                  checked={dsAutoSeed}
                  onChange={(e) => setDsAutoSeed(e.target.checked)}
                />
                <span style={{ fontSize: "0.85rem" }}>
                  Auto‑seed local file (demo)
                </span>
              </label>
              <div style={{ display: "flex", alignItems: "flex-end" }}>
                <button type="submit">Create</button>
              </div>
            </form>
            {dsMsg && (
              <p style={{ ...pStyle, marginTop: "0.5rem" }}>
                <strong>{dsMsg}</strong>
              </p>
            )}
          </section>

          <section style={cardStyle}>
            <h2 style={h2Style}>Plan Migration</h2>
            <form
              onSubmit={planMigration}
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit,minmax(220px,1fr))",
                gap: "0.6rem",
              }}
            >
              <label style={{ display: "grid", gap: "0.25rem" }}>
                <span>Dataset ID</span>
                <input
                  type="number"
                  value={migDatasetId}
                  onChange={(e) => setMigDatasetId(Number(e.target.value || 0))}
                  min={1}
                  required
                />
              </label>
              <label style={{ display: "grid", gap: "0.25rem" }}>
                <span>Target Location</span>
                <input
                  value={migTarget}
                  onChange={(e) => setMigTarget(e.target.value)}
                  required
                />
              </label>
              <label style={{ display: "grid", gap: "0.25rem" }}>
                <span>Storage Class</span>
                <select
                  value={migClass}
                  onChange={(e) =>
                    setMigClass(e.target.value as "hot" | "warm" | "cold")
                  }
                >
                  <option value="hot">hot</option>
                  <option value="warm">warm</option>
                  <option value="cold">cold</option>
                </select>
              </label>
              <div style={{ display: "flex", alignItems: "flex-end" }}>
                <button type="submit">Enqueue</button>
              </div>
            </form>
            {migMsg && (
              <p style={{ ...pStyle, marginTop: "0.5rem" }}>
                <strong>{migMsg}</strong>
              </p>
            )}
          </section>

          <section style={cardStyle}>
            <h2 style={h2Style}>Model Training & Recommendations</h2>
            <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
              <button onClick={trainModel}>Train Model</button>
              {trainingMsg && (
                <span style={{ opacity: 0.85 }}>{trainingMsg}</span>
              )}
            </div>
            <div
              style={{
                display: "flex",
                gap: "0.75rem",
                marginTop: "0.6rem",
                flexWrap: "wrap",
                alignItems: "center",
              }}
            >
              <label
                style={{ display: "flex", gap: "0.4rem", alignItems: "center" }}
              >
                <span style={{ fontSize: "0.85rem" }}>
                  Dataset ID (optional)
                </span>
                <input
                  type="number"
                  value={recoDatasetId === "" ? "" : recoDatasetId}
                  onChange={(e) =>
                    setRecoDatasetId(
                      e.target.value === "" ? "" : Number(e.target.value),
                    )
                  }
                  style={{ width: "6rem" }}
                />
              </label>
              <button onClick={listRecommendations}>
                List Recommendations
              </button>
            </div>
            {recs.length > 0 && (
              <div
                style={{
                  marginTop: "0.75rem",
                  border: "1px solid #2a313b",
                  borderRadius: "6px",
                  overflow: "hidden",
                }}
              >
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "1fr 1fr 1fr 2fr",
                    fontSize: "0.75rem",
                    opacity: 0.75,
                    padding: "0.35rem 0.5rem",
                    background: "#121418",
                    borderBottom: "1px solid #2a313b",
                  }}
                >
                  <div>dataset</div>
                  <div>tier</div>
                  <div>confidence</div>
                  <div>reason</div>
                </div>
                {recs.map((r, i) => (
                  <div
                    key={i}
                    style={{
                      display: "grid",
                      gridTemplateColumns: "1fr 1fr 1fr 2fr",
                      fontSize: "0.75rem",
                      gap: "0.5rem",
                      padding: "0.35rem 0.5rem",
                      borderBottom: "1px dotted #2a313b",
                    }}
                  >
                    <div>{r.dataset_id}</div>
                    <div>{r.recommended_tier}</div>
                    <div>{Math.round(r.confidence * 100)}%</div>
                    <div
                      title={r.reason ? JSON.stringify(r.reason) : ""}
                      style={{
                        whiteSpace: "nowrap",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                      }}
                    >
                      {r.reason ? JSON.stringify(r.reason) : ""}
                    </div>
                  </div>
                ))}
              </div>
            )}
            {recs.length === 0 && (
              <p style={{ ...pStyle, opacity: 0.7, marginTop: "0.6rem" }}>
                No recommendations loaded yet.
              </p>
            )}
          </section>

          <section style={cardStyle}>
            <h2 style={h2Style}>Datasets</h2>
            <div
              style={{
                display: "flex",
                gap: "0.6rem",
                flexWrap: "wrap",
                marginBottom: "0.6rem",
              }}
            >
              <button
                onClick={async () => {
                  setDsListMsg("Loading...");
                  try {
                    const resp = await fetch(`${pyBase}/datasets`);
                    const body = await resp.json();
                    if (Array.isArray(body)) {
                      setDatasets(body);
                      setDsListMsg(`Loaded ${body.length} dataset(s)`);
                      showToast("success", "Datasets refreshed");
                    } else {
                      setDatasets([]);
                      setDsListMsg("Loaded 0 dataset(s)");
                      showToast("error", "Unexpected datasets response");
                    }
                  } catch (e: any) {
                    setDsListMsg(`Error: ${String(e)}`);
                    setDatasets([]);
                    showToast("error", "Failed to load datasets");
                  }
                }}
              >
                Refresh
              </button>
              {dsListMsg && (
                <span style={{ opacity: 0.8, fontSize: "0.85rem" }}>
                  {dsListMsg}
                </span>
              )}
            </div>
            <div
              style={{
                border: "1px solid #2a313b",
                borderRadius: "6px",
                overflow: "hidden",
              }}
            >
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "2fr 2fr 1fr 1fr 1fr",
                  gap: "0.5rem",
                  fontSize: "0.75rem",
                  padding: "0.4rem 0.6rem",
                  background: "#121418",
                  borderBottom: "1px solid #2a313b",
                  opacity: 0.75,
                }}
              >
                <div>name</div>
                <div>path</div>
                <div>tier</div>
                <div>size</div>
                <div>last access</div>
              </div>
              <div>
                {datasets.length === 0 && (
                  <div
                    style={{
                      padding: "0.55rem",
                      fontSize: "0.75rem",
                      opacity: 0.65,
                    }}
                  >
                    No datasets.
                  </div>
                )}
                {datasets.map((d) => (
                  <div
                    key={d.id}
                    style={{
                      display: "grid",
                      gridTemplateColumns: "2fr 2fr 1fr 1fr 1fr",
                      gap: "0.5rem",
                      fontSize: "0.75rem",
                      padding: "0.45rem 0.6rem",
                      borderBottom: "1px dotted #2a313b",
                      alignItems: "center",
                    }}
                  >
                    <div
                      style={{
                        whiteSpace: "nowrap",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                      }}
                      title={d.name}
                    >
                      {d.name}
                    </div>
                    <div
                      style={{
                        whiteSpace: "nowrap",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                      }}
                      title={d.path_uri}
                    >
                      {d.path_uri}
                    </div>
                    <div>{d.current_tier}</div>
                    <div>
                      {d.size_bytes != null
                        ? Math.round(d.size_bytes / 1024) + " KiB"
                        : ""}
                    </div>
                    <div>
                      {d.last_access_ts
                        ? new Date(d.last_access_ts * 1000).toLocaleTimeString()
                        : "—"}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </section>

          <section style={cardStyle}>
            <h2 style={h2Style}>Access Events</h2>
            <form
              onSubmit={async (ev) => {
                ev.preventDefault();
                const form = ev.currentTarget as HTMLFormElement;
                const fd = new FormData(form);
                const dataset_id = Number(fd.get("dataset_id") || 0);
                const op = String(fd.get("op") || "read");
                const size_bytes = Number(fd.get("size_bytes") || 0);
                const latRaw = fd.get("client_lat_ms");
                const client_lat_ms =
                  latRaw != null && String(latRaw).length > 0
                    ? Number(latRaw)
                    : undefined;
                try {
                  const resp = await fetch(`${pyBase}/access-events`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                      dataset_id,
                      op,
                      size_bytes,
                      client_lat_ms,
                    }),
                  });
                  const body = await resp.json().catch(() => ({}));
                  if (!resp.ok) {
                    showToast(
                      "error",
                      `Access event error (${resp.status}): ${
                        body.detail || JSON.stringify(body).slice(0, 120)
                      }`,
                    );
                  } else {
                    showToast(
                      "success",
                      `Access event queued for dataset ${dataset_id}`,
                    );
                    // Refresh datasets so last_access_ts is visible
                    try {
                      const r2 = await fetch(`${pyBase}/datasets`);
                      const b2 = await r2.json();
                      if (Array.isArray(b2)) {
                        setDatasets(b2);
                        setDsListMsg(`Loaded ${b2.length} dataset(s)`);
                      }
                    } catch {
                      /* ignore */
                    }
                    try {
                      form.reset();
                    } catch {
                      /* ignore */
                    }
                  }
                } catch (e: any) {
                  showToast("error", `Access event error: ${String(e)}`);
                }
              }}
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit,minmax(220px,1fr))",
                gap: "0.6rem",
              }}
            >
              <label style={{ display: "grid", gap: "0.25rem" }}>
                <span>Dataset ID</span>
                <input name="dataset_id" type="number" min={1} required />
              </label>
              <label style={{ display: "grid", gap: "0.25rem" }}>
                <span>Operation</span>
                <select name="op" defaultValue="read">
                  <option value="read">read</option>
                  <option value="write">write</option>
                </select>
              </label>
              <label style={{ display: "grid", gap: "0.25rem" }}>
                <span>Size (bytes)</span>
                <input
                  name="size_bytes"
                  type="number"
                  min={0}
                  defaultValue={4096}
                />
              </label>
              <label style={{ display: "grid", gap: "0.25rem" }}>
                <span>Client Latency (ms, optional)</span>
                <input name="client_lat_ms" type="number" min={0} step="0.1" />
              </label>
              <div style={{ display: "flex", alignItems: "flex-end" }}>
                <button type="submit">Post Event</button>
              </div>
            </form>
            <p style={{ ...pStyle, marginTop: "0.5rem", opacity: 0.8 }}>
              Posting an access event updates last_access_ts and helps training.
            </p>
          </section>

          {/*<section style={cardStyle}>
            <h2 style={h2Style}>Welcome</h2>
            <p style={pStyle}>
              This is a minimal React entry point. You can now build out pages,
              components, hooks, and API clients under <code>src/</code>.
            </p>
            <ul style={ulStyle}>
              <li>
                Python API base: <code>{String(API_PY)}</code>
              </li>
              <li>
                Go API base: <code>{String(API_GO)}</code>
              </li>
            </ul>
          </section>

          <section style={cardStyle}>
            <h2 style={h2Style}>Next Steps</h2>
            <ol style={olStyle}>
              <li>
                Add a root element in your HTML (e.g.{" "}
                <code>&lt;div id="root"&gt;</code>).
              </li>
              <li>
                Create pages/components (e.g. <code>src/App.tsx</code>) and
                route rendering through this entry point.
              </li>
              <li>
                Integrate API calls to the Python and Go services (configure via{" "}
                <code>.env</code> or Vite env variables).
              </li>
              <li>
                Expose Prometheus metrics (python_api, nginx stub_status) and
                surface basic charts in the UI.
              </li>
              <li>
                Add Server-Sent Events (SSE) stream (e.g.{" "}
                <code>/py/events</code>) for live job status updates.
              </li>
              <li>
                (Optional) Upgrade to a WebSocket channel for bi-directional
                control (cancel / retry migrations).
              </li>
              <li>
                Build a real-time Jobs dashboard fed by streaming events instead
                of polling.
              </li>
            </ol>
          </section>*/}

          <section style={cardStyle}>
            <h2 style={h2Style}>Service Metrics</h2>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "1fr 1fr",
                gap: "0.75rem",
              }}
            >
              <div>
                <h3 style={{ margin: "0 0 0.5rem", fontSize: "0.95rem" }}>
                  Python API (/py/metrics)
                </h3>
                <pre
                  style={{
                    maxHeight: "180px",
                    overflow: "auto",
                    background: "#121418",
                    padding: "0.5rem",
                    borderRadius: "6px",
                  }}
                >
                  {metrics}
                </pre>
                <div style={{ marginTop: "0.4rem" }}>
                  <svg width={180} height={40}>
                    <path
                      d={(() => {
                        const d = reqSeries;
                        const n = d.length;
                        if (n === 0) return "";
                        const max = Math.max(...d);
                        const min = Math.min(...d);
                        const w = 180;
                        const h = 40;
                        const sx = n > 1 ? w / (n - 1) : w;
                        const sy = (v: number) =>
                          max === min
                            ? h / 2
                            : h - ((v - min) / (max - min)) * h;
                        return d
                          .map(
                            (v, i) =>
                              `${i === 0 ? "M" : "L"}${i * sx},${sy(v)}`,
                          )
                          .join(" ");
                      })()}
                      stroke="#4caf50"
                      fill="none"
                      strokeWidth={1.5}
                    />
                  </svg>
                </div>
              </div>
              <div>
                <h3 style={{ margin: "0 0 0.5rem", fontSize: "0.95rem" }}>
                  Nginx (/nginx_status)
                </h3>
                {nginx ? (
                  <div>
                    <div
                      style={{
                        display: "grid",
                        gridTemplateColumns: "1fr 1fr",
                        gap: "0.25rem 0.75rem",
                        fontSize: "0.9rem",
                      }}
                    >
                      <div>active</div>
                      <div>{nginx.active}</div>
                      <div>accepts</div>
                      <div>{nginx.accepts}</div>
                      <div>handled</div>
                      <div>{nginx.handled}</div>
                      <div>requests</div>
                      <div>{nginx.requests}</div>
                      <div>reading</div>
                      <div>{nginx.reading}</div>
                      <div>writing</div>
                      <div>{nginx.writing}</div>
                      <div>waiting</div>
                      <div>{nginx.waiting}</div>
                    </div>
                    <div style={{ marginTop: "0.4rem" }}>
                      <svg width={180} height={40}>
                        <path
                          d={(() => {
                            const d = nginxSeries;
                            const n = d.length;
                            if (n === 0) return "";
                            const max = Math.max(...d);
                            const min = Math.min(...d);
                            const w = 180;
                            const h = 40;
                            const sx = n > 1 ? w / (n - 1) : w;
                            const sy = (v: number) =>
                              max === min
                                ? h / 2
                                : h - ((v - min) / (max - min)) * h;
                            return d
                              .map(
                                (v, i) =>
                                  `${i === 0 ? "M" : "L"}${i * sx},${sy(v)}`,
                              )
                              .join(" ");
                          })()}
                          stroke="#03a9f4"
                          fill="none"
                          strokeWidth={1.5}
                        />
                      </svg>
                    </div>
                  </div>
                ) : (
                  <div style={{ opacity: 0.7 }}>unavailable</div>
                )}
              </div>
            </div>

            {selectedJob && (
              <div
                style={{
                  marginTop: "0.6rem",
                  borderTop: "1px solid #2a313b",
                  paddingTop: "0.6rem",
                }}
              >
                <h3 style={{ margin: "0 0 0.4rem", fontSize: "0.95rem" }}>
                  Job Details
                </h3>
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "1fr 3fr",
                    rowGap: "0.3rem",
                    columnGap: "0.8rem",
                    fontSize: "0.9rem",
                  }}
                >
                  <div style={{ opacity: 0.8 }}>Job ID</div>
                  <div style={{ wordBreak: "break-all" }}>
                    {selectedJob.job_id}
                  </div>

                  <div style={{ opacity: 0.8 }}>Dataset ID</div>
                  <div>{String(selectedJob.dataset_id ?? "")}</div>

                  <div style={{ opacity: 0.8 }}>Status</div>
                  <div>{selectedJob.status}</div>

                  <div style={{ opacity: 0.8 }}>Error</div>
                  <div style={{ whiteSpace: "pre-wrap" }}>
                    {selectedJob.error ?? ""}
                  </div>

                  <div style={{ opacity: 0.8 }}>Time</div>
                  <div>
                    {new Date(
                      selectedJob.ts ? selectedJob.ts * 1000 : Date.now(),
                    ).toLocaleString()}
                  </div>
                </div>

                <div style={{ marginTop: "0.6rem" }}>
                  <button onClick={() => setSelectedJob(null)}>Close</button>
                </div>
              </div>
            )}
          </section>
          <section style={cardStyle}>
            <h2 style={h2Style}>Jobs Dashboard</h2>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "1fr",
                gap: "0.5rem",
              }}
            >
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "auto auto auto 1fr auto",
                  gap: "0.5rem",
                  alignItems: "center",
                }}
              >
                <label
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: "0.25rem",
                  }}
                >
                  <span>Source</span>
                  <select
                    value={jobsSource}
                    onChange={(e) => setJobsSource(e.target.value as any)}
                  >
                    <option value="python">python</option>
                    <option value="go">go</option>
                  </select>
                </label>

                <label
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: "0.25rem",
                  }}
                >
                  <span>Status</span>
                  <select
                    value={jobStatusFilter}
                    onChange={(e) => setJobStatusFilter(e.target.value)}
                  >
                    <option value="all">all</option>
                    <option value="queued">queued</option>
                    <option value="copying">copying</option>
                    <option value="verifying">verifying</option>
                    <option value="switching">switching</option>
                    <option value="completed">completed</option>
                    <option value="failed">failed</option>
                  </select>
                </label>

                <label
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: "0.25rem",
                  }}
                >
                  <span>Search</span>
                  <input
                    placeholder="job id or dataset id"
                    value={jobSearch}
                    onChange={(e) => setJobSearch(e.target.value)}
                    style={{ width: "16rem" }}
                  />
                </label>

                <div />

                <button
                  onClick={async () => {
                    try {
                      setJobsMsg("Loading...");
                      const base =
                        jobsSource === "python"
                          ? API_PY.replace(/\/$/, "")
                          : API_GO.replace(/\/$/, "");
                      const resp = await fetch(`${base}/jobs`);
                      const body = await resp.json();
                      if (Array.isArray(body)) {
                        setJobs(body);
                        setJobsMsg(`Loaded ${body.length} jobs`);
                      } else {
                        setJobs([]);
                        setJobsMsg(`Loaded 0 jobs`);
                      }
                    } catch (e: any) {
                      setJobsMsg(`Error: ${String(e)}`);
                      setJobs([]);
                    }
                  }}
                >
                  Refresh
                </button>
              </div>

              {jobsMsg && <div style={{ opacity: 0.8 }}>{jobsMsg}</div>}

              <div
                style={{
                  border: "1px solid #2a313b",
                  borderRadius: "6px",
                  overflow: "hidden",
                }}
              >
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "2fr 1fr 1fr 1fr 1fr",
                    padding: "0.4rem 0.6rem",
                    background: "#121418",
                    borderBottom: "1px solid #2a313b",
                    fontSize: "0.85rem",
                    opacity: 0.85,
                  }}
                >
                  <div>job</div>
                  <div>dataset</div>
                  <div>status</div>
                  <div>submitted</div>
                  <div>actions</div>
                </div>
                <div>
                  {jobs
                    .filter((j) => {
                      if (jobStatusFilter !== "all") {
                        if (String(j.status) !== jobStatusFilter) return false;
                      }
                      if (!jobSearch) return true;
                      const s = jobSearch.toLowerCase();
                      return (
                        String(j.job_id || "")
                          .toLowerCase()
                          .includes(s) ||
                        String(j.dataset_id || "")
                          .toLowerCase()
                          .includes(s)
                      );
                    })
                    .map((j, idx) => (
                      <div
                        key={`${j.job_id || idx}-${j.status}`}
                        style={{
                          display: "grid",
                          gridTemplateColumns: "2fr 1fr 1fr 1fr 1fr",
                          padding: "0.45rem 0.6rem",
                          borderBottom: "1px dotted #2a313b",
                          fontSize: "0.9rem",
                          alignItems: "center",
                        }}
                      >
                        <div
                          title={j.job_id}
                          style={{
                            whiteSpace: "nowrap",
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                          }}
                        >
                          {j.job_id}
                        </div>
                        <div>{String(j.dataset_id ?? "")}</div>
                        <div>{String(j.status)}</div>
                        <div>
                          {j.submitted_at
                            ? new Date(
                                Number(j.submitted_at) * 1000,
                              ).toLocaleTimeString()
                            : ""}
                        </div>
                        <div style={{ display: "flex", gap: "0.4rem" }}>
                          {/* Retry (Go mover) */}
                          <button
                            disabled={
                              jobsSource !== "go" ||
                              String(j.status) !== "failed"
                            }
                            title={
                              jobsSource !== "go"
                                ? "Retry only supported on Go mover"
                                : "Retry failed job"
                            }
                            onClick={async () => {
                              try {
                                const base = API_GO.replace(/\/$/, "");
                                const res = await fetch(
                                  `${base}/jobs/retry/${j.job_id}`,
                                  { method: "POST" },
                                );
                                if (!res.ok) {
                                  alert(`Retry failed (${res.status})`);
                                } else {
                                  alert("Retry requested");
                                }
                              } catch (e: any) {
                                alert(`Retry error: ${String(e)}`);
                              }
                            }}
                          >
                            Retry
                          </button>

                          {/* Cancel (Python API demo stub) */}
                          <button
                            disabled={
                              jobsSource !== "python" ||
                              String(j.status) === "completed"
                            }
                            title={
                              jobsSource !== "python"
                                ? "Cancel demo is wired on Python API"
                                : "Cancel job (demo)"
                            }
                            onClick={async () => {
                              try {
                                const base = API_PY.replace(/\/$/, "");
                                const res = await fetch(
                                  `${base}/jobs/cancel/${j.job_id}`,
                                  { method: "POST" },
                                );
                                if (!res.ok) {
                                  alert(`Cancel failed (${res.status})`);
                                } else {
                                  alert("Cancel requested");
                                }
                              } catch (e: any) {
                                alert(`Cancel error: ${String(e)}`);
                              }
                            }}
                          >
                            Cancel
                          </button>
                        </div>
                      </div>
                    ))}
                  {jobs.length === 0 && (
                    <div style={{ padding: "0.6rem", opacity: 0.7 }}>
                      No jobs.
                    </div>
                  )}
                </div>
              </div>
            </div>
          </section>

          <section style={cardStyle}>
            <h2 style={h2Style}>Live Job Status (SSE)</h2>
            <p style={pStyle}>
              Streaming from <code>{String(API_PY)}/events</code>. Status:{" "}
              <strong>{sseStatus}</strong>
            </p>
            <div
              style={{
                maxHeight: "240px",
                overflow: "auto",
                border: "1px solid #2a313b",
                borderRadius: "6px",
                padding: "0.5rem",
              }}
            >
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "1fr 1fr 1fr 1fr 1fr",
                  fontSize: "0.85rem",
                  opacity: 0.8,
                  paddingBottom: "0.25rem",
                  borderBottom: "1px solid #2a313b",
                }}
              >
                <div>time</div>
                <div>job</div>
                <div>dataset</div>
                <div>status</div>
                <div>error</div>
              </div>
              {events.length === 0 ? (
                <div style={{ opacity: 0.7, padding: "0.5rem" }}>
                  No events yet.
                </div>
              ) : (
                events.map((e, idx) => (
                  <div
                    key={idx}
                    onClick={() => setSelectedJob(e)}
                    style={{
                      display: "grid",
                      gridTemplateColumns: "1fr 1fr 1fr 1fr 1fr",
                      gap: "0.5rem",
                      fontSize: "0.85rem",
                      padding: "0.25rem 0",
                      borderBottom: "1px dotted #2a313b",
                      cursor: "pointer",
                      background:
                        selectedJob && selectedJob.job_id === e.job_id
                          ? "#18202a"
                          : "transparent",
                    }}
                    title="Click to view details"
                  >
                    <div>
                      {new Date(
                        e.ts ? e.ts * 1000 : Date.now(),
                      ).toLocaleTimeString()}
                    </div>
                    <div
                      style={{
                        whiteSpace: "nowrap",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                      }}
                      title={e.job_id}
                    >
                      {e.job_id}
                    </div>
                    <div>{String(e.dataset_id ?? "")}</div>
                    <div>{String(e.status)}</div>
                    <div
                      style={{
                        whiteSpace: "nowrap",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                      }}
                      title={e.error ?? ""}
                    >
                      {e.error ?? ""}
                    </div>
                  </div>
                ))
              )}
            </div>
          </section>
        </main>

        <footer style={footerStyle}>
          <small>
            Hackathon Prototype • <span>{new Date().toLocaleString()}</span>
          </small>
        </footer>
      </div>
    </React.StrictMode>
  );
}

// Inline styles for a minimal, dependency-free scaffold
const containerStyle: React.CSSProperties = {
  fontFamily:
    "system-ui, -apple-system, 'Segoe UI', Roboto, Ubuntu, Cantarell, 'Helvetica Neue', Arial, 'Noto Sans', 'Apple Color Emoji', 'Segoe UI Emoji', 'Segoe UI Symbol', sans-serif",
  color: "#e6e9ed",
  background: "#0f1115",
  minHeight: "100vh",
  display: "grid",
  gridTemplateRows: "auto 1fr auto",
};

const headerStyle: React.CSSProperties = {
  padding: "0.75rem 1rem",
  background: "#121418",
  borderBottom: "1px solid #2a313b",
  display: "flex",
  alignItems: "baseline",
  gap: "0.75rem",
};

const mainStyle: React.CSSProperties = {
  padding: "1rem",
  display: "grid",
  gap: "1rem",
  gridTemplateColumns: "1fr",
  maxWidth: "960px",
  width: "100%",
  margin: "0 auto",
};

const cardStyle: React.CSSProperties = {
  background: "#1c2026",
  border: "1px solid #2a313b",
  borderRadius: "8px",
  padding: "0.9rem 1rem 1.1rem",
};

const h2Style: React.CSSProperties = {
  margin: "0 0 0.65rem",
  fontSize: "1rem",
};

const pStyle: React.CSSProperties = {
  margin: "0 0 0.65rem",
  lineHeight: 1.5,
};

const ulStyle: React.CSSProperties = {
  margin: "0.25rem 0 0",
  paddingLeft: "1.25rem",
  lineHeight: 1.6,
};

const olStyle: React.CSSProperties = {
  margin: "0.25rem 0 0",
  paddingLeft: "1.25rem",
  lineHeight: 1.6,
};

const footerStyle: React.CSSProperties = {
  padding: "0.6rem 1rem",
  background: "#121418",
  borderTop: "1px solid #2a313b",
  textAlign: "center",
  fontSize: "0.8rem",
  opacity: 0.9,
};

const rootEl = document.getElementById("root");
if (!rootEl) {
  // If no root element is present, create and append one for convenience.
  const created = document.createElement("div");
  created.id = "root";
  document.body.appendChild(created);
}

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <App />,
);
