// Render the paper's diagrams as PNG with Playwright. Usage: node figs.js <outdir>
const { chromium } = require('playwright');
const out = process.argv[2] || '.';

const FONT = 'Arial, Helvetica, sans-serif';
function esc(s) { return s.replace(/&/g, '&amp;').replace(/</g, '&lt;'); }
function box(x, y, w, h, lines, o = {}) {
  const fill = o.fill || '#ffffff', fs = o.fs || 13, bold = o.bold ? 'bold' : 'normal';
  const rx = o.round === false ? 0 : 6;
  let s = `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="${rx}" fill="${fill}" stroke="${o.stroke || '#222'}" stroke-width="${o.sw || 1.2}" ${o.dash ? 'stroke-dasharray="5 3"' : ''}/>`;
  const lh = fs * 1.18, top = y + h / 2 - (lines.length - 1) * lh / 2 + fs * 0.35;
  lines.forEach((t, i) => {
    const b = i === 0 ? bold : 'normal';
    const size = i === 0 ? fs : fs - 1.5;
    s += `<text x="${x + w / 2}" y="${top + i * lh}" font-family="${FONT}" font-size="${size}" font-weight="${b}" text-anchor="middle" fill="#111">${esc(t)}</text>`;
  });
  return s;
}
function label(x, y, t, o = {}) {
  return `<text x="${x}" y="${y}" font-family="${FONT}" font-size="${o.fs || 12}" font-weight="${o.bold ? 'bold' : 'normal'}" font-style="${o.italic ? 'italic' : 'normal'}" text-anchor="${o.anchor || 'middle'}" fill="${o.color || '#111'}">${esc(t)}</text>`;
}
function arrow(pts, o = {}) {
  const d = pts.map((p, i) => (i ? 'L' : 'M') + p[0] + ' ' + p[1]).join(' ');
  return `<path d="${d}" fill="none" stroke="#222" stroke-width="1.3" ${o.dash ? 'stroke-dasharray="4 3"' : ''} marker-end="url(#ah)"/>`;
}
const DEFS = `<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" fill="#222"/></marker></defs>`;
function svg(w, h, body) {
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">${DEFS}<rect width="${w}" height="${h}" fill="#fff"/>${body}</svg>`;
}
const G = '#eef2f8', Y = '#fbf3df', Gr = '#e9f4ea', R = '#fbe9e7';

// ---------- Fig. 1: architecture ----------
function fig1() {
  const W = 460; let b = '';
  b += box(10, 8, W - 20, 34, ['Users: Admin · Teacher · Student (web browser)'], { fill: '#fff', bold: true });
  b += arrow([[W / 2, 42], [W / 2, 56]]);
  b += `<rect x="10" y="56" width="${W - 20}" height="64" rx="6" fill="${G}" stroke="#222"/>`;
  b += label(20, 72, 'Presentation (HTML/CSS, Alpine.js, SSE, VI/EN)', { anchor: 'start', bold: true, fs: 12 });
  const pw = (W - 20 - 50) / 4;
  ['Chat copilot', 'Topology canvas', 'Dashboard', 'Users / groups'].forEach((t, i) => { b += box(20 + i * (pw + 10), 80, pw, 30, [t], { fs: 11.5 }); });
  b += arrow([[W / 2, 120], [W / 2, 134]]);
  b += label(W / 2 + 6, 131, 'REST + JWT', { anchor: 'start', fs: 10.5, italic: true });
  b += `<rect x="10" y="134" width="${W - 20}" height="196" rx="6" fill="#f6f6f6" stroke="#222"/>`;
  b += label(20, 150, 'Application layer (Flask blueprints, Pydantic)', { anchor: 'start', bold: true, fs: 12 });
  const aw = (W - 20 - 40) / 3;
  const app = [
    ['Auth & governance', 'roles, scoped grants'], ['AI module', 'router, design advisor'], ['Topology & design', 'canvas, config gen.'],
    ['Change control', 'preview → apply'], ['Monitoring', 'parsers, CDP/LLDP'], ['Audit & checkpoints', 'redacted log, undo'],
  ];
  app.forEach((t, i) => { const c = i % 3, r = Math.floor(i / 3); b += box(20 + c * (aw + 10), 158 + r * 60, aw, 50, t, { fs: 12, bold: true, fill: r === 0 && c === 1 ? Y : '#fff' }); });
  b += box(20, 280, W - 40, 40, ['Persistence: SQLAlchemy / SQLite (~45 tables)', 'device credentials encrypted with Fernet, never serialised'], { fs: 12, bold: true, fill: '#fff', dash: true });
  b += arrow([[W / 2, 330], [W / 2, 344]]);
  b += `<rect x="10" y="344" width="${W - 20}" height="76" rx="6" fill="${Gr}" stroke="#222"/>`;
  b += label(20, 360, 'Integration adapters', { anchor: 'start', bold: true, fs: 12 });
  const iw = (W - 20 - 50) / 4;
  [['SSH', 'Paramiko / Netmiko'], ['PNETLab API', 'import / export'], ['Telnet console', 'SSH bootstrap'], ['LLM provider', 'Gemini / Claude']].forEach((t, i) => { b += box(20 + i * (iw + 10), 368, iw, 44, t, { fs: 11.5, bold: true }); });
  const lx = 10, lw = 330;
  b += `<rect x="${lx}" y="440" width="${lw}" height="84" rx="6" fill="#fff" stroke="#222" stroke-dasharray="5 3"/>`;
  b += label(20, 456, 'PNETLab lab · MGMT-NET (Cloud0)', { anchor: 'start', bold: true, fs: 12 });
  const dw = (lw - 20 - 30) / 4;
  [['R1', 'IOS router'], ['S1', 'IOSv-L2'], ['S2', 'IOSv-L2'], ['FW', 'ASA / PA-VM']].forEach((t, i) => { b += box(lx + 10 + i * (dw + 10), 466, dw, 48, t, { fs: 12, bold: true }); });
  for (const i of [0, 1, 2]) { const x = 20 + i * (iw + 10) + iw / 2; b += arrow([[x, 412], [x, 440]], { dash: true }); }
  const cx = 20 + 3 * (iw + 10);
  b += box(350, 440, W - 360, 84, ['Cloud LLM', 'sees no IPs', 'or credentials'], { fill: Y, bold: true, fs: 12, dash: true });
  b += arrow([[cx + iw / 2, 412], [cx + iw / 2, 440]]);
  return svg(W, 532, b);
}

// ---------- Fig. 2: AI pipeline ----------
function fig2() {
  const W = 460; let b = '';
  b += box(130, 8, 200, 40, ['User message (VI / EN)', '+ recent turns, device names'], { bold: true, fs: 12.5 });
  b += arrow([[230, 48], [230, 62]]);
  b += box(90, 62, 280, 46, ['Intent router (LLM, JSON schema)', 'keyword rules as fallback'], { bold: true, fs: 12.5, fill: Y });
  const cw = 104, xs = [10, 122, 234, 346];
  ['chat', 'read', 'configure', 'design'].forEach((t, i) => {
    b += arrow([[230, 108], [230, 118], [xs[i] + cw / 2, 118], [xs[i] + cw / 2, 134]]);
    b += box(xs[i], 134, cw, 28, [t], { bold: true, fs: 12.5, fill: G });
  });
  const col = [
    [['Answer with', 'topology context'], ['No device', 'access']],
    [['Allowlist +', 'role check'], ['SSH show →', 'parsers'], ['Troubleshoot:', 'LLM explains', 'redacted output']],
    [['AIAction JSON', '(Pydantic)'], ['Policy + risk', 'classification'], ['Frozen change', 'preview / batch']],
    [['Design advisor', '(full L2/L3)'], ['Trial apply →', 'validate → undo'], ['Proposal; retry', '≤ 2 on error']],
  ];
  col.forEach((steps, i) => {
    let y = 172;
    steps.forEach((s, j) => {
      const h = s.length > 2 ? 52 : 40;
      b += arrow([[xs[i] + cw / 2, y], [xs[i] + cw / 2, y + 10]]);
      b += box(xs[i], y + 10, cw, h, s, { fs: 11.5, bold: false, fill: j === steps.length - 1 && i >= 2 ? Gr : '#fff' });
      y += h + 10;
    });
  });
  b += `<rect x="10" y="352" width="${W - 20}" height="36" rx="6" fill="${R}" stroke="#222"/>`;
  b += label(W / 2, 366, 'Backend decides: role, project scope, allowlist, risk, approval', { fs: 12, bold: true });
  b += label(W / 2, 381, 'The model never opens SSH and never sees credentials or management IPs', { fs: 11 });
  return svg(W, 396, b);
}

// ---------- Fig. 3: design to lab ----------
function fig3() {
  const W = 460; let b = '';
  const steps = [
    [['Natural-language request', '"1 router, 1 switch, 6 PCs, 172.20.5.0/24"'], Y],
    [['AI design proposal', 'VLANs, SVIs, routes, OSPF, addressing'], Y],
    [['Topology canvas', 'blueprints, auto IPAM, lint, versions'], G],
    [['Export to PNETLab', 'nodes, p2p links, Cloud0 network'], Gr],
    [['Start nodes + console bootstrap', 'hostname, mgmt IP, user, RSA, SSH v2'], Gr],
    [['Managed over SSH', 'monitoring, read commands, changes'], '#fff'],
  ];
  let y = 8; const x = 44, w = 300, h = 46;
  steps.forEach(([t, f], i) => {
    b += box(x, y, w, h, t, { bold: true, fs: 12.5, fill: f });
    if (i < steps.length - 1) b += arrow([[x + w / 2, y + h], [x + w / 2, y + h + 14]]);
    y += h + 14;
  });
  // side labels
  // config generator branch
  b += box(x + w + 12, 124, W - (x + w + 12) - 6, 58, ['Config', 'generator', '→ change batch'], { fs: 11.5, fill: '#fff' });
  b += arrow([[x + w, 151], [x + w + 12, 151]]);
  // discovery loop
  const yb = 8 + 5 * 60 + h / 2;
  b += arrow([[x, yb], [24, yb], [24, 8 + 2 * 60 + h / 2], [x, 8 + 2 * 60 + h / 2]], { dash: true });
  b += `<text x="14" y="${(yb + 8 + 2 * 60 + h / 2) / 2}" font-family="${FONT}" font-size="11" text-anchor="middle" transform="rotate(-90 14 ${(yb + 8 + 2 * 60 + h / 2) / 2})">CDP/LLDP: design vs. reality</text>`;
  return svg(W, y, b);
}

// ---------- Fig. 4: change control ----------
function fig4() {
  const W = 460; let b = '';
  const x = 110, w = 240, h = 38; let y = 8;
  const S = (t, f, opts = {}) => { const r = box(x, y, w, opts.h || h, t, { bold: true, fs: 12, fill: f }); const yy = y; y += (opts.h || h) + 14; return [r, yy]; };
  const nodes = [];
  nodes.push(S(['Proposal', 'AI chat · canvas · manual · restore'], Y));
  nodes.push(S(['Policy check', 'forbidden / unknown → reject'], '#fff'));
  nodes.push(S(['Risk classification (backend)', 'Low / High, never from AI or user'], '#fff'));
  nodes.push(S(['Preview', 'frozen targets, commands, verification'], G));
  nodes.push(S(['Approval', 'Smart Approval (Low) or Teacher/Admin'], G));
  nodes.push(S(['Typed confirmation (High)', 'exact hostname or CONFIRM ALL'], G));
  nodes.push(S(['Pre-deploy recheck', 'grant, digest, baseline, window, lease'], '#fff'));
  nodes.push(S(['Backup running-config'], Gr));
  nodes.push(S(['Apply over SSH'], Gr));
  nodes.push(S(['Verify (read-only checks)'], Gr));
  nodes.forEach(([r, yy], i) => { b += r; if (i < nodes.length - 1) b += arrow([[x + w / 2, yy + h], [x + w / 2, yy + h + 14]]); });
  const yv = nodes[nodes.length - 1][1];
  // outcomes
  b += arrow([[x, yv + h / 2], [90, yv + h / 2], [90, yv + h + 18]]);
  b += box(36, yv + h + 18, 108, 46, ['success', '→ checkpoint'], { bold: true, fs: 12, fill: Gr });
  b += arrow([[x + w, yv + h / 2], [W - 64, yv + h / 2], [W - 64, yv + h + 18]]);
  b += box(W - 120, yv + h + 18, 110, 46, ['failed /', 'partial_success'], { bold: true, fs: 12, fill: R });
  b += box(160, yv + h + 18, 140, 46, ['Audit log', '(secrets redacted)'], { fs: 12, bold: true });
  const yp = nodes[3][1] + h / 2, ys = yv + h + 41;
  b += `<path d="M 36 ${ys} L 24 ${ys} L 24 ${yp} L ${x} ${yp}" fill="none" stroke="#222" stroke-width="1.3" stroke-dasharray="4 3" marker-end="url(#ah)"/>`;
  b += `<text x="14" y="${(yp + ys) / 2}" font-family="${FONT}" font-size="11" text-anchor="middle" transform="rotate(-90 14 ${(yp + ys) / 2})">Undo/Redo: config diff → new preview</text>`;
  // reject branch from policy
  const ypol = nodes[1][1] + h / 2;
  b += arrow([[x + w, ypol], [W - 14, ypol]]);
  b += label(W - 14, ypol - 6, 'reject', { anchor: 'end', fs: 11, italic: true });
  return svg(W, yv + h + 72, b);
}

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ deviceScaleFactor: 4 });
  for (const [name, fn] of [['fig1_architecture', fig1], ['fig2_ai_pipeline', fig2], ['fig3_design_to_lab', fig3], ['fig4_change_control', fig4]]) {
    const s = fn();
    require('fs').writeFileSync(`${out}/${name}.svg`, s);
    await page.setContent(`<html><body style="margin:0">${s}</body></html>`);
    const el = await page.$('svg');
    await el.screenshot({ path: `${out}/${name}.png` });
  }
  await browser.close();
})();
