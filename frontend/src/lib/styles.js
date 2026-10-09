// Urban Loft style tokens. Palette: #9C9A9A grey, #A35E47 terracotta,
// #000000 black, #464646 charcoal. Page bg warm off-white, cards white.
export const STATUS_STYLE = {
  queued: 'bg-[#F1ECE6] text-[#464646]',
  running: 'bg-[#F5E6DE] text-[#8C4A33]',
  ready: 'bg-[#F5E6DE] text-[#8C4A33]',
  done: 'bg-[#E9EFE7] text-[#3F5A3C]',
  ok: 'bg-[#E9EFE7] text-[#3F5A3C]',
  error: 'bg-[#F7E3E0] text-[#96382B]',
  stopped: 'bg-[#EDEDED] text-[#464646]',
  paused: 'bg-[#EDEDED] text-[#464646]',
}

export const SEV_STYLE = {
  info: 'bg-[#EDEDED] text-[#464646]',
  low: 'bg-[#EDEEF2] text-[#4A5568]',
  medium: 'bg-[#F3EBD8] text-[#7A5B1F]',
  high: 'bg-[#F5E0D6] text-[#8C4A33]',
  critical: 'bg-[#F7D9D4] text-[#96382B]',
}

export const input =
  'w-full rounded-lg border border-[#E2DAD2] bg-white px-3 py-2 text-sm text-[#1C1A18] placeholder:text-[#9C9A9A] focus:outline-none focus:ring-2 focus:ring-[#A35E47]/40 focus:border-[#A35E47]'
export const btn =
  'rounded-lg bg-[#A35E47] px-4 py-2 text-sm font-semibold text-white shadow-[0_1px_2px_rgba(163,94,71,0.4)] hover:bg-[#8E513D] disabled:opacity-50'
export const btnDark =
  'rounded-lg bg-black px-4 py-2 text-sm font-semibold text-white hover:bg-[#1c1c1c] disabled:opacity-50'
export const btnSecondary =
  'rounded-lg border border-[#E2DAD2] bg-white px-4 py-2 text-sm font-medium text-[#464646] hover:bg-[#F7F3EF] disabled:opacity-50'
export const btnDanger = 'text-xs font-medium text-[#A33A2E] hover:text-[#7E2C23] hover:underline'
export const card =
  'rounded-xl border border-[#E9E1D9] bg-white p-5 shadow-[0_1px_3px_rgba(70,70,70,0.06)]'
export const miniBtn =
  'rounded-md border border-[#E2DAD2] bg-white px-2 py-0.5 text-xs font-medium text-[#464646] hover:bg-[#F7F3EF]'
export const labelCls =
  'text-[11px] font-semibold uppercase tracking-[0.08em] text-[#9C9A9A]'
