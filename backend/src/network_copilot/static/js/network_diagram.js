// Shared renderer so a proposed diagram looks the same in chat and Projects.
window.NCDiagram = Object.freeze({
  render(diagram) {
    if (!diagram || !Array.isArray(diagram.nodes) || !Array.isArray(diagram.links)) return "";
    const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (character) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[character]));
    const kinds = ["internet", "firewall", "router", "switch", "server", "host"];
    const names = {
      internet: "Internet",
      firewall: "Firewall",
      router: "Router",
      switch: "Switch",
      server: "Server",
      host: "Thiết bị đầu cuối",
    };
    const nodes = diagram.nodes.slice(0, 16).filter((node) => node && kinds.includes(node.kind));
    const rows = kinds.map((kind) => nodes.filter((node) => node.kind === kind)).filter((row) => row.length);
    if (!nodes.length) return "";
    const boxHeight = (node) => 58 + Math.min(4, Array.isArray(node.ip_addresses) ? node.ip_addresses.length : 0) * 14;
    const circular = rows.length === 1 && nodes.length > 2;
    const radius = Math.max(240, nodes.length * 44);
    const rowHeights = rows.map((row) => Math.max(...row.map(boxHeight)));
    const width = circular ? radius * 2 + 220 : Math.max(500, ...rows.map((row) => row.length * 210));
    const height = circular
      ? radius * 2 + 140
      : Math.max(220, rowHeights.reduce((sum, value) => sum + value, 0) + (rows.length - 1) * 40 + 48);
    const positions = new Map();
    if (circular) {
      nodes.forEach((node, index) => {
        const angle = -Math.PI / 2 + 2 * Math.PI * index / nodes.length;
        positions.set(node.id, {
          x: width / 2 + radius * Math.cos(angle),
          y: height / 2 + radius * Math.sin(angle),
        });
      });
    } else {
      let top = 24;
      rows.forEach((row, rowIndex) => {
        const rowHeight = rowHeights[rowIndex];
        row.forEach((node, columnIndex) => positions.set(node.id, {
          x: width * (columnIndex + 0.5) / row.length,
          y: top + rowHeight / 2,
        }));
        top += rowHeight + 40;
      });
    }
    const edges = diagram.links.slice(0, 32).map((link) => {
      if (!link) return "";
      const source = positions.get(link.source), target = positions.get(link.target);
      if (!source || !target) return "";
      const centerX = (source.x + target.x) / 2;
      const centerY = (source.y + target.y) / 2;
      const label = [link.label, link.network].filter(Boolean).join(" · ");
      const addresses = [link.ip_source, link.ip_target].filter(Boolean).join(" ↔ ");
      return `<g><line x1="${source.x}" y1="${source.y}" x2="${target.x}" y2="${target.y}"/>` +
        (label ? `<text class="diagram-link-label" x="${centerX}" y="${centerY - (addresses ? 14 : 7)}" text-anchor="middle">${escape(label)}</text>` : "") +
        (addresses ? `<text class="diagram-link-address" x="${centerX}" y="${centerY + 8}" text-anchor="middle">${escape(addresses)}</text>` : "") +
        `</g>`;
    }).join("");
    const vertices = nodes.map((node) => {
      const position = positions.get(node.id);
      const addresses = Array.isArray(node.ip_addresses) ? node.ip_addresses.slice(0, 4).filter(Boolean) : [];
      const hasAddresses = addresses.length > 0;
      const height = boxHeight(node);
      const top = position.y - height / 2;
      const addressText = addresses.join(" · ");
      const addressLines = addresses.map((address, index) =>
        `<text class="diagram-address" x="${position.x}" y="${top + 51 + index * 13}" text-anchor="middle">${escape(address)}</text>`
      ).join("");
      return `<g><title>${escape(node.label)}${addressText ? ` — ${escape(addressText)}` : ""}</title>` +
        `<rect x="${position.x - 88}" y="${top}" width="176" height="${height}" rx="10"/>` +
        `<text x="${position.x}" y="${top + 20}" text-anchor="middle">${escape(String(node.label).slice(0, 24))}</text>` +
        `<text class="diagram-kind" x="${position.x}" y="${top + 37}" text-anchor="middle">${names[node.kind]}</text>` +
        (hasAddresses ? addressLines : "") +
        `</g>`;
    }).join("");
    return `<svg class="ai-network-diagram" style="min-width:${width}px" viewBox="0 0 ${width} ${height}" role="img" aria-label="${escape(diagram.title)}">${edges}${vertices}</svg>`;
  },
});
