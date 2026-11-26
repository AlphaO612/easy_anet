export interface RegionOption {
  code: string
  flag: string
  name: string
  title: string
}

export const REGION_OPTIONS: RegionOption[] = [
  { code: 'RU', flag: '🇷🇺', name: 'Россия', title: '🇷🇺 RU — Россия' },
  { code: 'FR', flag: '🇫🇷', name: 'Франция', title: '🇫🇷 FR — Франция' },
  { code: 'DE', flag: '🇩🇪', name: 'Германия', title: '🇩🇪 DE — Германия' },
  { code: 'NL', flag: '🇳🇱', name: 'Нидерланды', title: '🇳🇱 NL — Нидерланды' },
  { code: 'FI', flag: '🇫🇮', name: 'Финляндия', title: '🇫🇮 FI — Финляндия' },
  { code: 'SE', flag: '🇸🇪', name: 'Швеция', title: '🇸🇪 SE — Швеция' },
  { code: 'EE', flag: '🇪🇪', name: 'Эстония', title: '🇪🇪 EE — Эстония' },
  { code: 'PL', flag: '🇵🇱', name: 'Польша', title: '🇵🇱 PL — Польша' },
  { code: 'GB', flag: '🇬🇧', name: 'Великобритания', title: '🇬🇧 GB — Великобритания' },
  { code: 'EU', flag: '🇪🇺', name: 'Европа', title: '🇪🇺 EU — Европа' },
  { code: 'US', flag: '🇺🇸', name: 'США', title: '🇺🇸 US — США' },
  { code: 'TR', flag: '🇹🇷', name: 'Турция', title: '🇹🇷 TR — Турция' },
  { code: 'KZ', flag: '🇰🇿', name: 'Казахстан', title: '🇰🇿 KZ — Казахстан' },
  { code: 'SG', flag: '🇸🇬', name: 'Сингапур', title: '🇸🇬 SG — Сингапур' },
  { code: 'JP', flag: '🇯🇵', name: 'Япония', title: '🇯🇵 JP — Япония' },
]

const REGION_BY_CODE = new Map(REGION_OPTIONS.map((r) => [r.code, r]))

export function formatRegionBadge(code?: string | null): string {
  const clean = (code || '').trim().toUpperCase()
  if (!clean) return ''
  const found = REGION_BY_CODE.get(clean)
  return found ? `${found.flag} ${found.code}` : `🌐 ${clean}`
}

export function formatRegionFull(code?: string | null): string {
  const clean = (code || '').trim().toUpperCase()
  if (!clean) return 'Без группы / страны'
  const found = REGION_BY_CODE.get(clean)
  return found ? `${found.flag} ${found.name} (${found.code})` : `🌐 ${clean}`
}

export function parseTags(tags?: string | null): string[] {
  if (!tags) return []
  return tags
    .split(',')
    .map((t) => t.trim())
    .filter(Boolean)
}
