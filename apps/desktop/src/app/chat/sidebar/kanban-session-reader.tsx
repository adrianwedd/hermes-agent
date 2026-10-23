import { $workerCardRequest } from '@/plugins/kanban/kanban-card-navigation'
import { useEffect, useState } from 'react'
import { Dialog, DialogContent, DialogTitle, MessageTextContent, host } from '@hermes/plugin-sdk'
import { getSessionMessages, type SessionInfo, type SessionMessagesResponse } from '@/hermes'
import { hermesApi } from '@/api/client'

// Stored tool results may contain credentials; never render those values.
export function redactStoredText(text: string): string {
  return text.replace(/\bBearer\s+[A-Za-z0-9._~+\/-]+/gi, 'Bearer [redacted]')
    .replace(/((?:["\\]*)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|authorization)(?:["\\]*)\s*[:=]\s*)(?:\\?["'][^"'\n]*\\?["']|[^\s,;}\n]+)/gi, '$1[redacted]')
}

type CardLink = { board: string; card_id: string; card_title: string; run_id: number }
export function KanbanSessionReader({ session, onClose }: { session: SessionInfo; onClose: () => void }) {
  const [page, setPage] = useState<SessionMessagesResponse | null>(null)
  const [error, setError] = useState('')
  const [link, setLink] = useState<CardLink | null>(null)
  const [offset, setOffset] = useState(0)
  useEffect(() => {
    let active = true
    setPage(null); setError('')
    getSessionMessages(session.id, session.profile, { limit: 120, offset, order: 'latest', includeCompacted: true }, { passive: true })
      .then(value => { if (active) setPage(value) }).catch(() => { if (active) setError('Stored worker conversation could not be read.') })
    return () => { active = false }
  }, [session.id, session.profile, offset])
  useEffect(() => {
    let active = true
    hermesApi<{ link: CardLink | null }>({ path: `/api/plugins/kanban/worker-session-link?session_id=${encodeURIComponent(session.id)}`, profile: session.profile, passive: true })
      .then(value => { if (active) setLink(value.link) }).catch(() => { if (active) setLink(null) })
    return () => { active = false }
  }, [session.id, session.profile])
  return <Dialog open onOpenChange={open => { if (!open) onClose() }}>
    <DialogContent className="flex max-h-[85vh] w-[min(90vw,64rem)] min-w-0 flex-col overflow-hidden">
      <DialogTitle>Kanban · {session.title || session.id}</DialogTitle>
      <p className="text-xs text-(--ui-text-tertiary)">Stored conversation · read only · {session.profile || 'default'} · {session.id}</p>
      <p className="text-xs">{link ? <button className="text-left underline" onClick={() => { $workerCardRequest.set({ board: link.board, card_id: link.card_id }); onClose(); host.navigate('/kanban') }}>{link.card_id} · {link.card_title} · run {link.run_id} · board {link.board}</button> : 'Card link not recorded or unavailable'}</p>
      <div className="min-h-0 min-w-0 flex-1 overflow-y-auto [overflow-wrap:anywhere]">
        {error ? <p role="alert">{error}</p> : !page ? <p>Loading stored messages…</p> : page.messages.map((message, index) => <section className="mb-4 border-b border-(--ui-stroke-tertiary) pb-3" key={index}>
          <h3 className="mb-1 text-xs font-medium">{message.role}</h3>
          <MessageTextContent media={false} text={typeof message.content === 'string' ? redactStoredText(message.content) : '[Structured message; inspect native run log for full details]'} />
        </section>)}
      </div>
      <div className="flex flex-wrap gap-3 text-xs">
        <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 120))}>Newer messages</button>
        <button disabled={!page || page.messages.length < 120} onClick={() => setOffset(offset + 120)}>Earlier messages</button>
      </div>
    </DialogContent>
  </Dialog>
}
