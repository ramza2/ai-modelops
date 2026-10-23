import type { ReactNode } from 'react'

type HelpHintProps = {
  /** Short name used in the accessible summary label. */
  label: string
  children: ReactNode
  /**
   * When true, show ``label`` visibly next to ``?`` so glossary/toolbar rows
   * are identifiable. Compact ``?``-only mode stays the default for TermLabel.
   */
  showTopic?: boolean
}

/**
 * Keyboard- and mobile-friendly help disclosure (no hover-only tooltip).
 *
 * Uses a real ``details`` element (flow content). Callers must not nest this
 * inside phrasing-only parents such as ``h1``–``h6`` or ``span``; prefer
 * ``TermLabel`` (div wrapper) or place ``HelpHint`` beside a heading.
 */
export function HelpHint({
  label,
  children,
  showTopic = false,
}: HelpHintProps) {
  return (
    <details
      className={showTopic ? 'help-hint help-hint--topic' : 'help-hint'}
    >
      <summary className="help-hint__summary">
        {showTopic ? <span className="help-hint__topic">{label}</span> : null}
        <span className="help-hint__mark" aria-hidden="true">
          ?
        </span>
        {showTopic ? null : (
          <span className="help-hint__sr">{label} 도움말</span>
        )}
      </summary>
      <div className="help-hint__body">{children}</div>
    </details>
  )
}

type TermLabelProps = {
  /** Korean (or short) display label. */
  text: string
  /** Optional API / English term. Shown beside the label when showApiTerm. */
  apiTerm?: string
  help?: ReactNode
  helpLabel?: string
  /**
   * When false (default for beginner screens), hide the inline English API
   * term and surface it inside the help disclosure instead.
   */
  showApiTerm?: boolean
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
  showApiTerm = true,
}: TermLabelProps) {
  const showInlineApi = Boolean(apiTerm) && showApiTerm
  const helpContent =
    help || apiTerm ? (
      <>
        {apiTerm && !showApiTerm ? (
          <p className="term-label__api-note">
            API 필드: <code>{apiTerm}</code>
          </p>
        ) : null}
        {help}
      </>
    ) : null

  return (
    <div className="term-label">
      {text ? <span className="term-label__text">{text}</span> : null}
      {showInlineApi ? (
        <span className="term-label__api" title={apiTerm}>
          ({apiTerm})
        </span>
      ) : null}
      {helpContent ? (
        <HelpHint label={helpLabel || text || apiTerm || '항목'}>
          {helpContent}
        </HelpHint>
      ) : null}
    </div>
  )
}
