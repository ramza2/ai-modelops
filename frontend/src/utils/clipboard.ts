/**
 * Copy plain text to the clipboard. Prefer ``navigator.clipboard``; fall back
 * to a temporary textarea + ``document.execCommand('copy')`` for LAN HTTP
 * Admin origins where Clipboard API may be unavailable.
 */
export async function copyTextToClipboard(text: string): Promise<void> {
  if (typeof text !== 'string' || text.length === 0) {
    throw new Error('복사할 내용이 없습니다.')
  }

  if (
    typeof navigator !== 'undefined' &&
    navigator.clipboard &&
    typeof navigator.clipboard.writeText === 'function'
  ) {
    try {
      await navigator.clipboard.writeText(text)
      return
    } catch {
      // Fall through to execCommand path (common on insecure HTTP origins).
    }
  }

  if (typeof document === 'undefined') {
    throw new Error('클립보드에 복사할 수 없습니다.')
  }

  const textarea = document.createElement('textarea')
  textarea.value = text
  textarea.setAttribute('readonly', '')
  textarea.style.position = 'fixed'
  textarea.style.left = '-9999px'
  textarea.style.top = '0'
  document.body.appendChild(textarea)
  textarea.focus()
  textarea.select()
  textarea.setSelectionRange(0, text.length)
  let ok = false
  try {
    ok = document.execCommand('copy')
  } finally {
    document.body.removeChild(textarea)
  }
  if (!ok) {
    throw new Error('클립보드에 복사할 수 없습니다. 브라우저 권한을 확인하세요.')
  }
}
