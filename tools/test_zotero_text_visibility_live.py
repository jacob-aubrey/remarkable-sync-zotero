"""Actual Windows Zotero text visibility, record selection, and draft-preservation QA.

Uses a disposable profile and synthetic native references only. Clipboard writes
are captured by test instrumentation, so the user's clipboard is preserved.
"""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools import test_zotero_plugin_live as live

COMMANDS=r'''
          else if (command.action === 'text_prepare') {
            clearTimeout(ui.inboxTimer);clearTimeout(ui.timer);clearTimeout(ui.paperTagTimer);
            const originalPicker=ui.supportingInformationPicker;
            ui.supportingInformationPicker=function(...args){const picker=originalPicker.apply(this,args);this.syntheticTextPicker=picker;return picker;};
            try {
              ui.latest={...ui.latest,reviews:[{id:'text-review',identity:'text-review',kind:'cloud',status:'pending',digest:'text-fixture',payload:{
                title:command.title,filename:command.filename,path:'Synthetic.pdf',document_type:'supporting_information',guidance_version:1,guidance_state:'ready',findings:[],review_token:'fixture',
                matches:[{key:'TXTMAT23',parent:'TXTPAP23',title:command.title,filename:command.filename}]}}],links:[],deletions:[],local_deletions:[]};
              ui.tab='inbox';ui.selected='text-review';ui.reviewDrafts=new Map();ui.busy=false;ui.render();clearTimeout(ui.paperTagTimer);
            } finally {ui.supportingInformationPicker=originalPicker;}
            ui.view.resizeTo(command.width,command.height);await Zotero.Promise.delay(150);ui.refreshReadableFields();
            const doc=ui.view.document,title=doc.querySelector('[data-notable-field="Title"]'),filename=doc.querySelector('[data-notable-field="Proposed filename"]');
            const ensure=(ok,message)=>{if(!ok)throw Error(message);};
            ensure(title.localName==='textarea','Title is still a clipping single-line input');
            ensure(title.value===command.title,'Long Unicode title changed');
            ensure(title.clientHeight>45 && title.scrollWidth<=title.clientWidth+1,'Wrapped title clips horizontally');
            ensure(title.scrollHeight<=title.clientHeight+2,'Wrapped title does not grow to fit its contents');
            ensure(filename.notableValueDetails && !filename.notableValueDetails.hidden,'Long filename has no full-value expansion');
            ensure(filename.title===command.filename,'Long filename tooltip does not expose exact full value');
            filename.notableValueDetails.open=true;
            ensure(filename.notableValueDetails.querySelector('pre').textContent===command.filename,'Filename full-value display is incomplete');
            const controls=[...doc.querySelectorAll('.review-form input,.review-form textarea,.review-form button')].filter(node=>node.getClientRects().length && !node.closest('dialog'));
            for(let a=0;a<controls.length;a++)for(let b=a+1;b<controls.length;b++) {
              const x=controls[a].getBoundingClientRect(),y=controls[b].getBoundingClientRect();
              ensure(Math.min(x.right,y.right)-Math.max(x.left,y.left)<=1 || Math.min(x.bottom,y.bottom)-Math.max(x.top,y.top)<=1,'Long review controls overlap');
            }
            response.titleTop=title.getBoundingClientRect().top+(doc.querySelector('body')?.scrollTop||0);
            response.filenameTop=filename.getBoundingClientRect().top+(doc.querySelector('body')?.scrollTop||0);
            response.titleHeight=title.clientHeight;response.filenameOverflow=filename.scrollWidth-filename.clientWidth;
            response.viewport={width:ui.view.innerWidth,height:ui.view.innerHeight,scrollWidth:doc.documentElement.scrollWidth};
            ensure(response.viewport.scrollWidth<=response.viewport.width+1,'Long review fields overflow viewport');
          }
          else if (command.action === 'text_focus_programmatic') {
            const doc=ui.view.document,ensure=(ok,message)=>{if(!ok)throw Error(message);};
            let title=doc.querySelector('[data-notable-field="Title"]');
            title.value=command.title+'\nSecond line – Δ';title.focus();title.setSelectionRange(8,20);
            title.dispatchEvent(new ui.view.Event('input',{bubbles:true}));
            const normalized=title.value,start=title.selectionStart,end=title.selectionEnd;
            ensure(!/[\r\n]/.test(normalized),'Title normalization retained a line break');
            ui.render();clearTimeout(ui.paperTagTimer);
            title=doc.querySelector('[data-notable-field="Title"]');
            ensure(title.value===normalized && doc.activeElement===title,'Rerender lost title draft or keyboard focus');
            ensure(title.selectionStart===start && title.selectionEnd===end,'Rerender lost title caret/selection');
            ensure(doc.querySelector('[data-notable-field="Proposed filename"]').notableValueDetails.open,'Rerender collapsed the expanded full filename');
            const filename=doc.querySelector('[data-notable-field="Proposed filename"]');
            let edits=0;filename.addEventListener('input',()=>edits++);filename.addEventListener('change',()=>edits++);
            filename.value=command.filename;filename.notableRefreshValue();
            ensure(edits===0,'Programmatic readability refresh dispatched a draft edit');
            ensure(filename.title===command.filename && filename.notableValueDetails.querySelector('pre').textContent===command.filename,'Programmatic update kept stale full-value text');
            filename.notableValueDetails.open=true;
            const copy=[...filename.notableValueDetails.querySelectorAll('button')].find(node=>node.textContent==='Copy full value');
            const original=Zotero.Utilities.Internal.copyTextToClipboard,copied=[];
            Zotero.Utilities.Internal.copyTextToClipboard=value=>copied.push(value);
            try {copy.click();await Zotero.Promise.delay(50);} finally {Zotero.Utilities.Internal.copyTextToClipboard=original;}
            ensure(copied[0]===command.filename,'Copy did not receive the exact current full value');
            filename.value='Short.pdf';filename.notableRefreshValue();ensure(filename.notableValueDetails.hidden,'Short field still shows expansion controls');
            response.normalizedTitle=normalized;response.selection=[start,end];response.copyExact=true;
          }
          else if (command.action === 'text_mainpaper') {
            const ensure=(ok,message)=>{if(!ok)throw Error(message);},picker=ui.syntheticTextPicker;
            // Capture a fresh picker after a redraw, using the same real renderer.
            const original=ui.supportingInformationPicker;
            ui.supportingInformationPicker=function(...args){const value=original.apply(this,args);this.syntheticTextPicker=value;return value;};
            try {ui.render();clearTimeout(ui.paperTagTimer);} finally {ui.supportingInformationPicker=original;}
            const current=ui.syntheticTextPicker;current.query.value='';await current.load();
            const select=current.select,option=[...select.options].find(option=>option.value==='TXTPAP23');
            ensure(option && option.dataset.metadata,'Native main-paper metadata missing');
            ensure(option.dataset.detail.includes(command.nativefilename),'Main-paper chooser omitted its actual PDF filename');
            const before=option.dataset.metadata;
            select.notableRecordChooser.open();await Zotero.Promise.delay(50);
            const doc=ui.view.document,dialog=doc.querySelector('.folder-chooser-dialog[open]'),record=dialog.querySelector('[role="option"][data-value="TXTPAP23"]');
            ensure(record && record.textContent.includes(command.title),'Long native main-paper title is incomplete');
            ensure(!dialog.querySelector('.folder-chooser-expand'),'Flat paper choices show a misleading folder expansion control');
            const box=record.getBoundingClientRect(),text=record.querySelector('.folder-chooser-option-text');
            ensure(text.scrollWidth<=text.clientWidth+1 && box.height>45,'Native record choice remains a clipping single line');
            const d=dialog.getBoundingClientRect(),footer=dialog.querySelector('.folder-chooser-footer').getBoundingClientRect();
            ensure(d.left>=0 && d.right<=ui.view.innerWidth+1 && d.top>=0 && d.bottom<=ui.view.innerHeight+1,'Record dialog overflows short/narrow viewport');
            ensure(footer.bottom<=ui.view.innerHeight+1,'Record dialog footer is clipped');
            if(command.capture) {
              const win=ui.view,canvas=doc.createElementNS('http://www.w3.org/1999/xhtml','canvas');canvas.width=win.innerWidth;canvas.height=win.innerHeight;
              const context=canvas.getContext('2d');
              if(context.drawWindow) context.drawWindow(win,0,win.scrollY,canvas.width,canvas.height,'white');
              else {const snapshot=await win.browsingContext.currentWindowGlobal.drawSnapshot({x:0,y:0,width:canvas.width,height:canvas.height},1,'white');context.drawImage(snapshot,0,0);snapshot.close();}
              const raw=atob(canvas.toDataURL('image/png').split(',')[1]);
              await IOUtils.write(PathUtils.join(PathUtils.parent(config.ready),command.capture),Uint8Array.from(raw,c=>c.charCodeAt(0)));
            }

            record.focus();record.dispatchEvent(new ui.view.KeyboardEvent('keydown',{key:'Enter',bubbles:true}));
            ensure(select.value==='TXTPAP23' && !dialog.open,'Keyboard main-paper selection did not commit stable native key');
            ensure(select.selectedOptions[0].dataset.metadata===before,'Wrapping chooser changed native citation metadata');
            const metadata=JSON.parse(before);ensure(metadata.title===command.title && metadata.publicationTitle==='Synthetic visibility journal','Wrong native metadata selected');
            const match=doc.querySelector('select[data-notable-field="PDF already found in Zotero"]');
            match.notableRecordChooser.open();await Zotero.Promise.delay(25);
            const matchDialog=doc.querySelector('.folder-chooser-dialog[open]'),matchRow=matchDialog.querySelector('[role="option"][data-value="TXTMAT23"]');
            ensure(matchRow.textContent.includes(command.title),'Existing-PDF record title is incomplete');matchRow.click();
            ensure(match.value==='TXTMAT23' && ui.reviewDrafts.get('text-review').match==='TXTMAT23','Existing-PDF choice changed identity or failed to save draft');
            response.nativeKey=select.value;response.metadata=metadata;response.recordHeight=box.height;response.dialog={width:d.width,height:d.height};
          }
          else if (command.action === 'text_setup_paths') {
            const ensure=(ok,message)=>{if(!ok)throw Error(message);};
            ui.setupDraft={...ui.latest.config,inbox:command.path,templates:{journal_article:command.template}};ui.tab='setup';ui.render();clearTimeout(ui.paperTagTimer);
            ui.view.resizeTo(command.width,command.height);await Zotero.Promise.delay(80);
            const doc=ui.view.document,path=doc.querySelector('[data-notable-field="Watched ToSort folder (optional)"]');
            const template=[...doc.querySelectorAll('[data-notable-field]')].find(node=>node.dataset.notableField===ui.latest.types.journal_article);
            template.closest('details').open=true;await Zotero.Promise.delay(50);ui.refreshReadableFields();
            for(const [input,value] of [[path,command.path],[template,command.template]]) {
              ensure(input.title===value && input.notableValueDetails && !input.notableValueDetails.hidden,'Path/template cannot show full current value');
              input.notableValueDetails.open=true;
              const preview=input.notableValueDetails.querySelector('pre');ensure(preview.textContent===value,'Path/template full value changed');
              ensure(preview.scrollWidth<=preview.clientWidth+1,'Expanded path/template overflows horizontally');
            }
            const pairing=doc.querySelector('[data-notable-field="Single-use reMarkable pairing code"]');pairing.value='ABCD2345';
            ensure(!pairing.notableValueDetails && !pairing.title,'Pairing code exposes full-value/copy controls');
            ensure(doc.documentElement.scrollWidth<=ui.view.innerWidth+1,'Expanded long setup values overflow viewport');
            const controls=[...doc.querySelectorAll('main input,main textarea,main button')].filter(node=>node.getClientRects().length && !node.closest('dialog'));
            for(let a=0;a<controls.length;a++)for(let b=a+1;b<controls.length;b++) {
              const x=controls[a].getBoundingClientRect(),y=controls[b].getBoundingClientRect();
              ensure(Math.min(x.right,y.right)-Math.max(x.left,y.left)<=1 || Math.min(x.bottom,y.bottom)-Math.max(x.top,y.top)<=1,'Expanded setup controls overlap');
            }
            response.pathTop=path.getBoundingClientRect().top+(doc.querySelector('body')?.scrollTop||0);
            response.templateTop=template.getBoundingClientRect().top+(doc.querySelector('body')?.scrollTop||0);
            response.pathLength=path.value.length;response.templateLength=template.value.length;response.pairingProtected=true;
          }
'''

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir',type=Path,required=True)
    parser.add_argument('--zotero-exe',type=Path,required=True)
    parser.add_argument('--port',type=int,default=23379)
    parser.add_argument('--xpi',type=Path)
    args=parser.parse_args()
    marker="          else if (command.action === 'reindex')"
    assert marker in live.BOOTSTRAP
    live.BOOTSTRAP=live.BOOTSTRAP.replace(marker,COMMANDS+marker,1)
    def checks(client,root,checked):
        client.authorize()
        title=('Long Unicode scientific title: α-particle attenuation, Δ spectral reconstruction, and nanoparticle contrast material discrimination – '*5)+'END TITLE Ω'
        filename=('An_extremely_long_research_filename_with_Unicode_Δ_and_α_'*5)+'END_FILENAME.pdf'
        path='C:\\Research\\'+('A_very_long_project_folder_Δ_with_detailed_descriptive_names\\'*7)+'ToSort'
        template=('{author_last}_{journal}_{year}_{title}_'*7)+'END_TEMPLATE'
        client.create('item',{'key':'TXTPAP23','itemType':'journalArticle','title':title,'publicationTitle':'Synthetic visibility journal','date':'2026','creators':[{'creatorType':'author','firstName':'Jane','lastName':'Example'}]})
        client.create('collection',{'key':'TXTCL234','name':'Synthetic readable fields'})
        native_filename='Full main article filename with contrast materials and Unicode Δ.pdf'
        source=root/'native-main.pdf';source.write_bytes(live.pdf_bytes())
        client.create('item',{'key':'TXTATT23','itemType':'attachment','parentItem':'TXTPAP23','title':'Synthetic full main article','contentType':'application/pdf','linkMode':'imported_file','filename':native_filename})
        client.upload('TXTATT23',source,'')
        evidence=[]
        for width,height in [(1040,760),(640,760),(640,480)]:
            result=live.command(root,'text_prepare',width=width,height=height,title=title,filename=filename)
            live.command(root,'snapshot',filename=f'text-title-{width}-{height}.png',scrollY=max(0,int(result['titleTop'])-30))
            live.command(root,'snapshot',filename=f'text-filename-{width}-{height}.png',scrollY=max(0,int(result['filenameTop'])-30))
            checked(f'wrapped_title_and_filename_expansion_{width}_{height}')
            result['focus']=live.command(root,'text_focus_programmatic',title=title,filename='Programmatically_updated_'+filename)
            checked(f'caret_draft_programmatic_value_and_exact_copy_{width}_{height}')
            result['record']=live.command(root,'text_mainpaper',title=title,nativefilename=native_filename,capture=f'text-paper-choice-{width}-{height}.png')
            checked(f'wrapping_native_paper_choices_metadata_and_keyboard_{width}_{height}')
            result['setup']=live.command(root,'text_setup_paths',width=width,height=height,path=path,template=template)
            live.command(root,'snapshot',filename=f'text-path-{width}-{height}.png',scrollY=max(0,int(result['setup']['pathTop'])-30))
            live.command(root,'snapshot',filename=f'text-template-{width}-{height}.png',scrollY=max(0,int(result['setup']['templateTop'])-30))
            checked(f'full_paths_templates_and_sensitive_pairing_exclusion_{width}_{height}')
            evidence.append(result)
        (root/'text-visibility-native.json').write_text(json.dumps(evidence,indent=2),encoding='utf-8')
        return None
    live.native_checks=checks
    sys.argv=[sys.argv[0],'--work-dir',str(args.work_dir),'--zotero-exe',str(args.zotero_exe),'--port',str(args.port),'--skip-ocr']
    if args.xpi:sys.argv.extend(['--xpi',str(args.xpi.resolve(strict=True))])
    live.main()
if __name__=='__main__':main()
