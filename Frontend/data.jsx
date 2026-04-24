// Fake data + time helpers.

const now = new Date();
const pad = n => String(n).padStart(2, '0');
const fmtTime = (d) => `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
const fmtFullTime = (d) => `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${fmtTime(d)}`;

function rndTrend(base, variance, n = 24) {
  const out = [];
  let v = base;
  for (let i = 0; i < n; i++) {
    v += (Math.random() - 0.5) * variance;
    out.push(v);
  }
  return out;
}

const INITIAL_EQUIPMENT = [
  { id: 'CVD-01', type: 'CVD', recipe: 'R-042', state: 'RUN',   health: 96.3, trend: rndTrend(96, 1) },
  { id: 'CVD-02', type: 'CVD', recipe: 'R-042', state: 'IDLE',  health: 91.7, trend: rndTrend(92, 1) },
  { id: 'CVD-03', type: 'CVD', recipe: 'R-018', state: 'RUN',   health: 88.2, trend: rndTrend(88, 1.5) },
  { id: 'PVD-01', type: 'PVD', recipe: 'R-103', state: 'RUN',   health: 94.5, trend: rndTrend(94, 1) },
  { id: 'PVD-02', type: 'PVD', recipe: 'R-103', state: 'MAINTENANCE', health: 82.1, trend: rndTrend(82, 2) },
  { id: 'PVD-03', type: 'PVD', recipe: 'R-101', state: 'ALARM', health: 47.8, trend: rndTrend(55, 5) },
  { id: 'ETC-01', type: 'ETCH', recipe: 'R-221', state: 'RUN',   health: 92.9, trend: rndTrend(93, 1) },
  { id: 'ETC-02', type: 'ETCH', recipe: 'R-221', state: 'OFFLINE',  health: 0,    trend: rndTrend(5, 1) },
  { id: 'LP-1',   type: 'LPCVD', recipe: 'R-307', state: 'RUN',  health: 97.4, trend: rndTrend(97, 0.8) },
  { id: 'LP-2',   type: 'LPCVD', recipe: 'R-307', state: 'PAUSED',  health: 95.8, trend: rndTrend(96, 1) },
  { id: 'CMP-01', type: 'CMP', recipe: 'R-512', state: 'IDLE',   health: 89.3, trend: rndTrend(89, 1.5) },
  { id: 'CMP-02', type: 'CMP', recipe: 'R-512', state: 'E-STOP',    health: 0, trend: rndTrend(0, 0.2) },
];

const INITIAL_ALARMS = [
  { id: 'a1', alid: 7042, equipment: 'PVD-03', severity: 'CRITICAL', message: 'Chamber pressure out of range', time: '14:29:12', acked: false },
  { id: 'a2', alid: 3015, equipment: 'ETC-02', severity: 'MAJOR',    message: 'Communication timeout — host link lost', time: '13:58:41', acked: false },
  { id: 'a3', alid: 1108, equipment: 'CVD-03', severity: 'MINOR',    message: 'Gas flow MFC-3 drift > 2 %', time: '13:44:07', acked: true },
  { id: 'a4', alid: 5201, equipment: 'PVD-02', severity: 'MINOR',    message: 'Recipe checksum verified', time: '13:12:55', acked: true },
];

function genEvents() {
  const events = [];
  const kinds = [
    { kind: 'CEID', stream: 'S6F11', msgs: ['Process start', 'Process complete', 'Wafer loaded', 'Wafer unloaded', 'Recipe activated'] },
    { kind: 'ALID', stream: 'S5F1',  msgs: ['Alarm set', 'Alarm cleared'] },
    { kind: 'SVID', stream: 'S1F4',  msgs: ['Temperature updated', 'Pressure updated', 'Flow rate updated'] },
    { kind: 'PPID', stream: 'S7F3',  msgs: ['Recipe downloaded', 'Recipe verified'] },
    { kind: 'INFO', stream: 'S1F2',  msgs: ['Online acknowledge', 'Host command accepted'] },
  ];
  const sources = ['CVD-01', 'CVD-02', 'CVD-03', 'PVD-01', 'PVD-02', 'PVD-03', 'ETC-01', 'LP-1'];
  const start = new Date(now.getTime() - 30 * 60 * 1000);
  for (let i = 0; i < 40; i++) {
    const t = new Date(start.getTime() + i * 45 * 1000 + Math.random() * 30000);
    const k = kinds[Math.floor(Math.random() * kinds.length)];
    events.push({
      time: fmtTime(t),
      kind: k.kind,
      stream: k.stream,
      source: sources[Math.floor(Math.random() * sources.length)],
      message: k.msgs[Math.floor(Math.random() * k.msgs.length)],
    });
  }
  return events.reverse();
}

const INITIAL_EVENTS = genEvents();

// KPI helpers derived from equipment list
function computeKpis(equipment, alarms) {
  const running = equipment.filter(e => e.state === 'RUN').length;
  const alarming = equipment.filter(e => e.state === 'ALARM').length;
  const down = equipment.filter(e => e.state === 'DOWN').length;
  const healths = equipment.filter(e => e.health > 0).map(e => e.health);
  const avg = healths.reduce((a, b) => a + b, 0) / healths.length;
  const min = Math.min(...healths);
  const active = alarms.filter(a => !a.acked).length;
  return [
    { label: 'Tools Running',  value: `${running}`, sub: `of ${equipment.length} monitored`, color: '#5ee38f' },
    { label: 'Active Alarms',  value: `${active}`,  sub: `${alarms.filter(a => !a.acked && a.severity === 'CRITICAL').length} critical`, color: active > 0 ? '#ff5470' : '#eaf2ff' },
    { label: 'Avg. Health',    value: avg.toFixed(1), unit: '%', sub: 'across fleet' },
    { label: 'Min. Health',    value: min.toFixed(1), unit: '%', sub: 'worst in fleet', color: min < 70 ? '#ffc048' : '#eaf2ff' },
    { label: 'OEE',            value: '82.4', unit: '%', sub: 'last 24 h' },
    { label: 'Wafers Today',   value: '1,248', sub: `+42 last hour` },
  ];
}

Object.assign(window, { INITIAL_EQUIPMENT, INITIAL_ALARMS, INITIAL_EVENTS, computeKpis, fmtTime, fmtFullTime });
