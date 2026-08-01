/**
 * Turning "which files are ticked" into a retrieval scope.
 *
 * The case that carries the risk is an empty selection. The platform reads an
 * empty collection filter as *tenant-wide*, so the obvious encoding of
 * "nothing" is in fact "everything" — the opposite of what the customer asked
 * for, and a leak across every space on the deployment.
 */

import { describe, expect, it, vi } from 'vitest';
import { applyDocumentScope, parseDocumentScope } from '../src/api/server';
import { retrieveTurnEvidence } from '../src/agent/intelligence';
import type { IntelligenceConfig } from '../src/types';

const INTELLIGENCE: IntelligenceConfig = {
  enabled: true,
  apiUrl: 'http://127.0.0.1:8000',
  tenantId: 'personal',
  principalId: 'nano-claw',
  collectionIds: ['configured-default'],
  limit: 5,
  candidatePool: 40,
  maxChars: 16000,
  timeoutMs: 750,
  groundingMode: 'strict',
};

describe('applyDocumentScope', () => {
  it('scopes to the space when every document is ticked', () => {
    const scoped = applyDocumentScope(INTELLIGENCE, {
      collectionId: 'taxes-2025-abc',
      selected: ['doc-a', 'doc-b'],
      ready: 2,
      allSelected: true,
    });
    expect(scoped?.collectionIds).toEqual(['taxes-2025-abc']);
    // Redundant when everything is ticked — the collection filter is exact.
    expect(scoped?.documentIds).toEqual([]);
  });

  it('narrows to the ticked documents when only some are', () => {
    const scoped = applyDocumentScope(INTELLIGENCE, {
      collectionId: 'taxes-2025-abc',
      selected: ['doc-a'],
      ready: 2,
      allSelected: false,
    });
    expect(scoped?.collectionIds).toEqual(['taxes-2025-abc']);
    expect(scoped?.documentIds).toEqual(['doc-a']);
  });

  it('marks an empty selection rather than sending an empty filter', () => {
    const scoped = applyDocumentScope(INTELLIGENCE, {
      collectionId: 'taxes-2025-abc',
      selected: [],
      ready: 3,
    });
    expect(scoped?.documentScopeEmpty).toBe(true);
    expect(scoped?.collectionIds).toEqual([]);
  });

  it('leaves a space with nothing indexed yet on the configured default', () => {
    // Mid-upload, or before the first upload. Narrowing here would make a
    // half-configured deployment answer nothing at all.
    const scoped = applyDocumentScope(INTELLIGENCE, {
      collectionId: 'taxes-2025-abc',
      selected: [],
      ready: 0,
    });
    expect(scoped?.collectionIds).toEqual(['configured-default']);
    expect(scoped?.documentScopeEmpty).toBeUndefined();
  });

  it('leaves the config alone when no space is active', () => {
    expect(applyDocumentScope(INTELLIGENCE, undefined)).toBe(INTELLIGENCE);
  });
});

describe('parseDocumentScope', () => {
  it('accepts the shape the voice server sends', () => {
    expect(
      parseDocumentScope({
        collectionId: 'taxes-2025-abc',
        selected: ['doc-a'],
        ready: 2,
        allSelected: false,
      })
    ).toEqual({
      collectionId: 'taxes-2025-abc',
      selected: ['doc-a'],
      ready: 2,
      allSelected: false,
    });
  });

  it.each([
    ['not an object', 'nope'],
    ['a missing collection', { selected: [], ready: 0 }],
    ['a non-array selection', { collectionId: 'a', selected: 'doc-a', ready: 1 }],
    ['a non-numeric ready count', { collectionId: 'a', selected: [], ready: 'two' }],
  ])('drops %s', (_label, raw) => {
    expect(parseDocumentScope(raw)).toBeUndefined();
  });

  it('drops the whole scope when any id is malformed, rather than part of it', () => {
    // Honouring the readable half would silently widen retrieval, and widening
    // is the direction that leaks.
    expect(
      parseDocumentScope({
        collectionId: 'taxes',
        selected: ['doc-a', 'not a valid id!'],
        ready: 2,
      })
    ).toBeUndefined();
  });
});

describe('the retrieval request', () => {
  const post = (data: unknown = { evidence: [] }) =>
    vi.fn().mockResolvedValue({ data, status: 200 });

  const ask = [{ role: 'user' as const, content: 'what were my wages?' }];

  it('sends document_ids only when they narrow something', async () => {
    const http = post();
    await retrieveTurnEvidence(ask, { ...INTELLIGENCE, documentIds: ['doc-a'] }, {
      post: http,
    } as never);
    expect(http.mock.calls[0][1].scope).toEqual({
      tenant_id: 'personal',
      collection_ids: ['configured-default'],
      document_ids: ['doc-a'],
    });
  });

  it('omits the key entirely when no documents are named', async () => {
    // Not `document_ids: []` — the request must stay byte-identical to what it
    // was before per-document scoping existed.
    const http = post();
    await retrieveTurnEvidence(ask, { ...INTELLIGENCE, documentIds: [] }, {
      post: http,
    } as never);
    expect(http.mock.calls[0][1].scope).toEqual({
      tenant_id: 'personal',
      collection_ids: ['configured-default'],
    });
  });
});
