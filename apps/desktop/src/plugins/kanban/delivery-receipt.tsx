import type { DeliveryReceipt } from './types'

export function acceptanceEvidence(receipt: DeliveryReceipt) {
  const note = receipt.acceptance
  return receipt.commit_sha && receipt.pr_verified && note && /no commit SHA\/PR exists/i.test(note)
    ? 'Historical acceptance note conflicts with verified publication above. Required acceptance evidence still needs independent review.'
    : note || 'No test evidence recorded'
}

export function DeliveryBadges({ receipt }: { receipt?: DeliveryReceipt | null }) {
  if (!receipt) return <span>Delivery: no verified receipt</span>
  return <span title={`Publication receipt checked ${receipt.checked_at || 'time unknown'}`}>Commit {receipt.commit_sha?.slice(0, 8) || 'unknown'} · {receipt.push_status === 'verified' ? 'push verified' : receipt.push_status === 'reported_unverified' ? 'push reported · branch unverified' : 'push unknown'} · {receipt.pr_verified ? `PR head verified${receipt.pr_draft ? ' · draft' : ''}` : 'PR unverified'}</span>
}

export function DeliveryDetails({ receipt }: { receipt?: DeliveryReceipt | null }) {
  if (!receipt) return <p className="text-xs text-(--ui-text-tertiary)">No structured delivery receipt. Commit, push, PR and acceptance remain unknown.</p>
  return <dl className="space-y-1 text-xs [overflow-wrap:anywhere]">
    <div><dt className="inline font-medium">Repository: </dt><dd className="inline">{receipt.repository}</dd></div>
    <div><dt className="inline font-medium">Commit: </dt><dd className="inline font-mono">{receipt.commit_sha || 'Unknown'}</dd></div>
    <div><dt className="inline font-medium">Branch: </dt><dd className="inline">{receipt.branch || 'Unknown'}</dd></div>
    <div><dt className="inline font-medium">Remote branch / push: </dt><dd className="inline">{receipt.push_status === 'verified' ? receipt.remote_branch_head : receipt.push_status === 'reported_unverified' ? 'Push reported; remote branch not independently verified' : 'Unknown'}</dd></div>
    <div><dt className="inline font-medium">PR: </dt><dd className="inline">{receipt.pr_url ? <a className="underline" href={receipt.pr_url} target="_blank" rel="noreferrer">{receipt.pr_url}</a> : 'Unknown'} · {receipt.pr_verified ? `head ${receipt.pr_head_sha} verified · ${receipt.pr_state}${receipt.pr_draft ? ' · draft' : ''}` : 'head unverified'}</dd></div>
    <div><dt className="inline font-medium">Issue: </dt><dd className="inline">{receipt.issue_url ? <a className="underline" href={receipt.issue_url} target="_blank" rel="noreferrer">{receipt.issue_url}</a> : 'Unknown'} · broader acceptance remains separate</dd></div>
    <div title={receipt.acceptance_basis}><dt className="inline font-medium">Acceptance evidence: </dt><dd className="inline">{acceptanceEvidence(receipt)}</dd></div>
    <div><dt className="inline font-medium">Checked: </dt><dd className="inline">PR {receipt.checked_at || 'Unknown'} · remote branch {receipt.remote_branch_checked_at || 'Unknown'}</dd></div>
    <div><dt className="inline font-medium">Provenance: </dt><dd className="inline">Attachment {receipt.source.attachment_id} · {receipt.source.filename} · SHA256 {receipt.source.sha256} · {receipt.source.basis}</dd></div>
  </dl>
}
