const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const context = {}; vm.createContext(context); vm.runInContext(fs.readFileSync('zotero-support/ui.js','utf8'), context);
const ui=context.NotableUI;
const row={payload:{title:'Printed article title',filename:'Author_Journal_2026.pdf',document_type:'journal_article',previous_proposal:{title:'nm5c00178 1..7',filename:'nm5c00178.pdf',document_type:'unknown'}}};
const folderOnlyDraft={title:'nm5c00178 1..7',filename:'nm5c00178.pdf',document_type:'unknown',collection:'SAVED_FOLDER'};
for(const field of ['title','filename','document_type']) assert.equal(ui.reviewProposalValue(row,folderOnlyDraft,field),row.payload[field]);
assert.equal(folderOnlyDraft.collection,'SAVED_FOLDER');
for(const field of ['title','filename','document_type']) {
 const manual={...folderOnlyDraft,[field]:'My manual value'};
 assert.equal(ui.reviewProposalValue(row,manual,field),'My manual value');
 const deliberate={...folderOnlyDraft,edited_fields:[field]};
 assert.equal(ui.reviewProposalValue(row,deliberate,field),folderOnlyDraft[field]);
}
const savedBaseline={...folderOnlyDraft,proposal_baseline:row.payload.previous_proposal};
const newer={payload:{title:'New title',filename:'Template_2026.pdf',document_type:'journal_article'}};
assert.equal(ui.reviewProposalValue(newer,savedBaseline,'filename'),'Template_2026.pdf');
assert.equal(ui.reviewProposalValue(newer,{filename:'Manual.pdf'},'filename'),'Manual.pdf');
console.log('PASS: refreshed automatic proposals update untouched fields, retain destination drafts, and preserve explicit and legacy manual edits');
