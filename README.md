# apipriceindex-mcp

An MCP server that gives any AI agent live, verified LLM API prices.

Ask your assistant *"what would 200M input / 15M output tokens a month cost
me on Sonnet vs GPT?"* and it answers from today's index — every price with
its verification date, cross-check confidence and source URL. Data from
[apipriceindex.com](https://apipriceindex.com/use-this-data/) (CC BY 4.0,
re-checked daily against official pricing pages).

## Tools

| Tool | What it does |
|---|---|
| `search_models` | Find model ids by name/provider, filter by context window or open weights |
| `get_price` | Verified price of one model: input/output/cached input USD per Mtok, `verified_at`, `confidence`, source |
| `estimate_cost` | Monthly bill of a workload; ranks chosen models — or the whole catalog — cheapest first |

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

- Prices are fetched live from the index (15-minute in-memory cache) —
  never stale training data.
- **Index unreachable → explicit error.** No cached or invented price is
  ever served silently.
- Index older than 14 days → answers still come, with a warning pointing
  at the index's [health endpoint](https://apipriceindex.com/health.json).
- Model not tracked → says so, links the dataset. No price invented.
- Cache discount only where the provider publishes a cached-input price.
- Cost only: the index does not rank model quality, and third-party
  benchmark scores are not redistributed.

Environment: `APIPRICEINDEX_URL` overrides the dataset URL (testing).

## License

Code: MIT. Price data: [CC BY 4.0](https://apipriceindex.com/use-this-data/),
attribution "API Price Index".
