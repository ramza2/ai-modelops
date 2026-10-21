import type { ReactNode } from 'react'

type HelpHintProps = {
  /** Short name used in the accessible summary label. */
  label: string
  children: ReactNode
}

/**
 * Keyboard- and mobile-friendly help disclosure (no hover-only tooltip).
 *
 * Uses a real ``details`` element (flow content). Callers must not nest this
 * inside phrasing-only parents such as ``h1``–``h6`` or ``span``; prefer
 * ``TermLabel`` (div wrapper) or place ``HelpHint`` beside a heading.
 */
export function HelpHint({ label, children }: HelpHintProps) {
  return (
    <details className="help-hint">
      <summary className="help-hint__summary">
        <span className="help-hint__mark" aria-hidden="true">
          ?
        </span>
        <span className="help-hint__sr">{label} 도움말</span>
      </summary>
      <div className="help-hint__body">{children}</div>
    </details>
  )
}

type TermLabelProps = {
  /** Korean (or short) display label. */
  text: string
  /** Optional API / English term shown beside the label. */
  apiTerm?: string
  help?: ReactNode
  helpLabel?: string
}

/**
 * Field / column label with optional API term + accessible help.
 *
 * Outer element is a ``div`` so ``HelpHint`` (``details``) is valid HTML in
 * table headers (``th``), definition terms (``dt``), and toolbar rows.
 * Do not place ``TermLabel`` with ``help`` inside ``h1``–``h6`` — put the
 * heading text in the heading and ``TermLabel``/``HelpHint`` beside it.
 */
export function TermLabel({
  text,
  apiTerm,
  help,
  helpLabel,
}: TermLabelProps) {
  return (
    <div className="term-label">
      {text ? <span className="term-label__text">{text}</span> : null}
      {apiTerm ? (
        <span className="term-label__api" title={apiTerm}>
          ({apiTerm})
        </span>
      ) : null}
      {help ? (
        <HelpHint label={helpLabel || text || apiTerm || '항목'}>
          {help}
        </HelpHint>
      ) : null}
    </div>
  )
}
