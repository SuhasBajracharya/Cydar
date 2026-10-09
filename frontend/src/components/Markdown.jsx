// Tiny markdown renderer for agent report bodies (no dependency).
// Handles ``` fences (dark code box + copy), #/##/### headings, **bold**,
// - bullets, 1. numbered items, and plain paragraphs. Unknown syntax falls
// through as text — never crashes on model output.
import { CopyBtn } from './ui.jsx'

function inline(text, keyPrefix) {
  // Split **bold** runs into <strong>, keep the rest as text.
  const parts = String(text).split(/(\*\*[^*]+\*\*)/g)
  return parts.map((p, i) => {
    const m = /^\*\*([^*]+)\*\*$/.exec(p)
    if (m) return <strong key={`${keyPrefix}-${i}`} className="font-semibold text-[#1C1A18]">{m[1]}</strong>
    return <span key={`${keyPrefix}-${i}`}>{p}</span>
  })
}

export default function Markdown({ text }) {
  const lines = String(text || '').split('\n')
  const blocks = []
  let i = 0
  let key = 0
  const push = (node) => { blocks.push(<div key={key++}>{node}</div>) }

  while (i < lines.length) {
    const line = lines[i]
    // Fenced code (evidence / curl PoCs).
    if (/^```/.test(line.trim())) {
      const buf = []
      i += 1
      while (i < lines.length && !/^```/.test(lines[i].trim())) { buf.push(lines[i]); i += 1 }
      i += 1 // consume closing fence
      const code = buf.join('\n')
      push(
        <div className="my-2 overflow-hidden rounded-lg border border-[#2A2A2A] bg-[#1C1A18]">
          <div className="flex items-center justify-between px-3 py-1.5">
            <span className="font-mono text-[10px] tracking-wider text-[#9C9A9A]">EVIDENCE</span>
            <CopyBtn text={code} label="⧉ Copy" />
          </div>
          <pre className="nice-scroll overflow-x-auto whitespace-pre px-3 pb-3 font-mono text-[12px] leading-relaxed text-[#EDE6DF]">{code}</pre>
        </div>,
      )
      continue
    }
    const h1 = /^#\s+(.*)/.exec(line)
    if (h1) { push(<h4 className="mb-1 mt-4 text-[15px] font-bold text-[#1C1A18] first:mt-0">{inline(h1[1], `h${key}`)}</h4>); i += 1; continue }
    const h2 = /^##\s+(.*)/.exec(line)
    if (h2) { push(<h5 className="mb-1 mt-3 text-[13px] font-bold text-[#1C1A18] first:mt-0">{inline(h2[1], `h${key}`)}</h5>); i += 1; continue }
    const h3 = /^###\s+(.*)/.exec(line)
    if (h3) { push(<h6 className="mb-1 mt-3 text-[12px] font-bold uppercase tracking-wide text-[#A35E47] first:mt-0">{inline(h3[1], `h${key}`)}</h6>); i += 1; continue }
    const bullet = /^\s*[-*]\s+(.*)/.exec(line)
    if (bullet) { push(<p className="ml-4 text-[13px] leading-relaxed text-[#464646]">• {inline(bullet[1], `b${key}`)}</p>); i += 1; continue }
    const numbered = /^\s*\d+\.\s+(.*)/.exec(line)
    if (numbered) { push(<p className="ml-4 text-[13px] leading-relaxed text-[#464646]">{inline(line.trim(), `n${key}`)}</p>); i += 1; continue }
    if (/^\s*$/.test(line)) { i += 1; continue }
    push(<p className="text-[13px] leading-relaxed text-[#464646]">{inline(line, `p${key}`)}</p>)
    i += 1
  }

  return <div className="space-y-1.5">{blocks}</div>
}
