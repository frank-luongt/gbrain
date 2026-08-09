import { describe, expect, test } from 'bun:test';
import { assertFounderCanonicalWriteAllowed } from '../src/core/founder-ingestion-guard.ts';

describe('FounderBrain canonical-ingestion guard', () => {
  test('allows human-authored canonical pages', () => {
    expect(() => assertFounderCanonicalWriteAllowed('frankbrain', {
      status: 'published', generated: false, ingest_to_canonical: true,
    })).not.toThrow();
  });

  test.each([
    { generated: true },
    { ingest_to_canonical: false },
    { projection: 'frankbrain-one-way' },
    { type: 'frankbrain-projection' },
  ])('rejects generated canonical write %#', (frontmatter) => {
    expect(() => assertFounderCanonicalWriteAllowed('frankbrain', frontmatter)).toThrow(
      'refusing generated or projection content',
    );
  });

  test('does not constrain evidence-source writes', () => {
    expect(() => assertFounderCanonicalWriteAllowed('gdrive-workspaces', {
      generated: true, ingest_to_canonical: false,
    })).not.toThrow();
  });
});
