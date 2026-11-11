"""Native shared-chooser QA in a disposable Zotero profile on an inactive desktop."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import test_zotero_plugin_live as live

COMMANDS = r'''
          else if (command.action === 'folder_fresh_stop') {
            clearTimeout(ui.timer);clearTimeout(ui.inboxTimer);
            if(ui.process) await ui.closeProcess(ui.process,true,'synthetic_folder_catalog_restart');
            ui.view?.close();ui.view=null;
          }
          else if (command.action === 'folder_fresh_inbox') {
            const ensure=(ok,message)=>{if(!ok)throw Error(message);};
            clearTimeout(ui.timer);clearTimeout(ui.inboxTimer);
            const raw=await ui.rpc('status');
            ensure(raw.collections.length===0,'Fresh worker must really return an empty transient collection catalog');
            ensure(Object.keys(raw.mappings).length===3,'Saved mapped folders were not loaded from real worker state');
            if(!ui.syntheticTagRPCOriginal) {
              ui.syntheticTagRPCOriginal=ui.rpc;ui.syntheticTagRequests=[];ui.syntheticTagEvents=[];
              ui.rpc=async function(method,params={}) {ui.syntheticTagRequests.push({method,params});return ui.syntheticTagRPCOriginal.call(this,method,params);};
              ui.syntheticTagObserver=Zotero.Notifier.registerObserver({notify(event,type,ids){if(type==='item')ui.syntheticTagEvents.push({event,type,ids});}},['item'],'synthetic-auto-tag-native');
            }
            ui.latest=raw;ui.collectionsCurrent=false;delete ui.nativeCollections;ui.folderCatalog=null;
            ui.requestedTab='inbox';ui.open();
            const until=Date.now()+10000;
            while((!ui.view?.document.querySelector('h1') || ui.busy) && Date.now()<until) await Zotero.Promise.delay(20);
            await ui.refresh();ui.tab='inbox';ui.selected=ui.latest.reviews[0].id;ui.render();
            ensure(ui.collectionsCurrent===true,'Native collection catalog was not hydrated');
            const first=ui.view.document.querySelector('select[data-folder-destination]');
            ensure(first.options.length===3,'Freshly reopened Inbox discarded persisted destinations');
            const roundtrip=await ui.rpc('status');ensure(roundtrip.collections.length===0,'Worker unexpectedly supplied a hydrated native catalog');
            await ui.checkInbox();ui.render();
            const doc=ui.view.document,backing=doc.querySelector('select[data-folder-destination]');
            ensure(backing.options.length===3,'Intake status erased the native collection catalog');
            const research=[...doc.querySelectorAll('select')].find(n=>n.dataset.notableField==='Research collections (optional)');
            ensure(research.options.length>=4,'Optional research collection picker omitted existing mapped collections');
            const filename=doc.querySelector('input[data-notable-field="Proposed filename"]');filename.value='My retained folder draft.pdf';filename.dispatchEvent(new ui.view.Event('input'));ui.syntheticFolderFilename=filename;
            ui.view.resizeTo(command.width,760);await Zotero.Promise.delay(100);
            backing.notableChooser.open();
            const dialog=doc.querySelector('.folder-chooser-dialog[open]');
            ensure(dialog.querySelectorAll('[role="treeitem"]').length>0,'Blank-query Browse folders is empty');
            let rootRow=dialog.querySelector('[role="treeitem"][data-value="FCDRT234"]');
            ensure(rootRow,'Actual native root is absent from Browse folders');
            if(rootRow.getAttribute('aria-expanded')==='true') rootRow.querySelector('.folder-chooser-expand').click();
            rootRow=dialog.querySelector('[role="treeitem"][data-value="FCDRT234"]');
            ensure(rootRow.getAttribute('aria-expanded')==='false' && !dialog.querySelector('[role="treeitem"][data-value="FCDMN234"]'),'Collapsed native root still shows its child');
            rootRow.querySelector('.folder-chooser-expand').click();
            rootRow=dialog.querySelector('[role="treeitem"][data-value="FCDRT234"]');
            ensure(rootRow.getAttribute('aria-expanded')==='true' && !!dialog.querySelector('[role="treeitem"][data-value="FCDMN234"]'),'Expanding native root does not reveal its actual child');

            const search=dialog.querySelector('input[type="search"]'),button=dialog.querySelector('.folder-chooser-toolbar > button');
            const searchRect=search.getBoundingClientRect(),buttonRect=button.getBoundingClientRect(),dialogRect=dialog.getBoundingClientRect();
            response.gap=buttonRect.left-searchRect.right;ensure(response.gap>=12,'Search and New controls are crowded');
            ensure(dialogRect.left>=0 && dialogRect.right<=ui.view.innerWidth+1,'Chooser overflows viewport');
            search.value='nanoparticle';search.dispatchEvent(new ui.view.Event('input'));
            ensure([...dialog.querySelectorAll('[role="treeitem"]')].some(n=>n.dataset.value==='FCDSN234'),'Search cannot find real nested nanoparticle collection');
            response.nativeRootBrowse='collapsed and expanded';
            response.rawWorkerCollections=roundtrip.collections.length;response.visibleCollections=ui.latest.collections.length;
            response.mappedOptions=backing.options.length;response.researchOptions=research.options.length;
            response.nativeKeys=Zotero.Collections.getByLibrary(Zotero.Libraries.userLibraryID,true).map(row=>row.key);
          }
          else if (command.action === 'folder_auto_tags') {
            const ensure=(ok,message)=>{if(!ok)throw Error(message);};
            const doc=ui.view.document,row=ui.latest.reviews[0],scope={identity:row.id};
            const entry=ui.paperTagEntry(scope),deadline=Date.now()+20000;
            while(['waiting','loading'].includes(entry.state) && Date.now()<deadline) {
              ensure(!ui.busy,'Automatic tags activated global busy state');
              ensure(doc.getElementById('activity').hidden,'Automatic tags displayed a loading panel');
              await Zotero.Promise.delay(25);
            }
            ensure(entry.state==='ready','Actual automatic tags did not become ready: '+entry.state+' '+entry.error);
            ensure(entry.proposal.suggestions.length>0,'Scientific PDF produced no actual topic suggestions');
            ensure(![...doc.querySelectorAll('button')].some(node=>node.textContent==='Find keywords and suggest tags'),'Manual preparation button still appears');
            const panel=doc.querySelector('.paper-tag-proposal');panel.open=true;
            const wanted=entry.proposal.suggestions[0].tag;
            const checkbox=[...panel.querySelectorAll('input[type="checkbox"]')].find(input=>input.closest('label').textContent===wanted);
            ensure(checkbox,'Actual tag proposal checkbox missing');checkbox.checked=true;checkbox.dispatchEvent(new ui.view.Event('change'));
            let filename=doc.querySelector('input[data-notable-field="Proposed filename"]');filename.value='My automatic-tag draft.pdf';filename.dispatchEvent(new ui.view.Event('input'));
            ui.render();await ui.checkInbox();ui.render();
            const current=ui.paperTagEntry(scope);
            ensure(current.draft.selected.has(wanted),'Checked tag choice was lost during render/intake');
            filename=doc.querySelector('input[data-notable-field="Proposed filename"]');ensure(filename.value==='My automatic-tag draft.pdf','Typed filename was lost during automatic tags/intake');
            ensure(ui.syntheticTagEvents.length===0,'Automatic suggestions wrote native Zotero items or tags');
            const calls=ui.syntheticTagRequests.filter(request=>request.method==='suggest_paper_tags');
            ensure(calls.length>0 && calls.every(request=>request.params.quiet===true),'Actual tag RPC did not use quiet preparation');
            ensure(!ui.syntheticTagRequests.some(request=>['save_paper_tags','approve','approve_routine','sync'].includes(request.method)),'Automatic tags performed an unapproved write');
            ensure(!ui.busy && doc.getElementById('activity').hidden,'Automatic tag processing left a loading indicator');
            response.tags={state:current.state,keywords:entry.proposal.keywords,suggestions:entry.proposal.suggestions,selected:[...current.draft.selected],requests:calls,nativeItemEvents:ui.syntheticTagEvents,filename:filename.value};
            current.draft.open=true;
            const visiblePanel=doc.querySelector('.paper-tag-proposal');visiblePanel.open=true;
            await Zotero.Promise.delay(50);
            response.tagPanelTop=visiblePanel.getBoundingClientRect().top+(doc.querySelector('body')?.scrollTop || 0);

            Zotero.Notifier.unregisterObserver(ui.syntheticTagObserver);ui.syntheticTagObserver=null;
            ui.rpc=ui.syntheticTagRPCOriginal;ui.syntheticTagRPCOriginal=null;
          }
          else if (command.action === 'folder_naming_refresh') {
            const first=await ui.rpc('scan_inbox');await Zotero.Promise.delay(2200);
            const result=await ui.rpc('scan_inbox');
            const row=result.reviews.find(row=>row.kind==='inbox');
            if(!row) throw Error('Watched naming fixture was not rediscovered after settling');
            const payload=row.payload;
            if(payload.title!=='Gold nanoparticle contrast agents for spectral computed tomography') throw Error('Bundled worker used embedded publisher code as title: '+payload.title);
            if(payload.document_type!=='journal_article') throw Error('Bundled worker missed the printed journal evidence: '+payload.document_type);
            if(!payload.filename.endsWith('_2026.pdf') || payload.filename==='real-intake.pdf') throw Error('Bundled worker discarded configured naming template: '+payload.filename);
            response.naming={title:payload.title,document_type:payload.document_type,filename:payload.filename,metadata_title:'nm5c00178 1..7'};
            ui.latest=await ui.rpc('recheck_review');ui.refreshCollectionNames();
            ui.selected=row.id;ui.tab='inbox';ui.render();
          }
          else if (command.action === 'folder_prepare') {
            clearTimeout(ui.inboxTimer); clearTimeout(ui.timer);
            ui.latest = {...ui.latest, collections: Zotero.Collections.getByLibrary(Zotero.Libraries.userLibraryID, true)
              .map(row => ({key: row.key, name: row.name, parentCollection: row.parentKey || false})),
              mappings: {FCDRT234: {cloud: 'tablet-root', name: 'Research'}, FCDMN234: {cloud: 'tablet-one', name: 'Methods', zparent: 'FCDRT234'}, FCDSN234: {cloud: 'tablet-two', name: 'Silanes', zparent: 'FCDMN234'}},
              reviews: [{id:'chooser-paper',identity:'chooser-paper',kind:'inbox',status:'pending',digest:'fixture',payload:{title:'Synthetic chooser paper',filename:'Paper.pdf',path:'synthetic.pdf',document_type:'journal_article',guidance_version:1,guidance_state:'ready',findings:[],review_token:'fixture'}}],
              links: [], deletions: [], local_deletions: []};
            ui.folderCatalog = {collections:ui.latest.collections, folders:[{id:'tablet-root',name:'Tablet papers',parent:''},{id:'tablet-one',name:'Methods',parent:'tablet-root'},{id:'tablet-two',name:'Silanes',parent:'tablet-one'}]};
            ui.tab='inbox'; ui.selected='chooser-paper'; ui.reviewDrafts=new Map(); ui.filingQueue=[]; ui.busy=false; ui.render();
            const doc=ui.view.document;
            const filename=doc.querySelector('input[data-notable-field="Proposed filename"]');
            filename.value='My retained folder draft.pdf';filename.dispatchEvent(new ui.view.Event('input'));
            ui.syntheticFolderFilename=filename;
            ui.view.resizeTo(command.width,760); await Zotero.Promise.delay(150);
            const input=doc.querySelector('select[data-folder-destination]'); input.notableChooser.open();
            await Zotero.Promise.delay(100);
            const dialog=doc.querySelector('.folder-chooser-dialog[open]'), r=dialog.getBoundingClientRect();
            if(!dialog || r.width<=0 || r.right>ui.view.innerWidth+1 || r.left<0) throw Error('Chooser overflows native workspace');
            if(doc.activeElement.type!=='search') throw Error('Chooser search did not receive focus');
            response.geometry={width:r.width,right:r.right,left:r.left,viewport:ui.view.innerWidth};
            response.open=true;
          }
          else if (command.action === 'folder_search_select') {
            const doc=ui.view.document, dialog=doc.querySelector('.folder-chooser-dialog[open]');
            const search=dialog.querySelector('input[type="search"]');search.value='silanes';search.dispatchEvent(new ui.view.Event('input'));
            const rows=[...dialog.querySelectorAll('[role="treeitem"]')];
            if(rows.length!==1 || rows[0].dataset.value!=='FCDSN234') throw Error('Deep folder search did not find exact nested collection');
            search.dispatchEvent(new ui.view.KeyboardEvent('keydown',{key:'ArrowDown',bubbles:true}));
            doc.activeElement.dispatchEvent(new ui.view.KeyboardEvent('keydown',{key:'Enter',bubbles:true}));
            const backing=doc.querySelector('select[data-folder-destination]');
            if(backing.value!=='FCDSN234' || dialog.open) throw Error('Keyboard folder selection did not commit and close');
            if(!ui.syntheticFolderFilename.isConnected || ui.syntheticFolderFilename.value!=='My retained folder draft.pdf') throw Error('Changing folders replaced the filename draft');
            response.selected=backing.value;
          }
          else if (command.action === 'folder_poll_race') {
            const originalRPC=ui.rpc, originalInbox=ui.latest.config.inbox;
            const reply=JSON.parse(JSON.stringify(ui.latest));
            reply.reviews.push({...reply.reviews[0],id:'new-arrival',identity:'new-arrival'});
            ui.latest.config.inbox='Synthetic intake';reply.config.inbox='Synthetic intake';
            let release;ui.rpc=()=>new Promise(resolve=>{release=resolve;});
            try {
              const check=ui.checkInbox();
              const input=ui.view.document.querySelector('select[data-folder-destination]');input.notableChooser.open();
              const dialog=ui.view.document.querySelector('.folder-chooser-dialog[open]');
              release(reply);await check;
              if(!dialog.isConnected || !dialog.open || !ui.inboxRenderPending) throw Error('In-flight intake replaced an open chooser');
              if(!ui.view.document.getElementById('activity').hidden) throw Error('Intake scan showed visible loading');
              dialog.dispatchEvent(new ui.view.KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
              response.passed=true;
            } finally {ui.rpc=originalRPC;ui.latest.config.inbox=originalInbox;ui.inboxRenderPending=false;}
          }
          else if (command.action === 'folder_multi') {
            const doc=ui.view.document;
            const input=[...doc.querySelectorAll('select')].find(n=>n.dataset.notableField==='Research collections (optional)');
            input.notableChooser.open();const dialog=doc.querySelector('.folder-chooser-dialog[open]');
            const search=dialog.querySelector('input[type="search"]');search.value='Auxiliary';search.dispatchEvent(new ui.view.Event('input'));
            dialog.querySelector('[role="treeitem"]').click();
            dialog.dispatchEvent(new ui.view.KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
            if(input.selectedOptions.length) throw Error('Cancel committed staged multi-selection');
            input.notableChooser.open();search.value='Auxiliary';search.dispatchEvent(new ui.view.Event('input'));
            dialog.querySelector('[role="treeitem"]').click();
            [...dialog.querySelectorAll('button')].find(n=>n.textContent==='Use selected collections').click();
            if(input.selectedOptions.length!==1 || input.value!=='FCDAU234') throw Error('Multi-selection did not commit');
            response.selected=Array.from(input.selectedOptions,n=>n.value);
          }
          else if (command.action === 'folder_new_cancel') {
            const pending=ui.newFolderDialog('paired','FCDMN234');
            const dialog=ui.view.document.querySelector('.folder-create[open]');
            if(!dialog || dialog.getBoundingClientRect().width<=0) throw Error('Native creation dialog failed to open');
            [...dialog.querySelectorAll(':scope > .actions > button')].find(n=>n.textContent==='Cancel').click();
            if(await pending!==null) throw Error('Creation cancel did not resolve without changes');
            if(!ui.syntheticFolderFilename.isConnected || ui.syntheticFolderFilename.value!=='My retained folder draft.pdf') throw Error('Creation cancel lost the paper draft');
            response.passed=true;
          }
          else if (command.action === 'folder_native_create') {
            const oldTransaction=Zotero.DB.executeTransaction;ui.syntheticFolderNativeError=null;
            Zotero.DB.executeTransaction=async function(...args){try{return await oldTransaction.apply(this,args);}catch(error){ui.syntheticFolderNativeError=error.message+'\n'+error.stack;throw error;}};
            const pending=ui.newFolderDialog('zotero','FCDMN234');
            const dialog=ui.view.document.querySelector('.folder-create[open]');
            dialog.querySelector('input[data-notable-field="Collection name"]').value='Native chooser creation';
            [...dialog.querySelectorAll('button')].find(n=>n.textContent==='Create').click();
            const deadline=Date.now()+30000;
            while(dialog.isConnected && Date.now()<deadline) await Zotero.Promise.delay(25);
            if(dialog.isConnected) {Zotero.DB.executeTransaction=oldTransaction;throw Error('Native collection creation failed: '+dialog.textContent+' Native error: '+ui.syntheticFolderNativeError);}
            const result=await pending;Zotero.DB.executeTransaction=oldTransaction;
            const collection=await Zotero.Collections.getByLibraryAndKeyAsync(Zotero.Libraries.userLibraryID,result.key);
            if(!collection || collection.name!=='Native chooser creation' || collection.parentKey!=='FCDMN234') throw Error('Native collection has wrong parent or name');
            if(!ui.syntheticFolderFilename.isConnected || ui.syntheticFolderFilename.value!=='My retained folder draft.pdf') throw Error('Native creation replaced the paper draft');
            response.created={key:collection.key,name:collection.name,parent:collection.parentKey};
          }
'''

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir',type=Path,required=True)
    parser.add_argument('--zotero-exe',type=Path,required=True)
    parser.add_argument('--port',type=int,default=23269)
    parser.add_argument('--xpi',type=Path,help='Validate the exact packaged extension and bundled helper instead of source')
    args=parser.parse_args()
    marker="          else if (command.action === 'reindex')"
    assert marker in live.BOOTSTRAP
    live.BOOTSTRAP=live.BOOTSTRAP.replace(marker,COMMANDS+marker,1)
    def checks(client,root,checked):
        client.authorize()
        for key,name,parent in [('FCDRT234','Research',None),('FCDMN234','Methods','FCDRT234'),('FCDSN234','Nanoparticle Silanes','FCDMN234'),('FCDAU234','Auxiliary collection',None)]:
            record={'key':key,'name':name}
            if parent:record['parentCollection']=parent
            client.create('collection',record)
        live.command(root,'folder_fresh_stop')
        from note_organizer.zotero_worker import Worker
        from tests.test_cloud_outbox import pdf_bytes
        import time
        inbox=root/'Real watched intake';inbox.mkdir()
        source=inbox/'real-intake.pdf'
        import pymupdf
        document=pymupdf.open();page=document.new_page()
        text='Gold nanoparticle contrast agents for spectral computed tomography\nJane Example and Alex Researcher\nSynthetic Imaging Research 2026\nCite This: Synthetic Imaging Research 2026, 1, 10–15\nKeywords: gold nanoparticles; spectral computed tomography; X-ray attenuation; contrast agents\n\n'
        text+=('Gold nanoparticles provide X-ray attenuation for spectral computed tomography. Nanoparticle contrast agents support material decomposition in biomedical imaging. This synthetic scientific paper compares gold nanoparticles, contrast materials, spectral imaging, and attenuation measurements.\n\n'*4)
        page.insert_textbox(pymupdf.Rect(45,45,550,780),text,fontsize=10)
        document.set_metadata({'title':'nm5c00178 1..7'})
        document.save(source);document.close()
        worker=Worker(root/'profile/notable',port=args.port)
        worker.config.update(server_id=client.server_id,zotero_root='FCDRT234',tablet_root='tablet-root',
                             templates={'journal_article':'{author_last}_{year}'},default_folder='tablet-root',zotero_path='Research',tablet_path='Tablet papers',automatic=False,sync_paused=True,inbox=str(inbox))
        worker.save()
        worker.state.write('folders',{'FCDRT234':{'cloud':'tablet-root','root':True},
            'FCDMN234':{'cloud':'tablet-one','name':'Methods','zparent':'FCDRT234','cparent':'tablet-root'},
            'FCDSN234':{'cloud':'tablet-two','name':'Nanoparticle Silanes','zparent':'FCDMN234','cparent':'tablet-one'}})
        worker.scan_inbox();time.sleep(2.1);worker.scan_inbox()
        proposal=worker.review.rows()[0]['payload']
        assert proposal['title']=='Gold nanoparticle contrast agents for spectral computed tomography',proposal
        assert proposal['document_type']=='journal_article',proposal
        assert proposal['filename'].endswith('_2026.pdf') and proposal['filename']!='real-intake.pdf',proposal
        worker.recheck_review()
        worker=Worker(root/'profile/notable',port=args.port)
        assert worker.collections==[]
        for width in [1040,640]:
            fresh=live.command(root,'folder_fresh_inbox',width=width)
            (root/f'fresh-worker-catalog-{width}.json').write_text(json.dumps(fresh,indent=2),encoding='utf-8')
            live.command(root,'snapshot',filename=f'fresh-worker-folders-{width}.png')
            assert fresh['rawWorkerCollections']==0 and fresh['mappedOptions']==3 and fresh['researchOptions']>=4
            checked(f'fresh_worker_empty_catalog_preserves_actual_native_browse_search_research_and_spacing_{width}')
            # Close just the chooser before the next native-width iteration.
            live.command(root,'folder_search_select')
        tags=live.command(root,'folder_auto_tags')
        (root/'automatic-tags-native.json').write_text(json.dumps(tags,indent=2),encoding='utf-8')
        live.command(root,'snapshot',filename='automatic-tags-native.png',scrollY=max(0,int(tags.get('tagPanelTop',400))-30))
        checked('actual_automatic_tag_RPC_readies_suggestions_quietly_without_native_writes_and_preserves_choices_and_filename')
        # Trigger a real watched-file revision so the bundled helper regenerates naming,
        # rather than merely reading the source-worker proposal seeded above.
        source.write_bytes(source.read_bytes()+b'\n \n')
        naming=live.command(root,'folder_naming_refresh')
        (root/'watched-naming-native.json').write_text(json.dumps(naming,indent=2),encoding='utf-8')
        checked('actual_bundled_watched_revision_prefers_printed_title_and_preserves_journal_template')
        for width in [1040,640]:
            live.command(root,'folder_prepare',width=width)
            live.command(root,'snapshot',filename=f'folder-chooser-{width}.png')
            live.command(root,'folder_search_select')
            checked(f'native_chooser_{width}_bounded_search_keyboard_and_draft')
            live.command(root,'folder_poll_race')
            checked(f'native_chooser_{width}_survives_inflight_intake_without_loading')
            live.command(root,'folder_multi')
            checked(f'native_chooser_{width}_multi_stage_cancel_and_apply')
            live.command(root,'folder_new_cancel')
            checked(f'native_creation_{width}_cancel_preserves_draft')
        created=live.command(root,'folder_native_create')
        checked('actual_native_Zotero_collection_created_under_selected_parent_without_filing')
        (root/'folder-creation.json').write_text(json.dumps(created,indent=2),encoding='utf-8')
        return None
    live.native_checks=checks
    sys.argv=[sys.argv[0],'--work-dir',str(args.work_dir),'--zotero-exe',str(args.zotero_exe),'--port',str(args.port),'--skip-ocr']
    if args.xpi:
        sys.argv.extend(['--xpi',str(args.xpi.resolve(strict=True))])
    live.main()
if __name__=='__main__':main()
