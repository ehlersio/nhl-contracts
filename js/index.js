// Thin loader for the nhl-contracts dataset published on GitHub Releases.
// No data ships in this package; every call fetches the latest files.

const REPO = process.env.NHL_CONTRACTS_REPO ?? "ehlersio/nhl-contracts";
const TAG = process.env.NHL_CONTRACTS_TAG ?? "contracts";

export const BASE_URL = `https://github.com/${REPO}/releases/download/${TAG}`;
export const TABLES = ["contracts", "claims", "contract_seasons", "season_claims", "clauses", "clause_claims"];

export function fileUrl(name) {
  return `${BASE_URL}/${name}`;
}

async function fetchText(name, fetchImpl = fetch) {
  const res = await fetchImpl(fileUrl(name), { redirect: "follow" });
  if (!res.ok) throw new Error(`nhl-contracts: ${name} returned HTTP ${res.status}`);
  return res.text();
}

/** Release metadata: updated_at, row counts per table and tier, licence. */
export async function timestamp(options = {}) {
  return JSON.parse(await fetchText("timestamp.json", options.fetch));
}

/** Load a table as an array of objects. `table` is one of TABLES; `sources` is CSV-only. */
export async function load(table = "contracts", options = {}) {
  if (table === "sources") return parseCsv(await fetchText("sources.csv", options.fetch));
  if (!TABLES.includes(table)) throw new Error(`nhl-contracts: unknown table ${table}`);
  return JSON.parse(await fetchText(`${table}.json`, options.fetch));
}

/** Minimal RFC 4180 parser (quotes, embedded commas and newlines). Numbers stay strings. */
export function parseCsv(text) {
  const rows = [];
  let row = [], field = "", quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quoted) {
      if (c === '"') {
        if (text[i + 1] === '"') { field += '"'; i++; } else quoted = false;
      } else field += c;
    } else if (c === '"') quoted = true;
    else if (c === ",") { row.push(field); field = ""; }
    else if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i++;
      row.push(field); rows.push(row); row = []; field = "";
    } else field += c;
  }
  if (field !== "" || row.length) { row.push(field); rows.push(row); }
  const [header, ...body] = rows;
  return body.filter((r) => r.length > 1 || r[0] !== "").map((r) => Object.fromEntries(header.map((h, j) => [h, r[j] ?? ""])));
}
