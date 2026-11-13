export function parseXtreamM3uLink(value) {
  const parsed = new URL(value.trim());
  if (!["http:", "https:"].includes(parsed.protocol)) {
    throw new Error("Only HTTP and HTTPS playlist links are supported.");
  }
  if (!parsed.pathname.toLowerCase().endsWith("/get.php")) {
    throw new Error("Playlist link must point to get.php.");
  }
  const username = parsed.searchParams.get("username")?.trim();
  const password = parsed.searchParams.get("password");
  if (!username || !password) {
    throw new Error("Playlist link must include username and password query parameters.");
  }
  const directory = parsed.pathname.slice(0, parsed.pathname.lastIndexOf("/"));
  return {
    provider_url: `${parsed.origin}${directory}`.replace(/\/+$/, ""),
    username,
    password,
    user_agent: "Mozilla",
  };
}
