export async function waitForM3uJob(api, jobId, onStatus) {
  while (true) {
    const { data } = await api.get(`/playlists/jobs/${jobId}`);
    onStatus?.(data);
    if (data.status === "COMPLETED") return data;
    if (data.status === "FAILED") {
      throw new Error(data.error || "M3U import failed. Check the backend logs.");
    }
    await new Promise((resolve) => window.setTimeout(resolve, 1500));
  }
}
