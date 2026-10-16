# Roll back the native MCP switch

The legacy extension source is still in this directory. Only its Pi load entry was removed.

From a shell:

```bash
# 1. Remove the native server from the user MCP config.
python3 - <<'PY'
import json
from pathlib import Path
path = Path.home() / ".pi" / "agent" / "mcp.json"
data = json.loads(path.read_text())
data["mcpServers"].pop("allsearch", None)
path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
PY

# 2. Load the legacy extension again. Do not do this while the native server remains enabled.
ln -s /path/to/allsearch/integrations/pi ~/.pi/agent/extensions/allsearch
```

A copy of the previous `mcp.json` is at `~/.pi/agent/mcp.json.bak-allsearch-native`. The removed symlink target was saved at `~/.pi/agent/extensions/allsearch.disabled-target`.

Start a new Pi session after either the switch or the rollback. An already running session keeps the tools it loaded at startup.

Do not enable the native server and the legacy extension together. Both can search, and the model may call both.
