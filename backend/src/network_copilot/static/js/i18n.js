/* VI / EN language switch.
 *
 * The templates and scripts are written in Vietnamese (with a few English
 * leftovers). Rather than rewriting every template, this script translates
 * what is on screen: each visible text node and the placeholder / title /
 * aria-label / alt attributes are looked up in the dictionary in
 * i18n-dict.js, whole string to whole string. A MutationObserver repeats the
 * lookup whenever Alpine renders new text, and confirm / alert / prompt are
 * wrapped so their messages follow the chosen language too.
 *
 * Only exact matches (or the patterns with {n} / {s} / {ip} holes) are
 * replaced, so user data such as hostnames, chat messages and command output
 * is left as it is. Containers that hold such data are skipped outright.
 */
(function () {
  "use strict";

  var STORAGE_KEY = "netai-lang";
  var LANGS = ["vi", "en"];
  var dict = window.NETAI_I18N || { pairs: [], patterns: [] };

  // ---- language choice (remembered per browser) ---------------------------
  function readLang() {
    try {
      var saved = window.localStorage.getItem(STORAGE_KEY);
      if (LANGS.indexOf(saved) !== -1) return saved;
    } catch (e) { /* storage blocked: fall back to Vietnamese */ }
    return "vi";
  }
  function saveLang(lang) {
    try { window.localStorage.setItem(STORAGE_KEY, lang); } catch (e) { /* ignore */ }
  }
  var lang = readLang();

  // ---- lookup tables --------------------------------------------------------
  function norm(text) { return text.replace(/\s+/g, " ").trim(); }

  // toLang[l][any known form] = the form in language l
  var toLang = { vi: Object.create(null), en: Object.create(null) };
  // A pair may carry extra aliases after [vi, en]: other spellings the code
  // shows (e.g. "Approve rồi Apply") that should read like the pair.
  dict.pairs.forEach(function (pair) {
    var vi = norm(pair[0]);
    var en = norm(pair[1]);
    pair.map(norm).forEach(function (form) {
      if (!form) return;
      // The first entry wins, so a later duplicate cannot change it.
      if (!(form in toLang.vi)) toLang.vi[form] = vi;
      if (!(form in toLang.en)) toLang.en[form] = en;
    });
  });

  var HOLE = { "{n}": "(\\d+)", "{ip}": "([0-9./]+)", "{s}": "(.+?)" };
  function compile(template) {
    var parts = norm(template).split(/(\{n\}|\{ip\}|\{s\})/);
    var order = [];
    var source = parts.map(function (part) {
      if (HOLE[part]) { order.push(part); return HOLE[part]; }
      return part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    }).join("");
    return { re: new RegExp("^" + source + "$"), template: norm(template), holes: order };
  }
  function fill(template, values) {
    var i = 0;
    return template.replace(/\{n\}|\{ip\}|\{s\}/g, function () { return values[i++]; });
  }
  var patterns = dict.patterns.map(function (pair) {
    return { vi: compile(pair[0]), en: compile(pair[1]), forms: pair.map(compile) };
  });

  function translate(text, target) {
    var core = norm(text);
    if (!core) return null;
    var hit = toLang[target][core];
    if (hit !== undefined) return hit === core ? null : hit;
    for (var i = 0; i < patterns.length; i += 1) {
      var p = patterns[i];
      var forms = p.forms;
      for (var j = 0; j < forms.length; j += 1) {
        var m = forms[j].re.exec(core);
        if (!m) continue;
        var values = m.slice(1).map(function (value) {
          // A hole may itself hold a translatable phrase ("Hoàn tác" in a sentence).
          var inner = toLang[target][value];
          return inner !== undefined ? inner : value;
        });
        var out = fill(p[target].template, values);
        return out === core ? null : out;
      }
    }
    return null;
  }

  // Translate one message that may span several lines (confirm / alert texts).
  function translateMessage(message, target) {
    if (typeof message !== "string") return message;
    var whole = translate(message, target);
    if (whole !== null) return whole;
    return message.split("\n").map(function (line) {
      var t = translate(line, target);
      if (t === null) return line;
      var lead = line.match(/^\s*/)[0];
      var trail = line.match(/\s*$/)[0];
      return lead + t + trail;
    }).join("\n");
  }

  // ---- DOM walking ------------------------------------------------------------
  // Containers whose text is user data, AI output or device output.
  var SKIP = "script,style,textarea,pre,code,svg,[contenteditable],[data-i18n-skip]," +
    ".bubble-content,.ai-diagram-scroll,.account-name,.lang-switch";
  var ATTRS = ["placeholder", "title", "aria-label", "alt"];

  function skipped(el) {
    return !el || (el.closest && el.closest(SKIP));
  }

  function translateTextNode(node) {
    var parent = node.parentElement;
    if (skipped(parent)) return;
    var value = node.nodeValue;
    var t = translate(value, lang);
    if (t === null) return;
    var lead = value.match(/^\s*/)[0];
    var trail = value.match(/\s*$/)[0];
    var next = lead + t + trail;
    if (next !== value) node.nodeValue = next;
  }

  function translateAttrs(el) {
    // A textarea's or input's typed value is user data, but its placeholder
    // and tooltip are interface text, so judge those by the container.
    var host = /^(TEXTAREA|INPUT)$/.test(el.tagName) ? el.parentElement : el;
    if (skipped(host)) return;
    for (var i = 0; i < ATTRS.length; i += 1) {
      var name = ATTRS[i];
      if (!el.hasAttribute(name)) continue;
      var t = translate(el.getAttribute(name), lang);
      if (t !== null) el.setAttribute(name, t);
    }
    // Buttons rendered as <input type="button|submit"> carry their label in value.
    if (el.tagName === "INPUT" && /^(button|submit|reset)$/i.test(el.type) && el.value) {
      var v = translate(el.value, lang);
      if (v !== null) el.value = v;
    }
  }

  function translateTree(root) {
    if (!root) return;
    if (root.nodeType === 3) { translateTextNode(root); return; }
    if (root.nodeType !== 1 && root.nodeType !== 9 && root.nodeType !== 11) return;
    if (root.nodeType === 1) {
      translateAttrs(root);
      if (skipped(root)) return;
    }
    var walker = document.createTreeWalker(
      root,
      NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT,
      {
        acceptNode: function (node) {
          if (node.nodeType === 1 && node.matches && node.matches(SKIP)) {
            if (node.tagName === "TEXTAREA") translateAttrs(node);
            return NodeFilter.FILTER_REJECT;
          }
          return NodeFilter.FILTER_ACCEPT;
        },
      }
    );
    var node = walker.nextNode();
    while (node) {
      if (node.nodeType === 3) translateTextNode(node);
      else translateAttrs(node);
      node = walker.nextNode();
    }
  }

  function translateTitle() {
    var t = translate(document.title, lang);
    if (t !== null) document.title = t;
  }

  function applyAll() {
    document.documentElement.setAttribute("lang", lang);
    translateTitle();
    translateTree(document.body);
    syncSwitches();
  }

  // ---- keep translating as Alpine renders ------------------------------------
  var observer = new MutationObserver(function (records) {
    for (var i = 0; i < records.length; i += 1) {
      var r = records[i];
      if (r.type === "characterData") translateTextNode(r.target);
      else if (r.type === "attributes") translateAttrs(r.target);
      else {
        for (var j = 0; j < r.addedNodes.length; j += 1) translateTree(r.addedNodes[j]);
      }
    }
  });

  function start() {
    applyAll();
    observer.observe(document.body, {
      subtree: true,
      childList: true,
      characterData: true,
      attributes: true,
      attributeFilter: ATTRS.concat(["value"]),
    });
    var titleEl = document.querySelector("title");
    if (titleEl) {
      new MutationObserver(translateTitle).observe(titleEl, { childList: true, characterData: true, subtree: true });
    }
  }

  // ---- dialogs ------------------------------------------------------------------
  var nativeConfirm = window.confirm.bind(window);
  var nativeAlert = window.alert.bind(window);
  var nativePrompt = window.prompt.bind(window);
  window.confirm = function (message) { return nativeConfirm(translateMessage(message, lang)); };
  window.alert = function (message) { return nativeAlert(translateMessage(message, lang)); };
  window.prompt = function (message, value) { return nativePrompt(translateMessage(message, lang), value); };

  // ---- the VI / EN buttons --------------------------------------------------------
  function syncSwitches() {
    var buttons = document.querySelectorAll(".lang-switch [data-lang]");
    for (var i = 0; i < buttons.length; i += 1) {
      var on = buttons[i].getAttribute("data-lang") === lang;
      buttons[i].classList.toggle("is-active", on);
      buttons[i].setAttribute("aria-pressed", on ? "true" : "false");
    }
  }

  function setLang(next) {
    if (LANGS.indexOf(next) === -1 || next === lang) return;
    lang = next;
    saveLang(lang);
    applyAll();
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest && event.target.closest(".lang-switch [data-lang]");
    if (!button) return;
    event.preventDefault();
    setLang(button.getAttribute("data-lang"));
  });

  window.netaiI18n = {
    get lang() { return lang; },
    setLang: setLang,
    t: function (text) { var t = translate(text, lang); return t === null ? text : t; },
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
