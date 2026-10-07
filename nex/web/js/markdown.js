/* Safe markdown renderer.
 *
 * No dependencies, no raw HTML ever — the output is built entirely from
 * DOM nodes, so model output cannot inject markup. Supports the subset
 * that actually appears in chat: headings, lists (nested one level),
 * emphasis, inline code, fenced code with language + copy button,
 * blockquotes, links (http/https only), hr, tables.
 */

const HEADING = /^(#{1,4})\s+(.*)$/;
const HR = /^ {0,3}(?:-{3,}|\*{3,}|_{3,})\s*$/;
const UL = /^(\s*)[-*+]\s+(.*)$/;
const OL = /^(\s*)\d+[.)]\s+(.*)$/;
const QUOTE = /^>\s?(.*)$/;
const TABLE_ROW = /^\s*\|(.+)\|\s*$/;

/* inline: `code`, **bold**, *italic*, [text](url) — built as DOM so no
   HTML injection is possible. */
function renderInline(text, doc) {
  const frag = doc.createDocumentFragment();
  let i = 0;
  const n = text.length;

  function pushText(str) {
    if (str) frag.appendChild(doc.createTextNode(str));
  }

  while (i < n) {
    const ch = text[i];

    // `code`
    if (ch === '`') {
      const end = text.indexOf('`', i + 1);
      if (end > i) {
        pushText(text.slice(i, i + 1) === '`' ? '' : '');
        const code = doc.createElement('code');
        code.textContent = text.slice(i + 1, end);
        frag.appendChild(code);
        i = end + 1;
        continue;
      }
    }
    // **bold**
    if (ch === '*' && text[i + 1] === '*') {
      const end = text.indexOf('**', i + 2);
      if (end > i + 1) {
        const b = doc.createElement('strong');
        b.textContent = text.slice(i + 2, end);
        frag.appendChild(b);
        i = end + 2;
        continue;
      }
    }
    // *italic* (not part of **)
    if (ch === '*' && text[i + 1] !== '*') {
      const end = text.indexOf('*', i + 1);
      if (end > i && text[end + 1] !== '*') {
        const em = doc.createElement('em');
        em.textContent = text.slice(i + 1, end);
        frag.appendChild(em);
        i = end + 1;
        continue;
      }
    }
    // [text](url)
    if (ch === '[') {
      const closeText = text.indexOf('](', i + 1);
      if (closeText > i) {
        const closeUrl = text.indexOf(')', closeText + 2);
        if (closeUrl > closeText) {
          const label = text.slice(i + 1, closeText);
          const url = text.slice(closeText + 2, closeUrl);
          if (/^https?:\/\//i.test(url)) {
            const a = doc.createElement('a');
            a.href = url;
            a.target = '_blank';
            a.rel = 'noopener noreferrer';
            a.textContent = label;
            frag.appendChild(a);
            i = closeUrl + 1;
            continue;
          }
        }
      }
    }
    // consume a run of ordinary chars up to the next special char
    let j = i + 1;
    while (j < n && '`*['.indexOf(text[j]) < 0) j++;
    pushText(text.slice(i, j));
    i = j;
  }
  return frag;
}

export function renderMarkdown(source, doc = document) {
  const root = doc.createElement('div');
  root.className = 'md';

  const lines = String(source || '').replace(/\r\n?/g, '\n').split('\n');
  let i = 0;

  function addBlock(el) {
    root.appendChild(el);
  }

  while (i < lines.length) {
    const line = lines[i];

    // fenced code — scan forward for the matching closing fence so content
    // AFTER the block (very common: "here's the code: ```...``` and here's
    // how it works") is not swallowed into the code body.
    if (line.startsWith('```')) {
      const lang = line.slice(3).trim();
      let close = -1;
      for (let j = i + 1; j < lines.length; j++) {
        if (/^```\s*$/.test(lines[j])) { close = j; break; }
      }
      if (close >= 0) {
        addBlock(buildCode(lang, lines.slice(i + 1, close).join('\n'), doc));
        i = close + 1;
      } else {
        // genuinely unterminated (e.g. the model was cut off mid-block):
        // treat the rest of the message as code, same as before.
        addBlock(buildCode(lang, lines.slice(i + 1).join('\n'), doc));
        i = lines.length;
      }
      continue;
    }

    if (!line.trim()) {
      i++;
      continue;
    }

    // heading
    const h = line.match(HEADING);
    if (h) {
      const el = doc.createElement('h' + Math.min(h[1].length, 4));
      el.appendChild(renderInline(h[2], doc));
      addBlock(el);
      i++;
      continue;
    }

    // hr
    if (HR.test(line)) {
      addBlock(doc.createElement('hr'));
      i++;
      continue;
    }

    // table (header + separator + rows)
    if (TABLE_ROW.test(line) && i + 1 < lines.length
        // '-' is placed at the END of the class so it is literal, not a
        // range operator: "[\s:-|]" (the original form) was parsed as
        // \s plus the RANGE ':' through '|' (0x3A-0x7C) — which excludes
        // a literal '-' entirely, so a standard "|---|---|" separator row
        // never matched and tables never rendered as tables.
        && /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[i + 1])
        && lines[i + 1].includes('-')) {
      const headCells = splitRow(line);
      i += 2;
      const rows = [];
      while (i < lines.length && TABLE_ROW.test(lines[i])) {
        rows.push(splitRow(lines[i]));
        i++;
      }
      const table = doc.createElement('table');
      const thead = doc.createElement('thead');
      const trh = doc.createElement('tr');
      for (const c of headCells) {
        const th = doc.createElement('th');
        th.appendChild(renderInline(c.trim(), doc));
        trh.appendChild(th);
      }
      thead.appendChild(trh);
      table.appendChild(thead);
      const tbody = doc.createElement('tbody');
      for (const r of rows) {
        const tr = doc.createElement('tr');
        for (let k = 0; k < headCells.length; k++) {
          const td = doc.createElement('td');
          td.appendChild(renderInline((r[k] || '').trim(), doc));
          tr.appendChild(td);
        }
        tbody.appendChild(tr);
      }
      table.appendChild(tbody);
      addBlock(table);
      continue;
    }

    // blockquote
    if (QUOTE.test(line)) {
      const buf = [];
      while (i < lines.length && QUOTE.test(lines[i])) {
        buf.push(lines[i].replace(QUOTE, '$1'));
        i++;
      }
      const q = doc.createElement('blockquote');
      q.appendChild(renderInline(buf.join(' '), doc));
      addBlock(q);
      continue;
    }

    // lists (one nesting level)
    const ul = line.match(UL);
    const ol = line.match(OL);
    if (ul || ol) {
      const isUl = !!ul;
      const list = doc.createElement(isUl ? 'ul' : 'ol');
      while (i < lines.length) {
        const m = lines[i].match(isUl ? UL : OL);
        if (m) {
          const li = doc.createElement('li');
          li.appendChild(renderInline(m[2], doc));
          // nested list (one level)
          const nested = [];
          let j = i + 1;
          while (j < lines.length) {
            const nm = lines[j].match(isUl ? UL : OL);
            if (nm && nm[1].length > m[1].length) {
              nested.push(nm[2]);
              j++;
            } else break;
          }
          if (nested.length) {
            const sub = doc.createElement(isUl ? 'ul' : 'ol');
            for (const item of nested) {
              const sli = doc.createElement('li');
              sli.appendChild(renderInline(item, doc));
              sub.appendChild(sli);
            }
            li.appendChild(sub);
            i = j - 1;
          }
          list.appendChild(li);
          i++;
        } else if (lines[i].match(isUl ? OL : UL)) {
          break;   // list type switch ends the list
        } else if (lines[i].trim() && list.children.length
                   && !lines[i].startsWith('  ')) {
          break;
        } else if (!lines[i].trim()) {
          // blank line: list continues only if the next line is an item
          const nm = (lines[i + 1] || '').match(isUl ? UL : OL);
          if (!nm) break;
          i++;
        } else {
          i++;
        }
      }
      addBlock(list);
      continue;
    }

    // paragraph — merge consecutive plain lines
    const buf = [line];
    i++;
    while (i < lines.length && lines[i].trim()
           && !lines[i].match(HEADING) && !lines[i].match(UL)
           && !lines[i].match(OL) && !lines[i].match(QUOTE)
           && !lines[i].startsWith('```') && !HR.test(lines[i])
           && !TABLE_ROW.test(lines[i])) {
      buf.push(lines[i]);
      i++;
    }
    const p = doc.createElement('p');
    p.appendChild(renderInline(buf.join('\n'), doc));
    addBlock(p);
  }

  return root;
}

function splitRow(line) {
  return line.trim().replace(/^\||\|$/g, '').split('|');
}

function buildCode(lang, body, doc) {
  const pre = doc.createElement('pre');
  if (lang) {
    const l = doc.createElement('span');
    l.className = 'code-lang';
    l.textContent = lang;
    pre.appendChild(l);
  }
  const code = doc.createElement('code');
  code.textContent = body.replace(/\n$/, '');
  pre.appendChild(code);
  if (body.trim()) {
    const copy = doc.createElement('button');
    copy.className = 'code-copy';
    copy.textContent = 'copy';
    copy.addEventListener('click', () => {
      navigator.clipboard && navigator.clipboard.writeText(code.textContent)
        .then(() => {
          copy.textContent = 'copied';
          setTimeout(() => (copy.textContent = 'copy'), 1400);
        })
        .catch(() => {});
    });
    pre.appendChild(copy);
  }
  return pre;
}

/* Plain-text-ish streaming render: escaped text with paragraph breaks.
   Cheap enough to run on every flush; replaced by full markdown at
   chat.done. */
export function renderStreaming(text, doc = document) {
  const root = doc.createElement('div');
  root.className = 'md';
  const paras = String(text || '').replace(/\r\n?/g, '\n').split(/\n{2,}/);
  for (const p of paras) {
    if (!p.trim()) continue;
    if (p.trimStart().startsWith('```')) {
      // rough streaming code block
      const parts = p.trimStart().split('\n');
      const lang = (parts[0].slice(3) || '').trim();
      const body = parts.slice(1).join('\n').replace(/```$/m, '');
      const pre = doc.createElement('pre');
      if (lang) {
        const l = doc.createElement('span');
        l.className = 'code-lang';
        l.textContent = lang;
        pre.appendChild(l);
      }
      const code = doc.createElement('code');
      code.textContent = body;
      pre.appendChild(code);
      root.appendChild(pre);
    } else {
      const el = doc.createElement('p');
      el.appendChild(renderInline(p.replace(/\n/g, ' '), doc));
      root.appendChild(el);
    }
  }
  return root;
}
