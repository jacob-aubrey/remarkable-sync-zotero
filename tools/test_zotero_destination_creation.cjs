const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const rows = new Map(); let saves = 0, editable = true;
class Collection {
  constructor() {this.libraryID = 1; this.deleted = false; this.loaded = false;}
  async loadPrimaryData() {this.loaded = true;}
  set name(value) {if (this.key && !this.loaded) throw Error('primaryData not loaded'); this._name = value;}
  get name() {return this._name;}
  async save() {saves++; rows.set(this.key, this);}
  toJSON() {return {key: this.key, name: this.name, parentCollection: this.parentKey || false};}
}
const parent = Object.assign(new Collection(), {loaded: true, key: 'PARENT23', name: 'Literature'}); rows.set(parent.key, parent);
const context = vm.createContext({notableEndpoints: {}, Zotero: {
  Libraries: {userLibraryID: 1, get: () => ({editable})},
  Collections: {getByLibraryAndKeyAsync: async (library, key) => rows.get(key), getByLibrary: () => [...rows.values()]},
  DB: {executeTransaction: async fn => fn()}, Collection,
}});
vm.runInContext(fs.readFileSync('zotero-support/library.js', 'utf8'), context);
context.notableRegisterLibraryEndpoint(class {});
const endpoint = new context.notableEndpoints['/api/notable/action']();
const request = async (data = {}) => {
 const result = await endpoint.run({headers: new Map([['Zotero-Server-ID', 'synthetic']]),
 data: {action: 'create_collection', key: 'CREATE23', parent: 'PARENT23', name: 'New collection', ...data}});
 return {status: result[0], value: JSON.parse(result[2])};
};
async function main() {
 const first = await request(); assert.equal(first.status, 200); assert.equal(first.value.collection.parentCollection, 'PARENT23');
 const retry = await request(); assert.equal(retry.status, 200); assert.equal(saves, 1, 'A lost response must not create twice');
 const sibling = await request({key: 'CREATE24'}); assert.equal(sibling.value.collection.key, 'CREATE23'); assert.equal(saves, 1);
 assert.equal((await request({name: 'Wrong name'})).status, 409);
 assert.equal((await request({parent: 'MISSING2'})).status, 409);
 assert.equal((await request({name: 'Bad/Name'})).status, 400);
 editable = false; assert.equal((await request()).status, 403); editable = true;
 rows.get('PARENT23').deleted = true; assert.equal((await request()).status, 409);
 assert.equal(saves, 1);
 console.log('PASS: native collection creation, parent and editable-library guards, sibling reuse, stable retry keys, and no document writes');
}
main().catch(error => {console.error(error); process.exitCode = 1;});
