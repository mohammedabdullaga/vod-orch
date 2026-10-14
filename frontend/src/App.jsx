import { useCallback, useEffect, useState } from "react";
import axios from "axios";
import ContentBrowser from "./ContentBrowser";
import {
  Activity, Check, CircleAlert, Clapperboard, Film, Gauge, LogOut, LockKeyhole,
  Play, Plus, Radio, RefreshCw, Server, Settings2, Trash2, Tv, Wifi,
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

function App() {
  const [authenticated, setAuthenticated] = useState(null);
  const [adminUsername, setAdminUsername] = useState("");
  const [tab, setTab] = useState(tabs[0]);
  const [status, setStatus] = useState(null);
  const [queue, setQueue] = useState([]);
  const [categories, setCategories] = useState([]);
  const [accounts, setAccounts] = useState([]);
  const [proxies, setProxies] = useState([]);
  const [nodes, setNodes] = useState([]);
  const [routing, setRouting] = useState({ series_node_id: null, movie_node_id: null });
  const [editingNodeId, setEditingNodeId] = useState(null);
  const [nodeForm, setNodeForm] = useState({
    name: "", host: "", port: 22, username: "", private_key_path: "", staging_path: "", watch_path: "",
  });
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [accountForm, setAccountForm] = useState({ provider_url: "", username: "", password: "", user_agent: "Mozilla/5.0" });
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
      const [s, q] = await Promise.all([api.get("/status"), api.get("/queue")]);
      setStatus(s.data); setQueue(q.data);
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
      const [a, p, n, r] = await Promise.all([
        api.get("/accounts"), api.get("/proxies"), api.get("/nodes"), api.get("/settings/storage-routing"),
      ]);
      setAccounts(a.data); setProxies(p.data); setNodes(n.data); setRouting(r.data);
    } catch (error) { setNotice(error.response?.data?.detail || "Unable to load settings."); }
  };
  const loadCategories = async () => {
    try {
      const categoryResponse = await api.get("/categories");
      setCategories(categoryResponse.data);
    }
    catch (error) { setNotice(error.response?.data?.detail || "Unable to load categories."); }
  };
  useEffect(() => {
    if (!authenticated) return;
    if (tab === tabs[1]) loadSettings();
    if (tab === tabs[2]) {
      loadCategories();
      loadSettings();
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

  const toggleCategory = async (category) => {
    const next = !category.monitored;
    await act(async () => {
      await api.post("/categories/toggle", {
        category_id: category.category_id, name: category.name, kind: category.kind,
        monitored: next, destination_path: category.destination_path || null,
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
  const addAccount = (event) => {
    event.preventDefault();
    act(async () => { await api.post("/accounts", accountForm); setAccountForm({ provider_url: "", username: "", password: "", user_agent: "Mozilla/5.0" }); await loadSettings(); }, "Provider account added.");
  };
  const addProxy = (event) => {
    event.preventDefault();
    act(async () => { await api.post("/proxies", { url: proxyUrl }); setProxyUrl(""); await loadSettings(); }, "Proxy added.");
  };
  const saveNode = (event, test = false) => {
    event?.preventDefault();
    act(async () => {
      const payload = { ...nodeForm, port: Number(nodeForm.port), test };
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
    setNodeForm({ ...node, port: Number(node.port) });
  };
  const resetNodeForm = () => {
    setEditingNodeId(null);
    setNodeForm({ name: "", host: "", port: 22, username: "", private_key_path: "", staging_path: "", watch_path: "" });
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
            <div className="flex items-center justify-between border-b border-slate-800 px-5 py-4"><div><h2 className="font-semibold text-white">Ingestion queue</h2><p className="mt-1 text-xs text-slate-500">Latest 500 discovered streams</p></div><button onClick={refresh} className="rounded-lg p-2 text-slate-400 hover:bg-slate-800 hover:text-white" aria-label="Refresh queue"><RefreshCw size={16} /></button></div>
            <div className="overflow-x-auto"><table className="w-full min-w-[780px] text-left text-sm">
              <thead className="bg-slate-900/60 text-xs uppercase tracking-wide text-slate-500"><tr><th className="px-5 py-3">Title</th><th className="px-4 py-3">Type</th><th className="px-4 py-3">Status</th><th className="px-4 py-3">Progress</th><th className="px-4 py-3">Account</th><th className="px-4 py-3">Action</th></tr></thead>
              <tbody className="divide-y divide-slate-800/80">{queue.length ? queue.map((item) => <tr key={item.id} className="hover:bg-slate-800/20">
                <td className="px-5 py-4"><div className="font-medium text-slate-200">{item.title}</div>{item.series_title && <div className="mt-1 text-xs text-slate-500">{item.series_title} · S{item.season ?? "—"} E{item.episode ?? "—"}</div>}</td>
                <td className="px-4 py-4 text-slate-400">{item.kind === "series" ? <Tv size={16} /> : <Film size={16} />}</td>
                <td className="px-4 py-4"><Status status={item.status} /></td>
                <td className="px-4 py-4"><div className="flex items-center gap-2"><div className="h-1.5 w-20 overflow-hidden rounded bg-slate-800"><div className="h-full bg-indigo-400" style={{ width: `${item.progress || 0}%` }} /></div><span className="text-xs text-slate-500">{Math.round(item.progress || 0)}%</span></div></td>
                <td className="px-4 py-4 text-slate-400">{item.account || "—"}</td><td className="px-4 py-4">{item.status === "FAILED" && <button disabled={busy} onClick={() => retry(item.id)} className="flex items-center gap-1.5 text-xs text-indigo-300 hover:text-indigo-200"><RefreshCw size={13} />Retry</button>}</td>
              </tr>) : <tr><td colSpan="6" className="px-5 py-14 text-center text-slate-500">No streams discovered yet. Enable a monitored category to begin.</td></tr>}</tbody>
            </table></div>
          </section>
        </>}

        {tab === tabs[1] && <ContentBrowser api={api} routing={routing} onQueued={refresh} />}

        {tab === tabs[2] && <section className="rounded-2xl border border-slate-800 bg-[#0d1320]">
          <div className="border-b border-slate-800 px-5 py-4"><h2 className="font-semibold text-white">Category watcher</h2><p className="mt-1 text-xs text-slate-500">Watched items inherit the storage root unless this category has its own remote path. Provider IDs keep same-named shows and movies separate.</p>
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
              <button disabled={busy} className="secondary w-full">Save path · blank uses root</button>
            </form>
          </div>)}{!categories.length && <div className="col-span-full py-12 text-center text-sm text-slate-500">Add an enabled provider account to load categories.</div>}</div>
        </section>}

        {tab === tabs[3] && <div className="grid gap-6 lg:grid-cols-2">
          <section className="rounded-2xl border border-slate-800 bg-[#0d1320]">
            <div className="flex items-center gap-2 border-b border-slate-800 px-5 py-4"><Radio size={17} className="text-indigo-300" /><div><h2 className="font-semibold text-white">Provider accounts</h2><p className="text-xs text-slate-500">One active download per enabled account</p></div></div>
            <div className="space-y-2 p-5">{accounts.map((account) => <div key={account.id} className="flex items-center justify-between rounded-lg bg-slate-900/70 px-3 py-2.5"><div><div className="text-sm text-slate-200">{account.username}</div><div className="text-xs text-slate-500">{account.provider_url}</div></div><button onClick={() => remove("/accounts", account.id)} className="p-2 text-slate-500 hover:text-rose-300" aria-label={`Remove ${account.username}`}><Trash2 size={15} /></button></div>)}
              <form onSubmit={addAccount} className="mt-4 space-y-2 border-t border-slate-800 pt-4">
                <input required type="url" placeholder="Provider base URL" value={accountForm.provider_url} onChange={(e) => setAccountForm({ ...accountForm, provider_url: e.target.value })} className="field" />
                <div className="grid grid-cols-2 gap-2"><input required placeholder="Username" value={accountForm.username} onChange={(e) => setAccountForm({ ...accountForm, username: e.target.value })} className="field" /><input required type="password" placeholder="Password" value={accountForm.password} onChange={(e) => setAccountForm({ ...accountForm, password: e.target.value })} className="field" /></div>
                <input placeholder="User-Agent" value={accountForm.user_agent} onChange={(e) => setAccountForm({ ...accountForm, user_agent: e.target.value })} className="field" />
                <button disabled={busy} className="primary"><Plus size={15} />Add account</button>
              </form>
            </div>
          </section>
          <section className="rounded-2xl border border-slate-800 bg-[#0d1320]">
            <div className="flex items-center gap-2 border-b border-slate-800 px-5 py-4"><Gauge size={17} className="text-indigo-300" /><div><h2 className="font-semibold text-white">Proxy pool</h2><p className="text-xs text-slate-500">Proxy URLs are encrypted at rest</p></div></div>
            <div className="space-y-2 p-5">{proxies.map((proxy) => <div key={proxy.id} className="flex items-center justify-between rounded-lg bg-slate-900/70 px-3 py-2.5"><span className="font-mono text-xs text-slate-300">{proxy.url}</span><div className="flex items-center"><button onClick={() => testProxy(proxy.id)} disabled={busy} className="px-2 text-xs text-indigo-300 hover:text-indigo-200">Test latency/IP</button><button onClick={() => remove("/proxies", proxy.id)} className="p-2 text-slate-500 hover:text-rose-300" aria-label="Remove proxy"><Trash2 size={15} /></button></div></div>)}
              <form onSubmit={addProxy} className="mt-4 flex gap-2 border-t border-slate-800 pt-4"><input required placeholder="http://user:pass@host:port" value={proxyUrl} onChange={(e) => setProxyUrl(e.target.value)} className="field" /><button disabled={busy} className="primary"><Plus size={15} />Add</button></form>
            </div>
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
                  <div className="min-w-0"><div className="truncate text-sm text-slate-200">{node.name}</div><div className="truncate text-xs text-slate-500">{node.username}@{node.host}:{node.port} · {node.watch_path}</div><div className="mt-1 text-[10px] text-indigo-300">{[routing.series_node_id === node.id && "Series", routing.movie_node_id === node.id && "Movies"].filter(Boolean).join(" · ") || "Not assigned"}</div></div>
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
                <div className="flex flex-wrap gap-2 sm:col-span-2 lg:col-span-3">
                  <button disabled={busy} className="primary"><Plus size={15} />{editingNodeId ? "Save server" : "Add server"}</button>
                  <button type="button" disabled={busy} onClick={(event) => saveNode(event, true)} className="secondary"><Wifi size={15} />Test connection &amp; save</button>
                  {editingNodeId && <button type="button" onClick={resetNodeForm} className="secondary">Cancel edit</button>}
                </div>
              </form>
            </div>
          </section>
        </div>}
      </main>
      <style>{`.field{width:100%;border:1px solid #263244;background:#090d16;color:#e2e8f0;border-radius:.6rem;padding:.65rem .8rem;font-size:.8rem;outline:none}.field:focus{border-color:#818cf8}.primary,.secondary{display:inline-flex;align-items:center;justify-content:center;gap:.5rem;border-radius:.6rem;padding:.65rem 1rem;font-size:.8rem;font-weight:600}.primary{background:#4f46e5;color:white}.primary:hover{background:#6366f1}.secondary{border:1px solid #334155;color:#cbd5e1}.secondary:hover{background:#1e293b}`}</style>
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
  const tones = { PENDING: "slate", DOWNLOADING: "blue", VERIFYING: "yellow", COMPLETED: "green", FAILED: "red" };
  return <Pill tone={tones[status] || "slate"}>{status}</Pill>;
}

export default App;
