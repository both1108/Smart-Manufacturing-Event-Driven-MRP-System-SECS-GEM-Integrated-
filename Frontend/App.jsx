// =============================================================================
// App.jsx — top-level React app for the SECS/GEM monitor.
//
// This file is the ONLY integration layer between the backend (Flask @ :5000)
// and the presentational components in Primitives.jsx / Sections.jsx. Those
// two files are frozen on purpose — re-rendering them through new props is
// fine, but changing their API would ripple into styling work we don't want
// to do. data.jsx is no longer imported (see index.html); every helper and
// every piece of live data comes from this file + the API.
//
// Responsibilities:
//   1. Poll six backend read endpoints every 3 s.
//   2. Transform backend DTOs into the shapes each Section component expects
//      (machine_id→id, status→state, alarm_text→message, etc.).
//   3. Dispatch host commands (POST /machines/{id}/commands) and alarm
//      acknowledgements (POST /alarms/{id}/ack) in reaction to UI actions.
//   4. Derive the KPI strip and a minimal MRP card from the event stream —
//      the MRPPlanUpdated event is the system of record, so we read it, we
//      don't duplicate into a new read model.
//
// Non-goals:
//   - No routing library. Page switching is a single `page` state string.
//   - No global store. Fan-out is narrow and 3 s polling makes stale-read
//     risk a non-issue for a three-machine demo fleet.
// =============================================================================
const { useState: useStateA, useEffect: useEffectA, useMemo: useMemoA, useCallback: useCallbackA } = React;

// ---------- Config -----------------------------------------------------------
// Base URL for the Flask API. docker-compose exposes port 5000 on the host.
// The UI is a static page (file:// or static server) so every request crosses
// origins — the backend must have CORS enabled for this to work.
const API_BASE = 'http://localhost:5000';
const POLL_MS = 3000;

// ---------- Wall-clock formatters -------------------------------------------
// Local to App.jsx so data.jsx can be deleted without breaking these.
const _pad = (n) => String(n).padStart(2, '0');
const fmtTime = (d) => `${_pad(d.getHours())}:${_pad(d.getMinutes())}:${_pad(d.getSeconds())}`;
const fmtFullTime = (d) => `${d.getFullYear()}-${_pad(d.getMonth() + 1)}-${_pad(d.getDate())} ${fmtTime(d)}`;
const fmtTimeFromIso = (iso) => {
  if (!iso) return '—';
  const d = new Date(iso);
  return isNaN(d.getTime()) ? '—' : fmtTime(d);
};
const fmtDateFromIso = (iso) => {
  if (!iso) return '—';
  const d = new Date(iso);
  return isNaN(d.getTime()) ? '—' : `${d.getFullYear()}-${_pad(d.getMonth() + 1)}-${_pad(d.getDate())}`;
};

// ---------- API client -------------------------------------------------------
// Tiny fetch wrappers. We keep them promise-based with hard errors so the
// polling loop can catch once and surface one connection banner instead of
// six separate error states.
async function apiGet(path) {
  const res = await fetch(`${API_BASE}${path}`, { method: 'GET', credentials: 'omit' });
  if (!res.ok) throw new Error(`GET ${path} → ${res.status}`);
  return res.json();
}
async function apiPost(path, body, extraHeaders) {
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    credentials: 'omit',
    headers: { 'Content-Type': 'application/json', ...(extraHeaders || {}) },
    body: JSON.stringify(body || {}),
  });
  if (!res.ok) throw new Error(`POST ${path} → ${res.status}`);
  // 202 Accepted typically carries a JSON body with correlation_id, but tolerate empty.
  return res.json().catch(() => ({}));
}

// ---------- Backend → UI transforms -----------------------------------------
// Backend alarm severity is an int (E5 convention: higher == worse). Map to
// the UI's CRITICAL / MAJOR / MINOR chip keys.
function severityToUi(sev) {
  const n = Number(sev);
  if (n >= 3) return 'CRITICAL';
  if (n === 2) return 'MAJOR';
  return 'MINOR';
}

// Backend state string → UI chip key. UNKNOWN isn't a UI state; OFFLINE keeps
// the chip rendering without a console warning.
const STATE_UI_MAP = {
  RUN: 'RUN',
  IDLE: 'IDLE',
  ALARM: 'ALARM',
  UNKNOWN: 'OFFLINE',
};

// Each tool type has a "primary metric" — the axis its alarms key off and
// the one that belongs on its sparkline. Matches the authoritative mapping
// on the Python side (config.machines.MACHINE_PROFILES).
//
//   ETCH  — plasma etcher, temperature dominant (→ OVERHEAT ALID)
//   PVD   — deposition tool, rpm on the chart for visible "power instability"
//           (but OVERHEAT on the alarm path; see config.machines comment)
//   CMP   — polisher, vibration dominant (→ HIGH_VIBRATION ALID)
const PRIMARY_METRIC = {
  ETCH: 'temperature',
  PVD:  'rpm',
  CMP:  'vibration',
};
const PRIMARY_UNIT = {
  temperature: '°C',
  vibration:   'mm/s',
  rpm:         'rpm',
};

// Pretty a numeric metric with type-appropriate precision.
function fmtMetric(metric, value) {
  if (value == null) return '—';
  const n = Number(value);
  if (isNaN(n)) return '—';
  if (metric === 'vibration') return n.toFixed(3);
  if (metric === 'rpm')       return String(Math.round(n));
  return n.toFixed(1);
}

function transformMachine(m) {
  const primary = PRIMARY_METRIC[m.machine_type] || 'temperature';
  // Sparkline rides on the primary metric: an ETCH tile watches chamber
  // temperature creep, a CMP tile watches vibration. When history hasn't
  // arrived yet we duplicate the current sample so TrendChart's min/max
  // scaler doesn't divide by zero.
  const rawTrend = (m.sparkline || []).map(p => Number(p?.[primary] ?? 0));
  const trend = rawTrend.length >= 2
    ? rawTrend
    : [Number(m[primary] ?? 0), Number(m[primary] ?? 0)];

  // Cheap health heuristic for the UI's "avg/min health %" KPIs. We don't
  // persist health — it's a derived presentation number so the KPI strip
  // has something to show without a new read model:
  //   RUN → 94, IDLE → 80, ALARM → 45, UNKNOWN → 50, minus 5 per active alarm.
  let health;
  switch (m.status) {
    case 'RUN':   health = 94; break;
    case 'IDLE':  health = 80; break;
    case 'ALARM': health = 45; break;
    default:      health = 50;
  }
  health = Math.max(0, health - (m.active_alarm_count || 0) * 5);

  // `recipe` is repurposed as a compact live reading line — the tile layout
  // dedicates a small monospaced subtitle under the ID, perfect for
  // "ETCH · 85.4 °C". This is a data-flow choice; the tile component is untouched.
  const reading = `${fmtMetric(primary, m[primary])} ${PRIMARY_UNIT[primary] || ''}`.trim();

  return {
    id: m.machine_id,
    type: m.machine_type || '—',
    recipe: reading,
    state: STATE_UI_MAP[m.status] || 'OFFLINE',
    health,
    trend,
    // Keep the raw payload for the Monitor page's metric tiles. The /machines
    // list endpoint already carries everything we need, so we avoid a second
    // GET /machines/{id} fetch.
    _raw: m,
    _primary: primary,
  };
}

function transformAlarm(a) {
  return {
    // alarm_id is the backend composite "{machine_id}:{alid}"
    id: a.alarm_id,
    alid: a.alid,
    equipment: a.machine_id,
    severity: severityToUi(a.severity),
    message: a.alarm_text,
    time: fmtTimeFromIso(a.triggered_at),
    // The /alarms feed can return cleared + acknowledged alarms when status=all;
    // either of those should paint the row as "acked" from the UI's POV.
    acked: !!a.acknowledged_at || !!a.cleared_at,
  };
}

// Event type → UI kind/stream. Keeps EventLog's color coding meaningful
// even though our event taxonomy is richer than the classic SECS stream map.
// We route MRP/capacity events to a made-up "APP" stream to signal "this is
// an application-layer event, not wire SECS".
function classifyEvent(evt) {
  switch (evt.event_type) {
    case 'AlarmTriggered':
    case 'AlarmReset':
    case 'AlarmAcknowledged':
      return { kind: 'ALID', stream: 'S5F1' };
    case 'StateChanged':
      return { kind: 'CEID', stream: 'S6F11' };
    case 'MachineHeartbeat':
      return { kind: 'SVID', stream: 'S1F4' };
    // Host-command lifecycle. All four ride S2F41 in the SECS map: the
    // operator's intent (Requested), the actor's accept (Dispatched),
    // the wire-level ack we'll wire up later (Acked), and the explicit
    // refusal path (Rejected). Keeping them on one stream means the
    // Event Log groups them together visually.
    case 'HostCommandRequested':
    case 'HostCommandDispatched':
    case 'HostCommandAcked':
    case 'HostCommandRejected':
      return { kind: 'INFO', stream: 'S2F41' };
    case 'MRPRecomputeRequested':
    case 'MRPPlanUpdated':
    case 'CapacityRecomputeRequested':
    case 'CapacityRecomputed':
      return { kind: 'PPID', stream: 'APP' };
    default:
      return { kind: 'INFO', stream: evt.event_type || 'APP' };
  }
}

// Turn an event's payload into a one-line log message. Kept terse because
// the EventLog slices to 14 rows on screen — no room for verbose sentences.
function describeEvent(evt) {
  const p = evt.payload || {};
  switch (evt.event_type) {
    case 'StateChanged':
      return `${p.from_state || '?'} → ${p.to_state || '?'}${p.reason ? ' · ' + p.reason : ''}`;
    case 'AlarmTriggered':
      return `Alarm set · ALID ${p.alid ?? '?'} · ${p.alarm_text || ''}`;
    case 'AlarmReset':
      return `Alarm cleared · ALID ${p.alid ?? '?'}`;
    case 'AlarmAcknowledged':
      return `Alarm acknowledged · ALID ${p.alid ?? '?'}${p.by ? ' by ' + p.by : ''}`;
    case 'MachineHeartbeat': {
      const m = p.metrics || {};
      return `T=${fmtMetric('temperature', m.temperature)} V=${fmtMetric('vibration', m.vibration)} R=${fmtMetric('rpm', m.rpm)}`;
    }
    case 'HostCommandRequested':
      return `Host command ${p.command || ''} requested${p.user ? ' by ' + p.user : ''}${p.requested_to_state ? ' → ' + p.requested_to_state : ''}`;
    case 'HostCommandDispatched':
      return `Host command ${p.command || ''} dispatched · ${p.from_state || '?'} → ${p.to_state || '?'}`;
    case 'HostCommandAcked':
      return `Host command ${p.command || ''} acked · HCACK=${p.hcack ?? 0}`;
    case 'HostCommandRejected':
      return `Host command ${p.command || ''} REJECTED · ${p.reason || 'invalid state'}`;
    case 'MRPRecomputeRequested':
      return `MRP recompute requested${p.reason ? ' · ' + p.reason : ''}`;
    case 'MRPPlanUpdated':
      return `MRP plan updated · part ${p.part_no || '?'}${p.has_shortage ? ' · SHORTAGE ' + (p.total_shortage_qty ?? '') : ' · OK'}`;
    case 'CapacityRecomputed':
      return `Capacity recomputed · parts ${Object.keys(p.affected_parts || {}).join(', ') || '—'}`;
    default:
      return evt.event_type || '—';
  }
}

function transformEvent(evt) {
  const { kind, stream } = classifyEvent(evt);
  return {
    time: fmtTimeFromIso(evt.occurred_at),
    kind,
    stream,
    source: evt.machine_id || '—',
    message: describeEvent(evt),
    // Expose event_seq + correlation_id on the row so debug tooling (or a
    // future drill-down modal) can link back to the underlying event.
    _seq: evt.event_seq,
    _corr: evt.correlation_id,
  };
}

// Pull the most recent MRP plan out of the event stream. Reading it from
// MRPPlanUpdated directly (rather than a dedicated /api/mrp endpoint) keeps
// event_store as the single source of truth — the card will always reflect
// exactly what the MRP engine last emitted.
function extractMrp(mrpEvents) {
  if (!mrpEvents || !mrpEvents.length) return null;
  // Defensive sort — don't assume endpoint returns newest-first.
  const sorted = [...mrpEvents].sort((a, b) => (b.event_seq ?? 0) - (a.event_seq ?? 0));
  const latest = sorted[0];
  const p = latest.payload || {};
  return {
    part_no: p.part_no,
    has_shortage: !!p.has_shortage,
    total_shortage_qty: p.total_shortage_qty,
    earliest_shortage_date: p.earliest_shortage_date,
    suggested_order_date: p.suggested_order_date,
    suggested_po_qty: p.suggested_po_qty,
    machine_id: p.machine_id || latest.machine_id,
    updated_at: latest.occurred_at,
    correlation_id: latest.correlation_id,
  };
}

// ---------- KPI derivation --------------------------------------------------
// We compute fleet KPIs locally from equipment+alarms (always reliable) and
// overlay optional fields from /dashboard/summary when the backend provides
// them — a graceful-degradation pattern so the strip never goes blank.
function computeKpis(equipment, alarms, summary) {
  const running = equipment.filter(e => e.state === 'RUN').length;
  const alarming = equipment.filter(e => e.state === 'ALARM').length;
  const active = alarms.filter(a => !a.acked).length;
  const critical = alarms.filter(a => !a.acked && a.severity === 'CRITICAL').length;
  const healths = equipment.filter(e => e.health > 0).map(e => e.health);
  const avg = healths.length ? healths.reduce((a, b) => a + b, 0) / healths.length : 0;
  const min = healths.length ? Math.min(...healths) : 0;

  const oee = summary?.oee_pct ?? summary?.oee;
  const wafers = summary?.wafers_today;
  const wafersDelta = summary?.wafers_last_hour;

  return [
    { label: 'Tools Running', value: `${running}`, sub: `of ${equipment.length} monitored`, color: '#5ee38f' },
    { label: 'Active Alarms', value: `${active}`,  sub: `${critical} critical`, color: active > 0 ? '#ff5470' : '#eaf2ff' },
    { label: 'Avg. Health',   value: avg.toFixed(1), unit: '%', sub: 'across fleet' },
    { label: 'Min. Health',   value: min.toFixed(1), unit: '%', sub: 'worst in fleet', color: min < 70 ? '#ffc048' : '#eaf2ff' },
    oee != null
      ? { label: 'OEE',         value: Number(oee).toFixed(1), unit: '%', sub: 'last 24 h' }
      : { label: 'In Alarm',    value: `${alarming}`, sub: 'tools firing',           color: alarming > 0 ? '#ff5470' : '#eaf2ff' },
    wafers != null
      ? { label: 'Wafers Today', value: String(wafers), sub: wafersDelta != null ? `+${wafersDelta} last hour` : '' }
      : { label: 'Fleet Size',   value: `${equipment.length}`, sub: 'connected tools' },
  ];
}

// =============================================================================
// <App/>
// =============================================================================
function App() {
  const [page, setPage] = useStateA('dashboard');
  const [lang, setLang] = useStateA('EN');

  // Core read models (mirror of backend; refreshed every 3 s)
  const [equipment, setEquipment]       = useStateA([]);
  const [alarms, setAlarms]             = useStateA([]);
  const [events, setEvents]             = useStateA([]);
  const [summary, setSummary]           = useStateA(null);
  const [alarmSummary, setAlarmSummary] = useStateA(null);
  const [mrp, setMrp]                   = useStateA(null);

  // UI-local state
  const [selectedId, setSelectedId]   = useStateA(null);
  const [eventFilter, setEventFilter] = useStateA('ALL');
  const [clock, setClock]             = useStateA(fmtFullTime(new Date()));
  const [lastCommand, setLastCommand] = useStateA(null);
  const [connErr, setConnErr]         = useStateA(null);

  // Monitor page: per-machine telemetry timeseries (fetched only when visible)
  const [telemetry, setTelemetry] = useStateA({ machineId: null, points: [] });

  // Optimistic ack overrides — keyed by alarm.id. Hides an acked alarm from
  // the active list immediately, then the next /alarms poll confirms it.
  const [ackedLocally, setAckedLocally] = useStateA(() => new Set());

  // ---- Wall clock ticker (independent of polling) -----------------------
  useEffectA(() => {
    const t = setInterval(() => setClock(fmtFullTime(new Date())), 1000);
    return () => clearInterval(t);
  }, []);

  // ---- Main polling loop ------------------------------------------------
  // One tick fans out to six endpoints in parallel. Partial failures (e.g.
  // /dashboard/summary missing) are caught locally and fall back to derived
  // values — the screen shouldn't go blank because one optional endpoint 404s.
  useEffectA(() => {
    let cancelled = false;

    const fetchAll = async () => {
      try {
        const [machinesRes, alarmsRes, alarmSumRes, sumRes, eventsRes, mrpEventsRes] = await Promise.all([
          apiGet('/api/machines'),
          apiGet('/api/alarms?status=active'),
          apiGet('/api/alarms/summary').catch(() => null),
          apiGet('/api/dashboard/summary').catch(() => null),
          // 200 is a generous window; MachineHeartbeat is the dominant event
          // type by volume so a smaller limit would starve the log of the
          // interesting CEID/ALID rows.
          apiGet('/api/events?limit=200'),
          apiGet('/api/events?event_type=MRPPlanUpdated&limit=10').catch(() => ({ events: [] })),
        ]);
        if (cancelled) return;

        const machines = (Array.isArray(machinesRes) ? machinesRes : []).map(transformMachine);
        setEquipment(machines);

        const rawAlarms = (alarmsRes?.alarms || []);
        setAlarms(rawAlarms.map(transformAlarm));
        setAlarmSummary(alarmSumRes);

        setSummary(sumRes);

        const rawEvents = (eventsRes?.events || (Array.isArray(eventsRes) ? eventsRes : []));
        // Backend returns ascending by event_seq; the UI shows newest-first.
        const sortedDesc = [...rawEvents].sort((a, b) => (b.event_seq ?? 0) - (a.event_seq ?? 0));
        setEvents(sortedDesc.map(transformEvent));

        const rawMrp = (mrpEventsRes?.events || (Array.isArray(mrpEventsRes) ? mrpEventsRes : []));
        setMrp(extractMrp(rawMrp));

        setConnErr(null);
      } catch (e) {
        if (!cancelled) setConnErr(String(e.message || e));
      }
    };

    fetchAll();
    const t = setInterval(fetchAll, POLL_MS);
    return () => { cancelled = true; clearInterval(t); };
  }, []);

  // Default selectedId once equipment arrives. Separate effect so the user's
  // click choice survives subsequent polls.
  useEffectA(() => {
    if (!selectedId && equipment.length) setSelectedId(equipment[0].id);
  }, [equipment, selectedId]);

  // ---- Monitor page: telemetry timeseries -------------------------------
  // Only fetch when page is visible + a machine is selected, so a user sitting
  // on the Dashboard doesn't hammer the telemetry endpoint.
  useEffectA(() => {
    if (page !== 'monitor' || !selectedId) return;
    let cancelled = false;
    const fetchTelemetry = async () => {
      try {
        const res = await apiGet(`/api/machines/${encodeURIComponent(selectedId)}/telemetry?range=5m`);
        if (cancelled) return;
        const points = res?.points || (Array.isArray(res) ? res : []);
        setTelemetry({ machineId: selectedId, points });
      } catch (e) {
        if (!cancelled) setTelemetry({ machineId: selectedId, points: [] });
      }
    };
    fetchTelemetry();
    const t = setInterval(fetchTelemetry, POLL_MS);
    return () => { cancelled = true; clearInterval(t); };
  }, [page, selectedId]);

  // ---- Derived state ----------------------------------------------------
  const visibleAlarms = useMemoA(
    () => alarms.map(a => ackedLocally.has(a.id) ? { ...a, acked: true } : a),
    [alarms, ackedLocally],
  );
  const selected = equipment.find(e => e.id === selectedId) || equipment[0];
  const kpis = useMemoA(
    () => computeKpis(equipment, visibleAlarms, summary),
    [equipment, visibleAlarms, summary],
  );

  // ---- Actions ----------------------------------------------------------
  const ackAlarm = useCallbackA(async (alarmId) => {
    // Optimistic UI — operator doesn't want a 3 s wait to see the row flip.
    setAckedLocally(prev => {
      const next = new Set(prev);
      next.add(alarmId);
      return next;
    });
    try {
      await apiPost(
        `/api/alarms/${encodeURIComponent(alarmId)}/ack`,
        {},
        { 'X-User': 'dashboard-operator' },
      );
    } catch (e) {
      console.error('alarm ack failed', e);
      // Roll back optimistic ack on error so operator can retry.
      setAckedLocally(prev => {
        const next = new Set(prev);
        next.delete(alarmId);
        return next;
      });
    }
  }, []);

  const sendCommand = useCallbackA(async (cmd) => {
    if (!selected) return;
    const time = fmtTime(new Date());
    setLastCommand({ cmd, time });
    try {
      // Backend normalizes START/STOP/PAUSE/RESUME/RESET/ABORT and turns them
      // into a HostCommandDispatched event → SECS S2F41 on the wire.
      await apiPost(
        `/api/machines/${encodeURIComponent(selected.id)}/commands`,
        { command: cmd },
      );
    } catch (e) {
      console.error('host command failed', e);
      setLastCommand({ cmd, time, error: String(e.message || e) });
    }
  }, [selected]);

  // ---- Render -----------------------------------------------------------
  const titles = {
    dashboard: 'Equipment Overview',
    monitor:   'Real-time Monitoring',
    alarms:    'Alarm Monitor',
    events:    'Event Log',
    control:   'Remote Control',
  };

  return (
    <Shell>
      <Sidebar active={page} onNav={setPage} />
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
        <TopBar title={titles[page]} lang={lang} onLang={setLang} clock={clock} />
        {connErr && (
          <div style={{
            padding: '6px 18px',
            background: 'rgba(255,45,74,0.14)',
            borderBottom: '1px solid #ff2d4a',
            color: '#ffd1d9',
            fontSize: 11,
            fontFamily: 'JetBrains Mono, monospace',
          }}>
            Backend unreachable · {connErr} · retrying every {POLL_MS / 1000}s
          </div>
        )}
        <main style={{ padding: 14, flex: 1, overflow: 'auto' }}>
          {page === 'dashboard' && (
            <DashboardPage
              kpis={kpis}
              equipment={equipment}
              alarms={visibleAlarms}
              mrp={mrp}
              selectedId={selected?.id}
              onSelect={(e) => setSelectedId(e.id)}
              onAck={ackAlarm}
            />
          )}
          {page === 'monitor' && selected && (
            <MonitorPage
              equipment={equipment}
              selected={selected}
              telemetry={telemetry}
              onSelect={(e) => setSelectedId(e.id)}
            />
          )}
          {page === 'alarms' && (
            <AlarmsPage alarms={visibleAlarms} onAck={ackAlarm} />
          )}
          {page === 'events' && (
            <EventsPage events={events} filter={eventFilter} onFilter={setEventFilter} />
          )}
          {page === 'control' && selected && (
            <ControlPage
              equipment={equipment}
              selected={selected}
              onSelect={(e) => setSelectedId(e.id)}
              onCommand={sendCommand}
              lastCommand={lastCommand}
            />
          )}
          {!selected && (page === 'monitor' || page === 'control') && (
            <Card title="Loading fleet…" meta={`GET ${API_BASE}/api/machines`}>
              <div style={{ padding: 18, color: '#8fb7ff', fontSize: 12 }}>
                Waiting for the first poll to complete.
              </div>
            </Card>
          )}
        </main>
      </div>
    </Shell>
  );
}

// ============================================================================
// Pages
// ============================================================================
function DashboardPage({ kpis, equipment, alarms, mrp, selectedId, onSelect, onAck }) {
  const active = alarms.filter(a => !a.acked);
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      <KpiStrip kpis={kpis} />
      {active.length > 0 && active[0].severity === 'CRITICAL' && (
        <AlarmBanner alarm={active[0]} onAck={() => onAck(active[0].id)} />
      )}
      <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr', gap: 10 }}>
        <Card title="Equipment Fleet" meta={`${equipment.length} tools · click to inspect`}>
          {equipment.length
            ? <EquipmentGrid equipment={equipment} selectedId={selectedId} onSelect={onSelect} />
            : <div style={{ padding: 12, color: '#5a7aa8', fontSize: 12, fontFamily: 'JetBrains Mono, monospace' }}>
                No tools reported yet.
              </div>
          }
        </Card>
        <Card title="Active Alarms" meta={`${active.length} requiring ack`}>
          <AlarmList alarms={alarms} onAck={onAck} />
        </Card>
      </div>
      {/* MRP card — minimal surface, reads the latest MRPPlanUpdated event.
          Placed on the dashboard (not a separate page) so the
          equipment → capacity → MRP cause/effect chain is visible at a glance.  */}
      <MrpCard mrp={mrp} />
    </div>
  );
}

function MrpCard({ mrp }) {
  if (!mrp) {
    return (
      <Card title="Material Requirements Plan" meta="no MRPPlanUpdated event yet">
        <div style={{ padding: 12, color: '#5a7aa8', fontSize: 12, fontFamily: 'JetBrains Mono, monospace' }}>
          Waiting for the next MRP recompute. Triggered by AlarmTriggered / StateChanged
          events on capacity-bearing machines.
        </div>
      </Card>
    );
  }
  const shortage = mrp.has_shortage;
  const tone = shortage ? '#ff5470' : '#5ee38f';
  const tile = (label, value, sub, color) => (
    <div style={{ padding: 14, background: '#0d1830', border: '1px solid #17375f', borderRadius: 10 }}>
      <div style={{ fontSize: 10, color: '#8fb7ff', letterSpacing: '0.08em', textTransform: 'uppercase', fontWeight: 500 }}>
        {label}
      </div>
      <div style={{ marginTop: 6, fontFamily: 'JetBrains Mono, monospace', fontSize: 20, fontWeight: 600, color: color || '#eaf2ff' }}>
        {value}
      </div>
      {sub && <div style={{ fontSize: 10, color: '#5a7aa8', marginTop: 4, fontFamily: 'JetBrains Mono, monospace' }}>{sub}</div>}
    </div>
  );
  return (
    <Card
      title="Material Requirements Plan"
      meta={`part ${mrp.part_no || '—'} · updated ${fmtTimeFromIso(mrp.updated_at)}${mrp.machine_id ? ' · cause ' + mrp.machine_id : ''}`}
      right={<StatusChip state={shortage ? 'CRITICAL' : 'OK'}>{shortage ? 'SHORTAGE' : 'COVERED'}</StatusChip>}
    >
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12 }}>
        {tile('Part at risk',       mrp.part_no || '—',                                                    'triggered by capacity loss')}
        {tile('Total shortage qty', mrp.total_shortage_qty != null ? String(mrp.total_shortage_qty) : '—', 'units below demand', tone)}
        {tile('Suggested PO qty',   mrp.suggested_po_qty   != null ? String(mrp.suggested_po_qty)   : '—', 'safety-stock covered')}
        {tile('Order by',           fmtDateFromIso(mrp.suggested_order_date),                              mrp.earliest_shortage_date ? `shortage on ${fmtDateFromIso(mrp.earliest_shortage_date)}` : 'no shortage window')}
      </div>
    </Card>
  );
}

function AlarmBanner({ alarm, onAck }) {
  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 12,
      background: 'rgba(255,45,74,0.32)',
      border: '1px solid #ff2d4a', borderRadius: 10, padding: '10px 14px',
      boxShadow: '0 0 0 1px #ff2d4a, 0 0 14px rgba(255,45,74,0.5)',
    }}>
      <LED color="#ff2d4a" pulse size={10} />
      <div>
        <div style={{ fontSize: 13, color: '#fff', fontWeight: 700, letterSpacing: '0.02em' }}>{alarm.message}</div>
        <div style={{ fontSize: 10, color: '#ffd1d9', fontFamily: 'JetBrains Mono, monospace', marginTop: 2 }}>
          ALID {alarm.alid} · {alarm.equipment} · {alarm.time}
        </div>
      </div>
      <div style={{ flex: 1 }} />
      <StatusChip state="CRITICAL" />
      <Button variant="default" onClick={onAck}>Acknowledge</Button>
    </div>
  );
}

function MonitorPage({ equipment, selected, telemetry, onSelect }) {
  // Build chart series from the live telemetry feed for the selected tool.
  // TrendChart needs ≥ 2 points; duplicate the latest when the series is
  // still warming up so we don't NaN the scale.
  const points = (telemetry?.machineId === selected.id ? telemetry.points : []) || [];
  const padded = points.length >= 2
    ? points
    : (points.length
        ? [points[0], points[0]]
        : [
            { recorded_at: null, temperature: 0, vibration: 0, rpm: 0 },
            { recorded_at: null, temperature: 0, vibration: 0, rpm: 0 },
          ]);

  const primary = PRIMARY_METRIC[selected.type] || 'temperature';
  const primaryColor = primary === 'vibration' ? '#5ee38f'
                     : primary === 'rpm'       ? '#c27bff'
                     : '#4a9eff';
  const primarySeries = [{
    label: `${primary.charAt(0).toUpperCase()}${primary.slice(1)}`,
    color: primaryColor,
    data: padded.map(p => Number(p[primary] ?? 0)),
  }];
  // Secondary chart: always show RPM (useful for all tool types as a
  // proxy for "is the actuator moving"). For PVD where rpm IS the primary,
  // show temperature instead to avoid a redundant double chart.
  const secondaryMetric = primary === 'rpm' ? 'temperature' : 'rpm';
  const secondaryLabel = secondaryMetric === 'rpm' ? 'Spindle RPM' : 'Temperature';
  const secondaryUnit  = PRIMARY_UNIT[secondaryMetric] || '';
  const secondarySeries = [{
    label: secondaryMetric === 'rpm' ? 'RPM' : 'Temp',
    color: '#ffc048',
    data: padded.map(p => Number(p[secondaryMetric] ?? 0)),
  }];

  const latest = points.length ? points[points.length - 1] : null;
  const raw = selected._raw || {};

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <Card
        title={`Tool · ${selected.id}`}
        meta={`${selected.type} · ${selected.state} · last ${fmtTimeFromIso(raw.last_update)}`}
        right={<StatusChip state={selected.state} dot />}
      >
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12 }}>
          {[
            { label: 'Temperature',   v: fmtMetric('temperature', latest?.temperature ?? raw.temperature), u: '°C' },
            { label: 'Vibration',     v: fmtMetric('vibration',   latest?.vibration   ?? raw.vibration),   u: 'mm/s' },
            { label: 'RPM',           v: fmtMetric('rpm',         latest?.rpm         ?? raw.rpm),         u: 'rpm' },
            { label: 'Active alarms', v: String(raw.active_alarm_count ?? 0),                               u: '' },
          ].map(m => (
            <div key={m.label} style={{ padding: 14, background: '#0d1830', border: '1px solid #17375f', borderRadius: 10 }}>
              <div style={{ fontSize: 10, color: '#8fb7ff', letterSpacing: '0.08em', textTransform: 'uppercase', fontWeight: 500 }}>
                {m.label}
              </div>
              <div style={{ marginTop: 6, fontFamily: 'JetBrains Mono, monospace', fontSize: 22, fontWeight: 600, color: '#eaf2ff' }}>
                {m.v}{m.u && <span style={{ fontSize: 12, color: '#8fb7ff', marginLeft: 4 }}>{m.u}</span>}
              </div>
            </div>
          ))}
        </div>
      </Card>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
        <Card title={`${primary.charAt(0).toUpperCase()}${primary.slice(1)}`} meta="last 5 min">
          <TrendChart series={primarySeries} yLabel={PRIMARY_UNIT[primary] || ''} />
        </Card>
        <Card title={secondaryLabel} meta="last 5 min">
          <TrendChart series={secondarySeries} yLabel={secondaryUnit} />
        </Card>
      </div>
      <Card title="Fleet · quick select">
        <EquipmentGrid equipment={equipment} selectedId={selected.id} onSelect={onSelect} />
      </Card>
    </div>
  );
}

function AlarmsPage({ alarms, onAck }) {
  const active = alarms.filter(a => !a.acked);
  const acked = alarms.filter(a => a.acked);
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr', gap: 16 }}>
      <Card title={`Active · ${active.length}`} meta="awaiting acknowledge">
        <AlarmList alarms={active} onAck={onAck} />
      </Card>
      <Card title="Severity Summary">
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {['CRITICAL', 'MAJOR', 'MINOR'].map(sev => {
            const count = alarms.filter(a => a.severity === sev && !a.acked).length;
            return (
              <div key={sev} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '8px 12px', background: '#0d1830', border: '1px solid #17375f', borderRadius: 8 }}>
                <StatusChip state={sev} />
                <div style={{ flex: 1 }} />
                <span style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 18, fontWeight: 600, color: '#eaf2ff' }}>{count}</span>
              </div>
            );
          })}
        </div>
      </Card>
      <div style={{ gridColumn: '1 / -1' }}>
        <Card title={`Acknowledged · ${acked.length}`} meta="audit log">
          <AlarmList alarms={acked} onAck={onAck} />
        </Card>
      </div>
    </div>
  );
}

function EventsPage({ events, filter, onFilter }) {
  return (
    <Card title="Event Stream" meta={`${events.length} events in window`}>
      <EventLog events={events} filter={filter} onFilter={onFilter} />
    </Card>
  );
}

function ControlPage({ equipment, selected, onSelect, onCommand, lastCommand }) {
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1.6fr', gap: 16 }}>
      <Card title="Remote Control" meta={`target · ${selected.id}`}>
        <ControlPanel equipment={selected} onCommand={onCommand} lastCommand={lastCommand} />
      </Card>
      <Card title="Select Target Equipment">
        <EquipmentGrid equipment={equipment} selectedId={selected.id} onSelect={onSelect} />
      </Card>
    </div>
  );
}