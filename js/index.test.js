import test from "node:test";
import assert from "node:assert/strict";
import { load, parseCsv, timestamp, fileUrl } from "./index.js";

const fakeFetch = (body) => async () => ({ ok: true, status: 200, text: async () => body });

test("fileUrl points at the rolling release", () => {
  assert.equal(fileUrl("contracts.json"), "https://github.com/ehlersio/nhl-contracts/releases/download/contracts/contracts.json");
});

test("load parses JSON tables and sources.csv", async () => {
  const rows = await load("contracts", { fetch: fakeFetch('[{"player_name":"Marat Khusnutdinov","aav_usd":3950000}]') });
  assert.equal(rows[0].aav_usd, 3950000);
  const src = await load("sources", { fetch: fakeFetch('source_id,url,headline\nforge:a,https://x,"Signs, with ""quotes"""\n') });
  assert.deepEqual(src, [{ source_id: "forge:a", url: "https://x", headline: 'Signs, with "quotes"' }]);
  await assert.rejects(load("nope"), /unknown table/);
});

test("timestamp returns the metadata object", async () => {
  const ts = await timestamp({ fetch: fakeFetch('{"updated_at":"2026-10-23T15:00:00+00:00","n_contracts":6961}') });
  assert.equal(ts.n_contracts, 6961);
});

test("parseCsv handles embedded newlines", () => {
  assert.deepEqual(parseCsv('a,b\n1,"x\ny"\n'), [{ a: "1", b: "x\ny" }]);
});
