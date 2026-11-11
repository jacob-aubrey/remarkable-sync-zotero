"""Verify progress stability in a disposable native Zotero workspace."""
import sys,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools import test_zotero_plugin_live as live
BRANCH=r"""
          else if(command.action==='stable_activity') {
            clearTimeout(ui.timer);clearTimeout(ui.inboxTimer);clearInterval(ui.activityTimer);
            ui.view.resizeTo(command.width,command.height);await Zotero.Promise.delay(150);
            ui.busy=true;ui.inboxChecking=false;ui.operation={started:Date.now(),updated:Date.now(),title:'Syncing with reMarkable',detail:'Ready'};
            ui.render();clearTimeout(ui.inboxTimer);
            const doc=ui.view.document,ensure=(v,s)=>{if(!v)throw Error(s);};
            const panel=doc.getElementById('activity'),main=doc.querySelector('main');
            const baseline={height:panel.getBoundingClientRect().height,top:main.getBoundingClientRect().top};
            for(const text of ['Ready','Publishing '+('Long scientific paper title and path Δ / '*55)+'END', 'Checking file 4 of 20', 'x'.repeat(1200)]) {
              ui.operation.detail=text;ui.operation.title=text;ui.operation.updated=Date.now()-60000;ui.updateActivity();
              const box=panel.getBoundingClientRect();ensure(Math.abs(box.height-baseline.height)<1,'Progress panel height changed');
              ensure(Math.abs(main.getBoundingClientRect().top-baseline.top)<1,'Content below progress moved');
              for(const id of ['activity-title','activity-detail','activity-time']) { const n=doc.getElementById(id);ensure(n.tabIndex===0,'Progress text not keyboard reachable');ensure(n.scrollWidth<=n.clientWidth+1,'Progress text clips horizontally'); }
              ensure(doc.getElementById('activity-detail').textContent===text,'Progress detail lost text');
            }
            const detail=doc.getElementById('activity-detail');detail.focus();detail.scrollTop=detail.scrollHeight;ensure(detail.scrollTop>0,'Long detail cannot scroll');
            ensure(doc.documentElement.scrollWidth<=ui.view.innerWidth+1,'Activity overflows window');
            response.geometry=baseline;response.viewport=ui.view.innerWidth;response.fullText=true;
            ui.busy=false;ui.operation=null;ui.render();
          }
"""
def checks(client,root,checked):
    client.authorize();evidence=[]
    for width,height in [(1040,760),(640,480)]:
        evidence.append(live.command(root,'stable_activity',width=width,height=height));checked(f'stable_progress_wrapping_scrolling_keyboard_{width}_{height}')
    (root/'activity-layout.json').write_text(json.dumps(evidence,indent=2),encoding='utf-8')
    return None
if __name__=='__main__':
    marker="          else if (command.action === 'reindex')"
    live.BOOTSTRAP=live.BOOTSTRAP.replace(marker,BRANCH+marker,1);live.native_checks=checks;live.main()
