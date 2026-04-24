# Dashboard UI Kit · SECS/GEM Equipment Monitor

Interactive, high-fidelity mock of the equipment monitoring product. Five surfaces stitched together by a left sidebar:

1. **Dashboard** — KPI strip, equipment fleet grid, active alarms pane
2. **Monitor** — single-tool live sensor readout + trend charts
3. **Alarms** — active / acknowledged, severity breakdown
4. **Events** — filterable SECS event stream (CEID / ALID / SVID / PPID)
5. **Control** — remote command panel with Start / Stop / Reset / Pause / Resume / Abort + confirm

## Files

| File | Purpose |
|---|---|
| `index.html` | Entry point — boots React, loads Inter/JetBrains Mono + Lucide from CDN |
| `Primitives.jsx` | Icon, StatusChip, Card, Button, Input, Select, LED, Sparkline, Shell, Sidebar, TopBar |
| `Sections.jsx` | KpiStrip, EquipmentGrid, EquipmentTile, TrendChart, AlarmList, EventLog, ControlPanel |
| `data.jsx` | Fake equipment / alarm / event data + `computeKpis()` |
| `App.jsx` | Page router + state; 5 page components |

## Interactions that work

- Sidebar navigation across all 5 surfaces
- Equipment tile selection (updates Monitor + Control targets)
- Alarm `Ack` button — moves alarm from Active to Acknowledged
- Control panel: Start / Pause / Resume fire immediately; Stop / Reset / Abort open a confirm banner
- Event stream filter chips (ALL · CEID · ALID · SVID · PPID)
- Language toggle in top bar (EN / 中文 button states — strings not translated in this mock)
- Live-updating clock + trend sparklines (5 s tick)

## Stack note

Built with React via Babel standalone for instant-preview. For production, port to Vue 3 + Tailwind — component boundaries are kept small and prop-driven so the translation is mechanical.
