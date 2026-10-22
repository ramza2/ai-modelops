import { afterEach, describe, expect, it, vi } from 'vitest'
import { copyTextToClipboard } from '../utils/clipboard'

describe('copyTextToClipboard', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('uses navigator.clipboard.writeText when available', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', { clipboard: { writeText } })
    await copyTextToClipboard('svc-abc')
    expect(writeText).toHaveBeenCalledWith('svc-abc')
  })

  it('falls back to execCommand when clipboard API fails', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('denied'))
    vi.stubGlobal('navigator', { clipboard: { writeText } })
    const exec = vi.fn().mockReturnValue(true)
    Object.defineProperty(document, 'execCommand', {
      configurable: true,
      value: exec,
    })
    await copyTextToClipboard('X-AI-Client: svc-1')
    expect(exec).toHaveBeenCalledWith('copy')
  })

  it('rejects empty text', async () => {
    await expect(copyTextToClipboard('')).rejects.toThrow(/복사할 내용/)
  })
})
