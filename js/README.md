# nhl-contracts (npm)

Thin loader for the open NHL player-contracts dataset: term, cap hit, total
value, year-by-year salary and signing-bonus structure, and trade clauses, every
row with its public source and a confidence tier. Data is CC0; see the
[repository](https://github.com/ehlersio/nhl-contracts) for the data dictionary.

```js
import { load, timestamp } from "nhl-contracts";

const meta = await timestamp();            // { updated_at, n_contracts, tiers, ... }
const contracts = await load("contracts"); // Contract[]
const seasons = await load("contract_seasons");
const clauses = await load("clauses");
```

No data ships in the package; each call downloads the latest files from the
`contracts` GitHub release. Node 18+ (global `fetch`).
