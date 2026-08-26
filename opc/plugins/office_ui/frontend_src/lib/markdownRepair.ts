/**
 * Repair Markdown structure lost by streaming providers that discard
 * whitespace-only chunks. The detection is deliberately conservative so
 * ordinary prose, URLs, dates, and already-valid Markdown remain unchanged.
 */
export function restoreCollapsedMarkdown(value: string): string {
  let text = String(value ?? '').replace(/\r\n/g, '\n').trim()
  if (!text) return ''

  const collapsedHeading = /[^\n][ \t]*#{2,6}[ \t]*\S/.test(text)
  const collapsedHeadingBody = /^#{1,6}[ \t]*[^*|\n]{1,119}\S(?=(?:\*\*[^*\n]{1,80}\*\*[：:]|\|[^|\n]{1,80}\|))/m.test(text)
  const collapsedOrdered = text.match(/(?<![\d.])\d{1,2}\.[ \t]+(?=(?:\*\*|[A-Za-z\u4e00-\u9fff]))/g) ?? []
  const collapsedBullets = text.match(
    /(?<![-\sA-Za-z0-9/])-(?!-)[ \t]*(?=(?:\*\*|[A-Za-z\u4e00-\u9fff]|[✅❌⚠☑🔹▪•]))/g,
  ) ?? []
  if (!collapsedHeading && !collapsedHeadingBody && collapsedOrdered.length < 2 && collapsedBullets.length < 2) {
    return text
  }

  text = text.replace(/([^\n])[ \t]*(#{2,6})[ \t]*(?=\S)/g, '$1\n\n$2 ')
  text = text.replace(/^(#{1,6})[ \t]*(?=\S)/gm, '$1 ')
  text = text.replace(
    /^(#{1,6} [^*|\n]{1,119}?\S)(?=\*\*[^*\n]{1,80}\*\*[：:])/gm,
    '$1\n\n',
  )
  text = text.replace(
    /^(#{1,6} [^|\n]{1,119}?\S)(?=\|[^|\n]{1,80}\|)/gm,
    '$1\n',
  )
  text = text.replace(
    /^(#{1,6} [^\n]{1,80}?[）)])(?=[A-Za-z0-9\u4e00-\u9fff])/gm,
    '$1\n\n',
  )

  if (collapsedOrdered.length >= 2) {
    text = text.replace(
      /([^\n])[ \t]*(\d{1,2}\.)[ \t]+(?=(?:\*\*|[A-Za-z\u4e00-\u9fff]))/g,
      '$1\n$2 ',
    )
  }

  if (collapsedBullets.length >= 2) {
    text = text.replace(
      /(?<![-\sA-Za-z0-9/])-(?!-)[ \t]*(?=(?:\*\*|[A-Za-z\u4e00-\u9fff]|[✅❌⚠☑🔹▪•]))/g,
      '\n- ',
    )
  }

  text = text.replace(/^\|[^\n#]+/gm, (table: string) => {
    if (!/\|\|[ \t]*:?-{3,}/.test(table)) return table
    return table.replace(/\|\|/g, '|\n|')
  })

  text = text.replace(/(?<=[`。！？；：）)])---(?=\S)/g, '\n\n---\n\n')

  return text.trim()
}
