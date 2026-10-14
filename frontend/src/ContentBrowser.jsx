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

export default function ContentBrowser({ api, routing, onQueued }) {
  const [type, setType] = useState("series");
  const [categories, setCategories] = useState([]);
  const [selectedCategories, setSelectedCategories] = useState(new Set());
  const [categorySearch, setCategorySearch] = useState("");
  const [items, setItems] = useState([]);
  const [selectedItems, setSelectedItems] = useState(new Set());
  const [viewMode, setViewMode] = useState("grid");
  const [itemSearch, setItemSearch] = useState("");
  const [details, setDetails] = useState(null);
  const [selectedEpisodes, setSelectedEpisodes] = useState(new Map());
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");

  useEffect(() => {
    let current = true;
    setCategories([]);
    setSelectedCategories(new Set());
    setItems([]);
    setSelectedItems(new Set());
    setDetails(null);
    setNotice("");
    api.get("/content/categories", { params: { type } })
      .then(({ data }) => { if (current) setCategories(data); })
      .catch((error) => { if (current) setNotice(error.response?.data?.detail || "Could not load provider categories."); });
    return () => { current = false; };
  }, [api, type]);

  const visibleCategories = useMemo(() => categories.filter((category) =>
    category.category_name.toLocaleLowerCase().includes(categorySearch.toLocaleLowerCase())
  ), [categories, categorySearch]);
  const visibleItems = useMemo(() => items.filter((item) =>
    `${item.name} ${item.category_name}`.toLocaleLowerCase().includes(itemSearch.toLocaleLowerCase())
  ), [items, itemSearch]);
  const selectedTitles = useMemo(
    () => items.filter((item) => selectedItems.has(item.id)),
    [items, selectedItems],
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

  const monitorSelected = async () => {
    const selected = categories.filter((category) => selectedCategories.has(category.category_id));
    if (!selected.length) return setNotice("Select at least one category to monitor.");
    setBusy(true);
    setNotice("");
    try {
      for (const category of selected) {
        await api.post("/categories/toggle", {
          category_id: category.category_id,
          name: category.category_name,
          kind: kindFor(type),
          monitored: true,
          destination_path: category.destination_path || null,
        });
      }
      setCategories((current) => current.map((category) =>
        selectedCategories.has(category.category_id) ? { ...category, monitored: true } : category
      ));
      setNotice(`Monitoring enabled for ${selected.length} categor${selected.length === 1 ? "y" : "ies"}.`);
    } catch (error) {
      setNotice(error.response?.data?.detail || "Could not update category monitoring.");
    } finally {
      setBusy(false);
    }
  };

  const browseSelected = async () => {
    if (!selectedCategories.size) return setNotice("Select one or more categories to browse.");
    setBusy(true);
    setNotice("");
    setSelectedItems(new Set());
    setItemSearch("");
    try {
      const { data } = await api.get("/content/items", {
        params: { type, category_ids: [...selectedCategories].join(",") },
      });
      setItems(data);
      setNotice(`Loaded ${data.length} ${type === "series" ? "series" : "movies"}.`);
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
    setSelectedItems((current) => new Set([...current, ...visibleItems.map((item) => item.id)]));
  };

  const fetchDetails = async (item) => {
    setBusy(true);
    setNotice("");
    try {
      const { data } = await api.get(`/content/series/${encodeURIComponent(item.id)}/details`);
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
      let queued = 0;
      let skipped = 0;
      for (let start = 0; start < entries.length; start += 500) {
        const { data } = await api.post("/queue/manual-batch", {
          content_type: type === "series" ? "SERIES" : "MOVIE",
          target_node_id: String(nodeId),
          items: entries.slice(start, start + 500),
        });
        queued += data.queued_count;
        skipped += data.skipped.length;
      }
      setNotice(`Queued ${queued} item${queued === 1 ? "" : "s"}; skipped ${skipped} duplicate${skipped === 1 ? "" : "s"}.`);
      onQueued?.();
    } catch (error) {
      setNotice(error.response?.data?.detail || "Could not queue selected content.");
    } finally {
      setBusy(false);
    }
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
        season: String(season),
        episode_num: number,
        filename: episodeFilename(season, number, ext),
        container_ext: ext,
        category_id: item.category_id,
        category_name: item.category_name,
      };
    })
  );

  const entriesForTitles = async (chosen) => {
    if (type === "movie") {
      return chosen.map((item) => ({
        stream_id: item.id,
        title: item.name,
        filename: `${item.name}.${item.container_ext || "mkv"}`,
        container_ext: item.container_ext || "mkv",
        category_id: item.category_id,
        category_name: item.category_name,
      }));
    }
    const entries = [];
    for (let start = 0; start < chosen.length; start += 8) {
      const batch = await Promise.all(chosen.slice(start, start + 8).map(async (item) => {
        const { data } = await api.get(`/content/series/${encodeURIComponent(item.id)}/details`);
        return detailsEntries(item, data);
      }));
      entries.push(...batch.flat());
    }
    return entries;
  };

  const queueFullSeries = async (item) => {
    setBusy(true);
    setNotice("");
    try {
      const { data } = await api.get(`/content/series/${encodeURIComponent(item.id)}/details`);
      await queueEntries(detailsEntries(item, data));
    } catch (error) {
      setNotice(error.response?.data?.detail || "Could not load the selected series.");
      setBusy(false);
    }
  };

  const queueSelectedTitles = async () => {
    if (!selectedTitles.length) return setNotice("Select at least one title to queue.");
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
          <div className="inline-flex rounded-lg border border-slate-700 bg-slate-900 p-1">
            {["series", "movie"].map((mediaType) => (
              <button key={mediaType} onClick={() => setType(mediaType)} className={`rounded-md px-4 py-2 text-sm ${type === mediaType ? "bg-indigo-500 text-white" : "text-slate-400 hover:text-white"}`}>
                {mediaType === "series" ? <><Tv size={14} className="mr-2 inline" />Series</> : <><Film size={14} className="mr-2 inline" />Movies</>}
              </button>
            ))}
          </div>
        </div>

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
                <button onClick={selectVisibleCategories} className="text-indigo-300 hover:text-indigo-200">Select visible</button>
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
              {!visibleCategories.length && <p className="px-3 py-8 text-center text-sm text-slate-500">{categories.length ? "No matching categories." : "No categories available."}</p>}
            </div>
            <div className="grid gap-2 border-t border-slate-800 p-3 sm:grid-cols-2">
              <button disabled={busy || !selectedCategories.size} onClick={monitorSelected} className="rounded-lg border border-slate-700 px-3 py-2 text-xs font-medium text-slate-300 hover:bg-slate-800 disabled:opacity-40">Monitor selected</button>
              <button disabled={busy || !selectedCategories.size} onClick={browseSelected} className="rounded-lg bg-indigo-500 px-3 py-2 text-xs font-semibold text-white hover:bg-indigo-400 disabled:opacity-40">Browse selected</button>
            </div>
          </div>

          <div className="min-w-0 rounded-xl border border-slate-800 bg-slate-900/40">
            <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-800 p-4">
              <div><h3 className="text-sm font-medium text-slate-200">2. Select {type === "series" ? "series" : "movies"}</h3><p className="mt-1 text-xs text-slate-500">{items.length} results</p></div>
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
            {items.length > 0 && <div className="sticky top-0 z-10 flex flex-wrap items-center justify-between gap-3 border-b border-slate-800 bg-[#101827]/95 px-4 py-3 backdrop-blur">
              <div className="flex flex-wrap items-center gap-2">
                <button type="button" onClick={selectAllVisible} disabled={!visibleItems.length} className="rounded-lg border border-slate-700 px-3 py-2 text-xs text-slate-300 hover:bg-slate-800 disabled:opacity-40">Select All ({visibleItems.length} items)</button>
                <button type="button" onClick={() => setSelectedItems(new Set())} disabled={!selectedItems.size} className="rounded-lg border border-slate-700 px-3 py-2 text-xs text-slate-300 hover:bg-slate-800 disabled:opacity-40">Deselect All</button>
                <span className="text-xs text-slate-400">Selected: {selectedTitles.length} {type === "series" ? `shows · ${knownEpisodeCount} known episodes${unknownEpisodeCount ? ` · ${unknownEpisodeCount} count${unknownEpisodeCount === 1 ? "" : "s"} unavailable` : ""}` : "movies"}</span>
              </div>
              <div className="flex flex-wrap gap-2">
                <button type="button" disabled={busy || !selectedTitles.length} onClick={queueSelectedTitles} className="inline-flex items-center gap-1.5 rounded-lg bg-emerald-500/15 px-3 py-2 text-xs font-medium text-emerald-300 hover:bg-emerald-500/25 disabled:opacity-40"><Download size={14} />Ingest Selected via Workers</button>
                <button type="button" disabled={busy || !selectedTitles.length} onClick={exportSelectedScript} className="inline-flex items-center gap-1.5 rounded-lg border border-indigo-400/40 bg-indigo-500/10 px-3 py-2 text-xs font-medium text-indigo-200 hover:bg-indigo-500/20 disabled:opacity-40"><Download size={14} />Export / Run as Direct Bash Script</button>
              </div>
            </div>}
            {!items.length ? <div className="grid min-h-64 place-items-center px-4 text-center text-sm text-slate-500">Choose categories and select <span className="text-indigo-300">Browse selected</span> to see provider content.</div> :
              !visibleItems.length ? <div className="grid min-h-64 place-items-center px-4 text-center text-sm text-slate-500">No titles match your search.</div> :
                viewMode === "list" ? <div className="max-h-[660px] overflow-auto">
                  <table className="w-full min-w-[760px] text-left text-sm">
                    <thead className="sticky top-0 z-[1] bg-slate-900 text-xs uppercase tracking-wide text-slate-500"><tr><th className="w-12 px-4 py-3">Select</th><th className="px-4 py-3">Title</th><th className="px-4 py-3">Category</th><th className="px-4 py-3">Season Count</th><th className="px-4 py-3">Episode Count</th><th className="px-4 py-3">Actions</th></tr></thead>
                    <tbody className="divide-y divide-slate-800/80">
                      {visibleItems.map((item) => {
                        const isSelected = selectedItems.has(item.id);
                        return <tr key={`${item.kind}-${item.id}`} role="button" tabIndex={0} aria-pressed={isSelected} onClick={() => toggleItemSelection(item.id)} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggleItemSelection(item.id); } }} className={`cursor-pointer transition ${isSelected ? "bg-indigo-950/40 outline outline-1 -outline-offset-1 outline-indigo-500" : "hover:bg-slate-800/40"}`}>
                          <td className="px-4 py-3"><span className={`grid h-5 w-5 place-items-center rounded border ${isSelected ? "border-indigo-400 bg-indigo-500 text-white" : "border-slate-600 text-transparent"}`}><Check size={13} /></span></td>
                          <td className="max-w-[280px] truncate px-4 py-3 font-medium text-slate-200">{item.name}{item.year && <span className="ml-2 text-xs font-normal text-slate-500">{item.year}</span>}</td>
                          <td className="max-w-[180px] truncate px-4 py-3 text-slate-400">{item.category_name}</td>
                          <td className="px-4 py-3 text-slate-400">{type === "series" ? item.total_seasons ?? "—" : "—"}</td>
                          <td className="px-4 py-3 text-slate-400">{type === "series" ? item.total_episodes ?? "—" : "—"}</td>
                          <td className="px-4 py-3" onClick={(event) => event.stopPropagation()}>{type === "series" ? <div className="flex gap-2">
                            <button type="button" disabled={busy} onClick={() => fetchDetails(item)} className="whitespace-nowrap rounded-lg border border-slate-700 px-2.5 py-2 text-xs text-slate-300 hover:bg-slate-800 disabled:opacity-40">Inspect / Episodes</button>
                            <button type="button" disabled={busy} onClick={() => queueFullSeries(item)} title="Queue full series" className="rounded-lg bg-indigo-500/15 px-2.5 text-indigo-300 hover:bg-indigo-500/25 disabled:opacity-40"><Download size={15} /></button>
                          </div> : <button type="button" disabled={busy} onClick={() => queueEntries([{
                            stream_id: item.id, title: item.name, filename: `${item.name}.${item.container_ext || "mkv"}`,
                            container_ext: item.container_ext || "mkv", category_id: item.category_id, category_name: item.category_name,
                          }])} className="rounded-lg bg-indigo-500/15 px-3 py-2 text-xs text-indigo-300 hover:bg-indigo-500/25 disabled:opacity-40"><Download size={14} /></button>}</td>
                        </tr>;
                      })}
                    </tbody>
                  </table>
                </div> : <div className="grid max-h-[660px] gap-3 overflow-y-auto p-4 sm:grid-cols-2 xl:grid-cols-3">
                  {visibleItems.map((item) => {
                    const isSelected = selectedItems.has(item.id);
                    return <article key={`${item.kind}-${item.id}`} role="button" tabIndex={0} aria-pressed={isSelected} onClick={() => toggleItemSelection(item.id)} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggleItemSelection(item.id); } }} className={`group cursor-pointer overflow-hidden rounded-xl border transition ${isSelected ? "border-indigo-500 bg-indigo-950/40" : "border-slate-800 bg-[#0b111d] hover:border-slate-600"}`}>
                      <div className="relative grid h-40 place-items-center bg-slate-900">
                        {item.cover ? <img src={item.cover} alt="" className="h-full w-full object-cover" loading="lazy" onError={(event) => { event.currentTarget.style.display = "none"; }} /> : (type === "series" ? <Tv className="text-slate-700" size={36} /> : <Film className="text-slate-700" size={36} />)}
                        <span className={`absolute right-3 top-3 grid h-7 w-7 place-items-center rounded-full border ${isSelected ? "border-indigo-300 bg-indigo-500 text-white" : "border-white/30 bg-black/60 text-transparent"}`}><Check size={16} /></span>
                        {item.year && <span className="absolute bottom-3 left-3 rounded-md bg-black/70 px-2 py-1 text-xs text-white">{item.year}</span>}
                      </div>
                      <div className="p-4">
                        <h4 className="line-clamp-2 min-h-10 text-sm font-semibold text-slate-100">{item.name}</h4>
                        <p className="mt-1 truncate text-xs text-slate-500">{item.category_name}</p>
                        {type === "series" ? <div className="mt-2 flex gap-2">
                          <span className="rounded-full bg-slate-800 px-2 py-1 text-[10px] text-slate-300">{item.total_seasons ?? "—"} seasons</span>
                          <span className="rounded-full bg-slate-800 px-2 py-1 text-[10px] text-slate-300">{item.total_episodes ?? "—"} episodes</span>
                        </div> : <p className="mt-2 text-xs text-slate-400">Stream ID {item.id}</p>}
                        <div className="mt-4 flex gap-2" onClick={(event) => event.stopPropagation()}>
                          {type === "series" ? <>
                            <button type="button" disabled={busy} onClick={() => fetchDetails(item)} className="flex-1 rounded-lg border border-slate-700 px-2 py-2 text-xs text-slate-300 hover:bg-slate-800 disabled:opacity-40">Inspect / Episodes</button>
                            <button type="button" disabled={busy} onClick={() => queueFullSeries(item)} title="Queue full series" className="rounded-lg bg-indigo-500/15 px-3 text-indigo-300 hover:bg-indigo-500/25 disabled:opacity-40"><Download size={15} /></button>
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
              <span className="text-xs text-slate-500">{selectedEpisodes.size} episodes selected</span>
              {notice && <p role="status" className="mt-1 text-xs text-slate-300">{notice}</p>}
            </div>
            <button onClick={queueSelectedEpisodes} disabled={busy || !selectedEpisodes.size} className="inline-flex items-center gap-2 rounded-lg bg-indigo-500 px-4 py-2.5 text-sm font-semibold text-white hover:bg-indigo-400 disabled:opacity-40">
              {busy ? <LoaderCircle size={15} className="animate-spin" /> : <Download size={15} />}Download selected episodes
            </button>
          </footer>
        </div>
      </div>}
      {busy && <span className="sr-only" role="status">Loading content</span>}
    </section>
  );
}
