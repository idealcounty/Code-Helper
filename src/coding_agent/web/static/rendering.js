(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.CodeHelperRendering = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const LANGUAGE_ALIASES = {
    c: "cpp", cc: "cpp", cxx: "cpp", h: "cpp", hpp: "cpp",
    py: "python", js: "javascript", jsx: "javascript",
    ts: "typescript", tsx: "typescript", md: "markdown",
    sh: "shell", bash: "shell", yml: "yaml",
  };

  const KEYWORDS = {
    cpp: new Set("alignas alignof and and_eq asm atomic_cancel atomic_commit atomic_noexcept auto bitand bitor break case catch class co_await co_return co_yield compl concept const consteval constexpr constinit const_cast continue coroutine default delete do dynamic_cast else enum explicit export extern for friend goto if inline mutable namespace new noexcept not not_eq operator or or_eq private protected public register reinterpret_cast requires return sizeof static static_assert static_cast struct switch synchronized template this thread_local throw transaction_safe transaction_safe_dynamic try typedef typeid typename union using virtual volatile while xor xor_eq".split(" ")),
    java: new Set("abstract assert break case catch class const continue default do else enum extends final finally for goto if implements import instanceof interface native new package private protected public return static strictfp super switch synchronized this throw throws transient try volatile while record sealed permits non-sealed var yield".split(" ")),
    python: new Set("and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield match case".split(" ")),
    javascript: new Set("as async await break case catch class const continue debugger default delete do else export extends finally for from function get if import in instanceof let new of return set static super switch this throw try typeof var void while with yield".split(" ")),
    typescript: new Set("abstract any as asserts async await bigint boolean break case catch class const constructor continue declare default delete do else enum export extends false finally for from function get if implements import in infer instanceof interface is keyof let module namespace never new null number object of override private protected public readonly require return satisfies set static string super switch symbol this throw true try type typeof undefined unique unknown var void while with yield".split(" ")),
  };

  const TYPES = {
    cpp: new Set("bool char char8_t char16_t char32_t double float int long short signed unsigned void wchar_t size_t string vector map unordered_map set unordered_set list deque queue stack pair tuple optional variant auto".split(" ")),
    java: new Set("boolean byte char double float int long short void String Integer Long Double Float Boolean Character Object List Map Set Collection Optional Stream".split(" ")),
    python: new Set("bool bytes dict float frozenset int list object set str tuple type None".split(" ")),
  };

  const LITERALS = new Set("true false null nullptr None True False undefined NaN Infinity".split(" "));

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[char]);
  }

  function normalizeLanguage(language) {
    const normalized = String(language || "text").toLowerCase().replace(/[^a-z0-9_+-]/g, "");
    return LANGUAGE_ALIASES[normalized] || normalized || "text";
  }

  function token(className, value) {
    return `<span class="tok-${className}">${escapeHtml(value)}</span>`;
  }

  function highlightCode(source, language) {
    const code = String(source || "");
    const lang = normalizeLanguage(language);
    if (!["cpp", "java", "python", "javascript", "typescript", "json", "shell", "powershell", "css", "html", "yaml", "toml"].includes(lang)) {
      return escapeHtml(code);
    }

    let output = "";
    let index = 0;
    let lineStart = true;
    const wordPattern = /[A-Za-z_$]/;
    const wordBodyPattern = /[\w$]/;
    const pythonLike = lang === "python" || lang === "shell" || lang === "powershell" || lang === "yaml" || lang === "toml";
    const slashComments = ["cpp", "java", "javascript", "typescript", "css"].includes(lang);

    while (index < code.length) {
      const char = code[index];
      const next = code[index + 1] || "";

      if (char === "\n") {
        output += "\n";
        index += 1;
        lineStart = true;
        continue;
      }

      if (lineStart && lang === "cpp" && char === "#") {
        const end = code.indexOf("\n", index);
        const stop = end < 0 ? code.length : end;
        output += token("meta", code.slice(index, stop));
        index = stop;
        lineStart = false;
        continue;
      }

      if ((pythonLike && char === "#") || (slashComments && char === "/" && next === "/")) {
        const end = code.indexOf("\n", index);
        const stop = end < 0 ? code.length : end;
        output += token("comment", code.slice(index, stop));
        index = stop;
        lineStart = false;
        continue;
      }

      if (slashComments && char === "/" && next === "*") {
        const end = code.indexOf("*/", index + 2);
        const stop = end < 0 ? code.length : end + 2;
        output += token("comment", code.slice(index, stop));
        lineStart = code.slice(index, stop).endsWith("\n");
        index = stop;
        continue;
      }

      if (char === '"' || char === "'" || ((lang === "javascript" || lang === "typescript") && char === "`")) {
        const quote = char;
        const triple = lang === "python" && code.slice(index, index + 3) === quote.repeat(3);
        const delimiter = triple ? quote.repeat(3) : quote;
        let end = index + delimiter.length;
        while (end < code.length) {
          if (code.slice(end, end + delimiter.length) === delimiter && code[end - 1] !== "\\") {
            end += delimiter.length;
            break;
          }
          if (!triple && quote !== "`" && code[end] === "\n") break;
          end += code[end] === "\\" ? 2 : 1;
        }
        output += token("string", code.slice(index, Math.min(end, code.length)));
        index = Math.min(end, code.length);
        lineStart = false;
        continue;
      }

      if (/\d/.test(char) && (index === 0 || !wordBodyPattern.test(code[index - 1]))) {
        const match = code.slice(index).match(/^(?:0[xX][\da-fA-F]+|0[bB][01]+|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)[uUlLfF]*/);
        output += token("number", match[0]);
        index += match[0].length;
        lineStart = false;
        continue;
      }

      if (wordPattern.test(char)) {
        let end = index + 1;
        while (end < code.length && wordBodyPattern.test(code[end])) end += 1;
        const word = code.slice(index, end);
        const keywords = KEYWORDS[lang] || new Set();
        const types = TYPES[lang] || new Set();
        if (keywords.has(word)) output += token("keyword", word);
        else if (types.has(word)) output += token("type", word);
        else if (LITERALS.has(word)) output += token("literal", word);
        else if (/^[A-Z][A-Za-z0-9_$]*$/.test(word)) output += token("class", word);
        else output += escapeHtml(word);
        index = end;
        lineStart = false;
        continue;
      }

      if (/[+\-*/%=!<>&|^~?:]/.test(char)) output += token("operator", char);
      else output += escapeHtml(char);
      lineStart = lineStart && /\s/.test(char);
      index += 1;
    }
    return output;
  }

  function safeUrl(value) {
    const url = String(value || "").trim();
    return /^(https?:|mailto:|#|\/)/i.test(url) ? url : "";
  }

  function renderInline(value) {
    const protectedParts = [];
    const protect = (html) => {
      const key = `\u0000MD${protectedParts.length}\u0000`;
      protectedParts.push(html);
      return key;
    };
    let source = String(value || "");
    source = source.replace(/`([^`\n]+)`/g, (_, code) => protect(`<code>${escapeHtml(code)}</code>`));
    source = source.replace(/\[([^\]\n]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g, (_, label, url) => {
      const href = safeUrl(url);
      if (!href) return label;
      return protect(`<a href="${escapeHtml(href)}" target="_blank" rel="noreferrer">${escapeHtml(label)}</a>`);
    });
    source = escapeHtml(source)
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      .replace(/__([^_\n]+)__/g, "<strong>$1</strong>")
      .replace(/~~([^~\n]+)~~/g, "<del>$1</del>")
      .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
      .replace(/(^|[^_])_([^_\n]+)_/g, "$1<em>$2</em>");
    protectedParts.forEach((html, index) => {
      source = source.replace(`\u0000MD${index}\u0000`, html);
    });
    return source;
  }

  function splitTableRow(line) {
    let value = String(line || "").trim();
    if (value.startsWith("|")) value = value.slice(1);
    if (value.endsWith("|")) value = value.slice(0, -1);
    const cells = [];
    let current = "";
    let inCode = false;
    for (let index = 0; index < value.length; index += 1) {
      const char = value[index];
      if (char === "`") inCode = !inCode;
      if (char === "|" && !inCode && value[index - 1] !== "\\") {
        cells.push(current.trim());
        current = "";
      } else {
        current += char;
      }
    }
    cells.push(current.trim());
    return cells;
  }

  function isTableDivider(line) {
    const cells = splitTableRow(line);
    return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell.trim()));
  }

  function isBlockStart(lines, index) {
    const line = lines[index] || "";
    const next = lines[index + 1] || "";
    return /^\s*$/.test(line) || /^ {0,3}(#{1,6})\s+/.test(line) || /^ {0,3}```/.test(line)
      || /^ {0,3}>/.test(line) || /^\s*(?:[-+*]|\d+[.)])\s+/.test(line)
      || /^ {0,3}(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)
      || (line.includes("|") && isTableDivider(next));
  }

  function renderMarkdown(markdown) {
    const lines = String(markdown || "").replace(/\r\n?/g, "\n").split("\n");
    const blocks = [];
    let index = 0;

    while (index < lines.length) {
      const line = lines[index];
      if (!line.trim()) {
        index += 1;
        continue;
      }

      // Models occasionally leave an empty heading marker at the end of a reply.
      // Treat it as an incomplete Markdown token instead of visible prose.
      if (/^ {0,3}#{1,6}\s*$/.test(line)) {
        index += 1;
        continue;
      }

      const fence = line.match(/^ {0,3}```\s*([\w+-]*)\s*$/);
      if (fence) {
        const language = normalizeLanguage(fence[1] || "text");
        const code = [];
        index += 1;
        while (index < lines.length && !/^ {0,3}```\s*$/.test(lines[index])) {
          code.push(lines[index]);
          index += 1;
        }
        if (index < lines.length) index += 1;
        blocks.push(`<div class="md-code-block"><div class="md-code-label">${escapeHtml(language)}</div><pre><code class="language-${escapeHtml(language)}">${highlightCode(code.join("\n"), language)}</code></pre></div>`);
        continue;
      }

      const heading = line.match(/^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$/);
      if (heading) {
        const level = heading[1].length;
        blocks.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
        index += 1;
        continue;
      }

      if (/^ {0,3}(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
        blocks.push("<hr>");
        index += 1;
        continue;
      }

      if (line.includes("|") && index + 1 < lines.length && isTableDivider(lines[index + 1])) {
        const headers = splitTableRow(line);
        const alignments = splitTableRow(lines[index + 1]).map((cell) => {
          const trimmed = cell.trim();
          return trimmed.startsWith(":") && trimmed.endsWith(":") ? "center" : trimmed.endsWith(":") ? "right" : "left";
        });
        index += 2;
        const rows = [];
        while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {
          rows.push(splitTableRow(lines[index]));
          index += 1;
        }
        const headerHtml = headers.map((cell, cellIndex) => `<th style="text-align:${alignments[cellIndex] || "left"}">${renderInline(cell)}</th>`).join("");
        const bodyHtml = rows.map((row) => `<tr>${headers.map((_, cellIndex) => `<td style="text-align:${alignments[cellIndex] || "left"}">${renderInline(row[cellIndex] || "")}</td>`).join("")}</tr>`).join("");
        blocks.push(`<div class="md-table-wrap"><table><thead><tr>${headerHtml}</tr></thead><tbody>${bodyHtml}</tbody></table></div>`);
        continue;
      }

      if (/^ {0,3}>/.test(line)) {
        const quote = [];
        while (index < lines.length && /^ {0,3}>/.test(lines[index])) {
          quote.push(lines[index].replace(/^ {0,3}> ?/, ""));
          index += 1;
        }
        blocks.push(`<blockquote>${renderMarkdown(quote.join("\n"))}</blockquote>`);
        continue;
      }

      const listMatch = line.match(/^\s*([-+*]|\d+[.)])\s+(.+)$/);
      if (listMatch) {
        const ordered = /^\d/.test(listMatch[1]);
        const tag = ordered ? "ol" : "ul";
        const items = [];
        while (index < lines.length) {
          const item = lines[index].match(/^\s*([-+*]|\d+[.)])\s+(.+)$/);
          if (!item || /^\d/.test(item[1]) !== ordered) break;
          const task = item[2].match(/^\[([ xX])\]\s*(.*)$/);
          if (task) {
            const checked = task[1].toLowerCase() === "x";
            items.push(`<li class="task-item"><input type="checkbox" disabled${checked ? " checked" : ""}><span>${renderInline(task[2])}</span></li>`);
          } else {
            items.push(`<li>${renderInline(item[2])}</li>`);
          }
          index += 1;
        }
        blocks.push(`<${tag}>${items.join("")}</${tag}>`);
        continue;
      }

      const paragraph = [line.trim()];
      index += 1;
      while (index < lines.length && !isBlockStart(lines, index)) {
        paragraph.push(lines[index].trim());
        index += 1;
      }
      blocks.push(`<p>${paragraph.map(renderInline).join("<br>")}</p>`);
    }
    return blocks.join("\n");
  }

  return { escapeHtml, highlightCode, normalizeLanguage, renderMarkdown };
});
