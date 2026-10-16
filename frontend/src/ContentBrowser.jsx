import { useEffect, useMemo, useState } from "react";
import {
  Check, ChevronDown, Download, Film, LayoutGrid, List, LoaderCircle, Search, Tv, X,
} from "lucide-react";

const kindFor = (type) => type === "series" ? "series" : "vod";

function seasonNumber(value, fallback = 0) {
  const match = String(value ?? "").match(/\d+/);
  return match ? Number(match[0]) : fallback;
}

function episodeFilename(season, number, extension) {
  return `S${String(season || 0).padStart(2, "0")}E${String(number || 0).padStart(2, "0")}.${extension}`;
}

function InventoryBadge({ status }) {
  const badge = {
    downloaded: ["On storage", "bg-emerald-500/15 text-emerald-300"],
    partial: ["Partial", "bg-sky-500/15 text-sky-200"],
    missing: ["Missing", "bg-amber-500/15 text-amber-200"],
    unknown: ["Storage unknown", "bg-slate-700 text-slate-300"],
  }[status] || ["Storage unknown", "bg-slate-700 text-slate-300"];
  return <span className={`ml-2 inline-flex rounded-full px-2 py-0.5 align-middle text-[10px] font-medium ${badge[1]}`}>{badge[0]}</span>;
}

export default function ContentBrowser({
  api,
  playlists,
  routing,
  onQueued,
  proxies,
  providerProxyId,
  setProviderProxyId,
  providerProxyBusy,
  providerProxyReady,
}) {
  const [type, setType] = useState("series");
  const [sourceType, setSourceType] = useState("xtream");
  const [sourcePlaylistId, setSourcePlaylistId] = useState("");
  const [providerAccounts, setProviderAccounts] = useState([]);
  const [sourceAccountId, setSourceAccountId] = useState("");
  const [categories, setCategories] = useState([]);
  const [selectedCategories, setSelectedCategories] = useState(new Set());
  const [categorySearch, setCategorySearch] = useState("");
  const [items, setItems] = useState([]);
  const [selectedItems, setSelectedItems] = useState(new Set());
  const [viewMode, setViewMode] = useState("grid");
  const [itemSearch, setItemSearch] = useState("");
  const [inventoryFilter, setInventoryFilter] = useState("all");
  const [details, setDetails] = useState(null);
  const [selectedEpisodes, setSelectedEpisodes] = useState(new Map());
  const [pendingQueue, setPendingQueue] = useState(null);
  const [pendingSeries, setPendingSeries] = useState(null);
  const [pendingMissingOnly, setPendingMissingOnly] = useState(false);
  const [availableAccounts, setAvailableAccounts] = useState([]);
  const [selectedWorkerIds, setSelectedWorkerIds] = useState(new Set());
  const [availableProxies, setAvailableProxies] = useState([]);
  const [selectedProxyId, setSelectedProxyId] = useState("");
  const [proxyAvailable, setProxyAvailable] = useState(false);
  const [proxyMode, setProxyMode] = useState("direct");
  const [downloader, setDownloader] = useState("axel");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const selectedProxy = availableProxies.find((proxy) => String(proxy.id) === selectedProxyId);
  const pendingCount = pendingSeries?.length ?? pendingQueue?.length ?? 0;
  const wgetProxyUnsupported = downloader === "wget"
    && proxyMode === "node"
    && /^socks5h?:/i.test(selectedProxy?.url || "");

  useEffect(() => {
    let current = true;
    api.get("/accounts")
      .then(({ data }) => {
        if (!current) return;
        const enabled = data.filter((account) => account.enabled);
        setProviderAccounts(enabled);
        setSourceAccountId((selected) =>
          enabled.some((account) => String(account.id) === selected)
            ? selected
            : String(enabled[0]?.id || "")
        );
      })
      .catch((error) => {
        if (current) setNotice(error.response?.data?.detail || "Could not load Xtream accounts.");
      });
    return () => { current = false; };
  }, [api]);

  useEffect(() => {
    setSourcePlaylistId((selected) =>
      playlists.some((playlist) => String(playlist.id) === selected)
        ? selected
        : String(playlists[0]?.id || "")
    );
  }, [playlists]);

  useEffect(() => {
    let current = true;
    setCategories([]);
    setSelectedCategories(new Set());
    setItems([]);
    setSelectedItems(new Set());
    setDetails(null);
    setNotice("");
    if (sourceType === "m3u") {
      if (!sourcePlaylistId) return () => { current = false; };
      api.get(`/playlists/${sourcePlaylistId}/categories`, { params: { type } })
        .then(({ data }) => { if (current) setCategories(data); })
        .catch((error) => { if (current) setNotice(error.response?.data?.detail || "Could not load M3U categories."); });
      return () => { current = false; };
    }
    if (!providerProxyReady || !sourceAccountId) return () => { current = false; };
    api.get("/content/categories", {
      params: {
        type,
        account_id: sourceAccountId,
        proxy_id: providerProxyId ? Number(providerProxyId) : 0,
      },
    })
      .then(({ data }) => { if (current) setCategories(data); })
      .catch((error) => { if (current) setNotice(error.response?.data?.detail || "Could not load provider categories."); });
    return () => { current = false; };
  }, [api, type, sourceType, sourcePlaylistId, sourceAccountId, providerProxyId, providerProxyReady]);

  const visibleCategories = useMemo(() => categories.filter((category) =>
    category.category_name.toLocaleLowerCase().includes(categorySearch.toLocaleLowerCase())
  ), [categories, categorySearch]);
  const visibleItems = useMemo(() => items.filter((item) =>
    `${item.name} ${item.category_name}`.toLocaleLowerCase().includes(itemSearch.toLocaleLowerCase())
  ), [items, itemSearch]);
  const inventoryCounts = useMemo(() => ({
    downloaded: items.filter((item) => item.download_status === "downloaded").length,
    partial: items.filter((item) => item.download_status === "partial").length,
    missing: items.filter((item) => item.download_status === "missing").length,
    unknown: items.filter((item) => item.download_status === "unknown").length,
  }), [items]);
  const displayedItems = useMemo(() => visibleItems.filter((item) =>
    inventoryFilter === "all" || item.download_status === inventoryFilter
  ), [visibleItems, inventoryFilter]);
  const selectedTitles = useMemo(
    () => items.filter((item) => selectedItems.has(item.id)),
    [items, selectedItems],
  );
  const selectedMonitoredCategories = categories.filter(
    (category) => selectedCategories.has(category.category_id) && category.monitored,
  );
  const knownEpisodeCount = selectedTitles.reduce(
    (total, item) => total + (Number(item.total_episodes) || 0),
    0,
  );
  const unknownEpisodeCount = selectedTitles.filter(
    (item) => type === "series" && item.total_episodes == null,
  ).length;
  const nodeId = type === "series" ? routing?.series_node_id : routing?.movie_node_id;
  const nodeLabel = type === "series" ? "Series" : "Movies";

  const toggleCategorySelection = (categoryId) => {
    setSelectedCategories((current) => {
      const next = new Set(current);
      if (next.has(categoryId)) next.delete(categoryId);
      else next.add(categoryId);
      return next;
    });
  };

  const selectVisibleCategories = () => {
    setSelectedCategories((current) => {
      const next = new Set(current);
      const allSelected = visibleCategories.every((category) => next.has(category.category_id));
      visibleCategories.forEach((category) => {
        if (allSelected) next.delete(category.category_id);
        else next.add(category.category_id);
      });
      return next;
    });
  };

  const setSelectedMonitoring = async (monitored) => {
    const selected = categories.filter((category) =>
      selectedCategories.has(category.category_id)
      && (monitored || category.monitored)
    );
    if (!selected.length) return setNotice("Select at least one category to monitor.");
    setBusy(true);
    setNotice("");
    try {
      for (const category of selected) {
        await api.post("/categories/toggle", {
          category_id: category.category_id,
          name: category.category_name,
          kind: kindFor(type),
          monitored,
          destination_path: category.destination_path || null,
          match_existing_series: category.match_existing_series || false,
          strip_prefixes: category.strip_prefixes || "",
        });
      }
      setCategories((current) => current.map((category) =>
        selected.some((selectedCategory) => selectedCategory.category_id === category.category_id)
          ? { ...category, monitored }
          : category
      ));
      setSelectedCategories((current) => {
        const next = new Set(current);
        selected.forEach((category) => next.delete(category.category_id));
        return next;
      });
      setNotice(
        monitored
          ? `Monitoring enabled for ${selected.length} categor${selected.length === 1 ? "y" : "ies"}.`
          : `Monitoring disabled for ${selected.length} categor${selected.length === 1 ? "y" : "ies"}.`
      );
    } catch (error) {
      setNotice(error.response?.data?.detail || "Could not update category monitoring.");
    } finally {
      setBusy(false);
    }
  };

  const browseSelected = async (forceRefresh = false) => {
    if (!selectedCategories.size) return setNotice("Select one or more categories to browse.");
    setBusy(true);
    setNotice("");
    setSelectedItems(new Set());
    setItemSearch("");
    try {
      if (sourceType === "m3u") {
        if (forceRefresh) {
          await api.post(`/playlists/${sourcePlaylistId}/refresh`, null, {
            params: { proxy_id: providerProxyId ? Number(providerProxyId) : 0 },
          });
          const { data: refreshedCategories } = await api.get(
            `/playlists/${sourcePlaylistId}/categories`,
            { params: { type } },
          );
          setCategories(refreshedCategories);
        }
        const { data } = await api.get(`/playlists/${sourcePlaylistId}/items`, {
          params: { type, category_ids: [...selectedCategories].join(",") },
        });
        setItems(data);
        setInventoryFilter("all");
        setNotice(`Loaded ${data.length} ${type === "series" ? "series" : "movies"} from the local M3U catalog.`);
        return;
      }
      if (forceRefresh) {
        const { data: refreshedCategories } = await api.get("/content/categories", {
          params: {
            type,
            account_id: sourceAccountId,
            proxy_id: providerProxyId ? Number(providerProxyId) : 0,
            refresh: true,
          },
        });
        setCategories(refreshedCategories);
      }
      const { data } = await api.get("/content/items", {
        params: {
          type,
          account_id: sourceAccountId,
          category_ids: [...selectedCategories].join(","),
          proxy_id: providerProxyId ? Number(providerProxyId) : 0,
          refresh: forceRefresh,
        },
      });
      setItems(data);
      setInventoryFilter("all");
      const missing = data.filter((item) => item.download_status === "missing").length;
      const partial = data.filter((item) => item.download_status === "partial").length;
      const unknown = data.filter((item) => item.download_status === "unknown").length;
      setNotice(
        `Loaded ${data.length} ${type === "series" ? "series" : "movies"}; `
        + `${missing} not found · ${partial} incomplete${unknown ? ` · ${unknown} storage status${unknown === 1 ? "" : "es"} unavailable` : ""}.`
      );
    } catch (error) {
      setNotice(error.response?.data?.detail || "Could not load content.");
    } finally {
      setBusy(false);
    }
  };

  const toggleItemSelection = (id) => {
    setSelectedItems((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const selectAllVisible = () => {
    setSelectedItems((current) => new Set([...current, ...displayedItems.map((item) => item.id)]));
  };

  const fetchDetails = async (item) => {
    setBusy(true);
    setNotice("");
    try {
      const { data } = sourceType === "m3u"
        ? await api.get(
          `/playlists/${sourcePlaylistId}/series/${encodeURIComponent(item.source_series_key)}/details`,
        )
        : await api.get(`/content/series/${encodeURIComponent(item.id)}/details`, {
          params: {
            account_id: sourceAccountId,
            category_id: item.category_id,
            category_name: item.category_name,
            proxy_id: providerProxyId ? Number(providerProxyId) : 0,
          },
        });
      setDetails({ item, data });
      setSelectedEpisodes(new Map());
    } catch (error) {
      setNotice(error.response?.data?.detail || "Could not load seasons and episodes.");
    } finally {
      setBusy(false);
    }
  };

  const queueEntries = async (entries) => {
    if (!nodeId) {
      setNotice(`Assign a storage server for ${nodeLabel} in Settings & Accounts before adding downloads.`);
      return;
    }
    if (!entries.length) {
      setNotice("Select at least one episode or title to queue.");
      return;
    }
    setBusy(true);
    setNotice("");
    try {
      const { data } = await api.get("/accounts/availability", {
        params: {
          content_type: type === "series" ? "SERIES" : "MOVIE",
          ...(sourceType === "xtream"
            ? { source_account_id: sourceAccountId }
            : { source_playlist_id: sourcePlaylistId }),
        },
      });
      const free = data.accounts.filter((account) => account.free);
      if (!free.length) {
        setNotice("No idle Xtream accounts are available for downloading. Wait for current downloads or add an account.");
        return;
      }
      setPendingQueue(entries);
      setPendingSeries(null);
      setAvailableAccounts(free);
      setSelectedWorkerIds(new Set(free.map((account) => account.id)));
      const proxies = data.proxies || [];
      setAvailableProxies(proxies);
      setProxyAvailable(proxies.length > 0);
      const preferredProxy = proxies.find(
        (proxy) => proxy.id === data.assigned_proxy_id,
      ) || proxies[0];
      setSelectedProxyId(preferredProxy ? String(preferredProxy.id) : "");
      setProxyMode(proxies.length > 0 ? "node" : "direct");
    } catch (error) {
      setNotice(error.response?.data?.detail || "Could not check free provider accounts.");
    } finally {
      setBusy(false);
    }
  };

  const prepareSeriesQueue = async (series, missingOnly = false) => {
    if (!nodeId) {
      setNotice(`Assign a storage server for ${nodeLabel} in Settings & Accounts before adding downloads.`);
      return;
    }
    if (!series.length) {
      setNotice("Select at least one series to queue.");
      return;
    }
    setBusy(true);
    setNotice("");
    try {
      const { data } = await api.get("/accounts/availability", {
        params: {
          content_type: "SERIES",
          ...(sourceType === "xtream"
            ? { source_account_id: sourceAccountId }
            : { source_playlist_id: sourcePlaylistId }),
        },
      });
      const free = data.accounts.filter((account) => account.free);
      if (!free.length) {
        setNotice("No idle Xtream accounts are available for downloading. Wait for current downloads or add an account.");
        return;
      }
      setPendingQueue([]);
      setPendingSeries(series);
      setPendingMissingOnly(missingOnly);
      setAvailableAccounts(free);
      setSelectedWorkerIds(new Set(free.map((account) => account.id)));
      const proxies = data.proxies || [];
      setAvailableProxies(proxies);
      setProxyAvailable(proxies.length > 0);
      const preferredProxy = proxies.find(
        (proxy) => proxy.id === data.assigned_proxy_id,
      ) || proxies[0];
      setSelectedProxyId(preferredProxy ? String(preferredProxy.id) : "");
      setProxyMode(proxies.length > 0 ? "node" : "direct");
    } catch (error) {
      setNotice(error.response?.data?.detail || "Could not check free provider accounts.");
    } finally {
      setBusy(false);
    }
  };

  const confirmQueueEntries = async () => {
    if ((!pendingQueue?.length && !pendingSeries?.length) || !selectedWorkerIds.size) {
      setNotice("Choose at least one free provider account.");
      return;
    }
    setBusy(true);
    setNotice("");
    try {
      let queued = 0;
      let skipped = 0;
      const accountIds = [...selectedWorkerIds];
      if (pendingSeries?.length) {
        const { data } = await api.post("/queue/prepare-series-batch", {
          ...(sourceType === "m3u"
            ? { source_playlist_id: Number(sourcePlaylistId) }
            : {
              source_account_id: Number(sourceAccountId),
              provider_proxy_id: providerProxyId ? Number(providerProxyId) : 0,
            }),
          account_ids: accountIds,
          proxy_mode: proxyMode === "node" ? "selected" : "direct",
          proxy_id: proxyMode === "node" ? Number(selectedProxyId) : null,
          downloader,
          missing_only: pendingMissingOnly,
          series: pendingSeries.map((item) => ({
            series_id: sourceType === "m3u" ? item.source_series_key : item.id,
            title: item.name,
            category_id: item.category_id,
            category_name: item.category_name,
          })),
        });
        queued = data.queued_count;
        skipped = data.skipped.length;
        const failed = data.failed || [];
        const firstFailure = failed[0]?.title
          ? ` First failed series: ${failed[0].title} — ${failed[0].error}`
          : "";
        setNotice(
          `Prepared ${data.processed_series}/${pendingSeries.length} series; `
          + `queued ${pendingMissingOnly ? "missing " : ""}${queued} episode${queued === 1 ? "" : "s"}; `
          + `skipped ${skipped} existing/duplicate item${skipped === 1 ? "" : "s"}; `
          + `failed ${failed.length}.${firstFailure}`
        );
        setPendingQueue(null);
        setPendingSeries(null);
        setPendingMissingOnly(false);
        if (queued) onQueued?.();
        return;
      }
      const { data } = await api.post("/queue/manual-batch", {
        content_type: type === "series" ? "SERIES" : "MOVIE",
        ...(sourceType === "m3u"
          ? { source_playlist_id: Number(sourcePlaylistId) }
          : { source_account_id: Number(sourceAccountId) }),
        target_node_id: String(nodeId),
        account_ids: accountIds,
        proxy_mode: proxyMode === "node" ? "selected" : "direct",
        proxy_id: proxyMode === "node" ? Number(selectedProxyId) : null,
        downloader,
        items: pendingQueue,
      });
      queued = data.queued_count;
      skipped = data.skipped.length;
      setNotice(`Queued ${queued} item${queued === 1 ? "" : "s"}; skipped ${skipped} duplicate${skipped === 1 ? "" : "s"}.`);
      setPendingQueue(null);
      setPendingSeries(null);
      setPendingMissingOnly(false);
      onQueued?.();
    } catch (error) {
      setNotice(error.response?.data?.detail || "Could not queue downloads. Refresh free account availability and retry.");
    } finally {
      setBusy(false);
    }
  };

  const cancelQueueConfirmation = () => {
    setPendingQueue(null);
    setPendingSeries(null);
    setPendingMissingOnly(false);
  };

  const toggleWorker = (accountId) => {
    setSelectedWorkerIds((current) => {
      const next = new Set(current);
      if (next.has(accountId)) next.delete(accountId);
      else next.add(accountId);
      return next;
    });
  };

  const detailsEntries = (item, data) => Object.entries(data.seasons || {}).flatMap(([seasonKey, episodes]) =>
    episodes.map((episode) => {
      const season = episode.season ?? seasonNumber(seasonKey);
      const number = episode.episode_num ?? 0;
      const ext = episode.ext || "mkv";
      return {
        stream_id: episode.id,
        title: `${data.name} S${String(season).padStart(2, "0")}E${String(number).padStart(2, "0")}`,
        series_name: data.name,
        series_id: data.series_id || item.id,
        source_entry_id: episode.source_entry_id,
        season: String(season),
        episode_num: number,
        filename: episodeFilename(season, number, ext),
        container_ext: ext,
        on_storage: episode.on_storage ?? null,
        category_id: item.category_id,
        category_name: item.category_name,
      };
    })
  );

  const entriesForTitles = async (chosen) => {
    if (type === "movie") {
      return chosen.map((item) => ({
        stream_id: item.id,
        source_entry_id: item.source_entry_id,
        title: item.name,
        filename: `${item.name}.${item.container_ext || "mkv"}`,
        container_ext: item.container_ext || "mkv",
        category_id: item.category_id,
        category_name: item.category_name,
      }));
    }
    const entries = [];
    for (const item of chosen) {
      const { data } = sourceType === "m3u"
        ? await api.get(
          `/playlists/${sourcePlaylistId}/series/${encodeURIComponent(item.source_series_key)}/details`,
        )
        : await api.get(`/content/series/${encodeURIComponent(item.id)}/details`, {
          params: {
            account_id: sourceAccountId,
            category_id: item.category_id,
            category_name: item.category_name,
            proxy_id: providerProxyId ? Number(providerProxyId) : 0,
          },
        });
      entries.push(...detailsEntries(item, data));
    }
    return entries;
  };

  const queueFullSeries = async (item) => {
    await prepareSeriesQueue([item]);
  };

  const queueMissingSeries = async (item) => {
    await prepareSeriesQueue([item], true);
  };

  const queueSelectedTitles = async () => {
    if (!selectedTitles.length) return setNotice("Select at least one title to queue.");
    if (type === "series") return prepareSeriesQueue(selectedTitles, true);
    setBusy(true);
    setNotice("");
    try {
      await queueEntries(await entriesForTitles(selectedTitles));
    } catch (error) {
      setNotice(error.response?.data?.detail || "Could not load selected titles.");
      setBusy(false);
    }
  };

  const exportSelectedScript = async () => {
    if (!selectedTitles.length) return setNotice("Select at least one title to export.");
    if (!window.confirm("The generated shell script contains provider stream URLs with account credentials. Only save it to a trusted location, and delete it after use. Continue?")) return;
    setBusy(true);
    setNotice("");
    try {
      const entries = await entriesForTitles(selectedTitles);
      if (!entries.length) throw new Error("No downloadable episodes or movies were found.");
      const { data } = await api.post("/content/generate-script", {
        content_type: type === "series" ? "SERIES" : "MOVIE",
        source_account_id: Number(sourceAccountId),
        items: entries,
      }, { responseType: "blob" });
      const content = new Blob([data], { type: "text/x-shellscript;charset=utf-8" });
      const url = URL.createObjectURL(content);
      const link = document.createElement("a");
      link.href = url;
      link.download = `media-ingest-${new Date().toISOString().replace(/[-:]/g, "").slice(0, 15)}.sh`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      setNotice("Script downloaded. Treat it as a secret: it contains credential-bearing stream URLs.");
    } catch (error) {
      let detail = error.response?.data?.detail;
      if (error.response?.data instanceof Blob) {
        try {
          detail = JSON.parse(await error.response.data.text()).detail;
        } catch {
          detail = null;
        }
      }
      setNotice(detail || error.message || "Could not generate the download script.");
    } finally {
      setBusy(false);
    }
  };

  const toggleEpisode = (key, entry) => {
    setSelectedEpisodes((current) => {
      const next = new Map(current);
      if (next.has(key)) next.delete(key);
      else next.set(key, entry);
      return next;
    });
  };

  const toggleSeason = (seasonKey) => {
    const entries = detailsEntries(details.item, details.data).filter((entry) => entry.season === String(seasonNumber(seasonKey)));
    const keys = entries.map((entry) => `${details.item.id}:${entry.stream_id}`);
    const allSelected = keys.length > 0 && keys.every((key) => selectedEpisodes.has(key));
    setSelectedEpisodes((current) => {
      const next = new Map(current);
      entries.forEach((entry, index) => {
        if (allSelected) next.delete(keys[index]);
        else next.set(keys[index], entry);
      });
      return next;
    });
  };

  const selectMissingEpisodes = () => {
    if (!details) return;
    const missing = detailsEntries(details.item, details.data)
      .filter((entry) => entry.on_storage === false);
    setSelectedEpisodes(new Map(
      missing.map((entry) => [
        `${details.item.id}:${entry.stream_id}`,
        entry,
      ]),
    ));
  };

  const queueSelectedEpisodes = () => queueEntries([...selectedEpisodes.values()]);
  const queueOneEpisode = (item, data, episode) => {
    const entry = detailsEntries(item, data).find((candidate) => candidate.stream_id === episode.id);
    if (entry) queueEntries([entry]);
  };

  return (
    <section className="space-y-5">
      <div className="rounded-2xl border border-slate-800 bg-[#0d1320] p-5">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <h2 className="font-semibold text-white">Browse &amp; Ingest</h2>
            <p className="mt-1 text-xs text-slate-500">Explore provider content and queue specific titles or episodes. {nodeId ? `${nodeLabel} will be sent to storage server #${nodeId}.` : `No ${nodeLabel.toLowerCase()} storage server is assigned.`}</p>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <label className="text-xs text-slate-400">
              Provider / M3U fetch proxy
              <select
                aria-label="Provider API proxy"
                value={providerProxyId}
                onChange={(event) => setProviderProxyId(event.target.value)}
                disabled={!providerProxyReady || busy || providerProxyBusy || Boolean(pendingQueue)}
                className="ml-2 max-w-64 rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-200 disabled:opacity-50"
              >
                <option value="">Direct connection</option>
                {proxies.map((proxy) => (
                  <option key={proxy.id} value={proxy.id}>Proxy #{proxy.id} · {proxy.url}</option>
                ))}
              </select>
            </label>
            <label className="text-xs text-slate-400">
              Catalog source
              <select
                aria-label="Catalog source"
                value={sourceType}
                onChange={(event) => setSourceType(event.target.value)}
                disabled={busy || Boolean(pendingQueue)}
                className="ml-2 max-w-64 rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-200"
              >
                <option value="xtream">Xtream account</option>
                <option value="m3u" disabled={!playlists.length}>Saved M3U playlist</option>
              </select>
            </label>
            {sourceType === "m3u" ? (
              <label className="text-xs text-slate-400">
                Saved playlist
                <select
                  aria-label="Saved M3U playlist"
                  value={sourcePlaylistId}
                  onChange={(event) => setSourcePlaylistId(event.target.value)}
                  disabled={!playlists.length || busy || Boolean(pendingQueue)}
                  className="ml-2 max-w-64 rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-200 disabled:opacity-50"
                >
                  {!playlists.length && <option value="">No saved M3U playlists</option>}
                  {playlists.map((playlist) => (
                    <option key={playlist.id} value={playlist.id}>
                      {playlist.name} · {playlist.entry_count} entries
                    </option>
                  ))}
                </select>
              </label>
            ) : <label className="text-xs text-slate-400">
              Browse using Xtream account
              <select
                aria-label="Browse using Xtream account"
                value={sourceAccountId}
                onChange={(event) => setSourceAccountId(event.target.value)}
                disabled={!providerAccounts.length || busy || Boolean(pendingQueue)}
                className="ml-2 max-w-64 rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-200 disabled:opacity-50"
              >
                {!providerAccounts.length && <option value="">No enabled Xtream accounts</option>}
                {providerAccounts.map((account) => (
                  <option key={account.id} value={account.id}>
                    #{account.id} · {account.username} · {account.provider_url}
                  </option>
                ))}
              </select>
            </label>}
            <div className="inline-flex rounded-lg border border-slate-700 bg-slate-900 p-1">
              {["series", "movie"].map((mediaType) => (
                <button key={mediaType} onClick={() => setType(mediaType)} className={`rounded-md px-4 py-2 text-sm ${type === mediaType ? "bg-indigo-500 text-white" : "text-slate-400 hover:text-white"}`}>
                  {mediaType === "series" ? <><Tv size={14} className="mr-2 inline" />Series</> : <><Film size={14} className="mr-2 inline" />Movies</>}
                </button>
              ))}
            </div>
          </div>
        </div>
        <p className="mt-3 text-xs text-slate-500">{sourceType === "m3u" ? "M3U categories and entries are read from the saved SQLite catalog. M3U downloads use each entry's exact stored stream URL; enabled Xtream accounts are only used as worker slots." : "Choose the account used to browse categories and titles, and whether provider API requests should use a proxy. This proxy is for category, title, and series-detail API requests; download proxy settings are chosen separately at queue time. At download time, the selected idle worker account supplies its own Xtream username and password for the same stream ID."}</p>

        <div className="mt-5 grid gap-5 lg:grid-cols-[minmax(260px,0.8fr)_minmax(0,1.6fr)]">
          <div className="rounded-xl border border-slate-800 bg-slate-900/40">
            <div className="border-b border-slate-800 p-4">
              <h3 className="text-sm font-medium text-slate-200">1. Choose categories</h3>
              <div className="relative mt-3">
                <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" />
                <input value={categorySearch} onChange={(event) => setCategorySearch(event.target.value)} placeholder="Search categories…" className="w-full rounded-lg border border-slate-700 bg-[#090d16] py-2 pl-9 pr-3 text-sm outline-none focus:border-indigo-400" />
              </div>
              <div className="mt-3 flex items-center justify-between text-xs">
                <span className="text-slate-500">{selectedCategories.size} selected</span>
                <div className="flex items-center gap-3">
                  {selectedCategories.size > 0 && <button onClick={() => setSelectedCategories(new Set())} className="text-slate-400 hover:text-slate-200">Clear selection</button>}
                  <button onClick={selectVisibleCategories} disabled={!visibleCategories.length} className="text-indigo-300 hover:text-indigo-200 disabled:opacity-40">
                    {visibleCategories.length && visibleCategories.every((category) => selectedCategories.has(category.category_id)) ? "Deselect visible" : "Select visible"}
                  </button>
                </div>
              </div>
            </div>
            <div className="max-h-72 overflow-y-auto p-2">
              {visibleCategories.map((category) => (
                <label key={category.category_id} className="flex cursor-pointer items-center gap-3 rounded-lg px-3 py-2.5 hover:bg-slate-800/60">
                  <input type="checkbox" checked={selectedCategories.has(category.category_id)} onChange={() => toggleCategorySelection(category.category_id)} className="accent-indigo-500" />
                  <span className="min-w-0 flex-1 truncate text-sm text-slate-300">{category.category_name}</span>
                  {category.monitored && <span className="rounded-full bg-emerald-400/10 px-2 py-0.5 text-[10px] text-emerald-300">Watching</span>}
                </label>
              ))}
              {!visibleCategories.length && <p className="px-3 py-8 text-center text-sm text-slate-500">{sourceType === "m3u" && !playlists.length ? "Import an M3U playlist in Settings & Accounts." : sourceType === "xtream" && !providerAccounts.length ? "Add an enabled Xtream account to browse." : categories.length ? "No matching categories." : "No categories available for this source and type."}</p>}
            </div>
            <div className="grid gap-2 border-t border-slate-800 p-3 sm:grid-cols-2">
              {sourceType === "xtream" && <>
                <button disabled={busy || !selectedCategories.size} onClick={() => setSelectedMonitoring(true)} className="rounded-lg border border-slate-700 px-3 py-2 text-xs font-medium text-slate-300 hover:bg-slate-800 disabled:opacity-40">Monitor selected</button>
                {selectedMonitoredCategories.length > 0 && <button disabled={busy} onClick={() => setSelectedMonitoring(false)} className="rounded-lg border border-amber-800/70 px-3 py-2 text-xs font-medium text-amber-300 hover:bg-amber-950/40 disabled:opacity-40">Stop monitoring selected ({selectedMonitoredCategories.length})</button>}
              </>}
              <button disabled={busy || !selectedCategories.size} onClick={() => browseSelected()} className="rounded-lg bg-indigo-500 px-3 py-2 text-xs font-semibold text-white hover:bg-indigo-400 disabled:opacity-40">Browse selected</button>
              <button disabled={busy || !selectedCategories.size} onClick={() => browseSelected(true)} title={sourceType === "m3u" ? "Fetch the latest playlist and replace its saved catalog." : "Fetch fresh categories and titles from the provider instead of using cached data."} className="rounded-lg border border-slate-700 px-3 py-2 text-xs font-semibold text-slate-300 hover:border-slate-500 hover:text-white disabled:opacity-40">{sourceType === "m3u" ? "Refresh playlist" : "Refresh from provider"}</button>
            </div>
          </div>

          <div className="min-w-0 rounded-xl border border-slate-800 bg-slate-900/40">
            <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-800 p-4">
              <div><h3 className="text-sm font-medium text-slate-200">2. Select {type === "series" ? "series" : "movies"}</h3><p className="mt-1 text-xs text-slate-500">{items.length} results · {inventoryCounts.downloaded} complete · {inventoryCounts.partial} partial · {inventoryCounts.missing} missing</p></div>
              <div className="flex flex-wrap items-center gap-3">
                <div className="inline-flex rounded-lg border border-slate-700 bg-[#090d16] p-1" aria-label="Items view">
                  <button type="button" aria-pressed={viewMode === "grid"} onClick={() => setViewMode("grid")} className={`rounded-md px-2.5 py-1.5 text-xs ${viewMode === "grid" ? "bg-indigo-500 text-white" : "text-slate-400 hover:text-white"}`}><LayoutGrid size={14} className="mr-1.5 inline" />Grid</button>
                  <button type="button" aria-pressed={viewMode === "list"} onClick={() => setViewMode("list")} className={`rounded-md px-2.5 py-1.5 text-xs ${viewMode === "list" ? "bg-indigo-500 text-white" : "text-slate-400 hover:text-white"}`}><List size={14} className="mr-1.5 inline" />List</button>
                </div>
                <div className="relative">
                  <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" />
                  <input value={itemSearch} onChange={(event) => setItemSearch(event.target.value)} placeholder="Search titles…" className="w-44 rounded-lg border border-slate-700 bg-[#090d16] py-2 pl-8 pr-3 text-xs outline-none focus:border-indigo-400" />
                </div>
              </div>
            </div>
            {items.length > 0 && <div role="status" aria-live="polite" className={`flex flex-wrap items-center justify-between gap-3 border-b border-slate-800 px-4 py-3 ${inventoryCounts.missing ? "bg-amber-500/5" : "bg-slate-900/40"}`}>
              <p className={`text-xs ${inventoryCounts.missing ? "text-amber-200" : inventoryCounts.partial ? "text-sky-200" : inventoryCounts.unknown ? "text-slate-400" : "text-emerald-300"}`}>
                {inventoryCounts.missing
                  ? `${inventoryCounts.missing} provider ${type === "series" ? "series" : "titles"} were not found in the selected storage folders.`
                  : inventoryCounts.partial
                    ? `${inventoryCounts.partial} series are present but do not have all known episodes on storage.`
                    : inventoryCounts.unknown
                    ? "Storage inventory could not be checked; unknown items are not marked as missing."
                    : "All listed provider content is complete in the selected storage folders."}
              </p>
              <div className="flex flex-wrap gap-1.5">
                {[
                  ["all", `All (${items.length})`],
                  ["missing", `Missing (${inventoryCounts.missing})`],
                  ["partial", `Partial (${inventoryCounts.partial})`],
                  ["downloaded", `On storage (${inventoryCounts.downloaded})`],
                  ["unknown", `Unknown (${inventoryCounts.unknown})`],
                ].map(([filter, label]) => <button key={filter} type="button" aria-pressed={inventoryFilter === filter} onClick={() => setInventoryFilter(filter)} className={`rounded-md px-2.5 py-1.5 text-[11px] ${inventoryFilter === filter ? "bg-indigo-500/20 text-indigo-200" : "text-slate-400 hover:bg-slate-800"}`}>{label}</button>)}
              </div>
            </div>}
            {items.length > 0 && <div className="sticky top-0 z-10 flex flex-wrap items-center justify-between gap-3 border-b border-slate-800 bg-[#101827]/95 px-4 py-3 backdrop-blur">
              <div className="flex flex-wrap items-center gap-2">
                <button type="button" onClick={selectAllVisible} disabled={!displayedItems.length} className="rounded-lg border border-slate-700 px-3 py-2 text-xs text-slate-300 hover:bg-slate-800 disabled:opacity-40">Select All ({displayedItems.length} items)</button>
                <button type="button" onClick={() => setSelectedItems(new Set())} disabled={!selectedItems.size} className="rounded-lg border border-slate-700 px-3 py-2 text-xs text-slate-300 hover:bg-slate-800 disabled:opacity-40">Deselect All</button>
                <span className="text-xs text-slate-400">Selected: {selectedTitles.length} {type === "series" ? `shows · ${knownEpisodeCount} known episodes${unknownEpisodeCount ? ` · ${unknownEpisodeCount} count${unknownEpisodeCount === 1 ? "" : "s"} unavailable` : ""}` : "movies"}</span>
              </div>
              <div className="flex flex-wrap gap-2">
                <button type="button" disabled={busy || !selectedTitles.length} onClick={queueSelectedTitles} className="inline-flex items-center gap-1.5 rounded-lg bg-emerald-500/15 px-3 py-2 text-xs font-medium text-emerald-300 hover:bg-emerald-500/25 disabled:opacity-40"><Download size={14} />{type === "series" ? "Queue Missing Episodes" : "Ingest Selected via Workers"}</button>
                {sourceType === "xtream" && <button type="button" disabled={busy || !selectedTitles.length} onClick={exportSelectedScript} className="inline-flex items-center gap-1.5 rounded-lg border border-indigo-400/40 bg-indigo-500/10 px-3 py-2 text-xs font-medium text-indigo-200 hover:bg-indigo-500/20 disabled:opacity-40"><Download size={14} />Export / Run as Direct Bash Script</button>}
              </div>
            </div>}
            {!items.length ? <div className="grid min-h-64 place-items-center px-4 text-center text-sm text-slate-500">Choose categories and select <span className="text-indigo-300">Browse selected</span> to see provider content.</div> :
              !displayedItems.length ? <div className="grid min-h-64 place-items-center px-4 text-center text-sm text-slate-500">No titles match this search and storage filter.</div> :
                viewMode === "list" ? <div className="max-h-[660px] overflow-auto">
                  <table className="w-full min-w-[760px] text-left text-sm">
                    <thead className="sticky top-0 z-[1] bg-slate-900 text-xs uppercase tracking-wide text-slate-500"><tr><th className="w-12 px-4 py-3">Select</th><th className="px-4 py-3">Title</th><th className="px-4 py-3">Category</th><th className="px-4 py-3">Season Count</th><th className="px-4 py-3">Episode Count</th><th className="px-4 py-3">Actions</th></tr></thead>
                    <tbody className="divide-y divide-slate-800/80">
                      {displayedItems.map((item) => {
                        const isSelected = selectedItems.has(item.id);
                        return <tr key={`${item.kind}-${item.id}`} role="button" tabIndex={0} aria-pressed={isSelected} onClick={() => toggleItemSelection(item.id)} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggleItemSelection(item.id); } }} className={`cursor-pointer transition ${isSelected ? "bg-indigo-950/40 outline outline-1 -outline-offset-1 outline-indigo-500" : "hover:bg-slate-800/40"}`}>
                          <td className="px-4 py-3"><span className={`grid h-5 w-5 place-items-center rounded border ${isSelected ? "border-indigo-400 bg-indigo-500 text-white" : "border-slate-600 text-transparent"}`}><Check size={13} /></span></td>
                          <td className="max-w-[280px] truncate px-4 py-3 font-medium text-slate-200">{item.name}{item.year && <span className="ml-2 text-xs font-normal text-slate-500">{item.year}</span>}<InventoryBadge status={item.download_status} /></td>
                          <td className="max-w-[180px] truncate px-4 py-3 text-slate-400">{item.category_name}</td>
                          <td className="px-4 py-3 text-slate-400">{type === "series" ? item.total_seasons ?? "—" : "—"}</td>
                          <td className="px-4 py-3 text-slate-400">{type === "series" ? item.total_episodes ?? "—" : "—"}</td>
                          <td className="px-4 py-3" onClick={(event) => event.stopPropagation()}>{type === "series" ? <div className="flex gap-2">
                            <button type="button" disabled={busy} onClick={() => fetchDetails(item)} className="whitespace-nowrap rounded-lg border border-slate-700 px-2.5 py-2 text-xs text-slate-300 hover:bg-slate-800 disabled:opacity-40">Inspect / Episodes</button>
                            <button type="button" disabled={busy} onClick={() => queueFullSeries(item)} title="Queue full series" className="rounded-lg bg-indigo-500/15 px-2.5 text-indigo-300 hover:bg-indigo-500/25 disabled:opacity-40"><Download size={15} /></button>
                            <button type="button" disabled={busy} onClick={() => queueMissingSeries(item)} title="Queue only missing episodes" className="rounded-lg border border-amber-500/30 px-2.5 text-amber-300 hover:bg-amber-500/10 disabled:opacity-40"><Download size={15} /></button>
                          </div> : <button type="button" disabled={busy} onClick={() => queueEntries([{
                            stream_id: item.id, title: item.name, filename: `${item.name}.${item.container_ext || "mkv"}`,
                            container_ext: item.container_ext || "mkv", category_id: item.category_id, category_name: item.category_name,
                          }])} className="rounded-lg bg-indigo-500/15 px-3 py-2 text-xs text-indigo-300 hover:bg-indigo-500/25 disabled:opacity-40"><Download size={14} /></button>}</td>
                        </tr>;
                      })}
                    </tbody>
                  </table>
                </div> : <div className="grid max-h-[660px] gap-3 overflow-y-auto p-4 sm:grid-cols-2 xl:grid-cols-3">
                  {displayedItems.map((item) => {
                    const isSelected = selectedItems.has(item.id);
                    return <article key={`${item.kind}-${item.id}`} role="button" tabIndex={0} aria-pressed={isSelected} onClick={() => toggleItemSelection(item.id)} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggleItemSelection(item.id); } }} className={`group cursor-pointer overflow-hidden rounded-xl border transition ${isSelected ? "border-indigo-500 bg-indigo-950/40" : "border-slate-800 bg-[#0b111d] hover:border-slate-600"}`}>
                      <div className="relative grid h-40 place-items-center bg-slate-900">
                        {item.cover ? <img src={item.cover} alt="" className="h-full w-full object-cover" loading="lazy" onError={(event) => { event.currentTarget.style.display = "none"; }} /> : (type === "series" ? <Tv className="text-slate-700" size={36} /> : <Film className="text-slate-700" size={36} />)}
                        <span className={`absolute right-3 top-3 grid h-7 w-7 place-items-center rounded-full border ${isSelected ? "border-indigo-300 bg-indigo-500 text-white" : "border-white/30 bg-black/60 text-transparent"}`}><Check size={16} /></span>
                        {item.year && <span className="absolute bottom-3 left-3 rounded-md bg-black/70 px-2 py-1 text-xs text-white">{item.year}</span>}
                      </div>
                      <div className="p-4">
                        <h4 className="line-clamp-2 min-h-10 text-sm font-semibold text-slate-100">{item.name}<InventoryBadge status={item.download_status} /></h4>
                        <p className="mt-1 truncate text-xs text-slate-500">{item.category_name}</p>
                        {type === "series" ? <div className="mt-2 flex gap-2">
                          <span className="rounded-full bg-slate-800 px-2 py-1 text-[10px] text-slate-300">{item.total_seasons ?? "—"} seasons</span>
                          <span className="rounded-full bg-slate-800 px-2 py-1 text-[10px] text-slate-300">{item.total_episodes ?? "—"} episodes</span>
                        </div> : <p className="mt-2 text-xs text-slate-400">Stream ID {item.id}</p>}
                        <div className="mt-4 flex gap-2" onClick={(event) => event.stopPropagation()}>
                          {type === "series" ? <>
                            <button type="button" disabled={busy} onClick={() => fetchDetails(item)} className="flex-1 rounded-lg border border-slate-700 px-2 py-2 text-xs text-slate-300 hover:bg-slate-800 disabled:opacity-40">Inspect / Episodes</button>
                            <button type="button" disabled={busy} onClick={() => queueFullSeries(item)} title="Queue full series" className="rounded-lg bg-indigo-500/15 px-3 text-indigo-300 hover:bg-indigo-500/25 disabled:opacity-40"><Download size={15} /></button>
                            <button type="button" disabled={busy} onClick={() => queueMissingSeries(item)} title="Queue only missing episodes" className="rounded-lg border border-amber-500/30 px-3 text-amber-300 hover:bg-amber-500/10 disabled:opacity-40"><Download size={15} /></button>
                          </> : <button type="button" disabled={busy} onClick={() => queueEntries([{
                            stream_id: item.id, title: item.name, filename: `${item.name}.${item.container_ext || "mkv"}`,
                            container_ext: item.container_ext || "mkv", category_id: item.category_id, category_name: item.category_name,
                          }])} className="flex w-full items-center justify-center gap-2 rounded-lg bg-indigo-500/15 px-3 py-2 text-xs text-indigo-300 hover:bg-indigo-500/25 disabled:opacity-40"><Download size={14} />Queue movie</button>}
                        </div>
                      </div>
                    </article>;
                  })}
                </div>
            }
          </div>
        </div>
      </div>

      {notice && !details && <div className="flex items-center justify-between rounded-xl border border-slate-700 bg-slate-900 px-4 py-3 text-sm text-slate-300"><span>{notice}</span><button onClick={() => setNotice("")} aria-label="Dismiss notice"><X size={15} /></button></div>}

      {details && <div role="dialog" aria-modal="true" aria-labelledby="series-details-title" className="fixed inset-0 z-50 flex items-center justify-center bg-black/75 p-4" onMouseDown={(event) => { if (event.target === event.currentTarget) setDetails(null); }}>
        <div className="flex max-h-[90vh] w-full max-w-3xl flex-col overflow-hidden rounded-2xl border border-slate-700 bg-[#0d1320] shadow-2xl">
          <header className="flex items-center justify-between gap-3 border-b border-slate-800 px-5 py-4">
            <div className="min-w-0"><h2 id="series-details-title" className="truncate font-semibold text-white">{details.data.name}</h2><p className="mt-1 text-xs text-slate-500">{Object.keys(details.data.seasons || {}).length} seasons</p></div>
            <button onClick={() => setDetails(null)} aria-label="Close series details" className="rounded-lg p-2 text-slate-400 hover:bg-slate-800 hover:text-white"><X size={17} /></button>
          </header>
          <div className="flex-1 space-y-3 overflow-y-auto p-5">
            {Object.entries(details.data.seasons || {}).map(([seasonKey, episodes]) => {
              const currentSeason = seasonNumber(seasonKey);
              const entries = detailsEntries(details.item, details.data).filter((entry) => entry.season === String(currentSeason));
              const selectedCount = entries.filter((entry) => selectedEpisodes.has(`${details.item.id}:${entry.stream_id}`)).length;
              return <details key={seasonKey} open className="rounded-xl border border-slate-800 bg-slate-900/40">
                <summary className="flex cursor-pointer list-none items-center gap-3 px-4 py-3">
                  <ChevronDown size={15} className="text-slate-500" />
                  <span className="flex-1 text-sm font-medium text-slate-200">Season {String(currentSeason).padStart(2, "0")} <span className="ml-2 text-xs text-slate-500">{episodes.length} episodes</span></span>
                  <label onClick={(event) => event.stopPropagation()} className="flex cursor-pointer items-center gap-2 text-xs text-slate-400">
                    <input type="checkbox" checked={entries.length > 0 && selectedCount === entries.length} onChange={() => toggleSeason(seasonKey)} className="accent-indigo-500" />Select season
                  </label>
                </summary>
                <div className="divide-y divide-slate-800/80 border-t border-slate-800">
                  {episodes.map((episode) => {
                    const key = `${details.item.id}:${episode.id}`;
                    const entry = detailsEntries(details.item, details.data).find((candidate) => candidate.stream_id === episode.id);
                    return <div key={key} className="flex items-center gap-3 px-4 py-3">
                      <input aria-label={`Select episode ${episode.episode_num}`} type="checkbox" checked={selectedEpisodes.has(key)} onChange={() => toggleEpisode(key, entry)} className="accent-indigo-500" />
                      <span className="w-14 shrink-0 font-mono text-xs text-indigo-300">E{String(episode.episode_num).padStart(2, "0")}</span>
                      <span className="min-w-0 flex-1 truncate text-sm text-slate-300">{episode.title}</span>
                      <span className="hidden text-xs text-slate-500 sm:inline">.{episode.ext}</span>
                      {episode.on_storage === false && <span className="text-[10px] text-amber-300">Missing</span>}
                      {episode.on_storage === true && <span className="text-[10px] text-emerald-300">On storage</span>}
                      <span className="hidden font-mono text-[10px] text-slate-600 md:inline">{episode.id}</span>
                      <button onClick={() => queueOneEpisode(details.item, details.data, episode)} title="Queue episode" disabled={busy} className="rounded-md p-2 text-indigo-300 hover:bg-indigo-500/10 disabled:opacity-40"><Download size={14} /></button>
                    </div>;
                  })}
                  {!episodes.length && <p className="px-4 py-6 text-center text-xs text-slate-500">No episodes listed for this season.</p>}
                </div>
              </details>;
            })}
          </div>
          <footer className="flex flex-wrap items-center justify-between gap-3 border-t border-slate-800 px-5 py-4">
            <div className="min-w-0 flex-1">
              <span className="text-xs text-slate-500">{selectedEpisodes.size} episodes selected · {!details.data.storage_inventory_available ? "storage inventory unavailable" : details.data.storage_inventory_ambiguous || details.data.storage_inventory_unclassified ? "missing count unknown" : `${details.data.missing_episode_count ?? 0} missing`}</span>
              {details.data.storage_inventory_ambiguous && <p className="mt-1 text-xs text-amber-300">Multiple matching series folders were found; missing episodes cannot be safely determined.</p>}
              {details.data.storage_inventory_unclassified && <p className="mt-1 text-xs text-amber-300">Some stored files have no recognizable season and episode number; missing episodes cannot be safely determined.</p>}
              {notice && <p role="status" className="mt-1 text-xs text-slate-300">{notice}</p>}
            </div>
            <button type="button" onClick={selectMissingEpisodes} disabled={!details.data.storage_inventory_available || details.data.storage_inventory_ambiguous || details.data.storage_inventory_unclassified || !details.data.missing_episode_count} className="rounded-lg border border-amber-500/30 px-3 py-2.5 text-xs font-medium text-amber-300 hover:bg-amber-500/10 disabled:opacity-40">Select missing</button>
            <button onClick={queueSelectedEpisodes} disabled={busy || !selectedEpisodes.size} className="inline-flex items-center gap-2 rounded-lg bg-indigo-500 px-4 py-2.5 text-sm font-semibold text-white hover:bg-indigo-400 disabled:opacity-40">
              {busy ? <LoaderCircle size={15} className="animate-spin" /> : <Download size={15} />}Download selected episodes
            </button>
          </footer>
        </div>
      </div>}
      {(pendingQueue !== null || pendingSeries !== null) && <div role="dialog" aria-modal="true" aria-labelledby="queue-confirm-title" className="fixed inset-0 z-[60] overflow-y-auto bg-black/80 p-3 sm:p-4">
        <div className="flex min-h-full items-center justify-center">
        <section className="flex max-h-[calc(100vh-1.5rem)] w-full max-w-xl flex-col overflow-hidden rounded-2xl border border-slate-700 bg-[#0d1320] shadow-2xl sm:max-h-[calc(100vh-2rem)]">
          <header className="flex items-start justify-between gap-4 border-b border-slate-800 px-5 py-4">
            <div><h2 id="queue-confirm-title" className="font-semibold text-white">Confirm download assignment</h2><p className="mt-1 text-xs text-slate-400">{pendingSeries ? `${pendingCount} series will be prepared and queued as each finishes.` : `${pendingCount} item${pendingCount === 1 ? "" : "s"} will be queued across the free accounts you select.`}</p><p className="mt-1 text-[11px] text-indigo-300">{sourceType === "m3u" ? `Using stream URLs saved from ${playlists.find((playlist) => String(playlist.id) === sourcePlaylistId)?.name || "the M3U playlist"}.` : `Browsing with ${providerAccounts.find((account) => String(account.id) === sourceAccountId)?.username || "the selected account"}; each download uses the credentials of its assigned account.`}</p></div>
            <button type="button" onClick={cancelQueueConfirmation} disabled={busy} aria-label="Cancel queue confirmation" className="rounded-lg p-2 text-slate-400 hover:bg-slate-800"><X size={16} /></button>
          </header>
          <div className="min-h-0 space-y-5 overflow-y-auto p-5">
            {pendingSeries && <p className="rounded-lg border border-indigo-900/70 bg-indigo-950/30 px-3 py-2 text-xs text-indigo-200">Series details are requested at least 3 seconds apart. Each series is added to the download queue as soon as its details are ready.</p>}
            <div>
              <div className="mb-2 flex items-center justify-between"><h3 className="text-sm font-medium text-slate-200">Idle Xtream accounts</h3><span className="text-xs text-slate-500">{selectedWorkerIds.size} selected</span></div>
              <p className="mb-3 text-xs text-slate-500">Jobs are distributed round-robin. Each account runs at most one download at a time; selecting more free accounts increases parallel downloads.</p>
              <p className="mb-3 text-[11px] text-indigo-300">{sourceType === "xtream" ? "Only accounts for the selected provider endpoint are shown; each worker uses its own credentials." : "M3U stream URLs are downloaded exactly as stored; Xtream accounts provide worker concurrency only."}</p>
              <div className="max-h-48 space-y-2 overflow-y-auto">
                {availableAccounts.map((account) => {
                  const sortedWorkers = [...selectedWorkerIds].sort((left, right) => left - right);
                  const assignedCount = Array.from({ length: pendingCount }, (_, index) => index).filter((index) =>
                    index % Math.max(1, sortedWorkers.length) === sortedWorkers.indexOf(account.id)
                  ).length;
                  return <label key={account.id} className="flex cursor-pointer items-center gap-3 rounded-lg border border-slate-800 bg-slate-900/60 px-3 py-2.5">
                    <input type="checkbox" checked={selectedWorkerIds.has(account.id)} onChange={() => toggleWorker(account.id)} className="accent-indigo-500" />
                    <span className="min-w-0 flex-1"><span className="block text-sm text-slate-200">{account.username}</span><span className="block truncate text-[10px] text-slate-500">{account.provider_url}</span></span>
                    <span className="text-[10px] text-emerald-300">Free</span>
                    {selectedWorkerIds.has(account.id) && <span className="text-[10px] text-slate-400">~{assignedCount} jobs</span>}
                  </label>;
                })}
              </div>
            </div>
            <fieldset>
              <legend className="mb-2 text-sm font-medium text-slate-200">Download client</legend>
              <div className="grid gap-2 sm:grid-cols-2">
                <label className={`cursor-pointer rounded-lg border px-3 py-3 ${downloader === "axel" ? "border-indigo-500 bg-indigo-950/30" : "border-slate-800 bg-slate-900/50"}`}>
                  <input type="radio" name="queue-downloader" value="axel" checked={downloader === "axel"} onChange={() => setDownloader("axel")} className="mr-2 accent-indigo-500" />
                  <span className="text-sm text-slate-200">Axel / aria2c · default</span>
                  <span className="mt-1 block text-xs text-slate-500">Uses configured parallel connections; SOCKS5 uses aria2c.</span>
                </label>
                <label className={`cursor-pointer rounded-lg border px-3 py-3 ${downloader === "wget" ? "border-indigo-500 bg-indigo-950/30" : "border-slate-800 bg-slate-900/50"}`}>
                  <input type="radio" name="queue-downloader" value="wget" checked={downloader === "wget"} onChange={() => setDownloader("wget")} className="mr-2 accent-indigo-500" />
                  <span className="text-sm text-slate-200">Wget · reference-script options</span>
                  <span className="mt-1 block text-xs text-slate-500">Resumes partial transfers; one connection per file.</span>
                </label>
              </div>
            </fieldset>
            <fieldset>
              <legend className="mb-2 text-sm font-medium text-slate-200">Connection mode</legend>
              <div className="grid gap-2 sm:grid-cols-2">
                <label className={`cursor-pointer rounded-lg border px-3 py-3 ${proxyMode === "direct" ? "border-indigo-500 bg-indigo-950/30" : "border-slate-800 bg-slate-900/50"}`}>
                  <input type="radio" name="queue-proxy-mode" value="direct" checked={proxyMode === "direct"} onChange={() => setProxyMode("direct")} className="mr-2 accent-indigo-500" />
                  <span className="text-sm text-slate-200">Direct · no proxy</span>
                </label>
                <label className={`cursor-pointer rounded-lg border px-3 py-3 ${proxyMode === "node" ? "border-indigo-500 bg-indigo-950/30" : "border-slate-800 bg-slate-900/50"} ${!proxyAvailable ? "cursor-not-allowed opacity-40" : ""}`}>
                  <input type="radio" name="queue-proxy-mode" value="node" checked={proxyMode === "node"} disabled={!proxyAvailable} onChange={() => setProxyMode("node")} className="mr-2 accent-indigo-500" />
                  <span className="text-sm text-slate-200">Use enabled proxy</span>
                </label>
              </div>
              {proxyAvailable && proxyMode === "node" && <label className="mt-3 block text-xs text-slate-400">
                Enabled proxy for this batch
                <select value={selectedProxyId} onChange={(event) => setSelectedProxyId(event.target.value)} className="mt-2 w-full rounded-lg border border-slate-700 bg-[#090d16] px-3 py-2 text-sm text-slate-200">
                  {availableProxies.map((proxy) => <option key={proxy.id} value={proxy.id}>Proxy #{proxy.id} · {proxy.url}</option>)}
                </select>
              </label>}
              {wgetProxyUnsupported && <p className="mt-2 text-xs text-amber-300">Wget cannot use SOCKS5 proxies. Choose Axel / aria2c or switch to a direct or HTTP(S) connection.</p>}
              {!proxyAvailable && <p className="mt-2 text-xs text-slate-500">There are no enabled proxies in the proxy pool.</p>}
            </fieldset>
          </div>
          <footer className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-t border-slate-800 px-5 py-4">
            <button type="button" onClick={cancelQueueConfirmation} disabled={busy} className="rounded-lg border border-slate-700 px-4 py-2.5 text-sm text-slate-300 hover:bg-slate-800">Cancel</button>
            <button type="button" onClick={confirmQueueEntries} disabled={busy || !selectedWorkerIds.size || (proxyMode === "node" && !selectedProxyId) || wgetProxyUnsupported} className="inline-flex items-center gap-2 rounded-lg bg-indigo-500 px-4 py-2.5 text-sm font-semibold text-white hover:bg-indigo-400 disabled:opacity-40">
              {busy ? <LoaderCircle size={15} className="animate-spin" /> : <Download size={15} />}
              {pendingSeries ? `${pendingMissingOnly ? "Queue missing episodes from" : "Prepare & queue"} ${pendingCount} series` : `Confirm & queue ${pendingCount} downloads`}
            </button>
          </footer>
        </section>
        </div>
      </div>}
      {busy && <span className="sr-only" role="status">Loading content</span>}
    </section>
  );
}
