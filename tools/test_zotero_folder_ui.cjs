// Real controller adapters; component tree/keyboard behavior has its own harness.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const {fixture:actionFixture}=require('./test_zotero_ui_recovery.cjs');
const plain=x=>JSON.parse(JSON.stringify(x));
function fixture(){
 const configs=[],buttons=[],doc={defaultView:{Event:class{constructor(type){this.type=type;}}}};
 function node(tag,text=''){return {tag,textContent:text,children:[],dataset:{},listeners:{},ownerDocument:doc,isConnected:true,
  appendChild(x){x.parentElement=this;this.children.push(x);return x;},replaceChildren(){this.children=[];},
  addEventListener(type,fn){(this.listeners[type]||=[]).push(fn);},dispatchEvent(e){for(const f of this.listeners[e.type]||[])f(e);},
  focus(){doc.activeElement=this;},close(){},showModal(){},remove(){this.isConnected=false;},
  get options(){return this.children.filter(x=>x.tag==='option');},get selectedOptions(){return this.options.filter(x=>x.selected);},
  get value(){return this.tag==='select'?this.selectedOptions[0]?.value||(!this.multiple?this.options[0]?.value||'':''):this._value||'';},
  set value(v){if(this.tag==='select')for(const o of this.options)o.selected=o.value===v;else this._value=v;}};}
 doc.body=node('body');
 const context=vm.createContext({NotableFolderChooser:{create(c){configs.push(c);const x=node('div');let values=Array.isArray(c.value)?c.value:[c.value];Object.defineProperties(x,{value:{get:()=>values[0]||'',set:v=>{values=Array.isArray(v)?v:[v];}},selectedOptions:{get:()=>values.map(value=>({value}))}});x.setOptions=o=>x.choices=o;x.choices=c.options;c.parent.appendChild(x);return x;}},setTimeout,clearTimeout,setInterval,clearInterval});
 vm.runInContext(fs.readFileSync('zotero-support/ui.js','utf8'),context);const ui=context.NotableUI;
 ui.latest={config:{connection_id:'C',zotero_root:'ROOT',tablet_root:'TR'},collections:[{key:'ROOT',name:'Literature'},{key:'ONE',name:'Methods',parentCollection:'ROOT'},{key:'TWO',name:'Silanes',parentCollection:'ONE'}],mappings:{ROOT:{cloud:'TR'},ONE:{cloud:'TO',zparent:'ROOT'},TWO:{cloud:'TT',zparent:'ONE'}},reviews:[],links:[]};
 ui.folderCatalog={collections:ui.latest.collections,folders:[{id:'TR',name:'Tablet papers'},{id:'TO',name:'Lab',parent:'TR'},{id:'TT',name:'Silanes',parent:'TO'}]};
 ui.view={document:doc,closed:false};ui.stopped=false;
 ui.el=(tag,text,parent,attrs={})=>{const x=node(tag,text);Object.assign(x,attrs);parent?.appendChild(x);return x;};
 ui.button=(text,parent,click)=>{const x=ui.el('button',text,parent);x.click=click;buttons.push(x);return x;};
 ui.refreshCollectionNames=()=>{};return {ui,doc,configs,buttons};
}
const walk=x=>[x,...x.children.flatMap(walk)];
async function main(){
 {const {ui,doc,configs}=fixture();const input=ui.folderSelect('Destination',doc.body,[['ROOT','Literature'],['ONE','Literature / Methods'],['TWO','Literature / Methods / Silanes']],'TWO',false,'paired');
  assert.equal(input.parentElement.hidden,true);assert.equal(configs[0].options[2].label,'Silanes');assert.equal(configs[0].options[2].parent,'ONE');assert.match(configs[0].options[2].detail,/reMarkable: Tablet papers.*Silanes/);
  let changes=0,inputs=0;input.addEventListener('change',()=>changes++);input.addEventListener('input',()=>inputs++);input.notableChooser.value='ONE';input.notableChooser.dispatchEvent({type:'change'});assert.equal(input.value,'ONE');assert.equal(changes,1);assert.equal(inputs,1);
  ui.latest.collections[1].name='Renamed';ui.reconcileOptions(input,[['ROOT','Literature'],['ONE','Literature / Renamed'],['TWO','Literature / Renamed / Silanes']]);assert.equal(input.value,'ONE');assert.equal(input.notableChooser.value,'ONE');assert.equal(input.notableChooser.choices[1].label,'Renamed');}
 {const {ui,doc}=fixture();const input=ui.folderSelect('Research',doc.body,[['ONE','Methods'],['TWO','Silanes']],['ONE'],true);input.notableChooser.value=['ONE','TWO'];input.notableChooser.dispatchEvent({type:'change'});assert.deepEqual(Array.from(input.selectedOptions,o=>o.value),['ONE','TWO']);ui.reconcileOptions(input,[['ONE','Methods'],['TWO','Silanes'],['NEW','New']]);assert.deepEqual(Array.from(input.notableChooser.selectedOptions,o=>o.value),['ONE','TWO']);}
 {const {ui,doc,configs}=fixture();const input=ui.folderSelect('Collection',doc.body,[['ROOT','Literature'],['ONE','Literature / Methods']],'ONE');ui.newFolderDialog=async(kind,parent)=>{assert.equal(kind,'zotero');assert.equal(parent,'ONE');ui.latest.collections.push({key:'NEW',name:'Topic',parentCollection:'ONE'});return {key:'NEW',complete:true};};const result=await configs[0].onNew('ONE');assert.equal(result.value,'NEW');assert.equal(result.parent,'ONE');assert.equal(input.value,'ONE');assert.ok(input.options.some(o=>o.value==='NEW'));ui.newFolderDialog=async()=>null;assert.equal(await configs[0].onNew('ONE'),undefined);assert.equal(input.value,'ONE');}
 {const {ui,doc,buttons}=fixture();let calls=0;ui.action=async()=>calls++;ui.reviewDrafts.set('p',{filename:'Edited',collection:'ONE'});const before=plain([...ui.reviewDrafts]);const pending=ui.newFolderDialog('zotero','ONE');await buttons.find(x=>x.textContent==='Cancel').click();assert.equal(await pending,null);assert.equal(calls,0);assert.deepEqual(plain([...ui.reviewDrafts]),before);assert.equal(doc.body.children.at(-1).isConnected,false);}
 {const {ui,doc,buttons}=fixture();const calls=[];ui.reviewDrafts.set('p',{filename:'Edited',collection:'ONE'});const before=plain([...ui.reviewDrafts]);ui.render=()=>{throw Error('Replaced filing form');};ui.action=async(method,params,render)=>{calls.push({method,params,render});return {created_destination:{key:'NEW',complete:true},destination_catalog:ui.folderCatalog};};const pending=ui.newFolderDialog('paired','ONE');walk(doc.body).find(x=>x.dataset.notableField==='Collection name').value='Topic';walk(doc.body).find(x=>x.dataset.notableField==='reMarkable folder name (optional; defaults to collection name)').value='Tablet topic';await buttons.find(x=>x.textContent==='Create').click();assert.equal((await pending).key,'NEW');assert.deepEqual(plain(calls),[{method:'create_destination',params:{kind:'paired',parent:'ONE',name:'Topic',tablet_name:'Tablet topic'},render:false}]);assert.deepEqual(plain([...ui.reviewDrafts]),before);}
 {const {ui,doc,buttons}=fixture();const requests=[];let count=0;
  ui.action=async(method,params)=>{requests.push(plain(params));count++;return {created_destination:count===1
    ? {key:'NEW',complete:false,locations:{zotero:'ready',tablet:'pending'},error:'Retry safely'}
    : {key:'NEW',complete:true},destination_catalog:ui.folderCatalog};};
  const pending=ui.newFolderDialog('paired','ONE');
  walk(doc.body).find(x=>x.dataset.notableField==='Collection name').value='Topic';
  const create=buttons.find(x=>x.textContent==='Create');await create.click();
  const dialog=doc.body.children.at(-1);assert.equal(dialog.isConnected,true);
  assert.match(walk(dialog).map(x=>x.textContent).join(' '),/Zotero collection is ready.*reMarkable folder is not yet confirmed/);
  await create.click();assert.equal((await pending).key,'NEW');assert.deepEqual(requests[0],requests[1]);}
 {const {ui,doc,buttons}=fixture();const body=doc.body;doc.body=null;doc.querySelector=selector=>selector==='body'?body:null;const pending=ui.newFolderDialog('zotero','ONE');assert.ok(body.children.some(node=>node.tag==='dialog'),'XMLDocument body fallback must attach creation modal');await buttons.find(x=>x.textContent==='Cancel').click();assert.equal(await pending,null);}
 {const {ui}=actionFixture();let drain=0;const calls=[];ui.filingQueue=[{id:'saved'}];ui.reviewDrafts.set('p',{filename:'Edited'});const before=plain([...ui.reviewDrafts]);ui.drainFilings=async()=>drain++;ui.rpc=async method=>{calls.push(method);return {...ui.latest,created_destination:{key:'NEW',complete:true}};};await ui.action('create_destination',{kind:'zotero',parent:'ONE',name:'Topic'},false);assert.deepEqual(calls,['create_destination']);assert.equal(drain,0);assert.deepEqual(plain([...ui.reviewDrafts]),before);assert.deepEqual(plain(ui.filingQueue),[{id:'saved'}]);}
 console.log('PASS: folder adapters, refresh, input/change events, multi-select, creation/cancel draft preservation, and queued filing isolation');
}
main().catch(e=>{console.error(e);process.exitCode=1;});
