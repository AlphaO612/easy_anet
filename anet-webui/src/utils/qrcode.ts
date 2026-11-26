// Pure TypeScript ISO/IEC 18004 QR Code SVG Generator (Offline, zero external requests)
// Supports UTF-8 Byte mode, Versions 1..20, Error Correction Levels L and M.

export type QrEccLevel = 'L' | 'M'

// [ecCodewordsPerBlock, totalBlocks] for versions 1..20 (index 0 unused)
const ECC_SPECS: Record<QrEccLevel, ReadonlyArray<readonly [number, number]>> = {
  L: [
    [0, 0],
    [7, 1], [10, 1], [15, 1], [20, 1], [26, 1],
    [18, 2], [20, 2], [24, 2], [30, 2], [18, 4],
    [20, 4], [24, 4], [26, 4], [30, 4], [22, 6],
    [24, 6], [28, 6], [30, 6], [28, 7], [28, 8],
  ],
  M: [
    [0, 0],
    [10, 1], [16, 1], [26, 1], [18, 2], [24, 2],
    [16, 4], [18, 4], [22, 4], [22, 5], [26, 5],
    [30, 5], [22, 8], [22, 9], [24, 9], [24, 10],
    [28, 10], [28, 11], [26, 13], [26, 14], [26, 16],
  ],
}

const ALIGNMENT_CENTERS: ReadonlyArray<readonly number[]> = [
  [],
  [],
  [6, 18],
  [6, 22],
  [6, 26],
  [6, 30],
  [6, 34],
  [6, 22, 38],
  [6, 24, 42],
  [6, 26, 46],
  [6, 28, 50],
  [6, 30, 54],
  [6, 32, 58],
  [6, 34, 62],
  [6, 26, 46, 66],
  [6, 26, 48, 70],
  [6, 26, 50, 74],
  [6, 30, 54, 78],
  [6, 30, 56, 82],
  [6, 30, 58, 86],
  [6, 34, 62, 90],
]

// Format level bits per ISO/IEC 18004: L=01, M=00
const ECC_FORMAT_BITS: Record<QrEccLevel, number> = {
  L: 1,
  M: 0,
}

// GF(256) with primitive polynomial x^8 + x^4 + x^3 + x^2 + 1 (0x11d)
const GF_EXP = new Uint8Array(512)
const GF_LOG = new Uint8Array(256)
{
  let x = 1
  for (let i = 0; i < 255; i++) {
    GF_EXP[i] = x
    GF_LOG[x] = i
    x <<= 1
    if (x & 0x100) x ^= 0x11d
  }
  for (let i = 255; i < 512; i++) {
    GF_EXP[i] = GF_EXP[i - 255]!
  }
}

function gfMul(a: number, b: number): number {
  if (a === 0 || b === 0) return 0
  return GF_EXP[GF_LOG[a]! + GF_LOG[b]!]!
}

function rsGeneratorPoly(degree: number): Uint8Array {
  let poly = new Uint8Array([1])
  for (let i = 0; i < degree; i++) {
    const next = new Uint8Array(poly.length + 1)
    const factor = GF_EXP[i]!
    for (let j = 0; j < poly.length; j++) {
      next[j] = (next[j] ?? 0) ^ poly[j]!
      next[j + 1] = (next[j + 1] ?? 0) ^ gfMul(poly[j]!, factor)
    }
    poly = next
  }
  return poly
}

function rsComputeRemainder(data: Uint8Array, ecLen: number): Uint8Array {
  const gen = rsGeneratorPoly(ecLen)
  const rem = new Uint8Array(ecLen)
  for (let i = 0; i < data.length; i++) {
    const factor = data[i]! ^ rem[0]!
    rem.copyWithin(0, 1)
    rem[ecLen - 1] = 0
    if (factor !== 0) {
      for (let j = 0; j < ecLen; j++) {
        rem[j] = rem[j]! ^ gfMul(gen[j + 1]!, factor)
      }
    }
  }
  return rem
}

function buildFunctionMatrix(version: number): { size: number; modules: Uint8Array; isFunc: Uint8Array; freeModules: number } {
  const size = version * 4 + 17
  const modules = new Uint8Array(size * size)
  const isFunc = new Uint8Array(size * size)

  const setFunc = (r: number, c: number, dark: boolean) => {
    if (r < 0 || r >= size || c < 0 || c >= size) return
    const idx = r * size + c
    modules[idx] = dark ? 1 : 0
    isFunc[idx] = 1
  }

  // Finder patterns + separators
  const drawFinder = (top: number, left: number) => {
    for (let dr = -1; dr <= 7; dr++) {
      for (let dc = -1; dc <= 7; dc++) {
        const r = top + dr
        const c = left + dc
        if (r < 0 || r >= size || c < 0 || c >= size) continue
        const inOuter = dr >= 0 && dr <= 6 && dc >= 0 && dc <= 6
        const dark =
          inOuter &&
          (dr === 0 || dr === 6 || dc === 0 || dc === 6 || (dr >= 2 && dr <= 4 && dc >= 2 && dc <= 4))
        setFunc(r, c, dark)
      }
    }
  }

  drawFinder(0, 0)
  drawFinder(0, size - 7)
  drawFinder(size - 7, 0)

  // Alignment patterns
  const centers = ALIGNMENT_CENTERS[version] ?? []
  for (const cr of centers) {
    for (const cc of centers) {
      if (isFunc[cr * size + cc]) continue
      for (let dr = -2; dr <= 2; dr++) {
        for (let dc = -2; dc <= 2; dc++) {
          const dist = Math.max(Math.abs(dr), Math.abs(dc))
          setFunc(cr + dr, cc + dc, dist !== 1)
        }
      }
    }
  }

  // Timing patterns
  for (let i = 8; i < size - 8; i++) {
    setFunc(6, i, i % 2 === 0)
    setFunc(i, 6, i % 2 === 0)
  }

  // Reserve format information areas (will be written after masking)
  for (let i = 0; i <= 8; i++) {
    if (i !== 6) {
      setFunc(8, i, false)
      setFunc(i, 8, false)
    }
  }
  for (let i = 0; i < 8; i++) {
    setFunc(8, size - 1 - i, false)
    setFunc(size - 1 - i, 8, false)
  }
  // Dark module
  setFunc(size - 8, 8, true)

  // Version information (versions >= 7)
  if (version >= 7) {
    let rem = version
    for (let i = 0; i < 12; i++) {
      rem = (rem << 1) ^ ((rem >>> 11) * 0x1f25)
    }
    const bits = (version << 12) | rem
    for (let i = 0; i < 18; i++) {
      const bit = ((bits >>> i) & 1) === 1
      const a = size - 11 + (i % 3)
      const b = Math.floor(i / 3)
      setFunc(a, b, bit)
      setFunc(b, a, bit)
    }
  }

  let freeModules = 0
  for (let i = 0; i < isFunc.length; i++) {
    if (!isFunc[i]) freeModules++
  }
  return { size, modules, isFunc, freeModules }
}

function writeFormatInfo(modules: Uint8Array, size: number, ecc: QrEccLevel, mask: number) {
  const data = (ECC_FORMAT_BITS[ecc] << 3) | mask
  let rem = data
  for (let i = 0; i < 10; i++) {
    rem = (rem << 1) ^ ((rem >>> 9) * 0x537)
  }
  const bits = ((data << 10) | rem) ^ 0x5412

  const set = (r: number, c: number, bitIndex: number) => {
    modules[r * size + c] = (bits >>> bitIndex) & 1
  }

  // Copy 1 (around top-left)
  for (let i = 0; i <= 5; i++) set(8, i, 14 - i)
  set(8, 7, 8)
  set(8, 8, 7)
  set(7, 8, 6)
  for (let i = 0; i <= 5; i++) set(5 - i, 8, 5 - i)

  // Copy 2 (bottom-left and top-right)
  for (let i = 0; i <= 6; i++) set(size - 1 - i, 8, 14 - i)
  for (let i = 0; i <= 7; i++) set(8, size - 8 + i, 7 - i)
}

function applyMask(base: Uint8Array, isFunc: Uint8Array, size: number, mask: number): Uint8Array {
  const out = new Uint8Array(base)
  for (let r = 0; r < size; r++) {
    for (let c = 0; c < size; c++) {
      const idx = r * size + c
      if (isFunc[idx]) continue
      let flip = false
      switch (mask) {
        case 0: flip = (r + c) % 2 === 0; break
        case 1: flip = r % 2 === 0; break
        case 2: flip = c % 3 === 0; break
        case 3: flip = (r + c) % 3 === 0; break
        case 4: flip = (Math.floor(r / 2) + Math.floor(c / 3)) % 2 === 0; break
        case 5: flip = ((r * c) % 2) + ((r * c) % 3) === 0; break
        case 6: flip = (((r * c) % 2) + ((r * c) % 3)) % 2 === 0; break
        case 7: flip = (((r + c) % 2) + ((r * c) % 3)) % 2 === 0; break
      }
      if (flip) out[idx] = out[idx]! ^ 1
    }
  }
  return out
}

function evaluatePenalty(modules: Uint8Array, size: number): number {
  let penalty = 0

  // Rule 1: adjacent modules in row/column
  for (let r = 0; r < size; r++) {
    let runColor = modules[r * size]!
    let runLen = 1
    for (let c = 1; c < size; c++) {
      const v = modules[r * size + c]!
      if (v === runColor) {
        runLen++
      } else {
        if (runLen >= 5) penalty += 3 + (runLen - 5)
        runColor = v
        runLen = 1
      }
    }
    if (runLen >= 5) penalty += 3 + (runLen - 5)
  }
  for (let c = 0; c < size; c++) {
    let runColor = modules[c]!
    let runLen = 1
    for (let r = 1; r < size; r++) {
      const v = modules[r * size + c]!
      if (v === runColor) {
        runLen++
      } else {
        if (runLen >= 5) penalty += 3 + (runLen - 5)
        runColor = v
        runLen = 1
      }
    }
    if (runLen >= 5) penalty += 3 + (runLen - 5)
  }

  // Rule 2: 2x2 blocks
  for (let r = 0; r < size - 1; r++) {
    for (let c = 0; c < size - 1; c++) {
      const v = modules[r * size + c]!
      if (
        v === modules[r * size + c + 1] &&
        v === modules[(r + 1) * size + c] &&
        v === modules[(r + 1) * size + c + 1]
      ) {
        penalty += 3
      }
    }
  }

  // Rule 3: finder-like 1:1:3:1:1 patterns
  const matchFinder = (arr: number[]): number => {
    let count = 0
    for (let i = 0; i <= arr.length - 11; i++) {
      const a =
        arr[i] === 1 && arr[i + 1] === 0 && arr[i + 2] === 1 && arr[i + 3] === 1 &&
        arr[i + 4] === 1 && arr[i + 5] === 0 && arr[i + 6] === 1 &&
        arr[i + 7] === 0 && arr[i + 8] === 0 && arr[i + 9] === 0 && arr[i + 10] === 0
      const b =
        arr[i] === 0 && arr[i + 1] === 0 && arr[i + 2] === 0 && arr[i + 3] === 0 &&
        arr[i + 4] === 1 && arr[i + 5] === 0 && arr[i + 6] === 1 &&
        arr[i + 7] === 1 && arr[i + 8] === 1 && arr[i + 9] === 0 && arr[i + 10] === 1
      if (a || b) count++
    }
    return count
  }

  const line = new Array<number>(size)
  for (let r = 0; r < size; r++) {
    for (let c = 0; c < size; c++) line[c] = modules[r * size + c]!
    penalty += matchFinder(line) * 40
  }
  for (let c = 0; c < size; c++) {
    for (let r = 0; r < size; r++) line[r] = modules[r * size + c]!
    penalty += matchFinder(line) * 40
  }

  // Rule 4: dark ratio balance
  let darkCount = 0
  for (let i = 0; i < modules.length; i++) darkCount += modules[i]!
  const total = size * size
  const k = Math.floor(Math.abs(darkCount * 20 - total * 10) / total)
  penalty += k * 10

  return penalty
}

export interface QrMatrix {
  version: number
  ecc: QrEccLevel
  size: number
  modules: Uint8Array
}

export function encodeQrMatrix(text: string, preferredEcc: QrEccLevel = 'M'): QrMatrix {
  const bytes = new TextEncoder().encode(text)

  const levelsToTry: QrEccLevel[] = preferredEcc === 'M' ? ['M', 'L'] : ['L']
  let chosenVersion = 0
  let chosenEcc: QrEccLevel = preferredEcc
  let layout: ReturnType<typeof buildFunctionMatrix> | null = null
  let totalDataBytes = 0
  let ecPerBlock = 0
  let numBlocks = 0

  outer: for (const ecc of levelsToTry) {
    for (let v = 1; v <= 20; v++) {
      const candidate = buildFunctionMatrix(v)
      const totalCodewords = Math.floor(candidate.freeModules / 8)
      const [ecLen, blocks] = ECC_SPECS[ecc][v]!
      const dataCodewords = totalCodewords - ecLen * blocks
      const charCountBits = v <= 9 ? 8 : 16
      const requiredBits = 4 + charCountBits + bytes.length * 8
      if (requiredBits <= dataCodewords * 8) {
        chosenVersion = v
        chosenEcc = ecc
        layout = candidate
        totalDataBytes = dataCodewords
        ecPerBlock = ecLen
        numBlocks = blocks
        break outer
      }
    }
  }

  if (!layout || chosenVersion === 0) {
    throw new Error('Текст слишком длинный для QR-кода (максимум ~850 байт UTF-8)')
  }

  // Build data bitstream (Byte mode: 0100)
  const bitBuf: number[] = []
  const pushBits = (value: number, len: number) => {
    for (let i = len - 1; i >= 0; i--) {
      bitBuf.push((value >>> i) & 1)
    }
  }

  pushBits(0b0100, 4)
  pushBits(bytes.length, chosenVersion <= 9 ? 8 : 16)
  for (const b of bytes) pushBits(b, 8)

  const dataCapacityBits = totalDataBytes * 8
  const terminator = Math.min(4, dataCapacityBits - bitBuf.length)
  pushBits(0, terminator)
  while (bitBuf.length % 8 !== 0) bitBuf.push(0)

  const dataCodewords = new Uint8Array(totalDataBytes)
  const byteCount = bitBuf.length / 8
  for (let i = 0; i < byteCount; i++) {
    let val = 0
    for (let b = 0; b < 8; b++) val = (val << 1) | bitBuf[i * 8 + b]!
    dataCodewords[i] = val
  }
  const padBytes = [0xec, 0x11]
  for (let i = byteCount; i < totalDataBytes; i++) {
    dataCodewords[i] = padBytes[(i - byteCount) % 2]!
  }

  // Split into RS blocks & interleave
  const shortBlockLen = Math.floor(totalDataBytes / numBlocks)
  const numLongBlocks = totalDataBytes % numBlocks
  const numShortBlocks = numBlocks - numLongBlocks

  const dataBlocks: Uint8Array[] = []
  const ecBlocks: Uint8Array[] = []
  let offset = 0
  for (let b = 0; b < numBlocks; b++) {
    const len = b < numShortBlocks ? shortBlockLen : shortBlockLen + 1
    const block = dataCodewords.slice(offset, offset + len)
    offset += len
    dataBlocks.push(block)
    ecBlocks.push(rsComputeRemainder(block, ecPerBlock))
  }

  const finalCodewords: number[] = []
  const maxDataLen = shortBlockLen + (numLongBlocks > 0 ? 1 : 0)
  for (let i = 0; i < maxDataLen; i++) {
    for (const block of dataBlocks) {
      if (i < block.length) finalCodewords.push(block[i]!)
    }
  }
  for (let i = 0; i < ecPerBlock; i++) {
    for (const ec of ecBlocks) {
      finalCodewords.push(ec[i]!)
    }
  }

  // Place codewords in zigzag pattern
  const { size, modules, isFunc } = layout
  let bitIdx = 0
  const totalBits = finalCodewords.length * 8
  let upward = true
  for (let right = size - 1; right >= 1; right -= 2) {
    if (right === 6) right = 5
    for (let step = 0; step < size; step++) {
      const r = upward ? size - 1 - step : step
      for (let dc = 0; dc < 2; dc++) {
        const c = right - dc
        const idx = r * size + c
        if (isFunc[idx]) continue
        let dark = 0
        if (bitIdx < totalBits) {
          const cw = finalCodewords[bitIdx >>> 3]!
          dark = (cw >>> (7 - (bitIdx & 7))) & 1
          bitIdx++
        }
        modules[idx] = dark
      }
    }
    upward = !upward
  }

  // Choose mask 0..7 with lowest penalty score
  let bestPenalty = Number.POSITIVE_INFINITY
  let bestModules = modules
  for (let mask = 0; mask < 8; mask++) {
    const masked = applyMask(modules, isFunc, size, mask)
    writeFormatInfo(masked, size, chosenEcc, mask)
    const penalty = evaluatePenalty(masked, size)
    if (penalty < bestPenalty) {
      bestPenalty = penalty
      bestModules = masked
    }
  }

  return {
    version: chosenVersion,
    ecc: chosenEcc,
    size,
    modules: bestModules,
  }
}

export function renderQrSvg(
  text: string,
  options: { ecc?: QrEccLevel; quietZone?: number; darkColor?: string; lightColor?: string } = {},
): string {
  const { ecc = 'M', quietZone = 4, darkColor = '#0F1211', lightColor = '#FFFFFF' } = options
  const qr = encodeQrMatrix(text, ecc)
  const dim = qr.size + quietZone * 2
  const parts: string[] = []
  for (let r = 0; r < qr.size; r++) {
    for (let c = 0; c < qr.size; c++) {
      if (qr.modules[r * qr.size + c]) {
        parts.push(`M${c + quietZone},${r + quietZone}h1v1h-1z`)
      }
    }
  }
  return [
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${dim} ${dim}" shape-rendering="crispEdges" role="img" aria-label="QR Code">`,
    `<rect width="${dim}" height="${dim}" fill="${lightColor}"/>`,
    `<path d="${parts.join('')}" fill="${darkColor}"/>`,
    `</svg>`,
  ].join('')
}

export function renderQrDataUri(text: string, ecc: QrEccLevel = 'M'): string {
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(renderQrSvg(text, { ecc }))}`
}
