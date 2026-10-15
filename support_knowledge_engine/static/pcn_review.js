/* Separate local read-only review; never calls the ordinary ask/model endpoint. */
(() => {
  'use strict';
  const $=id=>document.getElementById(id), panel=$('pcn-result'), start=$('assistant-pcn-open'), compare=$('pcn-compare');
  let preview=null, controller=null, generation=0;
  const message=text=>{$('pcn-message').textContent=text;};
  function clearContent() {
    $('pcn-case-source').replaceChildren();$('pcn-requirements').replaceChildren();$('pcn-checks').replaceChildren();
  }
  function input() {
    const context=window.SKECaseContext.getLocalContext();
    if(!context)return null;
    return {query:$('assistant-question').value.trim(),product_id:$('assistant-product').value,
      firmware_version:$('assistant-firmware').value.trim(),case_context:context};
  }
  function clear() {
    generation++;controller?.abort();preview=null;panel.hidden=true;
    clearContent();message('');
    $('pcn-applicability').checked=false;$('pcn-confirm-panel').hidden=true;
  }
  function source(requirement,parent) {
    const article=node('article','',parent,'assistant-step-card');
    const body=node('div','',article,'step-body-col');
    node('strong',requirement.name+' · '+requirement.node,body);
    node('p',requirement.conditions,body,'steps-intro');
    node('p',requirement.synthetic ? '虚构测试 PCN · 非真实厂商标准' : 'PCN 来源 · 已治理资料',body,'steps-intro');
    node('pre',requirement.pcn_quote,body,'citation-pre');
    const link=node('a',requirement.filename+' · 第 '+requirement.page_number+' 页',body,'step-citation-anchor');
    link.href=requirement.pdf_url;link.target='_blank';link.rel='noopener noreferrer';
    const details=node('details','',body,'citation-details');node('summary','Evidence ID 与 PDF SHA-256',details);
    node('p','Evidence ID：'+requirement.evidence_id,details);node('p','SHA-256：'+requirement.pdf_sha256,details);
  }
  function render(data, snapshot) {
    $('pcn-case-source').replaceChildren();$('pcn-requirements').replaceChildren();$('pcn-checks').replaceChildren();
    node('h3','案例来源 · 本次人工确认记录', $('pcn-case-source'));
    node('p','本次问题：'+snapshot.query,$('pcn-case-source'),'steps-intro');
    for(const record of data.case_records || []) {
      node('strong',record.label,$('pcn-case-source'));node('pre',record.quote,$('pcn-case-source'),'citation-pre');
    }
    node('h3','PCN 来源 · 完整要求候选（须人工确认）',$('pcn-requirements'));
    for(const requirement of data.requirements || [])source(requirement,$('pcn-requirements'));
    if(!(data.requirements || []).length)node('p','没有本次可批准引用的 PCN 要求，不能建立标准依据。',$('pcn-requirements'),'steps-intro');
    if((data.checks || []).length)node('h3','逐项审查结果 · 仅记录层面',$('pcn-checks'));
    for(const check of data.checks || []) {
      const card=node('article','', $('pcn-checks'),'assistant-step-card'),body=node('div','',card,'step-body-col');
      node('strong',check.state+' · '+check.name,body);node('p',check.message,body);
      node('p','匹配的案例原文（不是 PCN 证据）：',body,'steps-intro');
      for(const record of check.case_matches)node('pre',record.quote,body,'citation-pre');
      if(!check.case_matches.length)node('p','未识别到执行记录，请人工确认。',body);
      for(const missing of check.missing_information)node('p',missing,body,'steps-intro');
      node('p',check.human_review,body,'steps-intro');
    }
    panelCitationsContainer?.replaceChildren();
    const citations=data.requirements || [];
    if(panelCitationsContainer)panelCitationsContainer.style.display=citations.length?'block':'none';
    if(panelEmpty)panelEmpty.style.display=citations.length?'none':'block';
    if(panelCitationCountBadge)panelCitationCountBadge.textContent=citations.length+' 项 PCN 来源';
    citations.forEach((r,index)=>renderEvidenceCard({...r,original_text:r.pcn_quote},index,panelCitationsContainer,true));
    if(panelContextProduct)panelContextProduct.textContent=$('assistant-product').selectedOptions[0]?.textContent || '';
    if(panelContextFirmware)panelContextFirmware.textContent=snapshot.firmware_version || '未限定（保守适用）';
  }
  async function requestReview(confirm) {
    const snapshot=input();
    panel.hidden=false;
    if(!snapshot) {
      message('请先粘贴案例、编辑并人工核对后点击「确认并填入问答」。PCN 本地审查不需要模型发送同意。');
      $('assistant-case-open').click();return;
    }
    if(button.disabled) {message('普通问答正在进行，请完成后再主动进行 PCN 审查。');return;}
    if(confirm && (!preview || !$('pcn-applicability').checked || JSON.stringify(snapshot)!==JSON.stringify(preview.snapshot))) {
      message('请重新匹配当前案例，并人工确认 PCN 完整要求及适用条件。');return;
    }
    const body=confirm?{...snapshot,confirmation_token:preview.token}:snapshot;
    const id=++generation;controller?.abort();controller=new AbortController();
    clearContent();preview=null;
    const timer=setTimeout(()=>controller.abort(),15000);
    start.disabled=true;compare.disabled=true;
    $('assistant-result').style.display='none';$('assistant-welcome').style.display='none';$('assistant-user-bubble').style.display='none';
    $('pcn-confirm-panel').hidden=true;message('正在本地匹配 PCN、核验来源；不会调用模型。');
    try {
      const response=await fetch(panel.dataset.url,{method:'POST',headers:{'Content-Type':'application/json','X-Assistant-CSRF':form.dataset.csrf},body:JSON.stringify(body),signal:controller.signal});
      const data=await response.json();
      if(id!==generation)return;
      if(JSON.stringify(input())!==JSON.stringify(snapshot)) {preview=null;message('案例已变化，请重新确认并匹配 PCN。');return;}
      message(data.message || '审查服务没有返回有效结果。');render(data,snapshot);
      preview=data.status==='awaiting_confirmation'?{snapshot,token:data.confirmation_token}:null;
      $('pcn-applicability').checked=false;$('pcn-confirm-panel').hidden=!preview;
      panel.scrollIntoView({block:'start',behavior:'smooth'});
    } catch {
      if(id===generation) {preview=null;message('本地 PCN 审查服务失败或超时，未生成审查结论。');}
    } finally {
      clearTimeout(timer);start.disabled=false;compare.disabled=false;
    }
  }
  start.addEventListener('click',()=>requestReview(false));compare.addEventListener('click',()=>requestReview(true));
  $('assistant-clear-btn')?.addEventListener('click',clear);
  for(const id of ['assistant-question','assistant-product','assistant-firmware','case-raw','case-product','case-category','case-fault','case-detection','case-done','case-pending','case-firmware']) {
    $(id).addEventListener('input',()=>{if(preview){preview=null;$('pcn-applicability').checked=false;$('pcn-confirm-panel').hidden=true;message('输入已修改，旧 PCN 确认已撤销，请重新匹配。');}});
  }
  window.SKEPCNReview={clear};window.addEventListener('pagehide',clear);
})();
