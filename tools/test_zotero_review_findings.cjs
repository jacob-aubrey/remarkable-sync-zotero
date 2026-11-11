// Presentation uses structured identities; inspection never queues a decision.
const assert = require('node:assert/strict');
const {viewFixture} = require('./test_zotero_ui_recovery.cjs');
function text(node) { return [node.textContent, ...node.children.map(text)].join(' '); }

async function main() {
  const typeNames = {journalArticle: 'Journal Article', standard: 'Standard'};
  const {ui, context, buttons, node} = viewFixture({Zotero: {ItemTypes: {getLocalizedString: type => typeNames[type]}}}), navigations = [], operations = [];
  context.notableNavigateReview = async value => { navigations.push(value); return {navigated: true}; };
  ui.action = async (...args) => operations.push(args);
  const refs = [{key: 'PARENT23', title: 'First reference', date: '2024', DOI: '10.1000/fixture', itemType: 'journalArticle'},
    {key: 'OTHER234', title: 'Second reference', date: '2024', itemType: 'journalArticle'}];
  const attachments = [{key: 'SOURCE23', parent: 'PARENT23', title: 'Full Text', filename: 'Article.pdf', match_basis: 'current_bytes'},
    {key: 'SOURCE24', parent: 'OTHER234', title: 'Annotated article', filename: 'Annotated.pdf', match_basis: 'original_baseline'}];
  const row = {id: 'one', status: 'pending', digest: 'original', payload: {findings: [
    {code: 'repeated_pdf_content', domain: 'library', severity: 'decision', summary: 'A shared original appears under two references',
      explanation: 'At least one match uses a retained original PDF baseline; current annotated files may differ. Matching PDF content does not establish that the references, notes or Zotero annotations are interchangeable.', next_step: 'Inspect both references in Zotero.',
      evidence: ['The retained original matches.'], references: refs, attachments},
    {code: 'possible_duplicate_references', domain: 'library', severity: 'decision', summary: 'These references may describe the same work',
      explanation: 'This is a candidate, not proof.', next_step: 'Use Zotero to compare the references.', references: refs}
  ]}};
  const before = JSON.stringify({row, queue: ui.filingQueue, drafts: [...ui.reviewDrafts]});
  const panel = node(); ui.renderReviewFindings(row, panel);
  assert.match(text(panel), /Matching PDF copies/);
  assert.match(text(panel), /Item Type: Journal Article/);
  assert.match(text(panel), /right-click the selected references.*Merge Items/);
  assert.match(buttons.get('Compare in Zotero')[0].dataset.buttonHelp, /Merge Items/);
  assert.match(text(panel), /Why these may be duplicates/);
  assert.doesNotMatch(text(panel), /The same PDF is attached|Evidence and keep-separate|Why this was flagged/);
  assert.doesNotMatch(text(panel), /Inspect both references in Zotero|Use Zotero to compare the references/);
  assert.match(text(panel), /references, notes, or Zotero annotations/);
  assert.doesNotMatch(text(panel), /references, notes or Zotero annotations/);
  assert.match(text(panel), /current annotations or PDF bytes may differ/);
  assert.match(text(panel), /Zotero annotation records still belong/);
  assert.equal(buttons.get('Compare in Zotero').length, 1, 'Related findings share one participant navigation section');
  assert.equal(buttons.get('Show this reference').length, 2, 'Each exact reference is individually navigable');
  assert.equal(buttons.get('Show attachment in Zotero').length, 2);
  assert.equal(buttons.get('Open PDF in Zotero').length, 2);
  await buttons.get('Compare in Zotero')[0].click();
  await buttons.get('Show attachment in Zotero')[1].click();
  await buttons.get('Open PDF in Zotero')[0].click();
  assert.deepEqual(JSON.parse(JSON.stringify(navigations)), [
    {target: 'references', keys: ['PARENT23', 'OTHER234']},
    {target: 'attachment', keys: ['SOURCE24']}, {target: 'pdf', keys: ['SOURCE23']}
  ]);
  assert.equal([...buttons.keys()].some(label => /Apply (Zotero|reMarkable)/.test(label)), false, 'Library issues have no fictional tablet winner');
  assert.deepEqual(operations, []);
  assert.equal(JSON.stringify({row, queue: ui.filingQueue, drafts: [...ui.reviewDrafts]}), before);

  // Connection ownership must be visible before choosing a native merge master.
  refs.forEach(reference => { reference.itemType = 'journalArticle'; });
  ui.latest.links = [{parent: 'PARENT23', view_key: 'SOURCE23', name: 'Tablet article'}];
  const linkedPanel = node(); ui.renderReviewFindings(row, linkedPanel);
  assert.match(text(linkedPanel), /Linked to reMarkable/);
  assert.match(text(linkedPanel), /Tablet document: Tablet article/);
  assert.match(text(linkedPanel), /PDF used by this link: Full Text/);
  assert.match(text(linkedPanel), /Keep the reference marked “Linked to reMarkable” as the survivor/);
  assert.doesNotMatch(text(linkedPanel), /No reMarkable link in this connection/);
  const explanation = linkedPanel.children[0].children.find(child => child.tag === 'details');
  assert.equal(explanation.open, undefined, 'Repeated library warnings start collapsed');
  ui.reviewStale = true;
  const stalePanel = node(); ui.renderReviewFindings(row, stalePanel);
  assert.match(text(stalePanel), /Zotero changed.*Check my changes before deciding whether to merge/);
  assert.doesNotMatch(text(stalePanel), /Merge Items/);
  assert.doesNotMatch(buttons.get('Compare in Zotero').at(-1).dataset.buttonHelp, /Merge Items/);
  assert.doesNotMatch(text(stalePanel), /Keep the reference marked “Linked to reMarkable” as the survivor/);
  ui.reviewStale = false;
  delete refs[1].itemType;
  const unknownType = node(); ui.renderReviewFindings(row, unknownType);
  assert.doesNotMatch(text(unknownType), /Keep the reference marked/);
  assert.match(text(unknownType), /Item type unavailable/);
  assert.match(text(unknownType), /Item types are unavailable.*Recheck Zotero before merging/);
  assert.doesNotMatch(text(unknownType), /Merge Items/);
  assert.doesNotMatch(buttons.get('Compare in Zotero').at(-1).dataset.buttonHelp, /Merge Items/);
  refs[1].itemType = 'journalArticle';
  ui.latest.links.push({parent: 'OTHER234', name: 'Another tablet article'});
  const twoLinked = node(); ui.renderReviewFindings(row, twoLinked);
  assert.match(text(twoLinked), /automatic reassignment of sync links after a merge is not supported/);
  assert.doesNotMatch(text(twoLinked), /Keep the reference marked “Linked to reMarkable” as the survivor/);

  // A remembered missing source must not look like a second current PDF.
  {
    const f = viewFixture(), missing = {id: 'missing', status: 'pending', payload: {findings: [{
      code: 'local_review_unavailable', domain: 'file', severity: 'repair', summary: 'Saved source unavailable',
      references: [refs[0]], attachments: [
        {key: 'SOURCE23', title: 'Live copy', filename: 'Live.pdf', availability: 'current'},
        {key: 'REMOVED2', title: 'Saved source attachment', filename: 'Removed.pdf', availability: 'missing'}]}]}};
    const view = f.node(); f.ui.renderReviewFindings(missing, view, true);
    assert.match(text(view), /A saved attachment is unavailable/);
    assert.match(text(view), /Why this needs attention/);
    assert.match(text(view), /Attachments: 1 current · 1 unavailable saved record/);
    assert.match(text(view), /Unavailable in Zotero · saved review record/);
    assert.equal(f.buttons.get('Show attachment in Zotero').length, 1, 'Missing saved records must not expose live attachment navigation');
    assert.equal(f.buttons.get('Open PDF in Zotero').length, 1, 'Missing saved records must not expose PDF-open actions');
    missing.payload.findings[0].attachments[1].availability = 'trashed';
    assert.equal(f.ui.reviewProblemDescriptor(missing), 'A saved attachment is in Zotero Trash');
    assert.notEqual(f.ui.buttonIcons['Sync Conflicts'], f.ui.buttonIcons['Sync now'], 'Sync Conflicts must have a distinct icon');
  }

  const comparison = node();
  ui.latest.config.zotero_root = 'ROOT2345';
  ui.latest.collections = [{key: 'ROOT2345', name: 'Research', parentCollection: false},
    {key: 'HOME2345', name: 'Reading', parentCollection: 'ROOT2345'}];
  ui.latest.mappings = {ROOT2345: {cloud: 'tablet-root', root: true}, HOME2345: {cloud: 'tablet-home', name: 'Reading', zparent: 'ROOT2345', cparent: 'tablet-root'}};
  ui.latest.config.tablet_path = 'Tablet papers';
  ui.renderConflictComparison({payload: {type: 'paper', desktop: ['Desktop name', 'HOME2345'], tablet: ['Tablet name', 'HOME2345']}}, comparison);
  const output = text(comparison);
  assert.match(output, /Desktop name/); assert.match(output, /Tablet name/);
  assert.match(output, /Research \/ Reading/); assert.match(output, /Tablet papers \/ Reading/);
  assert.equal(output.includes('HOME2345'), false, 'Present folder paths rather than internal IDs');
  assert.match(output, /Current names and folders/);
  assert.doesNotMatch(output, /Why am I seeing this/);
  assert.match(ui.buttonHelp('Apply Zotero name and location'), /Check both devices.*now.*PDF contents and notes are kept/);

  // Each visible keep-separate choice retains its exact evidence scope.
  {
    const f = viewFixture(), calls = [], copy = JSON.parse(JSON.stringify(row));
    copy.payload.review_token = 'review-token';
    copy.payload.findings.forEach((finding, index) => { finding.acknowledgement = {eligible: true, acknowledged: false, token: 'scope-' + index}; });
    f.ui.action = async (...args) => calls.push(args);
    const view = f.node(); f.ui.renderReviewFindings(copy, view, true);
    const all = node => [node, ...node.children.flatMap(all)];
    const why = all(view).find(node => node['data-review-detail'] === 'finding-details');
    assert.ok(why && why.open !== true, 'Supporting facts start collapsed');
    assert.equal(all(why).filter(node => node.tag === 'details').length, 1, 'Facts do not contain another evidence dropdown');
    assert.doesNotMatch(text(why), /Keep these .* separate/, 'Decisions must be visible outside the explanation');
    assert.doesNotMatch(text(view), /Compare these entries in Zotero, then check your changes here/);
    await f.buttons.get('Keep these PDFs separate')[0].click();
    await f.buttons.get('Keep these references separate')[0].click();
    assert.deepEqual(JSON.parse(JSON.stringify(calls)), [
      ['acknowledge_review_finding', {identity: 'one', digest: 'original', code: 'repeated_pdf_content', token: 'scope-0', review_token: 'review-token', keep_separate: true}],
      ['acknowledge_review_finding', {identity: 'one', digest: 'original', code: 'possible_duplicate_references', token: 'scope-1', review_token: 'review-token', keep_separate: true}]
    ]);
    assert.equal(copy.status, 'pending'); assert.equal(f.ui.filingQueue.length, 0);
    copy.payload.findings.push({code: 'local_file_unavailable', severity: 'repair', domain: 'file', explanation: 'The file is missing.'});
    assert.equal(f.ui.reviewProblemDescriptor(copy), 'Local file unavailable', 'A file repair takes precedence over a possible duplicate');
    assert.equal(f.ui.reviewDetailsLabel(copy), 'Why this needs attention');
  }

  // PDF equality does not make differently classified references mergeable.
  {
    const lookups = [], navigation = [], writes = [];
    const nativeNames = {journalArticle: 'Journal Article', standard: 'Standard'};
    const f = viewFixture({Zotero: {ItemTypes: {getLocalizedString(type) {
      lookups.push(type);
      if (!(type in nativeNames)) throw Error('No native localized label');
      return nativeNames[type];
    }}}});
    f.context.notableNavigateReview = async params => { navigation.push(params); return {navigated: true}; };
    f.ui.action = async (...args) => writes.push(args);
    f.ui.latest.links = [{parent: 'OTHER234', view_key: 'SOURCE24', name: 'Linked notes copy'}];
    const mixed = JSON.parse(JSON.stringify(row));
    const setType = (key, type) => {
      for (const finding of mixed.payload.findings) for (const reference of finding.references || []) {
        if (reference.key === key) {
          if (type === undefined) delete reference.itemType;
          else reference.itemType = type;
        }
      }
    };
    setType('OTHER234', 'standard');
    const before = JSON.stringify({mixed, links: f.ui.latest.links, queue: f.ui.filingQueue});
    const mismatch = f.node(); f.ui.renderReviewFindings(mixed, mismatch, true);
    const body = text(mismatch);
    assert.match(body, /Item Type: Journal Article/);
    assert.match(body, /Item Type: Standard/);
    assert.match(body, /Different item types.*same publication.*correct Item Type in Zotero before merging/);
    assert.doesNotMatch(body, /Merge Items|Before merging in Zotero|Keep the reference marked/);
    const compare = f.buttons.get('Compare in Zotero').at(-1);
    assert.doesNotMatch(compare.dataset.buttonHelp, /Merge Items/);
    assert.match(body, /Linked to reMarkable/);
    assert.match(body, /Used by a reMarkable link/);
    assert.match(body, /Keep the linked PDF and its notes/);
    assert.match(body, /PDF used by this link: Annotated article/);
    await compare.click();
    assert.deepEqual(JSON.parse(JSON.stringify(navigation)), [{target: 'references', keys: ['PARENT23', 'OTHER234']}]);
    assert.deepEqual(writes, [], 'Comparison never converts item types, merges references, or approves papers');
    assert.equal(JSON.stringify({mixed, links: f.ui.latest.links, queue: f.ui.filingQueue}), before, 'Guidance preserves records, connections, and approvals');
    assert.ok(lookups.includes('journalArticle') && lookups.includes('standard'), 'Known type names come from Zotero localization');

    // After a native correction and recheck, the usual merge workflow returns.
    setType('OTHER234', 'journalArticle');
    const corrected = f.node(); f.ui.renderReviewFindings(mixed, corrected, true);
    assert.match(text(corrected), /right-click the selected references.*Merge Items/);
    assert.match(f.buttons.get('Compare in Zotero').at(-1).dataset.buttonHelp, /Merge Items/);
    assert.match(text(corrected), /Keep the reference marked.*Linked to reMarkable.*as the survivor/);
    assert.doesNotMatch(text(corrected), /Different item types/);

    // A missing label never silently means that the known type matches it.
    setType('OTHER234', undefined);
    const unknown = f.node(); f.ui.renderReviewFindings(mixed, unknown, true);
    assert.match(text(unknown), /Item type unavailable/);
    assert.match(text(unknown), /Item types are unavailable.*Recheck Zotero before merging/);
    assert.doesNotMatch(text(unknown), /Merge Items/);
    assert.doesNotMatch(f.buttons.get('Compare in Zotero').at(-1).dataset.buttonHelp, /Merge Items/);

    // Missing localization is readable, rather than exposing an internal ID.
    setType('PARENT23', 'conferencePaper'); setType('OTHER234', 'conferencePaper');
    const fallback = f.node(); f.ui.renderReviewFindings(mixed, fallback, true);
    assert.match(text(fallback), /Item Type: Conference Paper/);
    assert.doesNotMatch(text(fallback), /Item Type: conferencePaper/);
    assert.match(text(fallback), /Merge Items/);

    // Freshness takes precedence over a saved classification or merge advice.
    f.ui.reviewStale = true;
    const stale = f.node(); f.ui.renderReviewFindings(mixed, stale, true);
    assert.match(text(stale), /Zotero changed.*Check my changes before deciding whether to merge/);
    assert.doesNotMatch(text(stale), /Merge Items|Keep the reference marked/);
    assert.doesNotMatch(f.buttons.get('Compare in Zotero').at(-1).dataset.buttonHelp, /Merge Items/);
    assert.deepEqual(writes, []);
  }
  console.log('PASS: structured library guidance, exact participant navigation, shared context, evidence provenance, unchanged decisions, and readable conflict paths');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
