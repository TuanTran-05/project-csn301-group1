// Minimal, safe formatting for AI answers in chat bubbles.
// The text is HTML-escaped first; only headings (#..######), **bold**,
// `code` and "- " / "* " bullets are turned into markup. Line breaks stay
// as they are (the bubble uses white-space: pre-wrap).
window.NCMarkdown = function (text) {
  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const inline = (line) => line
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  return escape(text).split("\n").map((line) => {
    const heading = line.match(/^\s*#{1,6}\s+(.*)$/);
    if (heading) return `<strong class="md-heading">${inline(heading[1])}</strong>`;
    const bullet = line.match(/^(\s*)[-*]\s+(.*)$/);
    if (bullet) return `${bullet[1]}• ${inline(bullet[2])}`;
    return inline(line);
  }).join("\n");
};
