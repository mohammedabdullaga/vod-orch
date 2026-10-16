import { useCallback, useEffect, useRef, useState } from "react";
import axios from "axios";
import ContentBrowser from "./ContentBrowser";
import {
  Activity, Check, CircleAlert, Clapperboard, ClipboardCopy, Film, Gauge, LogOut, LockKeyhole,
  Play, Plus, Radio, RefreshCw, Server, Settings2, Terminal, Trash2, Tv, Wifi, X, XCircle,
} from "lucide-react";

const api = axios.create({ baseURL: "/api", withCredentials: true });
let csrfToken = null;
api.interceptors.request.use((config) => {
  if (csrfToken) config.headers["X-CSRF-Token"] = csrfToken;
  return config;
});
api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401 && !["/auth/login", "/auth/session"].includes(error.config?.url)) {
      csrfToken = null;
      window.dispatchEvent(new Event("admin-session-expired"));
    }
    return Promise.reject(error);
  },
);
const tabs = ["Overview & Queue", "Browse & Ingest", "Category Watcher", "Settings & Accounts"];

function Pill({ children, tone = "slate" }) {
  const tones = {
    slate: "bg-slate-800 text-slate-300", green: "bg-emerald-400/10 text-emerald-300",
    blue: "bg-blue-400/10 text-blue-300", yellow: "bg-amber-400/10 text-amber-300",
    red: "bg-rose-400/10 text-rose-300",
  };
  return <span className={`inline-flex rounded-full px-2.5 py-1 text-xs font-semibold ${tones[tone]}`}>{children}</span>;
}

function formatBytes(value) {
  if (value == null || !Number.isFinite(Number(value))) return "Size pending";
  const bytes = Number(value);
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let size = bytes;
  let unitIndex = -1;
  do {
    size /= 1000;
    unitIndex += 1;
  } while (size >= 1000 && unitIndex < units.length - 1);
  return `${size.toFixed(2)} ${units[unitIndex]}`;
}

function formatSpeed(value) {
  if (value == null || !Number.isFinite(Number(value))) return "Speed pending";
  return `${formatBytes(Number(value))}/s`;
}

function App() {
  const [authenticated, setAuthenticated] = useState(null);
  const [adminUsername, setAdminUsername] = useState("");
  const [tab, setTab] = useState(tabs[0]);
  const [queueFilter, setQueueFilter] = useState("all");
  const [status, setStatus] = useState(null);
  const [queue, setQueue] = useState([]);
  const [logTask, setLogTask] = useState(null);
  const [purgeDialogOpen, setPurgeDialogOpen] = useState(false);
  const [purgeOptions, setPurgeOptions] = useState({
    clearFailedCompleted: true,
    clearPending: false,
    resetAccountBusyStates: false,
  });
  const [categories, setCategories] = useState([]);
  const [accounts, setAccounts] = useState([]);
  const [catalogSyncJobs, setCatalogSyncJobs] = useState({});
  const [proxies, setProxies] = useState([]);
  const [providerProxyId, setProviderProxyId] = useState("");
  const [providerProxyReady, setProviderProxyReady] = useState(false);
  const [nodes, setNodes] = useState([]);
  const [routing, setRouting] = useState({ series_node_id: null, movie_node_id: null });
  const [editingNodeId, setEditingNodeId] = useState(null);
  const [nodeForm, setNodeForm] = useState({
    name: "", host: "", port: 22, username: "", private_key_path: "", staging_path: "", watch_path: "",
    proxy_id: "", axel_connections: "",
  });
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [accountForm, setAccountForm] = useState({ provider_url: "", username: "", password: "", user_agent: "Mozilla" });
  const [proxyUrl, setProxyUrl] = useState("");

  useEffect(() => {
    api.get("/auth/session")
      .then(({ data }) => {
        csrfToken = data.csrf_token || null;
        setAdminUsername(data.username || "");
        setAuthenticated(data.authenticated);
      })
      .catch(() => setAuthenticated(false));
    const onExpired = () => {
      setAuthenticated(false);
      setAdminUsername("");
    };
    window.addEventListener("admin-session-expired", onExpired);
    return () => window.removeEventListener("admin-session-expired", onExpired);
  }, []);

  const refresh = useCallback(async () => {
    try {
      const [s, q, syncJobs] = await Promise.all([
        api.get("/status"),
        api.get("/queue"),
        api.get("/content/catalog-sync"),
      ]);
      setStatus(s.data);
      setQueue(q.data);
      setCatalogSyncJobs(Object.fromEntries(syncJobs.data.map((job) => [job.account_id, job])));
    } catch (error) { setNotice(error.response?.data?.detail || "Could not reach the backend API."); }
  }, []);
  useEffect(() => {
    if (!authenticated) return undefined;
    refresh();
    const timer = setInterval(refresh, 3000);
    return () => clearInterval(timer);
  }, [authenticated, refresh]);

  const loadSettings = async () => {
    try {
      const [a, p, n, r, providerProxy, syncJobs] = await Promise.all([
        api.get("/accounts"),
        api.get("/proxies"),
        api.get("/nodes"),
        api.get("/settings/storage-routing"),
        api.get("/settings/provider-proxy"),
        api.get("/content/catalog-sync"),
      ]);
      setAccounts(a.data);
      setCatalogSyncJobs(Object.fromEntries(syncJobs.data.map((job) => [job.account_id, job])));
      setProxies(p.data);
      setProviderProxyId((selected) =>
        p.data.some((proxy) => proxy.enabled && String(proxy.id) === String(providerProxy.data.proxy_id))
          ? String(providerProxy.data.proxy_id)
          : ""
      );
      setNodes(n.data); setRouting(r.data);
      setProviderProxyReady(true);
      return p.data.some((proxy) =>
        proxy.enabled && String(proxy.id) === String(providerProxy.data.proxy_id)
      ) ? String(providerProxy.data.proxy_id) : "";
    } catch (error) {
      setNotice(error.response?.data?.detail || "Unable to load settings.");
      return null;
    }
  };
  const loadCategories = async (proxyId = providerProxyId) => {
    try {
      const categoryResponse = await api.get("/categories", {
        params: { proxy_id: proxyId ? Number(proxyId) : 0 },
      });
      setCategories(categoryResponse.data);
    }
    catch (error) { setNotice(error.response?.data?.detail || "Unable to load categories."); }
  };
  useEffect(() => {
    if (!authenticated) return;
    if (tab === tabs[1]) loadSettings();
    if (tab === tabs[2]) {
      loadSettings().then((proxyId) => {
        if (proxyId !== null) loadCategories(proxyId);
      });
    }
    if (tab === tabs[3]) loadSettings();
  }, [authenticated, tab]);

  const act = async (operation, success) => {
    setBusy(true); setNotice("");
    try {
      const result = await operation();
      setNotice(typeof success === "function" ? success(result) : success);
      await refresh();
    }
    catch (error) { setNotice(error.response?.data?.detail || error.message || "Request failed."); }
    finally { setBusy(false); }
  };

  const changeProviderProxy = async (proxyId) => {
    setBusy(true);
    setNotice("");
    try {
      const id = proxyId ? Number(proxyId) : null;
      await api.put("/settings/provider-proxy", { proxy_id: id });
      setProviderProxyId(proxyId);
      setNotice(id ? `Provider API requests will use proxy #${id}.` : "Provider API requests will use a direct connection.");
      if (tab === tabs[2]) await loadCategories(proxyId);
    } catch (error) {
      setNotice(error.response?.data?.detail || "Could not update the provider API proxy.");
    } finally {
      setBusy(false);
    }
  };

  const toggleCategory = async (category) => {
    const next = !category.monitored;
    await act(async () => {
      await api.post("/categories/toggle", {
        category_id: category.category_id, name: category.name, kind: category.kind,
        monitored: next, destination_path: category.destination_path || null,
        match_existing_series: category.match_existing_series || false,
        strip_prefixes: category.strip_prefixes || "",
      });
      setCategories((items) => items.map((item) =>
        item.category_id === category.category_id && item.kind === category.kind
          ? { ...item, monitored: next }
          : item
      ));
    }, next ? "Category monitoring enabled." : "Category monitoring disabled.");
  };
  const saveCategoryPath = (category) => act(
    async () => {
      const response = await api.post("/categories/toggle", {
        category_id: category.category_id,
        name: category.name,
        kind: category.kind,
        monitored: category.monitored,
        destination_path: category.destination_path?.trim() || null,
        match_existing_series: category.match_existing_series || false,
        strip_prefixes: category.strip_prefixes || "",
      });
      setCategories((items) => items.map((item) =>
        item.category_id === category.category_id && item.kind === category.kind
          ? { ...item, destination_path: response.data.destination_path }
          : item
      ));
    },
    "Category destination saved. Pending watched content will use the updated path.",
  );
  const retry = (id) => act(() => api.post("/queue/retry", { item_id: id }), "Task queued for retry.");
  const cleanFailedItem = (id) => {
    if (!window.confirm("Remove this failed job's known staging partials and only empty directories? Existing media files will not be deleted.")) return;
    act(
      () => api.post(`/queue/${id}/cleanup`),
      "Removed this failed download's remote partial file and empty folders, when present.",
    );
  };
  const cancelDownload = (id) => act(
    () => api.post(`/queue/${id}/cancel`),
    "Download cancellation requested; the remote partial file will be removed.",
  );
  const clearDownloadedList = () => {
    const completedCount = status?.queue?.completed || 0;
    if (!completedCount) return;
    if (!window.confirm(
      `Clear ${completedCount} completed download${completedCount === 1 ? "" : "s"} from the dashboard list? Downloaded files and completion records used to prevent duplicate downloads will remain.`
    )) return;
    act(
      () => api.post("/queue/clear-completed"),
      ({ data }) => `Cleared ${data.cleared_count} completed item${data.cleared_count === 1 ? "" : "s"} from the dashboard list. Downloaded files were not deleted.`,
    );
  };
  const forceRequeuePending = () => {
    const pendingCount = status?.queue?.pending || 0;
    if (!pendingCount) return;
    if (!window.confirm(
      `Force requeue all ${pendingCount} pending task${pendingCount === 1 ? "" : "s"}? Account assignments and retry history will be reset, then workers will be notified.`
    )) return;
    act(
      () => api.post("/queue/requeue-pending"),
      ({ data }) => `${data.message} Requeued ${data.requeued_count} task${data.requeued_count === 1 ? "" : "s"}.`,
    );
  };
  const stopAllAndClearQueue = () => {
    const counts = status?.queue || {};
    const total = [
      counts.pending, counts.downloading, counts.verifying, counts.cleaning, counts.completed,
      counts.failed, counts.cancelled, counts.cancelling,
    ].reduce((sum, value) => sum + (value || 0), 0);
    if (!total) return;
    if (!window.confirm(
      `Stop active downloads and clear all ${total} queue entries, including completed and pending items? Downloaded media files will remain on storage servers.`
    )) return;
    act(
      () => api.post("/queue/stop-all-and-clear"),
      ({ data }) => `Stopped ${data.active_cancelled_count} active and cancelled ${data.pending_cancelled_count} pending download${data.pending_cancelled_count === 1 ? "" : "s"}; cleared ${data.cleared_count} queue entr${data.cleared_count === 1 ? "y" : "ies"}. Downloaded media files were kept.`,
    );
  };
  const purgeTaskHistory = async () => {
    const scope = purgeOptions.clearFailedCompleted && purgeOptions.clearPending
      ? "all"
      : purgeOptions.clearFailedCompleted
        ? "failed_and_completed"
        : purgeOptions.clearPending
          ? "stuck_pending"
          : "none";
    setBusy(true);
    setNotice("");
    try {
      const { data } = await api.post("/tasks/purge-history", {
        scope,
        force_reset_account_busy_states: purgeOptions.resetAccountBusyStates,
      });
      await refresh();
      setNotice(
        `${data.message} Deleted ${data.deleted_tasks_count} task${data.deleted_tasks_count === 1 ? "" : "s"}.`
      );
      setPurgeDialogOpen(false);
    } catch (error) {
      setNotice(error.response?.data?.detail || error.message || "Could not clean task history.");
    } finally {
      setBusy(false);
    }
  };
  const addAccount = (event) => {
    event.preventDefault();
    act(async () => { await api.post("/accounts", accountForm); setAccountForm({ provider_url: "", username: "", password: "", user_agent: "Mozilla" }); await loadSettings(); }, "Provider account added.");
  };
  const syncCatalog = (accountId) => act(
    () => api.post("/content/catalog-sync", {
      account_id: accountId,
      proxy_id: providerProxyId ? Number(providerProxyId) : 0,
    }),
    "Xtream catalog sync queued. Progress will update here while the VPS caches categories, titles, and series episodes.",
  );
  const addProxy = (event) => {
    event.preventDefault();
    act(async () => { await api.post("/proxies", { url: proxyUrl }); setProxyUrl(""); await loadSettings(); }, "Proxy added.");
  };
  const toggleProxy = (proxy) => act(async () => {
    await api.put(`/proxies/${proxy.id}/enabled`, { enabled: !proxy.enabled });
    await loadSettings();
  }, `Proxy ${proxy.enabled ? "disabled" : "enabled"}.`);
  const saveNode = (event, test = false) => {
    event?.preventDefault();
    act(async () => {
      const { axel_connections, ...nodeFields } = nodeForm;
      const payload = {
        ...nodeFields,
        port: Number(nodeForm.port),
        proxy_id: nodeForm.proxy_id ? Number(nodeForm.proxy_id) : null,
        ...(axel_connections ? { axel_connections: Number(axel_connections) } : {}),
        test,
      };
      const response = editingNodeId
        ? await api.put(`/nodes/${editingNodeId}`, payload)
        : await api.post("/nodes", payload);
      await loadSettings();
      setEditingNodeId(response.data.id);
      setNodeForm({ ...response.data, port: Number(response.data.port) });
    }, test ? "Remote SSH connection succeeded and node saved." : "Storage node saved.");
  };
  const editNode = (node) => {
    setEditingNodeId(node.id);
    setNodeForm({
      ...node,
      port: Number(node.port),
      proxy_id: node.proxy_id ? String(node.proxy_id) : "",
      axel_connections: Number(node.axel_connections || 12),
    });
  };
  const resetNodeForm = () => {
    setEditingNodeId(null);
    setNodeForm({
      name: "", host: "", port: 22, username: "", private_key_path: "", staging_path: "", watch_path: "",
      proxy_id: "", axel_connections: "",
    });
  };
  const saveRouting = (event) => {
    event.preventDefault();
    act(async () => {
      const response = await api.put("/settings/storage-routing", {
        series_node_id: routing.series_node_id ? Number(routing.series_node_id) : null,
        movie_node_id: routing.movie_node_id ? Number(routing.movie_node_id) : null,
      });
      setRouting(response.data);
    }, "Storage routing saved. New and pending jobs will use the assigned server.");
  };
  const storageRootForCategory = (category) => {
    const nodeId = category.kind === "series" ? routing.series_node_id : routing.movie_node_id;
    return nodes.find((node) => node.id === nodeId)?.watch_path || "";
  };
  const remove = (url, id) => act(async () => { await api.delete(`${url}/${id}`); await loadSettings(); }, "Removed successfully.");
  const testProxy = (id) => act(
    async () => (await api.post(`/proxies/${id}/test`)).data,
    (result) => `Proxy reachable: ${result.ip || "IP unavailable"} · ${result.latency_ms} ms`,
  );
  const signIn = async (username, password) => {
    setBusy(true);
    setNotice("");
    try {
      const { data } = await api.post("/auth/login", { username, password });
      csrfToken = data.csrf_token;
      setAdminUsername(data.username);
      setAuthenticated(true);
      setNotice("Signed in.");
    } catch (error) {
      setNotice(error.response?.data?.detail || "Sign in failed.");
    } finally {
      setBusy(false);
    }
  };
  const signOut = async () => {
    try { await api.post("/auth/logout"); }
    catch (error) { setNotice(error.response?.data?.detail || "Could not confirm sign out; the local session was cleared."); }
    finally {
      csrfToken = null;
      setAdminUsername("");
      setAuthenticated(false);
    }
  };

  if (authenticated === null) {
    return <div className="grid min-h-screen place-items-center bg-[#090d16] text-slate-400">Checking admin session…</div>;
  }
  if (!authenticated) {
    return <LoginScreen busy={busy} notice={notice} onLogin={signIn} />;
  }

  const counts = status?.queue || {};
  const statCards = [
    ["Pending", counts.pending || 0, Play, "text-slate-300"],
    ["Downloading", counts.downloading || 0, Activity, "text-blue-300"],
    ["Completed", counts.completed || 0, Check, "text-emerald-300"],
    ["Failed", counts.failed || 0, CircleAlert, "text-rose-300"],
  ];
  const activeStatuses = ["DOWNLOADING", "VERIFYING", "CLEANING", "CANCELLING"];
  const activeCount = activeStatuses.reduce((total, key) => total + (counts[key.toLowerCase()] || 0), 0);
  const queueFilters = [
    { id: "open", label: "Open", count: activeCount + (counts.pending || 0) },
    { id: "active", label: "Active", count: activeCount },
    { id: "pending", label: "Pending", count: counts.pending || 0 },
    { id: "completed", label: "Completed", count: counts.completed || 0 },
    { id: "failed", label: "Failed", count: counts.failed || 0 },
    { id: "cancelled", label: "Cancelled", count: counts.cancelled || 0 },
    { id: "all", label: "All", count: Object.values(counts).reduce((total, value) => total + (value || 0), 0) },
  ];
  const visibleQueue = queue.filter((item) => {
    if (queueFilter === "open") return item.status === "PENDING" || activeStatuses.includes(item.status);
    if (queueFilter === "active") return activeStatuses.includes(item.status);
    if (queueFilter === "pending") return item.status === "PENDING";
    if (queueFilter === "completed") return item.status === "COMPLETED";
    if (queueFilter === "failed") return item.status === "FAILED";
    if (queueFilter === "cancelled") return item.status === "CANCELLED";
    return true;
  });

  return (
    <div className="min-h-screen bg-[#090d16]">
      <header className="border-b border-slate-800/80 bg-[#0d1320]">
        <div className="mx-auto flex max-w-7xl items-center justify-between gap-4 px-6 py-5">
          <div className="flex items-center gap-3">
            <div className="rounded-xl bg-indigo-500/15 p-2.5 text-indigo-300"><Clapperboard size={21} /></div>
            <div><h1 className="text-lg font-semibold text-white">Media Ingestion</h1><p className="text-xs text-slate-500">Stream orchestration control plane</p></div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Pill tone={status?.daemon_running ? "green" : "red"}><span className="mr-1.5 inline-block h-1.5 w-1.5 rounded-full bg-current" />Daemon {status?.daemon_running ? "Running" : "Offline"}</Pill>
            <Pill tone={status?.ssh_connected ? "green" : "yellow"}><Wifi size={13} className="mr-1.5" />SSH {status?.ssh_connected ? "Connected" : "Not verified"}</Pill>
            <Pill><Activity size={13} className="mr-1.5" />{status?.active_workers || 0} workers</Pill>
            <button onClick={signOut} className="ml-1 flex items-center gap-1.5 rounded-lg border border-slate-700 px-3 py-2 text-xs text-slate-300 hover:bg-slate-800"><LogOut size={14} />{adminUsername || "Sign out"}</button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-7xl px-6 py-8">
        {notice && <div className="mb-5 flex items-start justify-between rounded-xl border border-slate-700 bg-slate-900 px-4 py-3 text-sm text-slate-300"><span>{notice}</span><button onClick={() => setNotice("")} aria-label="Dismiss">×</button></div>}
        <nav className="mb-7 flex gap-1 border-b border-slate-800">
          {tabs.map((item) => <button key={item} onClick={() => setTab(item)} className={`border-b-2 px-4 py-3 text-sm ${tab === item ? "border-indigo-400 text-white" : "border-transparent text-slate-500 hover:text-slate-300"}`}>{item}</button>)}
        </nav>

        {tab === tabs[0] && <>
          <div className="mb-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {statCards.map(([label, value, Icon, color]) => <div key={label} className="rounded-2xl border border-slate-800 bg-[#0d1320] p-5"><div className="flex items-center justify-between"><span className="text-sm text-slate-400">{label}</span><Icon size={17} className={color} /></div><div className="mt-3 text-3xl font-semibold text-white">{value}</div></div>)}
          </div>
          <section className="overflow-hidden rounded-2xl border border-slate-800 bg-[#0d1320]">
            <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-800 px-5 py-4"><div><h2 className="font-semibold text-white">Ingestion queue</h2><p className="mt-1 text-xs text-slate-500">Latest 500 discovered streams</p></div><div className="flex flex-wrap items-center gap-2"><button onClick={forceRequeuePending} disabled={busy || !(counts.pending || 0)} className="inline-flex items-center gap-1.5 rounded-lg border border-amber-800/70 bg-amber-950/20 px-3 py-2 text-xs text-amber-200 hover:bg-amber-950/40 disabled:cursor-not-allowed disabled:opacity-40"><RefreshCw size={14} />Force requeue pending ({counts.pending || 0})</button><button onClick={clearDownloadedList} disabled={busy || !(counts.completed || 0)} className="inline-flex items-center gap-1.5 rounded-lg border border-slate-700 px-3 py-2 text-xs text-slate-300 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-40"><Trash2 size={14} />Clear downloaded list</button><button onClick={stopAllAndClearQueue} disabled={busy || ![counts.pending, counts.downloading, counts.verifying, counts.completed, counts.failed, counts.cancelled, counts.cancelling].some((value) => value > 0)} className="inline-flex items-center gap-1.5 rounded-lg border border-rose-900/70 px-3 py-2 text-xs text-rose-300 hover:bg-rose-950/40 disabled:cursor-not-allowed disabled:opacity-40"><XCircle size={14} />Stop all &amp; clear all</button><button onClick={refresh} disabled={busy} className="rounded-lg p-2 text-slate-400 hover:bg-slate-800 hover:text-white disabled:opacity-40" aria-label="Refresh queue"><RefreshCw size={16} /></button></div></div>
            <div className="flex flex-wrap gap-2 border-b border-slate-800 px-5 py-3">
              {queueFilters.map((filter) => (
                <button
                  key={filter.id}
                  onClick={() => setQueueFilter(filter.id)}
                  aria-pressed={queueFilter === filter.id}
                  className={`rounded-lg border px-3 py-1.5 text-xs font-medium transition-colors ${
                    queueFilter === filter.id
                      ? "border-indigo-400/50 bg-indigo-400/10 text-indigo-200"
                      : "border-slate-800 text-slate-400 hover:border-slate-700 hover:text-slate-200"
                  }`}
                >
                  {filter.label}<span className="ml-2 text-slate-500">{filter.count}</span>
                </button>
              ))}
            </div>
            <div className="overflow-x-auto"><table className="w-full min-w-[780px] text-left text-sm">
              <thead className="bg-slate-900/60 text-xs uppercase tracking-wide text-slate-500"><tr><th className="px-5 py-3">Title</th><th className="px-4 py-3">Type</th><th className="px-4 py-3">Status</th><th className="px-4 py-3">Progress</th><th className="px-4 py-3">Worker / Transfer</th><th className="px-4 py-3">Action</th></tr></thead>
              <tbody className="divide-y divide-slate-800/80">{visibleQueue.length ? visibleQueue.map((item) => <tr key={item.id} className="hover:bg-slate-800/20">
                <td className="px-5 py-4"><div className="font-medium text-slate-200">{item.title}</div>{item.series_title && <div className="mt-1 text-xs text-slate-500">{item.series_title} · S{item.season ?? "—"} E{item.episode ?? "—"}</div>}{item.final_path && <div className="mt-1 max-w-[30rem] truncate font-mono text-[10px] text-slate-500" title={item.final_path}>Saved to: {item.final_path}</div>}</td>
                <td className="px-4 py-4 text-slate-400">{item.kind === "series" ? <Tv size={16} /> : <Film size={16} />}</td>
                <td className="px-4 py-4"><Status status={item.status} /></td>
                <td className="px-4 py-4"><div className="flex items-center gap-2"><div className="h-1.5 w-20 overflow-hidden rounded bg-slate-800"><div className="h-full bg-indigo-400" style={{ width: `${item.progress || 0}%` }} /></div><span className="text-xs text-slate-500">{Math.round(item.progress || 0)}%</span></div></td>
                <td className="px-4 py-4"><div className="flex flex-col items-start gap-2"><button onClick={() => setLogTask(item)} className="flex items-center gap-1.5 text-xs text-indigo-300 hover:text-indigo-200"><Terminal size={13} />View log</button>{item.status === "FAILED" && <><button disabled={busy} onClick={() => cleanFailedItem(item.id)} className="flex items-center gap-1.5 text-xs text-amber-300 hover:text-amber-200"><Trash2 size={13} />Clean partial</button><button disabled={busy} onClick={() => retry(item.id)} className="flex items-center gap-1.5 text-xs text-indigo-300 hover:text-indigo-200"><RefreshCw size={13} />Retry</button></>}{["PENDING", "DOWNLOADING", "VERIFYING"].includes(item.status) && <button disabled={busy} onClick={() => cancelDownload(item.id)} className="flex items-center gap-1.5 text-xs text-rose-300 hover:text-rose-200"><XCircle size={13} />Cancel</button>}{item.status === "CLEANING" && <span className="text-xs text-amber-300">Cleaning storage…</span>}{item.status === "CANCELLING" && <span className="text-xs text-amber-300">Cancelling…</span>}</div></td>
              </tr>) : <tr><td colSpan="6" className="px-5 py-14 text-center text-slate-500">{queue.length ? `No ${queueFilter === "all" ? "" : `${queueFilter} `}queue items in the latest list.` : "No streams discovered yet. Enable a monitored category to begin."}</td></tr>}</tbody>
            </table></div>
          </section>
        </>}

        {tab === tabs[1] && <ContentBrowser
          api={api}
          routing={routing}
          onQueued={refresh}
          proxies={proxies.filter((proxy) => proxy.enabled)}
          providerProxyId={providerProxyId}
          setProviderProxyId={changeProviderProxy}
          providerProxyBusy={busy}
          providerProxyReady={providerProxyReady}
        />}

        {tab === tabs[2] && <section className="rounded-2xl border border-slate-800 bg-[#0d1320]">
          <div className="border-b border-slate-800 px-5 py-4"><h2 className="font-semibold text-white">Category watcher</h2><p className="mt-1 text-xs text-slate-500">Watched items inherit the storage root unless this category has its own remote path. Series can optionally match existing folders beneath a custom path; ambiguous matches are skipped. Configure leading labels to remove from downloaded names.</p>
            <label className="mt-4 flex flex-wrap items-center gap-3 text-xs text-slate-400">
              Fetch categories using
              <select value={providerProxyId} onChange={(event) => setProviderProxyId(event.target.value)} disabled={busy} className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-200 disabled:opacity-50">
                <option value="">Direct connection</option>
                {proxies.filter((proxy) => proxy.enabled).map((proxy) => <option key={proxy.id} value={proxy.id}>Proxy #{proxy.id} · {proxy.url}</option>)}
              </select>
              <span className="text-slate-500">This also applies to provider lists and series details in Browse &amp; Ingest.</span>
            </label>
            <p className="mt-2 text-xs text-slate-400">Series storage: <code className="text-indigo-300">{nodes.find((node) => node.id === routing.series_node_id)?.watch_path || "Unassigned"}</code> · Movies storage: <code className="text-indigo-300">{nodes.find((node) => node.id === routing.movie_node_id)?.watch_path || "Unassigned"}</code></p>
          </div>
          <div className="grid gap-3 p-5 sm:grid-cols-2 lg:grid-cols-3">{categories.map((category) => <div key={`${category.kind}-${category.category_id}`} className="rounded-xl border border-slate-800 bg-slate-900/40 p-4">
            <div className="flex items-center justify-between gap-3">
              <div className="flex min-w-0 items-center gap-3"><div className="rounded-lg bg-slate-800 p-2 text-indigo-300">{category.kind === "series" ? <Tv size={17} /> : <Film size={17} />}</div><div className="min-w-0"><div className="truncate text-sm font-medium text-slate-200">{category.name}</div><div className="mt-1 text-xs uppercase text-slate-500">{category.kind} · ID {category.category_id}</div></div></div>
              <button role="switch" aria-checked={category.monitored} aria-label={`Watch ${category.name}`} disabled={busy} onClick={() => toggleCategory(category)} className={`relative h-6 w-11 shrink-0 rounded-full transition ${category.monitored ? "bg-indigo-500" : "bg-slate-700"}`}><span className={`absolute top-1 h-4 w-4 rounded-full bg-white transition ${category.monitored ? "left-6" : "left-1"}`} /></button>
            </div>
            <form className="mt-4 space-y-2" onSubmit={(event) => { event.preventDefault(); saveCategoryPath(category); }}>
              <label className="block text-xs text-slate-400">Category destination override</label>
              <input value={category.destination_path || ""} onChange={(event) => setCategories((items) => items.map((item) =>
                item.category_id === category.category_id && item.kind === category.kind
                  ? { ...item, destination_path: event.target.value }
                  : item
              ))} placeholder={storageRootForCategory(category) ? `${storageRootForCategory(category)} (inherit assigned server)` : "No storage server assigned"} className="field" />
              {category.kind === "series" && <label className="flex items-start gap-2 rounded-lg border border-slate-800 p-3 text-xs text-slate-300">
                <input type="checkbox" checked={Boolean(category.match_existing_series)} onChange={(event) => setCategories((items) => items.map((item) =>
                  item.category_id === category.category_id && item.kind === category.kind
                    ? { ...item, match_existing_series: event.target.checked }
                    : item
                ))} className="mt-0.5 accent-indigo-500" />
                <span>Match new episodes to an existing series folder under this custom path. Only a unique close match will be used; no match or ambiguous matches fail safely.</span>
              </label>}
              <label className="block text-xs text-slate-400">Leading prefixes to strip (comma-separated)</label>
              <input value={category.strip_prefixes || ""} onChange={(event) => setCategories((items) => items.map((item) =>
                item.category_id === category.category_id && item.kind === category.kind
                  ? { ...item, strip_prefixes: event.target.value }
                  : item
              ))} placeholder="AR-SUBS, Fuiries, 4K-ar-fast" className="field" />
              <p className="text-[11px] text-slate-500">Known tag patterns are detected automatically. Add unusual leading labels here; this cleans new names and inventory matching but does not rename existing files.</p>
              <button disabled={busy} className="secondary w-full">Save path · blank uses root</button>
            </form>
          </div>)}{!categories.length && <div className="col-span-full py-12 text-center text-sm text-slate-500">Add an enabled provider account to load categories.</div>}</div>
        </section>}

        {tab === tabs[3] && <div className="grid gap-6 lg:grid-cols-2">
          <section className="rounded-2xl border border-slate-800 bg-[#0d1320]">
            <div className="flex items-center gap-2 border-b border-slate-800 px-5 py-4"><Radio size={17} className="text-indigo-300" /><div><h2 className="font-semibold text-white">Provider accounts</h2><p className="text-xs text-slate-500">One active download per enabled account</p></div></div>
            <div className="space-y-2 p-5">{accounts.map((account) => {
              const job = catalogSyncJobs[account.id];
              const syncing = job && ["PENDING", "RUNNING"].includes(job.status);
              return <div key={account.id} className="rounded-lg bg-slate-900/70 px-3 py-3">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0"><div className="text-sm text-slate-200">{account.username}</div><div className="truncate text-xs text-slate-500">{account.provider_url}</div></div>
                  <div className="flex shrink-0 items-center gap-2">
                    <button type="button" disabled={busy || syncing} onClick={() => syncCatalog(account.id)} className="inline-flex items-center gap-1.5 rounded-lg border border-indigo-400/30 px-2.5 py-1.5 text-xs text-indigo-200 hover:bg-indigo-500/10 disabled:opacity-40">
                      <RefreshCw size={13} className={syncing ? "animate-spin" : ""} />{syncing ? "Syncing…" : "Sync catalog"}
                    </button>
                    <button type="button" onClick={() => remove("/accounts", account.id)} className="p-2 text-slate-500 hover:text-rose-300" aria-label={`Remove ${account.username}`}><Trash2 size={15} /></button>
                  </div>
                </div>
                {job && <div className="mt-2 border-t border-slate-800 pt-2 text-[11px]">
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-slate-400">
                    <span className={job.status === "COMPLETED" ? "text-emerald-300" : job.status === "FAILED" ? "text-rose-300" : "text-indigo-300"}>{job.status} · {job.phase}</span>
                    <span>{job.categories_processed} categories</span>
                    <span>{job.lists_processed} lists · {job.cached_items} titles</span>
                    <span>{job.series_details_processed}/{job.total_series_details} series details</span>
                    {job.cache_hits > 0 && <span>{job.cache_hits} cached responses reused</span>}
                  </div>
                  {job.stale_responses > 0 && <p className="mt-1 text-amber-300">{job.stale_responses} refresh requests used stale cached data after provider/proxy errors.</p>}
                  {job.invalid_series_details > 0 && <p className="mt-1 text-amber-300">{job.invalid_series_details} series returned malformed episode data and were skipped; other series continued syncing.</p>}
                  {job.error && <p className="mt-1 text-rose-300">{job.error}</p>}
                </div>}
              </div>;
            })}
              <form onSubmit={addAccount} className="mt-4 space-y-2 border-t border-slate-800 pt-4">
                <input required type="url" placeholder="Provider base URL" value={accountForm.provider_url} onChange={(e) => setAccountForm({ ...accountForm, provider_url: e.target.value })} className="field" />
                <div className="grid grid-cols-2 gap-2"><input required placeholder="Username" value={accountForm.username} onChange={(e) => setAccountForm({ ...accountForm, username: e.target.value })} className="field" /><input required type="password" placeholder="Password" value={accountForm.password} onChange={(e) => setAccountForm({ ...accountForm, password: e.target.value })} className="field" /></div>
                <input placeholder="User-Agent" value={accountForm.user_agent} onChange={(e) => setAccountForm({ ...accountForm, user_agent: e.target.value })} className="field" />
                <button disabled={busy} className="primary"><Plus size={15} />Add account</button>
              </form>
            </div>
          </section>
          <section className="rounded-2xl border border-slate-800 bg-[#0d1320] lg:col-span-2">
            <div className="flex items-center gap-2 border-b border-slate-800 px-5 py-4"><Clapperboard size={17} className="text-indigo-300" /><div><h2 className="font-semibold text-white">Xtream catalog cache</h2><p className="text-xs text-slate-500">Catalog syncs run on the VPS and persist provider categories, titles, and episode mappings in SQLite.</p></div></div>
            <p className="p-5 text-xs leading-5 text-slate-400">Start a sync from an account above. Category and title lists are fetched first, then series episode details are fetched one series at a time and rate-limited to protect the provider. The selected provider API proxy is used for the entire sync. If a refresh fails but cached data exists, the job records and displays the stale response count instead of claiming every response was fresh. Existing saved M3U records are preserved but are no longer part of the primary browsing and import workflow.</p>
          </section>
          <section className="rounded-2xl border border-slate-800 bg-[#0d1320]">
            <div className="flex items-center gap-2 border-b border-slate-800 px-5 py-4"><Gauge size={17} className="text-indigo-300" /><div><h2 className="font-semibold text-white">Proxy pool</h2><p className="text-xs text-slate-500">Proxy URLs are encrypted at rest</p></div></div>
            <div className="space-y-2 p-5">{proxies.map((proxy) => <div key={proxy.id} className="flex items-center justify-between rounded-lg bg-slate-900/70 px-3 py-2.5"><div><span className="font-mono text-xs text-slate-300">{proxy.url}</span><span className={`ml-2 text-[10px] ${proxy.enabled ? "text-emerald-300" : "text-slate-500"}`}>{proxy.enabled ? "Enabled" : "Disabled"}</span></div><div className="flex items-center"><button onClick={() => toggleProxy(proxy)} disabled={busy} className="px-2 text-xs text-slate-300 hover:text-white">{proxy.enabled ? "Disable" : "Enable"}</button><button onClick={() => testProxy(proxy.id)} disabled={busy} className="px-2 text-xs text-indigo-300 hover:text-indigo-200">Test latency/IP</button><button onClick={() => remove("/proxies", proxy.id)} className="p-2 text-slate-500 hover:text-rose-300" aria-label="Remove proxy"><Trash2 size={15} /></button></div></div>)}
              <form onSubmit={addProxy} className="mt-4 flex gap-2 border-t border-slate-800 pt-4"><input required placeholder="http://user:pass@host:port" value={proxyUrl} onChange={(e) => setProxyUrl(e.target.value)} className="field" /><button disabled={busy} className="primary"><Plus size={15} />Add</button></form>
            </div>
          </section>
          <section className="flex flex-wrap items-center justify-between gap-4 rounded-2xl border border-indigo-900/50 bg-indigo-950/20 p-5 lg:col-span-2">
            <div>
              <h2 className="font-semibold text-white">SSH connectivity check</h2>
              <p className="mt-1 text-xs text-slate-400">Test the saved SSH credentials, host-key trust, and storage paths on every configured storage server.</p>
            </div>
            <button
              type="button"
              disabled={busy || !nodes.length}
              onClick={() => act(
                () => api.post("/nodes/check-ssh"),
                ({ data }) => data.results.map((result) =>
                  `${result.name} (${result.host}): ${result.connected ? "Connected" : `Failed — ${result.error}`}`
                ).join(" · "),
              )}
              className="secondary shrink-0 disabled:cursor-not-allowed disabled:opacity-40"
            ><Wifi size={15} />Check SSH connections</button>
          </section>
          <section className="rounded-2xl border border-slate-800 bg-[#0d1320] lg:col-span-2">
            <div className="flex items-center gap-2 border-b border-slate-800 px-5 py-4"><Server size={17} className="text-indigo-300" /><div><h2 className="font-semibold text-white">Storage servers &amp; routing</h2><p className="text-xs text-slate-500">Movies and series are assigned independently. SSH host keys must be trusted on the daemon.</p></div></div>
            <div className="grid gap-5 p-5 lg:grid-cols-2">
              <form onSubmit={saveRouting} className="space-y-3 rounded-xl border border-slate-800 bg-slate-900/40 p-4">
                <h3 className="text-sm font-medium text-slate-200">Content routing</h3>
                <label className="block text-xs text-slate-400">Series storage server
                  <select value={routing.series_node_id ?? ""} onChange={(event) => setRouting({ ...routing, series_node_id: event.target.value ? Number(event.target.value) : null })} className="field mt-2">
                    <option value="">Not assigned</option>{nodes.map((node) => <option key={node.id} value={node.id}>{node.name} · {node.host}</option>)}
                  </select>
                </label>
                <label className="block text-xs text-slate-400">Movies storage server
                  <select value={routing.movie_node_id ?? ""} onChange={(event) => setRouting({ ...routing, movie_node_id: event.target.value ? Number(event.target.value) : null })} className="field mt-2">
                    <option value="">Not assigned</option>{nodes.map((node) => <option key={node.id} value={node.id}>{node.name} · {node.host}</option>)}
                  </select>
                </label>
                <button disabled={busy} className="primary"><Settings2 size={15} />Save routing</button>
              </form>
              <div className="space-y-3">
                <h3 className="text-sm font-medium text-slate-200">Configured servers ({nodes.length})</h3>
                {nodes.map((node) => <div key={node.id} className="flex items-center justify-between gap-3 rounded-lg bg-slate-900/70 px-3 py-3">
                  <div className="min-w-0"><div className="truncate text-sm text-slate-200">{node.name}</div><div className="truncate text-xs text-slate-500">{node.username}@{node.host}:{node.port} · {node.watch_path}</div><div className="mt-1 text-[10px] text-slate-400">{node.axel_connections || 12} download connections · {proxies.find((proxy) => proxy.id === node.proxy_id)?.enabled ? `Proxy #${node.proxy_id}` : "Direct connection"}</div><div className="mt-1 text-[10px] text-indigo-300">{[routing.series_node_id === node.id && "Series", routing.movie_node_id === node.id && "Movies"].filter(Boolean).join(" · ") || "Not assigned"}</div></div>
                  <div className="flex shrink-0 items-center"><button onClick={() => editNode(node)} className="px-2 text-xs text-indigo-300 hover:text-indigo-200">Edit</button><button onClick={() => remove("/nodes", node.id)} className="p-2 text-slate-500 hover:text-rose-300" aria-label={`Remove ${node.name}`}><Trash2 size={15} /></button></div>
                </div>)}
                {!nodes.length && <p className="rounded-lg border border-dashed border-slate-700 px-3 py-6 text-center text-xs text-slate-500">Add a storage server, then assign it to Movies, Series, or both.</p>}
              </div>
              <form onSubmit={(event) => saveNode(event)} className="grid gap-3 border-t border-slate-800 pt-5 sm:grid-cols-2 lg:col-span-2 lg:grid-cols-3">
                <h3 className="text-sm font-medium text-slate-200 sm:col-span-2 lg:col-span-3">{editingNodeId ? `Edit storage server #${editingNodeId}` : "Add storage server"}</h3>
                {[
                  ["name", "Server name"], ["host", "SSH host"], ["port", "Port"], ["username", "SSH username"],
                  ["private_key_path", "Private key path on daemon"], ["staging_path", "Remote staging directory"], ["watch_path", "Storage root on this server"],
                ].map(([key, placeholder]) => <input key={key} required placeholder={placeholder} type={key === "port" ? "number" : "text"} value={nodeForm[key] ?? ""} onChange={(event) => setNodeForm({ ...nodeForm, [key]: event.target.value })} className="field" />)}
                <label className="block text-xs text-slate-400">Axel connections (1–12)
                  <input required min="1" max="12" type="number" value={nodeForm.axel_connections || status?.default_axel_connections || 12} onChange={(event) => setNodeForm({ ...nodeForm, axel_connections: event.target.value })} className="field mt-2" />
                </label>
                <label className="block text-xs text-slate-400">Worker proxy
                  <select value={nodeForm.proxy_id ?? ""} onChange={(event) => setNodeForm({ ...nodeForm, proxy_id: event.target.value })} className="field mt-2">
                    <option value="">Direct connection</option>
                    {proxies.map((proxy) => <option key={proxy.id} value={proxy.id}>Proxy #{proxy.id} · {proxy.url}{proxy.enabled ? "" : " (disabled; direct)"}</option>)}
                  </select>
                </label>
                <div className="flex flex-wrap gap-2 sm:col-span-2 lg:col-span-3">
                  <button disabled={busy} className="primary"><Plus size={15} />{editingNodeId ? "Save server" : "Add server"}</button>
                  <button type="button" disabled={busy} onClick={(event) => saveNode(event, true)} className="secondary"><Wifi size={15} />Test connection &amp; save</button>
                  {editingNodeId && <button type="button" onClick={resetNodeForm} className="secondary">Cancel edit</button>}
                </div>
              </form>
            </div>
          </section>
          <section className="flex flex-wrap items-center justify-between gap-4 rounded-2xl border border-rose-900/60 bg-rose-950/15 p-5 lg:col-span-2">
            <div>
              <h2 className="font-semibold text-rose-200">Queue &amp; Task History Management</h2>
              <p className="mt-1 max-w-3xl text-xs leading-relaxed text-slate-400">Remove selected queue records and reconcile stale worker state. Active work is stopped before its records are removed; completed media files are never deleted.</p>
            </div>
            <button type="button" onClick={() => setPurgeDialogOpen(true)} disabled={busy} className="inline-flex shrink-0 items-center gap-2 rounded-lg border border-rose-700/70 bg-rose-950/40 px-4 py-2.5 text-xs font-semibold text-rose-200 hover:bg-rose-900/50 disabled:opacity-40">
              <Trash2 size={14} />Clean Task History &amp; Reset Queue
            </button>
          </section>
        </div>}
      </main>
      {purgeDialogOpen && <div className="fixed inset-0 z-40 grid place-items-center bg-black/75 p-5" role="dialog" aria-modal="true" aria-labelledby="purge-history-title">
        <section className="w-full max-w-lg rounded-2xl border border-rose-900/70 bg-[#0d1320] p-6 shadow-2xl">
          <h2 id="purge-history-title" className="text-lg font-semibold text-rose-200">Clean task history and reset queue</h2>
          <p className="mt-2 text-sm leading-relaxed text-slate-400">Choose what to remove. Active downloads selected for removal will be cancelled and their remote staging files cleaned first. Final media files are preserved.</p>
          <div className="mt-5 space-y-3">
            <label className="flex items-start gap-3 rounded-lg border border-slate-800 p-3 text-sm text-slate-200">
              <input type="checkbox" checked={purgeOptions.clearFailedCompleted} onChange={(event) => setPurgeOptions((current) => ({ ...current, clearFailedCompleted: event.target.checked }))} className="mt-1 accent-rose-500" />
              <span><span className="block font-medium">Clear completed &amp; failed tasks</span><span className="mt-1 block text-xs text-slate-500">Removes their queue records and their deduplication history.</span></span>
            </label>
            <label className="flex items-start gap-3 rounded-lg border border-slate-800 p-3 text-sm text-slate-200">
              <input type="checkbox" checked={purgeOptions.clearPending} onChange={(event) => setPurgeOptions((current) => ({ ...current, clearPending: event.target.checked }))} className="mt-1 accent-rose-500" />
              <span><span className="block font-medium">Clear stuck / pending tasks</span><span className="mt-1 block text-xs text-slate-500">Deletes pending items instead of leaving them queued.</span></span>
            </label>
            <label className="flex items-start gap-3 rounded-lg border border-slate-800 p-3 text-sm text-slate-200">
              <input type="checkbox" checked={purgeOptions.resetAccountBusyStates} onChange={(event) => setPurgeOptions((current) => ({ ...current, resetAccountBusyStates: event.target.checked }))} className="mt-1 accent-rose-500" />
              <span><span className="block font-medium">Force reset account busy states</span><span className="mt-1 block text-xs text-slate-500">Reconciles orphaned active task leases and stale in-memory worker flags; live tasks remain assigned until cancelled or finished.</span></span>
            </label>
          </div>
          {purgeOptions.clearFailedCompleted && purgeOptions.clearPending && <p className="mt-4 rounded-lg border border-amber-800/60 bg-amber-950/20 p-3 text-xs text-amber-200">Both cleanup scopes are selected: this removes all queue history and cancels active downloads before deleting their records.</p>}
          {!purgeOptions.clearFailedCompleted && !purgeOptions.clearPending && !purgeOptions.resetAccountBusyStates && <p className="mt-4 text-xs text-rose-300">Select at least one maintenance action.</p>}
          <div className="mt-6 flex justify-end gap-2">
            <button type="button" disabled={busy} onClick={() => setPurgeDialogOpen(false)} className="secondary">Cancel</button>
            <button type="button" disabled={busy || (!purgeOptions.clearFailedCompleted && !purgeOptions.clearPending && !purgeOptions.resetAccountBusyStates)} onClick={purgeTaskHistory} className="inline-flex items-center gap-2 rounded-lg bg-rose-700 px-4 py-2.5 text-xs font-semibold text-white hover:bg-rose-600 disabled:opacity-40">{busy ? "Cleaning…" : "Confirm cleanup"}</button>
          </div>
        </section>
      </div>}
      {logTask && <TaskLogViewer
        key={logTask.id}
        task={logTask}
        onClose={() => setLogTask(null)}
        onAbort={async (id) => {
          const { data } = await api.post(`/tasks/${id}/abort`);
          await refresh();
          return data;
        }}
      />}
      <style>{`.field{width:100%;border:1px solid #263244;background:#090d16;color:#e2e8f0;border-radius:.6rem;padding:.65rem .8rem;font-size:.8rem;outline:none}.field:focus{border-color:#818cf8}.primary,.secondary{display:inline-flex;align-items:center;justify-content:center;gap:.5rem;border-radius:.6rem;padding:.65rem 1rem;font-size:.8rem;font-weight:600}.primary{background:#4f46e5;color:white}.primary:hover{background:#6366f1}.secondary{border:1px solid #334155;color:#cbd5e1}.secondary:hover{background:#1e293b}`}</style>
    </div>
  );
}

function TaskLogViewer({ task, onClose, onAbort }) {
  const [log, setLog] = useState("");
  const [command, setCommand] = useState("");
  const [status, setStatus] = useState(task.status);
  const [account, setAccount] = useState(task.account);
  const [host, setHost] = useState(task.host);
  const [lastError, setLastError] = useState(task.last_error || task.error || "");
  const [streamError, setStreamError] = useState("");
  const [aborting, setAborting] = useState(false);
  const [copyMessage, setCopyMessage] = useState("");
  const terminalBody = useRef(null);
  const terminal = ["COMPLETED", "FAILED", "CANCELLED"].includes(status);

  useEffect(() => {
    const source = new EventSource(`/api/tasks/${task.id}/logs/stream`, {
      withCredentials: true,
    });
    source.onmessage = (event) => {
      const update = JSON.parse(event.data);
      setLog((current) => update.reset ? update.log : current + update.log);
      setCommand(update.command || "");
      setStatus(update.status);
      setAccount(update.account || task.account);
      setHost(update.host || task.host);
      setLastError(update.last_error || "");
      setStreamError("");
      if (["COMPLETED", "FAILED", "CANCELLED"].includes(update.status)) {
        source.close();
      }
    };
    source.onerror = () => setStreamError("Log stream disconnected; reconnecting…");
    return () => source.close();
  }, [task.id, task.account, task.host]);

  useEffect(() => {
    if (terminalBody.current) {
      terminalBody.current.scrollTop = terminalBody.current.scrollHeight;
    }
  }, [log]);

  const copyCommand = async () => {
    try {
      await navigator.clipboard.writeText(command);
      setCopyMessage("Command copied.");
    } catch {
      setCopyMessage("Clipboard access is unavailable in this browser.");
    }
  };

  const abort = async () => {
    setAborting(true);
    setCopyMessage("");
    try {
      const result = await onAbort(task.id);
      setStatus(result.status);
    } catch (error) {
      setCopyMessage(error.response?.data?.detail || "Could not abort this task.");
    } finally {
      setAborting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/70" role="dialog" aria-modal="true" aria-label={`Task log: ${task.title}`}>
      <section className="flex h-full w-full max-w-4xl flex-col border-l border-slate-700 bg-[#090d16] shadow-2xl">
        <header className="flex flex-wrap items-start justify-between gap-4 border-b border-slate-800 bg-[#0d1320] px-5 py-4">
          <div className="min-w-0">
            <div className="flex items-center gap-2 text-indigo-300"><Terminal size={17} /><h2 className="font-semibold text-white">{task.title}</h2></div>
            <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-slate-400">
              <Pill tone={status === "FAILED" ? "red" : status === "COMPLETED" ? "green" : "blue"}>{status}</Pill>
              <span>Account: {account || "Unassigned"}</span><span>Storage host: {host || "Unknown"}</span>
            </div>
          </div>
          <div className="flex items-center gap-2">
            {["PENDING", "DOWNLOADING", "VERIFYING", "CANCELLING"].includes(status) && <button disabled={aborting || status === "CANCELLING"} onClick={abort} className="rounded-lg border border-rose-800 px-3 py-2 text-xs text-rose-300 hover:bg-rose-950/50 disabled:opacity-50">{aborting ? "Aborting…" : status === "CANCELLING" ? "Cancelling…" : "Abort job"}</button>}
            <button onClick={onClose} aria-label="Close log viewer" className="rounded-lg p-2 text-slate-400 hover:bg-slate-800 hover:text-white"><X size={17} /></button>
          </div>
        </header>
        <div className="border-b border-slate-800 p-4">
          <div className="mb-2 flex items-center justify-between gap-2">
            <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-400">Dispatched command</h3>
            <button disabled={!command} onClick={copyCommand} className="inline-flex items-center gap-1.5 text-xs text-indigo-300 hover:text-indigo-200 disabled:opacity-40"><ClipboardCopy size={13} />Copy</button>
          </div>
          <p className="mb-2 text-[11px] text-amber-300">The exact command contains provider credentials. Do not copy or share it outside trusted systems.</p>
          <pre className="max-h-32 overflow-auto whitespace-pre-wrap break-all rounded-lg border border-slate-800 bg-black/40 p-3 font-mono text-[11px] leading-relaxed text-slate-300">{command || "Waiting for the worker to dispatch the remote command…"}</pre>
          {copyMessage && <p className="mt-1 text-xs text-amber-300">{copyMessage}</p>}
        </div>
        {lastError && <div className="border-b border-rose-900/60 bg-rose-950/30 px-4 py-2 text-xs text-rose-300"><span className="font-semibold">Latest error: </span>{lastError}</div>}
        <div className="flex items-center justify-between border-b border-slate-800 px-4 py-2 text-[11px] text-slate-500"><span>Live task output</span><span>{streamError || (terminal ? "Task finished" : "Streaming")}</span></div>
        <pre ref={terminalBody} className="min-h-0 flex-1 overflow-auto whitespace-pre-wrap break-words p-4 font-mono text-xs leading-relaxed text-emerald-100/90">{log ? log.split(/(?<=\n)/).map((line, index) => <span key={index} className={/(?:\b(?:401|403)\b|unauthori[sz]ed|forbidden|connection refused|no space left|disk quota|quota exceeded)/i.test(line) ? "text-rose-300" : undefined}>{line}</span>) : <span className="text-slate-600">Waiting for task output…</span>}</pre>
      </section>
    </div>
  );
}

function LoginScreen({ busy, notice, onLogin }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const submit = (event) => {
    event.preventDefault();
    onLogin(username, password);
  };
  return (
    <main className="grid min-h-screen place-items-center bg-[#090d16] px-5">
      <section className="w-full max-w-md rounded-2xl border border-slate-800 bg-[#0d1320] p-8 shadow-2xl shadow-black/20">
        <div className="mb-7 flex items-center gap-3">
          <div className="rounded-xl bg-indigo-500/15 p-3 text-indigo-300"><LockKeyhole size={21} /></div>
          <div><h1 className="text-lg font-semibold text-white">Admin sign in</h1><p className="mt-1 text-xs text-slate-500">Media Ingestion control panel</p></div>
        </div>
        {notice && <div role="alert" className="mb-4 rounded-lg border border-rose-900/60 bg-rose-950/30 px-3 py-2 text-sm text-rose-200">{notice}</div>}
        <form onSubmit={submit} className="space-y-4">
          <label className="block text-sm text-slate-300">Username
            <input autoComplete="username" required value={username} onChange={(event) => setUsername(event.target.value)} className="field mt-2" />
          </label>
          <label className="block text-sm text-slate-300">Password
            <input type="password" autoComplete="current-password" required value={password} onChange={(event) => setPassword(event.target.value)} className="field mt-2" />
          </label>
          <button disabled={busy} className="primary w-full">{busy ? "Signing in…" : "Sign in"}</button>
        </form>
        <p className="mt-5 text-xs leading-relaxed text-slate-500">Admin credentials are configured on the server. Sessions use an HTTP-only, signed cookie.</p>
        <style>{`.field{width:100%;border:1px solid #263244;background:#090d16;color:#e2e8f0;border-radius:.6rem;padding:.65rem .8rem;font-size:.8rem;outline:none}.field:focus{border-color:#818cf8}.primary,.secondary{display:inline-flex;align-items:center;justify-content:center;gap:.5rem;border-radius:.6rem;padding:.65rem 1rem;font-size:.8rem;font-weight:600}.primary{background:#4f46e5;color:white}.primary:hover{background:#6366f1}`}</style>
      </section>
    </main>
  );
}

function Status({ status }) {
  const tones = { PENDING: "slate", DOWNLOADING: "blue", VERIFYING: "yellow", CLEANING: "yellow", CANCELLING: "yellow", COMPLETED: "green", FAILED: "red", CANCELLED: "slate" };
  return <Pill tone={tones[status] || "slate"}>{status}</Pill>;
}

export default App;
