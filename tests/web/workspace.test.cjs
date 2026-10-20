// Runs the real app.js event handlers against a small DOM/fetch test double.
// No browser, network, user data, or third-party npm packages are involved.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const web = path.resolve(__dirname, '../../src/web');
const source = fs.readFileSync(path.join(web, 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(web, 'index.html'), 'utf8');
const tick = () => new Promise((resolve) => setImmediate(resolve));

class Element {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.listeners = new Map();
    this.attributes = {};
    this.value = '';
    this.files = [];
    this.disabled = false;
    this.checked = false;
    this.className = '';
    this.text = '';
  }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map((child) => child.textContent).join(''); }
  set innerHTML(value) { throw new Error('Unsafe HTML rendering'); }
  set outerHTML(value) { throw new Error('Unsafe HTML rendering'); }
  insertAdjacentHTML() { throw new Error('Unsafe HTML rendering'); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.text = ''; this.children = [...children]; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(type, listener) { this.listeners.set(type, listener); }
  querySelectorAll(selector) {
    return this.children.flatMap((child) => [ ...(child.tagName === selector ? [child] : []), ...child.querySelectorAll(selector) ]);
  }
}

function doc(letter, status = 'parsed', extra = {}) {
  return { document_id: letter.repeat(32), document_name: letter + '.pdf', status, sha256: 'f'.repeat(64),
    page_count: status === 'parsed' ? 1 : null, nonempty_page_count: 1,
    chunk_count: status === 'parsed' ? 1 : null, chunk_size: 500,
    warnings: [], empty_page_numbers: [], detail: 'test document', ...extra };
}

function response(body, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

function hits(selected, text = 'alpha evidence', extra = {}) {
  return { document_id: selected.document_id, document_name: selected.document_name, source_sha256: selected.sha256,
    query: 'alpha', method: 'normalized', k: 5, indexed_chunk_count: 1, cache_hit: false,
    returned_count: 1, results: [{ score: 0, chunk: {
      document_id: selected.document_id, document_name: selected.document_name,
      pdf_page_number: 1, start_char: 0, end_char: text.length, text,
    } }], ...extra };
}

async function setup(documents = []) {
  const elements = new Map([...html.matchAll(/id="([^"]+)"/g)].map((match) => [match[1], new Element('div')]));
  elements.get('chunk-size').value = '500';
  elements.get('method').value = 'normalized';
  elements.get('k').value = '5';
  const calls = [];
  const routes = new Map();
  const control = { documents, listError: null };
  control.model = { provider: 'deepseek', model: 'deepseek-flash', status: 'ready', calls_used: 0, max_calls: 3, credential_verified: false };
  const document = {
    body: new Element('body'),
    getElementById: (id) => { assert.ok(elements.has(id), 'HTML missing #' + id); return elements.get(id); },
    createElement: (tag) => new Element(tag),
  };
  class FormData { constructor() { this.entries = []; } append(...args) { this.entries.push(args); } }
  const context = vm.createContext({ document, FormData, fetch: async (url, options = {}) => {
    const key = (options.method || 'GET') + ' ' + url;
    calls.push({ url, options });
    if (routes.has(key)) return routes.get(key)(options);
    if (url === '/generation-status') return response(control.model);
    if (url.startsWith('/documents?')) {
      if (control.listError) throw control.listError;
      const offset = Number(new URL(url, 'http://test.invalid').searchParams.get('offset'));
      const rows = control.documents.slice(offset, offset + 20);
      return response({ total: control.documents.length, offset, limit: 20, documents: rows, returned_count: rows.length });
    }
    const selected = control.documents.find((item) => url === '/documents/' + item.document_id);
    return selected ? response(selected) : response({ detail: 'not found' }, 404);
  } });
  vm.runInContext(source, context, { filename: 'app.js' });
  await tick();
  const fixture = {
    calls, routes, control, document,
    get: (expression) => vm.runInContext(expression, context),
    el: (id) => document.getElementById(id),
    async fire(target, type = 'click') {
      const element = typeof target === 'string' ? this.el(target) : target;
      const listener = element.listeners.get(type);
      assert.ok(listener, 'Missing event handler: ' + type);
      listener({ preventDefault() {} });
      await tick();
    },
    async choose(index = 0) { await this.fire(this.el('document-list').querySelectorAll('button')[index]); },
    async query(text = 'alpha') { this.el('query').value = text; await this.fire('query', 'input'); },
  };
  assert.equal(fixture.get('state.busy'), false);
  return fixture;
}

test('empty library has disabled actions and no writes', async () => {
  const f = await setup();
  assert.equal(f.el('parse').disabled, true);
  assert.equal(f.el('search').disabled, true);
  assert.match(f.el('document-list').textContent, /还没有文档/);
  assert.equal(f.calls.length, 2);
  assert.equal(f.calls[0].url, '/documents?offset=0&limit=20');
});

test('document status controls parse and search availability', async () => {
  const documents = [doc('a', 'uploaded'), doc('b'), doc('c', 'parsed', { chunk_count: 0 }), doc('d', 'invalid'), doc('e', 'incomplete')];
  const f = await setup(documents);
  await f.query();
  for (let index = 0; index < documents.length; index++) {
    await f.choose(index);
    assert.equal(f.el('parse').disabled, index !== 0);
    assert.equal(f.el('search').disabled, index !== 1);
  }
});

test('selection clears previous evidence and keeps literal filenames', async () => {
  const first = doc('a', 'parsed', { document_name: '<img onerror=alert(1)>.pdf' });
  const f = await setup([first, doc('b')]);
  await f.choose();
  f.routes.set('POST /documents/' + first.document_id + '/search', () => response(hits(first)));
  await f.query(); await f.fire('search-form', 'submit');
  assert.match(f.el('results').textContent, /alpha evidence/);
  assert.match(f.el('selected-info').textContent, /<img onerror=alert\(1\)>.pdf/);
  await f.choose(1);
  assert.doesNotMatch(f.el('results').textContent, /alpha evidence/);
  assert.equal(f.el('result-count').textContent, '尚未检索');
});

test('search sends selected id and JSON options, preserving source markup as text', async () => {
  const selected = doc('a'); const f = await setup([selected]); await f.choose();
  const text = '<script>alert(1)</script>\nalpha';
  f.routes.set('POST /documents/' + selected.document_id + '/search', () => response(hits(selected, text)));
  await f.query('  alpha  '); await f.fire('search-form', 'submit');
  const call = f.calls.at(-1);
  assert.deepEqual(JSON.parse(call.options.body), { query: 'alpha', method: 'normalized', k: 5 });
  assert.equal(call.options.headers['Content-Type'], 'application/json');
  assert.match(f.el('results').textContent, /<script>alert\(1\)<\/script>/);
  assert.equal(f.el('results').querySelectorAll('script').length, 0);
  assert.match(f.el('results').textContent, /BM25 0.0000/);
  assert.match(f.el('results').textContent, /PDF 物理第 1 页/);
});

test('no match has explanatory text, not an automatic refusal', async () => {
  const selected = doc('a'); const f = await setup([selected]); await f.choose(); await f.query();
  f.routes.set('POST /documents/' + selected.document_id + '/search', () => response(hits(selected, '', { returned_count: 0, results: [] })));
  await f.fire('search-form', 'submit');
  assert.equal(f.el('result-count').textContent, '0 条证据');
  assert.match(f.el('results').textContent, /不代表文档没有答案/);
});

test('failed search clears stale results and does not leave a pending label', async () => {
  const selected = doc('a'); const f = await setup([selected]); await f.choose(); await f.query();
  const route = 'POST /documents/' + selected.document_id + '/search';
  f.routes.set(route, () => response(hits(selected)));
  await f.fire('search-form', 'submit');
  f.routes.set(route, () => response({ detail: 'data changed' }, 409));
  await f.fire('search-form', 'submit');
  assert.doesNotMatch(f.el('results').textContent, /alpha evidence/);
  assert.equal(f.el('result-count').textContent, '检索失败');
  assert.match(f.el('notice').textContent, /HTTP 409/);
  assert.equal(f.get('state.busy'), false);
});

test('refresh removes a missing selected document instead of enabling stale search', async () => {
  const f = await setup([doc('a')]); await f.choose(); await f.query();
  f.control.documents = [];
  await f.fire('refresh');
  assert.equal(f.get('state.selected'), null);
  assert.equal(f.el('search').disabled, true);
  assert.equal(f.el('parse').disabled, true);
  assert.match(f.el('selected-info').textContent, /选择/);
  assert.match(f.el('notice').textContent, /404/);
});

test('refresh with changed status disables search and clears prior evidence', async () => {
  const f = await setup([doc('a')]); await f.choose(); await f.query();
  f.control.documents = [doc('a', 'invalid')];
  await f.fire('refresh');
  assert.equal(f.get('state.selected.status'), 'invalid');
  assert.equal(f.el('search').disabled, true);
});

test('refresh network failure also clears unconfirmed document selection', async () => {
  const f = await setup([doc('a')]); await f.choose(); await f.query();
  f.control.listError = new Error('offline');
  await f.fire('refresh');
  assert.equal(f.get('state.selected'), null);
  assert.equal(f.el('search').disabled, true);
  assert.match(f.el('notice').textContent, /已取消选择/);
});

test('invalid K makes no search request and replaces the old evidence', async () => {
  const selected = doc('a'); const f = await setup([selected]); await f.choose(); await f.query();
  f.routes.set('POST /documents/' + selected.document_id + '/search', () => response(hits(selected)));
  await f.fire('search-form', 'submit');
  const count = f.calls.length;
  f.el('k').value = '21';
  await f.fire('search-form', 'submit');
  assert.equal(f.calls.length, count);
  assert.equal(f.el('result-count').textContent, '检索失败');
  assert.doesNotMatch(f.el('results').textContent, /alpha evidence/);
});

test('upload uses FormData without JSON content type and selects returned id', async () => {
  const f = await setup(); const uploaded = doc('b', 'uploaded');
  f.el('pdf-file').files = [{ name: 'b.pdf', size: 10 }];
  f.routes.set('POST /documents', (options) => {
    assert.equal(options.headers, undefined);
    assert.equal(options.body.entries[0][0], 'file');
    f.control.documents = [uploaded]; return response(uploaded, 201);
  });
  await f.fire('upload-form', 'submit');
  assert.equal(f.get('state.selected.document_id'), uploaded.document_id);
  assert.equal(f.el('parse').disabled, false);
  assert.equal(f.el('search').disabled, true);
});

test('upload validation prevents unsupported, empty and oversized requests', async () => {
  for (const file of [{ name: 'x.txt', size: 10 }, { name: 'x.pdf', size: 0 }, { name: 'x.pdf', size: 10 * 1024 * 1024 + 1 }]) {
    const f = await setup(); f.el('pdf-file').files = [file];
    await f.fire('upload-form', 'submit');
    assert.equal(f.calls.filter((call) => call.options.method === 'POST').length, 0);
    assert.match(f.el('notice').className, /error/);
  }
});

test('successful upload followed by list failure preserves success and selected id', async () => {
  const f = await setup(); const uploaded = doc('b', 'uploaded');
  f.el('pdf-file').files = [{ name: 'b.pdf', size: 10 }];
  f.routes.set('POST /documents', () => { f.control.listError = new Error('offline'); return response(uploaded, 201); });
  await f.fire('upload-form', 'submit');
  assert.equal(f.get('state.selected.document_id'), uploaded.document_id);
  assert.match(f.el('notice').textContent, /上传成功/);
  assert.match(f.el('notice').textContent, /不要重复提交/);
});

test('parse transitions to searchable without a second parse request', async () => {
  const selected = doc('a', 'uploaded'); const f = await setup([selected]); await f.choose(); await f.query();
  f.routes.set('POST /documents/' + selected.document_id + '/parse', (options) => {
    assert.deepEqual(JSON.parse(options.body), { chunk_size: 500 });
    const parsed = doc('a'); f.control.documents = [parsed];
    return response({ ...parsed, source_sha256: 'f'.repeat(64) });
  });
  await f.fire('parse-form', 'submit');
  assert.equal(f.el('parse').disabled, true);
  assert.equal(f.el('search').disabled, false);
  const count = f.calls.length;
  await f.fire('parse-form', 'submit');
  assert.equal(f.calls.length, count);
});

test('all-empty parse stays non-searchable and explains OCR limitation', async () => {
  const selected = doc('a', 'uploaded'); const f = await setup([selected]); await f.choose(); await f.query();
  f.routes.set('POST /documents/' + selected.document_id + '/parse', () => response({ ...doc('a'), chunk_count: 0, source_sha256: 'f'.repeat(64) }));
  await f.fire('parse-form', 'submit');
  assert.equal(f.el('search').disabled, true);
  assert.match(f.el('notice').textContent, /OCR/);
});

test('busy action disables controls and prevents duplicate submissions', async () => {
  const selected = doc('a'); const f = await setup([selected]); await f.choose(); await f.query();
  let complete;
  f.routes.set('POST /documents/' + selected.document_id + '/search', () => new Promise((resolve) => { complete = resolve; }));
  await f.fire('search-form', 'submit');
  assert.equal(f.get('state.busy'), true);
  assert.equal(f.el('search').disabled, true);
  assert.equal(f.el('refresh').disabled, true);
  const count = f.calls.length;
  await f.fire('search-form', 'submit');
  assert.equal(f.calls.length, count);
  complete(response(hits(selected))); await tick();
  assert.equal(f.get('state.busy'), false);
  assert.equal(f.el('search').disabled, false);
});

test('pagination uses offset and rolls back when a request fails', async () => {
  const f = await setup(Array.from({ length: 21 }, (_, i) => doc('a', 'uploaded', { document_id: i.toString(16).padStart(32, '0') })));
  await f.fire('next');
  assert.equal(f.get('state.offset'), 20);
  assert.match(f.calls.at(-1).url, /offset=20/);
  f.control.listError = new Error('offline');
  await f.fire('previous');
  assert.equal(f.get('state.offset'), 20);
  assert.equal(f.get('state.busy'), false);
});

test('structured API validation errors are readable and unlock controls', async () => {
  const selected = doc('a'); const f = await setup([selected]); await f.choose(); await f.query();
  f.routes.set('POST /documents/' + selected.document_id + '/search', () => response({ detail: [{ msg: 'invalid query' }] }, 422));
  await f.fire('search-form', 'submit');
  assert.match(f.el('notice').textContent, /HTTP 422：invalid query/);
  assert.equal(f.get('state.busy'), false);
});

test('original document link works before parsing and binds the upload fingerprint', async () => {
  const selected = doc('a', 'uploaded', { document_name: '<img>.pdf' });
  const f = await setup([selected]); await f.choose();
  const link = f.el('selected-info').querySelectorAll('a')[0];
  assert.equal(link.href, '/documents/' + selected.document_id + '/file?sha256=' + selected.sha256 + '#page=1');
  assert.equal(link.target, '_blank');
  assert.equal(link.rel, 'noopener noreferrer');
  assert.equal(f.el('search').disabled, true);
});

test('evidence link uses physical page and search fingerprint, not printed page or filename', async () => {
  const selected = doc('a'); const f = await setup([selected]); await f.choose(); await f.query();
  const body = hits(selected);
  body.source_sha256 = 'e'.repeat(64);
  body.results[0].chunk.pdf_page_number = 9;
  f.routes.set('POST /documents/' + selected.document_id + '/search', () => response(body));
  await f.fire('search-form', 'submit');
  const link = f.el('results').querySelectorAll('a')[0];
  assert.equal(link.href, '/documents/' + selected.document_id + '/file?sha256=' + body.source_sha256 + '#page=9');
  assert.match(link.textContent, /第 9 页/);
});

test('PDF link builder refuses unsafe identifiers and invalid page numbers', async () => {
  const f = await setup();
  const id = 'a'.repeat(32), sha = 'f'.repeat(64);
  for (const [docId, hash, page] of [['../../.env', sha, 1], [id, 'javascript:alert(1)', 1], [id, sha, 0], [id, sha, 1.5]]) {
    assert.throws(() => f.get('pdfLink(' + [docId, hash, page, 'open'].map(JSON.stringify).join(',') + ')'), /来源编号、指纹或物理页号不合法/);
  }
});

function reviewed(selected, extra = {}) {
  return { document_id: selected.document_id, document_name: selected.document_name, source_sha256: selected.sha256,
    query: 'alpha', raw_context_id: 'b'.repeat(64), reviewed_context_id: 'c'.repeat(64), status: 'ready',
    source_kind: 'reviewed_formula_annotation', included_count: 1,
    evidence: [{ citation_id: 'E1', review_id: 'FR001', review_revision: 'd'.repeat(64), source_sha256: selected.sha256,
      pdf_page_number: 9, raw_start_char: 67, raw_end_char: 88, raw_quote: 'raw quote',
      reviewed_statement: 'z·conj(w) is a nonnegative real number.', review_method: 'assistant_visual_review', reviewed_at: '2026-10-15' }], ...extra };
}

function answerBody(packet, status = 'answered') {
  return { document_id: packet.document_id, source_sha256: packet.source_sha256, query: packet.query,
    raw_context_id: packet.raw_context_id, reviewed_context_id: packet.reviewed_context_id, status,
    generation: { provider: 'deepseek', model: 'deepseek-flash', model_called: true,
      usage: { prompt_tokens: 100, completion_tokens: 20, total_tokens: 120 }, calls_used: 1, max_calls: 3 },
    check: { status: status === 'rejected' ? 'rejected' : 'passed', candidate_decision: status === 'refused' ? 'refuse' : 'answer',
      refusal_reason: status === 'refused' ? 'Only a conclusion was reviewed, not a complete proof.' : null,
      issues: status === 'rejected' ? [{ code: 'reviewed_statement_mismatch', detail: 'unsafe draft blocked' }] : [],
      claims: status === 'answered' ? [{ text: packet.evidence[0].reviewed_statement, citations: [{ ...packet.evidence[0],
        document_id: packet.document_id, document_name: 'a.pdf', quote: packet.evidence[0].reviewed_statement }] }] : [] } };
}

async function previewFixture(packetOverrides = {}) {
  const selected = doc('a');
  const f = await setup([selected, doc('b')]);
  await f.choose(); await f.query();
  const packet = reviewed(selected, packetOverrides);
  f.packet = packet;
  f.routes.set('POST /documents/' + selected.document_id + '/search', () => response(hits(selected)));
  f.routes.set('POST /documents/' + selected.document_id + '/reviewed-context', () => response(packet));
  f.routes.set('POST /documents/' + selected.document_id + '/answer-reviewed', () => response(answerBody(packet)));
  await f.fire('preview');
  return f;
}

function paidCalls(f) { return f.calls.filter((call) => call.url.endsWith('/answer-reviewed')); }
async function consent(f) { f.el('external-consent').checked = true; await f.fire('external-consent', 'change'); }

test('free preview fetches raw and reviewed evidence but never calls the model', async () => {
  const f = await previewFixture();
  assert.match(f.el('review-preview').textContent, /FR001/);
  assert.match(f.el('review-preview').textContent, /z·conj\(w\)/);
  assert.match(f.el('answer-status').textContent, /未调用模型/);
  assert.match(f.el('model-status').textContent, /deepseek-flash/);
  assert.match(f.el('model-status').textContent, /不保证密钥有效或余额充足/);
  assert.equal(paidCalls(f).length, 0);
  assert.equal(f.el('external-consent').disabled, false);
  assert.equal(f.el('answer').disabled, true);
});

test('answer requires explicit consent even if a disabled handler is invoked directly', async () => {
  const f = await previewFixture(); await f.fire('answer');
  assert.equal(paidCalls(f).length, 0);
  assert.match(f.el('notice').textContent, /确认外发许可/);
});

test('paid answer automatically binds the exact preview and consumes permission', async () => {
  const f = await previewFixture(); await consent(f); await f.fire('answer');
  assert.equal(paidCalls(f).length, 1);
  assert.deepEqual(JSON.parse(paidCalls(f)[0].options.body), { query: 'alpha', method: 'normalized', k: 5,
    max_context_chars: 6000, allow_external: true, expected_reviewed_context_id: f.packet.reviewed_context_id });
  assert.match(f.el('answer-result').textContent, /z·conj\(w\)/);
  assert.match(f.el('answer-result').textContent, /deepseek-flash/);
  assert.match(f.el('answer-result').textContent, /合计 120 tokens/);
  assert.match(f.el('answer-status').textContent, /本次已调用模型/);
  assert.equal(f.get('state.preview'), null);
  assert.equal(f.el('external-consent').checked, false);
  assert.equal(f.el('answer').disabled, true);
  await f.fire('answer'); assert.equal(paidCalls(f).length, 1);
});

test('no reviewed evidence disables paid calls without pretending to observe a model refusal', async () => {
  const f = await previewFixture({ evidence: [], included_count: 0, status: 'no_reviewed_evidence' });
  assert.equal(f.el('external-consent').disabled, true);
  assert.match(f.el('review-preview').textContent, /不是模型已经作出了拒答/);
  assert.match(f.el('answer-status').textContent, /证据不足 · 未调用模型/);
  await consent(f); await f.fire('answer');
  assert.equal(paidCalls(f).length, 0);
});

test('changing question, method or K invalidates previews and clears answers', async () => {
  for (const [id, event, value] of [['query', 'input', 'another question'], ['k', 'input', '10'], ['method', 'change', 'original']]) {
    const f = await previewFixture(); await consent(f);
    f.el(id).value = value; await f.fire(id, event);
    assert.equal(f.get('state.preview'), null);
    assert.equal(f.el('external-consent').checked, false);
    assert.equal(f.el('answer').disabled, true);
    await f.fire('answer'); assert.equal(paidCalls(f).length, 0);
  }
});

test('switching documents clears preview and permission', async () => {
  const f = await previewFixture(); await consent(f); await f.choose(1);
  assert.equal(f.get('state.preview'), null);
  assert.doesNotMatch(f.el('review-preview').textContent, /FR001/);
  assert.equal(f.el('external-consent').checked, false);
  assert.equal(f.el('answer').disabled, true);
});

test('out-of-date inputs without DOM events still cannot send a paid request', async () => {
  const f = await previewFixture(); await consent(f);
  f.el('query').value = 'changed behind the UI'; await f.fire('answer');
  assert.equal(paidCalls(f).length, 0);
  assert.match(f.el('notice').textContent, /重新免费预览/);
});

test('model unavailable, busy or exhausted blocks paid requests but allows free previews', async () => {
  for (const status of ['disabled', 'missing_key', 'busy', 'limit_reached']) {
    const f = await previewFixture(); f.control.model.status = status;
    await f.fire('model-refresh');
    assert.equal(f.el('external-consent').disabled, true);
    assert.match(f.el('review-preview').textContent, /FR001/);
    await consent(f); await f.fire('answer');
    assert.equal(paidCalls(f).length, 0);
  }
});

test('missing status endpoint fails closed and explains that server code must be restarted', async () => {
  const f = await previewFixture();
  f.routes.set('GET /generation-status', () => response({ detail: 'not found' }, 404));
  await f.fire('model-refresh');
  assert.match(f.el('model-status').textContent, /重启服务/);
  assert.equal(f.el('answer').disabled, true);
  assert.equal(paidCalls(f).length, 0);
});

test('malformed or mismatched previews cannot enable a model call', async () => {
  for (const overrides of [{ document_id: 'f'.repeat(32) }, { query: 'other' }, { source_sha256: 'e'.repeat(64) },
    { reviewed_context_id: 'not-a-version' }, { included_count: 2 }, { status: 'no_evidence' }]) {
    const f = await previewFixture(overrides);
    assert.equal(f.get('state.preview'), null);
    assert.equal(f.el('answer').disabled, true);
    assert.equal(paidCalls(f).length, 0);
    assert.match(f.el('notice').className, /error/);
  }
});

test('server refusal and blocked draft are shown distinctly, without invented claims', async () => {
  for (const status of ['refused', 'rejected']) {
    const f = await previewFixture();
    f.routes.set('POST /documents/' + f.packet.document_id + '/answer-reviewed', () => response(answerBody(f.packet, status)));
    await consent(f); await f.fire('answer');
    assert.match(f.el('answer-status').textContent, /已调用模型/);
    assert.doesNotMatch(f.el('answer-result').textContent, /z·conj\(w\)/);
    assert.match(f.el('answer-result').textContent, status === 'refused' ? /Only a conclusion/ : /已拦截/);
  }
});

test('API failure never retries and requires a fresh preview and consent', async () => {
  for (const status of [409, 429, 503, 504]) {
    const f = await previewFixture();
    f.routes.set('POST /documents/' + f.packet.document_id + '/answer-reviewed', () => response({ detail: 'request stopped' }, status));
    await consent(f); await f.fire('answer');
    assert.equal(paidCalls(f).length, 1);
    assert.match(f.el('answer-result').textContent, /没有自动重试/);
    assert.match(f.el('notice').textContent, new RegExp('HTTP ' + status));
    assert.equal(f.get('state.preview'), null);
    assert.equal(f.el('external-consent').checked, false);
    await f.fire('answer'); assert.equal(paidCalls(f).length, 1);
  }
});

test('network loss after submission is reported as potentially billed, not as a free refusal', async () => {
  const f = await previewFixture();
  f.routes.set('POST /documents/' + f.packet.document_id + '/answer-reviewed', () => { throw new Error('offline'); });
  await consent(f); await f.fire('answer');
  assert.equal(paidCalls(f).length, 1);
  assert.match(f.el('answer-result').textContent, /可能已收费/);
  assert.match(f.el('answer-status').textContent, /请求未完成/);
});

test('answer with a different context, claim or citation is not displayed', async () => {
  for (const mutate of [(body) => { body.reviewed_context_id = 'e'.repeat(64); },
    (body) => { body.check.claims[0].text = 'wrong unreviewed claim'; },
    (body) => { body.check.claims[0].citations[0].pdf_page_number = 99; },
    (body) => { body.generation.usage.total_tokens = 1; }]) {
    const f = await previewFixture(); const body = answerBody(f.packet); mutate(body);
    f.routes.set('POST /documents/' + f.packet.document_id + '/answer-reviewed', () => response(body));
    await consent(f); await f.fire('answer');
    assert.match(f.el('answer-status').textContent, /请求未完成/);
    assert.doesNotMatch(f.el('answer-result').textContent, /wrong unreviewed claim/);
    assert.equal(paidCalls(f).length, 1);
  }
});

test('paid request locks the UI and prevents duplicate submissions', async () => {
  const f = await previewFixture(); let complete;
  f.routes.set('POST /documents/' + f.packet.document_id + '/answer-reviewed', () => new Promise((resolve) => { complete = resolve; }));
  await consent(f); await f.fire('answer');
  assert.equal(f.get('state.busy'), true);
  assert.equal(f.el('query').disabled, true);
  await f.fire('answer'); assert.equal(paidCalls(f).length, 1);
  complete(response(answerBody(f.packet))); await tick();
  assert.equal(f.get('state.busy'), false);
});

test('reviewed and generated text remains inert and citations bind the PDF fingerprint', async () => {
  const f = await previewFixture();
  f.packet.evidence[0].reviewed_statement = '<script>alert(1)</script> conclusion';
  await f.fire('preview'); await consent(f); await f.fire('answer');
  assert.match(f.el('answer-result').textContent, /<script>alert\(1\)<\/script>/);
  assert.equal(f.el('answer-result').querySelectorAll('script').length, 0);
  const link = f.el('answer-result').querySelectorAll('a')[0];
  assert.equal(link.href, '/documents/' + f.packet.document_id + '/file?sha256=' + f.packet.source_sha256 + '#page=9');
});

test('example buttons fill input without sending requests or retaining old consent', async () => {
  const f = await previewFixture(); await consent(f); const count = f.calls.length;
  await f.fire('example-proof');
  assert.match(f.el('query').value, /完整证明/);
  assert.equal(f.calls.length, count);
  assert.equal(f.get('state.preview'), null);
  assert.equal(f.el('external-consent').checked, false);
  await f.fire('example-condition');
  assert.equal(f.el('query').value, '复数三角不等式在什么条件下取等号？');
  assert.equal(f.calls.length, count);
});
