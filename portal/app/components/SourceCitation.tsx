import type { ReactNode } from 'react';
import type { EnrichmentSource } from '../lib/sse';
import { safeSourceUrl } from '../lib/source-url';

function SourceLink({ url, children }: { url?: string | null; children: ReactNode }) {
  const safe = safeSourceUrl(url);
  return safe ? (
    <a href={safe} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer">
      {children}
      <span className="srOnly"> (opens in a new tab)</span>
    </a>
  ) : (
    <span>
      {children} <small>(source link unavailable or blocked)</small>
    </span>
  );
}

export function SourceCitation({ source }: { source: EnrichmentSource }) {
  return (
    <>
      <SourceLink url={source.url}>{source.title}</SourceLink>
      <span className="sourceDates">
        {source.published_at ? `Published: ${source.published_at}` : 'Publication date unavailable'}
        {source.updated_at ? `; Updated: ${source.updated_at}` : ''}
        {source.crawled_at ? `; Crawled: ${source.crawled_at}` : ''}
      </span>
    </>
  );
}
