// Dashboard sections — KPI strip, equipment grid, trend chart, alarm list,
// event log, remote control panel. All read from fake in-memory state.

const { useState: useStateD, useEffect: useEffectD, useMemo: useMemoD } = React;

// ---------- KPI strip ----------------------------------------------------
function KpiStrip({ kpis }) {
  return (
    <div style={{ display: 'grid', gridTemplateColumns: `repeat(${kpis.length}, 1fr)`, gap: 16 }}>
      {kpis.map(k => (
        <Card key={k.label} padding={16}>
          <div style={{ fontSize: 11, color: '#8fb7ff', letterSpacing: '0.08em', textTransform: 'uppercase', fontWeight: 500 }}>{k.label}</div>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 6, marginTop: 8 }}>
            <span style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 26, fontWeight: 600, fontVariantNumeric: 'tabular-nums', color: k.color || '#eaf2ff' }}>
              {k.value}
            </span>
            {k.unit && <span style={{ fontSize: 13, color: '#8fb7ff', fontWeight: 500 }}>{k.unit}</span>}
          </div>
          {k.sub && <div style={{ fontSize: 11, color: '#5a7aa8', marginTop: 4, fontFamily: 'JetBrains Mono, monospace' }}>{k.sub}</div>}
        </Card>
      ))}
    </div>
  );
}

// ---------- Equipment tile grid -----------------------------------------
function EquipmentTile({ eq, onClick, selected }) {
  const s = STATE_MAP[eq.state] || STATE_MAP.OFFLINE;
  return (
    <button onClick={() => onClick(eq)} style={{
      textAlign: 'left', cursor: 'pointer',
      background: selected ? '#12315a' : '#0f1b33',
      border: `1px solid ${selected ? '#4a9eff' : '#17375f'}`,
      borderRadius: 10, padding: 10,
      display: 'flex', flexDirection: 'column', gap: 6, fontFamily: 'inherit', color: 'inherit',
      transition: 'background 0.15s, border-color 0.15s',
    }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 12, fontWeight: 600, color: '#eaf2ff' }}>{eq.id}</span>
        <StatusChip state={eq.state} dot={eq.state === 'ALARM' || eq.state === 'RUN' || eq.state === 'E-STOP'} />
      </div>
      <div style={{ fontSize: 10, color: '#8fb7ff', fontFamily: 'JetBrains Mono, monospace' }}>{eq.type} · {eq.recipe}</div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 2 }}>
        <div>
          <div style={{ fontSize: 9, color: '#5a7aa8', letterSpacing: '0.08em', textTransform: 'uppercase' }}>Health</div>
          <div style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 14, fontWeight: 600, color: eq.health < 50 ? '#ff5470' : eq.health < 70 ? '#ffc048' : '#eaf2ff' }}>
            {eq.health.toFixed(1)}<span style={{ fontSize: 9, color: '#8fb7ff', marginLeft: 2 }}>%</span>
          </div>
        </div>
        <div style={{ flex: 1 }}>
          <Sparkline values={eq.trend} color={s.color} height={26} width={96} />
        </div>
      </div>
    </button>
  );
}

function EquipmentGrid({ equipment, selectedId, onSelect }) {
  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 8 }}>
      {equipment.map(eq => (
        <EquipmentTile key={eq.id} eq={eq} selected={eq.id === selectedId} onClick={onSelect} />
      ))}
    </div>
  );
}

// ---------- Trend chart (SVG line chart, multi-series) -----------------
function TrendChart({ series, height = 220, yLabel = 'Value', yRange }) {
  const width = 700;
  const padL = 40, padR = 14, padT = 10, padB = 24;
  const w = width - padL - padR;
  const h = height - padT - padB;
  const allVals = series.flatMap(s => s.data);
  const yMin = yRange ? yRange[0] : Math.min(...allVals);
  const yMax = yRange ? yRange[1] : Math.max(...allVals);
  const yScale = v => padT + h - ((v - yMin) / (yMax - yMin)) * h;
  const n = series[0].data.length;
  const xScale = i => padL + (i / (n - 1)) * w;

  // y ticks
  const ticks = 5;
  const tickVals = Array.from({ length: ticks }, (_, i) => yMin + (i / (ticks - 1)) * (yMax - yMin));

  return (
    <svg viewBox={`0 0 ${width} ${height}`} style={{ width: '100%', height, display: 'block' }}>
      {/* gridlines */}
      {tickVals.map((v, i) => (
        <g key={i}>
          <line x1={padL} y1={yScale(v)} x2={width - padR} y2={yScale(v)} stroke="#17375f" strokeWidth="1" strokeDasharray="2 3" />
          <text x={padL - 8} y={yScale(v) + 3} textAnchor="end" fontFamily="JetBrains Mono, monospace" fontSize="10" fill="#5a7aa8">
            {v.toFixed(1)}
          </text>
        </g>
      ))}
      {/* y axis label */}
      <text x={10} y={padT + h / 2} textAnchor="middle" fontSize="10" fill="#8fb7ff" transform={`rotate(-90 10 ${padT + h / 2})`} fontFamily="Inter, sans-serif" letterSpacing="0.08em">{yLabel.toUpperCase()}</text>
      {/* x ticks (sparse) */}
      {[0, Math.floor(n / 2), n - 1].map(i => (
        <text key={i} x={xScale(i)} y={height - 6} textAnchor="middle" fontFamily="JetBrains Mono, monospace" fontSize="10" fill="#5a7aa8">
          -{((n - 1 - i) * 5)}s
        </text>
      ))}
      {/* series */}
      {series.map((s, idx) => {
        const path = s.data.map((v, i) => `${i === 0 ? 'M' : 'L'}${xScale(i).toFixed(1)},${yScale(v).toFixed(1)}`).join(' ');
        return (
          <g key={idx}>
            <path d={path} fill="none" stroke={s.color} strokeWidth="1.75" />
            {/* last point */}
            <circle cx={xScale(n - 1)} cy={yScale(s.data[n - 1])} r="3" fill={s.color} />
          </g>
        );
      })}
      {/* legend */}
      <g transform={`translate(${padL}, ${padT - 2})`}>
        {series.map((s, i) => (
          <g key={i} transform={`translate(${i * 140}, 0)`}>
            <line x1="0" y1="0" x2="16" y2="0" stroke={s.color} strokeWidth="2" />
            <text x="22" y="3" fontFamily="Inter, sans-serif" fontSize="11" fill="#eaf2ff">{s.label}</text>
          </g>
        ))}
      </g>
    </svg>
  );
}

// ---------- Alarm list ---------------------------------------------------
function AlarmList({ alarms, onAck }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      {alarms.length === 0 && (
        <div style={{ padding: 16, textAlign: 'center', color: '#5a7aa8', fontSize: 12 }}>No active alarms.</div>
      )}
      {alarms.map(a => {
        const s = STATE_MAP[a.severity];
        const isActive = !a.acked;
        const isCrit = a.severity === 'CRITICAL' && isActive;
        return (
          <div key={a.id} style={{
            display: 'flex', alignItems: 'center', gap: 10,
            padding: '8px 10px',
            background: isActive ? s.fill : 'rgba(108,122,146,0.06)',
            border: `1px solid ${isActive ? s.color : '#17375f'}`,
            borderRadius: 6,
            boxShadow: isCrit ? `0 0 0 1px ${s.color}, 0 0 10px rgba(255,45,74,0.35)` : 'none',
          }}>
            {isCrit && <LED color="#ff2d4a" pulse />}
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 12, color: isActive ? (isCrit ? '#fff' : s.color) : '#8fb7ff', fontWeight: 600 }}>
                {a.message}
              </div>
              <div style={{ fontSize: 10, color: '#8fb7ff', marginTop: 2, fontFamily: 'JetBrains Mono, monospace' }}>
                ALID {a.alid} · {a.equipment} · {a.time}
              </div>
            </div>
            <StatusChip state={a.severity} />
            {isActive
              ? <Button variant="default" size="sm" onClick={() => onAck(a.id)}>Ack</Button>
              : <span style={{ fontSize: 9, color: '#5a7aa8', fontFamily: 'JetBrains Mono, monospace' }}>ACKED</span>}
          </div>
        );
      })}
    </div>
  );
}

// ---------- Event log table ---------------------------------------------
const EVENT_COLORS = {
  CEID: '#4a9eff',
  ALID: '#ff5470',
  SVID: '#5ee38f',
  PPID: '#ffc048',
  INFO: '#8fb7ff',
};
function EventLog({ events, filter, onFilter }) {
  const filtered = filter === 'ALL' ? events : events.filter(e => e.kind === filter);
  return (
    <div>
      <div style={{ display: 'flex', gap: 6, marginBottom: 10, alignItems: 'center' }}>
        <span style={{ fontSize: 10, color: '#5a7aa8', letterSpacing: '0.08em', textTransform: 'uppercase', marginRight: 4 }}>Filter</span>
        {['ALL', 'CEID', 'ALID', 'SVID', 'PPID'].map(f => (
          <button key={f} onClick={() => onFilter(f)} style={{
            padding: '4px 10px', fontSize: 10, letterSpacing: '0.08em',
            background: filter === f ? 'rgba(74,158,255,0.12)' : 'transparent',
            border: `1px solid ${filter === f ? '#4a9eff' : '#1f4f8a'}`,
            color: filter === f ? '#4a9eff' : '#8fb7ff',
            borderRadius: 4, cursor: 'pointer', fontFamily: 'JetBrains Mono, monospace', fontWeight: 500,
          }}>{f}</button>
        ))}
        <div style={{ flex: 1 }} />
        <span style={{ fontSize: 11, color: '#5a7aa8', fontFamily: 'JetBrains Mono, monospace' }}>
          {filtered.length} of {events.length}
        </span>
      </div>
      <div style={{ border: '1px solid #1d3f66', borderRadius: 6, overflow: 'hidden' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontFamily: 'JetBrains Mono, monospace', fontSize: 12, color: '#eaf2ff' }}>
          <thead>
            <tr style={{ background: '#12315a' }}>
              {['Time', 'Type', 'Stream', 'Source', 'Message'].map(h => (
                <th key={h} style={{ padding: '8px 10px', borderBottom: '1px solid #1d3f66', textAlign: 'left', fontSize: 10, letterSpacing: '0.08em', textTransform: 'uppercase', color: '#8fb7ff', fontWeight: 600 }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {filtered.slice(0, 14).map((e, i) => (
              <tr key={e.time + i} style={{ background: i % 2 ? '#0d1830' : '#0f1b33' }}>
                <td style={{ padding: '6px 10px', borderBottom: '1px solid #1d3f66', color: '#8fb7ff' }}>{e.time}</td>
                <td style={{ padding: '6px 10px', borderBottom: '1px solid #1d3f66' }}>
                  <span style={{ color: EVENT_COLORS[e.kind], fontWeight: 600 }}>{e.kind}</span>
                </td>
                <td style={{ padding: '6px 10px', borderBottom: '1px solid #1d3f66', color: '#4a9eff' }}>{e.stream}</td>
                <td style={{ padding: '6px 10px', borderBottom: '1px solid #1d3f66', color: '#eaf2ff' }}>{e.source}</td>
                <td style={{ padding: '6px 10px', borderBottom: '1px solid #1d3f66', color: '#eaf2ff', fontFamily: 'Inter, sans-serif' }}>{e.message}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ---------- Remote control panel ----------------------------------------
function ControlPanel({ equipment, onCommand, lastCommand }) {
  const [confirm, setConfirm] = useStateD(null); // pending dangerous command
  const canStart = equipment.state === 'IDLE' || equipment.state === 'SETUP';
  const canStop = equipment.state === 'RUN';
  const canReset = equipment.state === 'ALARM' || equipment.state === 'DOWN';

  const fire = (cmd) => {
    if (cmd === 'STOP' || cmd === 'ABORT' || cmd === 'RESET') {
      setConfirm(cmd);
    } else {
      onCommand(cmd);
    }
  };
  const confirmFire = () => { onCommand(confirm); setConfirm(null); };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '10px 12px', background: '#0d1830', border: '1px solid #17375f', borderRadius: 8 }}>
        <LED color={STATE_MAP[equipment.state].color} pulse={equipment.state === 'ALARM' || equipment.state === 'RUN'} />
        <div style={{ flex: 1 }}>
          <div style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 14, fontWeight: 600 }}>{equipment.id}</div>
          <div style={{ fontSize: 11, color: '#8fb7ff', fontFamily: 'JetBrains Mono, monospace' }}>{equipment.type} · {equipment.recipe}</div>
        </div>
        <StatusChip state={equipment.state} />
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
        <Button variant="go" size="lg" icon="play" disabled={!canStart} onClick={() => fire('START')}>Start</Button>
        <Button variant="stop" size="lg" icon="square" disabled={!canStop} onClick={() => fire('STOP')}>Stop</Button>
        <Button variant="default" size="lg" icon="pause" disabled={equipment.state !== 'RUN'} onClick={() => fire('PAUSE')}>Pause</Button>
        <Button variant="default" size="lg" icon="play" disabled={equipment.state !== 'IDLE' && equipment.state !== 'SETUP'} onClick={() => fire('RESUME')}>Resume</Button>
        <Button variant="stop" size="lg" icon="rotate-cw" disabled={!canReset} onClick={() => fire('RESET')}>Reset</Button>
        <Button variant="stop" size="lg" icon="triangle-alert" onClick={() => fire('ABORT')}>Abort</Button>
      </div>

      {confirm && (
        <div style={{ padding: 12, background: 'rgba(255,107,107,0.1)', border: '1px solid #ff6b6b', borderRadius: 8 }}>
          <div style={{ fontSize: 12, color: '#ff6b6b', fontWeight: 600, marginBottom: 8 }}>
            Confirm {confirm} on {equipment.id}?
          </div>
          <div style={{ fontSize: 11, color: '#eaf2ff', fontFamily: 'JetBrains Mono, monospace', marginBottom: 10 }}>
            S2F41 · HCACK will be sent. This cannot be undone mid-wafer.
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            <Button variant="stop" size="sm" onClick={confirmFire}>Confirm {confirm}</Button>
            <Button variant="ghost" size="sm" onClick={() => setConfirm(null)}>Cancel</Button>
          </div>
        </div>
      )}

      {lastCommand && !confirm && (
        <div style={{ padding: 10, background: 'rgba(74,158,255,0.06)', border: '1px solid #17375f', borderRadius: 8, fontSize: 11, fontFamily: 'JetBrains Mono, monospace', color: '#8fb7ff' }}>
          <span style={{ color: '#4a9eff', fontWeight: 600 }}>→ {lastCommand.cmd}</span> {' '}
          · {lastCommand.time} · ack <span style={{ color: '#58d68d' }}>HCACK 0</span> (accepted)
        </div>
      )}
    </div>
  );
}

Object.assign(window, { KpiStrip, EquipmentGrid, EquipmentTile, TrendChart, AlarmList, EventLog, ControlPanel });
