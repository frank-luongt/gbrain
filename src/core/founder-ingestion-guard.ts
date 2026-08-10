/** One-way boundary for human-approved FounderBrain publishing. */
export function assertFounderCanonicalWriteAllowed(
  sourceId: string,
  frontmatter: Record<string, unknown> | null | undefined,
): void {
  if (sourceId !== 'frankbrain') return;
  const metadata = frontmatter ?? {};
  const generated = metadata.generated === true;
  const canonicalForbidden = metadata.ingest_to_canonical === false;
  const projection = metadata.projection === 'frankbrain-one-way'
    || metadata.type === 'frankbrain-projection';
  if (generated || canonicalForbidden || projection) {
    throw new Error(
      'refusing generated or projection content in canonical source frankbrain; publish an approved authored page instead',
    );
  }
}
