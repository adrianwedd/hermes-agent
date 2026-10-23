/**
 * The operator "make this start" flow, shared by every surface that offers a
 * Move-to-Ready (board drag + context menu, the drawer's status menu, the
 * drawer's diagnostic "Unblock", and the bulk bar).
 *
 * A plain Move-to-Ready on a held card is refused by the backend; it cannot
 * acquire release authority from the gesture. An explicit, reason-bearing
 * operator release can return a canonical outcome, which this module reports
 * without claiming Ready unless the kernel's verdict says so.
 */

import { host } from '@hermes/plugin-sdk'

import type { ReleaseOutcome } from './types'
import { shortId } from './ui'
import type { useKanban } from './ui'

/** The release outcome of a Move-to-Ready, if the gesture ran (or was upgraded to)
 *  the audited operator dispatch release; null for a plain move. */
export function releaseOutcome(response: unknown): null | ReleaseOutcome {
  return (response as { release?: ReleaseOutcome } | undefined)?.release ?? null
}

/** Report one id's explicit release response; silent for a plain move. */
export function notifyReadyRelease(
  id: string,
  response: unknown,
  k: ReturnType<typeof useKanban>
): null | ReleaseOutcome {
  const release = releaseOutcome(response)

  if (!release) {
    return null
  }

  if (release.ready) {
    host.notify({ kind: 'success', message: k.readyOverrideReached(shortId(id)) })
  } else {
    host.notify({
      kind: 'warning',
      message: k.readyOverrideHeld(shortId(id), release.summary ?? '')
    })
  }

  return release
}

/** Report explicit bulk releases; plain moves and refused held cards remain with
 *  the caller's ordinary success and partial-failure handling. */
export function notifyBulkReadyRelease(
  results: Array<Record<string, unknown>> | undefined,
  k: ReturnType<typeof useKanban>
): ReleaseOutcome[] {
  const releases = (results ?? [])
    .map(entry => (entry?.release ?? null) as null | ReleaseOutcome)
    .filter((release): release is ReleaseOutcome => release !== null)

  if (releases.length === 0) {
    return []
  }

  const reached = releases.filter(release => release.ready).length

  if (reached === releases.length) {
    host.notify({ kind: 'success', message: k.readyOverrideReached(`${reached} cards`) })
  } else {
    host.notify({
      kind: 'warning',
      message: k.readyOverrideHeld(`${reached}/${releases.length}`, k.refused)
    })
  }

  return releases
}
