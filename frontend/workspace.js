() => {
  const root=document;
  if(root.documentElement.dataset.dsBound)return [];
  root.documentElement.dataset.dsBound='1';
  const paths=['M4 5h16v11H9l-5 4V5Z','M7 3h7l4 4v14H7V3Zm7 0v5h4M10 12h5m-5 4h5','M4 4h6v6H4V4Zm10 0h6v6h-6V4ZM4 14h6v6H4v-6Zm10 0h6v6h-6v-6Z','M4 20V4m0 16h16M8 16v-5m5 5V7m5 9V4','M12 3 2 20h20L12 3Zm0 6v5m0 3h.01','M3 6h7l2 2h9v12H3V6Z','M12 3 4 6v6c0 5 8 9 8 9s8-4 8-9V6l-8-3Zm-4 9 3 3 5-6','M4 8a9 9 0 1 1-1 8M4 3v5h5m3-1v6l4 2','M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Zm-8 9v-2a6 6 0 0 1 6-6h4a6 6 0 0 1 6 6v2'];
  const icon=path=>{
    const svg=root.createElementNS('http://www.w3.org/2000/svg','svg');
    for(const[k,v]of Object.entries({viewBox:'0 0 24 24',fill:'none',stroke:'currentColor','stroke-width':'1.7','stroke-linecap':'round','stroke-linejoin':'round','aria-hidden':'true',focusable:'false'}))svg.setAttribute(k,v);
    svg.classList.add('ds-icon');const p=root.createElementNS(svg.namespaceURI,'path');p.setAttribute('d',path);svg.append(p);return svg;
  };
  let compact=false,opened=false;
  try{compact=localStorage.getItem('lux.nav.compact')==='1';}catch(_){}
  const mobile=()=>matchMedia('(max-width:760px)').matches;
  const apply=()=>{
    const ws=root.querySelector('#ds-workspace');if(!ws)return;
    ws.classList.toggle('ds-compact',compact&&!mobile());ws.classList.toggle('ds-nav-open',opened&&mobile());
    const b=root.querySelector('#ds-nav-toggle'),expanded=mobile()?opened:!compact;
    if(b){b.setAttribute('aria-expanded',String(expanded));b.setAttribute('aria-label',expanded?'收起导航':'展开导航');b.title=expanded?'收起导航':'展开导航';}
    const nav=root.querySelector('.lux-tabs [role=tablist]');
    if(nav){nav.id='ds-primary-nav';nav.setAttribute('aria-label','工作台导航');nav.inert=mobile()&&!opened;}
    root.querySelectorAll('.lux-tabs [role=tabpanel],.account-strip').forEach(el=>el.inert=mobile()&&opened);
  };
  let dialog=null,blobUrl=null,abort=null,opener=null;
  const closePreview=()=>{abort?.abort();abort=null;if(blobUrl)URL.revokeObjectURL(blobUrl);blobUrl=null;dialog?.querySelector('iframe')?.removeAttribute('src');opener?.focus();};
  const preview=async(button)=>{
    const href=button?.closest('.pdf-result,.ds-chat-file')?.querySelector('a[href]')?.href;if(!href)return;
    const url=new URL(href,location.href);if(url.origin!==location.origin||!url.pathname.startsWith('/library-files/'))return;
    if(!dialog){
      dialog=root.createElement('dialog');dialog.className='ds-pdf-dialog ds-dialog';dialog.setAttribute('aria-labelledby','ds-pdf-title');
      dialog.innerHTML='<div class="ds-pdf-head ds-dialog-head"><h2 id="ds-pdf-title"></h2><button type="button" class="ds-pdf-close">关闭预览</button></div><p class="ds-pdf-status" role="status" aria-live="polite"></p><button type="button" class="ds-pdf-retry" hidden>重新加载</button><iframe title="PDF 原件预览" hidden></iframe><footer class="ds-dialog-actions"><a class="ds-pdf-download" target="_blank" rel="noopener">下载 PDF 原件</a></footer>';
      root.querySelector('#ds-workspace').append(dialog);
      dialog.querySelector('.ds-pdf-close').onclick=()=>dialog.close();dialog.addEventListener('close',closePreview);
      dialog.querySelector('.ds-pdf-retry').onclick=()=>preview(opener);
    }
    dialog.classList.toggle('ds-library-drawer',!!button.closest('#tab-library'));
    abort?.abort();if(blobUrl)URL.revokeObjectURL(blobUrl);blobUrl=null;
    const controller=new AbortController();abort=controller;opener=button;
    const status=dialog.querySelector('.ds-pdf-status'),frame=dialog.querySelector('iframe'),retry=dialog.querySelector('.ds-pdf-retry');
    dialog.querySelector('h2').textContent=button.closest('.pdf-result,.ds-chat-file').querySelector('strong')?.textContent||'PDF 预览';dialog.querySelector('a').href=url.href;
    status.textContent='正在读取 PDF 原件…';status.classList.remove('is-error');frame.hidden=true;frame.removeAttribute('src');retry.hidden=true;
    if(!dialog.open)dialog.showModal();
    try{
      const response=await fetch(url.href,{credentials:'same-origin',signal:controller.signal});if(!response.ok)throw new Error('读取失败，请检查登录状态或文件权限后重试。');
      const blob=await response.blob();if(!(await blob.slice(0,5).text()).startsWith('%PDF'))throw new Error('返回内容不是有效 PDF，请下载原件核验。');
      if(controller.signal.aborted)return;
      blobUrl=URL.createObjectURL(new Blob([blob],{type:'application/pdf'}));frame.src=blobUrl;frame.hidden=false;
      status.textContent='已载入原件。若浏览器不支持内嵌预览，可下载后打开。';
    }catch(error){if(controller.signal.aborted||error.name==='AbortError')return;status.textContent=error.message||'预览失败，请重试。';status.classList.add('is-error');retry.hidden=false;}
  };
  const decorate=()=>{
    const chat=root.querySelector('#tab-chat'),active=!!chat?.querySelector('.message-row');
    if(chat){
      chat.classList.toggle('ds-conversation',active);
      if(!active)chat.classList.remove('ds-show-summary');
      const title=chat.querySelector('.chat-heading h1');
      const label=active?'AI 智能助手':'开始今天的工作';
      if(title&&title.textContent!==label)title.textContent=label;
    }

    root.querySelectorAll('.lux-tabs [role=tab]').forEach((b,i)=>{if(b.querySelector('.ds-icon'))return;const label=b.textContent.trim();b.setAttribute('aria-label',label);b.title=label;b.prepend(icon(paths[i]||paths[0]));});
    const toggle=root.querySelector('#ds-nav-toggle');if(toggle&&!toggle.querySelector('svg'))toggle.prepend(icon('M3 5h18v14H3V5Zm6 0v14m4-10h5m-5 4h5'));
    root.querySelectorAll('.pdf-result').forEach(card=>{if(card.querySelector('.ds-preview-btn')||!card.querySelector('a[href]'))return;const b=root.createElement('button');b.type='button';b.className='ds-preview-btn';b.textContent='预览 PDF';card.querySelector('a[href]').before(b);});
    root.querySelectorAll('.lux-chat a[href]').forEach(a=>{
      if(a.closest('.pdf-result'))return;
      let url;try{url=new URL(a.href,location.href);}catch{return;}
      if(url.origin!==location.origin||!url.pathname.startsWith('/library-files/'))return;
      const parent=a.parentElement;if(parent.querySelector('.ds-preview-btn'))return;
      parent.classList.add('ds-chat-file');
      const b=root.createElement('button');b.type='button';b.className='ds-preview-btn';b.textContent='预览 PDF';a.before(b);
    });
    // Decorate only action controls; accordion headers, list rows and native pickers keep their semantics.
    root.querySelectorAll('#ds-workspace button.primary,#ds-workspace button.secondary,#ds-nav-toggle,.ds-preview-btn,.delivery-detail-open,.ds-dialog button,#voice-cancel,#assistant-overview-toggle,.home-shortcuts button,.home-more').forEach(b=>{
      b.classList.add('ds-control');
      const small=b.classList.contains('sm')||b.matches('.delivery-detail-open,#voice-cancel,.home-more')||!!b.closest('.library-row-actions');
      const large=b.classList.contains('lg');
      b.classList.toggle('ds-small',small);b.classList.toggle('ds-large',!small&&large);
      b.classList.toggle('ds-ghost',b.matches('#ds-nav-toggle,#voice-cancel,#assistant-overview-toggle,.home-more,.ops-ghost-btn'));
    });
    const ws=root.querySelector('#ds-workspace');
    if(ws&&root.querySelector('#history-detail-source'))ws.classList.add('ds-history-ready');
    apply();
  };
  // Display the existing server-authorized HTML. Do not reconstruct or fetch task data here.
  let historyDialog=null,historyOpener=null;
  const openHistory=()=>{
    const source=root.querySelector('#history-detail-source .prose, #history-detail-source .html-container');
    if(!source)return;
    historyOpener=root.activeElement;
    if(!historyDialog){
      historyDialog=root.createElement('dialog');historyDialog.className='ds-history-drawer ds-dialog';
      historyDialog.setAttribute('aria-labelledby','ds-history-title');
      historyDialog.innerHTML='<header class="ds-dialog-head"><h2 id="ds-history-title">任务详情</h2><button type="button" class="secondary" data-history-close>关闭</button></header><div class="ds-dialog-body"></div><footer class="ds-dialog-actions"><button type="button" class="secondary" data-history-feedback>填写结果评价</button></footer>';
      root.querySelector('#ds-workspace').append(historyDialog);
      historyDialog.addEventListener('close',()=>historyOpener?.focus());
    }
    historyDialog.querySelector('.ds-dialog-body').replaceChildren(source.cloneNode(true));
    historyDialog.querySelector('[data-history-feedback]').hidden=!source.querySelector('.history-detail');
    if(!historyDialog.open)historyDialog.showModal();
  };
  root.addEventListener('lux:history-detail-ready',openHistory);
  let deliveryDialog=null,deliveryOpener=null;
  root.addEventListener('click',e=>{
    if(e.target.closest('[data-history-open]')){openHistory();return;}
    if(e.target.closest('[data-history-close]')){historyDialog?.close();return;}
    if(e.target.closest('[data-history-feedback]')){
      historyDialog?.close();const toggle=root.querySelector('#history-feedback>button');
      if(toggle?.getAttribute('aria-expanded')!=='true')toggle?.click();
      toggle?.focus();root.querySelector('#history-feedback')?.scrollIntoView({block:'center',behavior:'auto'});return;
    }
    const detailButton=e.target.closest('.delivery-detail-open');
    if(detailButton){
      if(!deliveryDialog){
        deliveryDialog=root.createElement('dialog');deliveryDialog.className='delivery-detail-drawer ds-dialog';deliveryDialog.setAttribute('aria-label','交付详情');
        deliveryDialog.innerHTML='<header class="ds-dialog-head"><span>交付详情</span><button type="button" data-delivery-close>关闭</button></header><div class="delivery-detail-body ds-dialog-body"></div><footer class="ds-dialog-actions"></footer>';
        root.querySelector('#ds-workspace').append(deliveryDialog);
        deliveryDialog.addEventListener('close',()=>deliveryOpener?.focus());
      }
      const template=detailButton.parentElement.querySelector('template');if(!template)return;
      deliveryDialog.querySelector('.delivery-detail-body').replaceChildren(template.content.cloneNode(true));
      const actions=deliveryDialog.querySelector('.ds-dialog-actions');actions.replaceChildren();
      const edit=deliveryDialog.querySelector('[data-delivery-edit]');if(edit)actions.append(edit);
      deliveryOpener=detailButton;deliveryDialog.showModal();return;
    }
    if(e.target.closest('[data-delivery-close]')){deliveryDialog?.close();return;}
    if(e.target.closest('[data-delivery-edit]')){
      deliveryDialog?.close();const toggle=root.querySelector('#delivery-workbench>button');
      if(toggle?.getAttribute('aria-expanded')!=='true')toggle?.click();
      root.querySelector('#delivery-workbench')?.scrollIntoView({block:'start',behavior:'auto'});return;
    }

    const overview=e.target.closest('#assistant-overview-toggle');
    if(overview){const chat=root.querySelector('#tab-chat');const show=chat.classList.toggle('ds-show-summary');overview.setAttribute('aria-expanded',String(show));overview.textContent=show?'收起工作摘要':'展开工作摘要';return;}

    if(e.target.closest('[data-home-pending]')){const panel=root.querySelector('.home-pending');if(panel){panel.setAttribute('tabindex','-1');panel.focus();panel.scrollIntoView({block:'center',behavior:matchMedia('(prefers-reduced-motion:reduce)').matches?'instant':'smooth'});}return;}
    const b=e.target.closest('.ds-preview-btn');if(b){preview(b);return;}
    if(e.target.closest('#ds-nav-toggle')){if(mobile())opened=!opened;else{compact=!compact;try{localStorage.setItem('lux.nav.compact',compact?'1':'0');}catch(_){}}apply();return;}
    if(e.target.closest('#ds-nav-backdrop')||(e.target.closest('.lux-tabs [role=tab]')&&mobile())){opened=false;apply();root.querySelector('#ds-nav-toggle')?.focus();}
  });
  root.addEventListener('keydown',e=>{
    if(e.key==='Escape'&&!root.querySelector('dialog[open]')){
      const menu=root.activeElement?.closest('#library-results details[open]');
      if(menu){menu.open=false;menu.querySelector('summary')?.focus();e.preventDefault();return;}
    }
    if(!opened||!mobile())return;
    if(e.key==='Escape'){opened=false;apply();root.querySelector('#ds-nav-toggle')?.focus();}
    if(e.key==='Tab'){
      const controls=[root.querySelector('#ds-nav-toggle'),...root.querySelectorAll('.lux-tabs [role=tab]:not([tabindex="-1"]):not([disabled])')].filter(Boolean);
      const first=controls[0],last=controls[controls.length-1];
      if(e.shiftKey&&root.activeElement===first){e.preventDefault();last?.focus();}
      else if(!e.shiftKey&&root.activeElement===last){e.preventDefault();first?.focus();}
    }
  });
  addEventListener('resize',()=>{if(!mobile())opened=false;apply();});
  let scheduled=false;new MutationObserver(()=>{if(scheduled)return;scheduled=true;requestAnimationFrame(()=>{scheduled=false;decorate();});}).observe(root.body,{childList:true,subtree:true});
  decorate();return [];
}
