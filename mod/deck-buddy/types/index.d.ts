/** A permission prompt the buddy is holding while you answer it on the desktop. */
export type BuddyPending = { id: string; tool: string; target: string }

declare module 'claude-code' {
  interface PluginState {
    'deck-buddy': { pending: BuddyPending | null }
  }
}
