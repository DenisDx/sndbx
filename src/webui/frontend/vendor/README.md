# Vendored frontend assets

Third-party browser assets served from the sndbx Web UI.

| File | Upstream | Version | License |
|---|---|---|---|
| `xterm.min.js` | https://github.com/xtermjs/xterm.js | 5.3.0 | MIT |
| `addon-fit.min.js` | https://github.com/xtermjs/xterm.js (`@xterm/addon-fit`) | 0.10.0 | MIT |
| `xterm.min.css` | https://github.com/xtermjs/xterm.js | 5.3.0 | MIT |

The Web UI loads these from `cdn.jsdelivr.net` first. When the CDN is
unreachable — an air-gapped host, a blocked CDN — the browser falls back to
the copies in this directory, so the Console tab keeps working offline.

## Updating

```bash
cd src/webui/frontend/vendor
curl -fsSL -o xterm.min.js    https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.min.js
curl -fsSL -o addon-fit.min.js https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0/lib/addon-fit.min.js
curl -fsSL -o xterm.min.css   https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.min.css
```

Keep the versions in `index.html` and in this file in sync.
