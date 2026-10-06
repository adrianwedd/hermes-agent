import { atom } from 'nanostores'
export const $workerCardRequest = atom<null | { board: string; card_id: string }>(null)
