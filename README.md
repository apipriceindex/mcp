# apipriceindex-mcp

An MCP server that gives any AI agent live, verified LLM API prices — and today's deals.

Ask your assistant *"which LLMs are free to call right now?"*, *"what's the
cheapest model at least as good as GPT-5.6 Sol?"* or *"what would 200M input /
15M output tokens a month cost me on Sonnet vs GPT?"* and it answers from
today's index — every price with its verification date, cross-check confidence
and source URL. Data from [apipriceindex.com](https://apipriceindex.com/use-this-data/)
(CC BY 4.0, re-checked daily against official pricing pages).

## Tools

| Tool | What it does |
|---|---|
| `search_models` | Find model ids by name/provider; filter by context window, open weights, max price (input + output), capabilities (tools, reasoning, vision, json) and min speed |
| `get_price` | Verified price of one model: input/output/cached input USD per Mtok, `verified_at`, `confidence`, source; speed, availability and Epoch score when known |
| `estimate_cost` | Monthly bill of a workload; ranks chosen models — or the whole catalog — cheapest first |
| `list_deals` | Models free to call right now, discounts, free credits and free tiers — each with its end date and official source; filter "ends within N days" |
| `offers_table` | Every host serving one model, with the columns you choose (input, output, cache, discount, speed, first token, uptime, context, verified_at) |
| `compare_models` | Two to four models side by side: lowest prices and who gives them, providers, fastest host, context, license, Epoch score, monthly cost |
| `cheapest_at_level` | Cheapest models reaching an Epoch Capabilities Index score — or as good as a model you name |
| `price_changes` | Price drops and rises over the last N days, and the models released in that time |

## Install

```
pip install apipriceindex-mcp
```

Claude Code / Claude Desktop:

```
claude mcp add apipriceindex -- apipriceindex-mcp
```

Or in any MCP client config:

```json
{ "mcpServers": { "apipriceindex": { "command": "apipriceindex-mcp" } } }
```

## Behavior

- Data is fetched live from the index (15-minute in-memory cache) —
  never stale training data.
- **Index unreachable → explicit error.** No cached or invented price is
  ever served silently.
- Index older than 14 days → answers still come, with a warning pointing
  at the index's [health endpoint](https://apipriceindex.com/health.json).
- Model not tracked → says so, links the dataset. No price invented.
- Cache discount only where the provider publishes a cached-input price.
- Deals always carry an end date — `no end date` when the host publishes
  none — and the link to the host's own page.
- Speed and availability are read from OpenRouter and always come with
  their window (median over 30 minutes, share of successful requests over
  24 hours). No tail latency or error rates.
- One third-party score: the [Epoch Capabilities Index](https://epoch.ai/eci)
  by Epoch AI (CC BY 4.0). Every answer that carries it carries its credit.
  Otherwise, cost only: the index does not rank model quality.

Environment: `APIPRICEINDEX_URL` overrides the dataset URL (testing);
`deals.json` and `pricing-history.csv` are read next to it.

## Changelog

- **0.2.0** — `list_deals`, `offers_table`, `compare_models`,
  `cheapest_at_level`, `price_changes`; `search_models` filters by max price,
  capabilities and min speed; Epoch score, speed and availability where known.
  The three 0.1 tools answer as before.
- **0.1.1** — `search_models`, `get_price`, `estimate_cost`.

## License

Code: MIT. Price data: [CC BY 4.0](https://apipriceindex.com/use-this-data/),
attribution "API Price Index". Epoch Capabilities Index: CC BY 4.0, credit Epoch AI.

<!-- mcp-name: io.github.apipriceindex/mcp -->
