/* No network, storage, URL transport, or implicit model submission. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const parser=window.SKECaseParser, products=JSON.parse($('assistant-product-catalog').textContent);
  const fields={product_id:'case-product', category:'case-category', fault_desc:'case-fault',
    detection_records:'case-detection', completed_actions:'case-done', pending_actions:'case-pending', firmware_version:'case-firmware'};
  let pending=false, confirmed=null, localConfirmed=null, applying=false;
  const mainValues=()=>({query:$('assistant-question').value.trim(),product_id:$('assistant-product').value,firmware_version:$('assistant-firmware').value.trim()});
  const matches=()=>!confirmed || JSON.stringify(mainValues())===JSON.stringify(confirmed.main);
  function invalidate() {
    localConfirmed=null;
    confirmed=null; pending=true; $('case-reviewed').checked=false; $('case-send-consent').checked=false;
    message('问题、产品、固件或案例已修改，旧发送确认已撤销。请重新核对并确认，或清除案例后使用普通问答。');
  }
  const message=text => { $('case-message').textContent=text; };
  function clear() {
    $('case-raw').value='';
    for (const id of Object.values(fields)) $(id).value='';
    $('case-anonymized').checked=false; $('case-reviewed').checked=false; $('case-send-consent').checked=false; confirmed=null; localConfirmed=null;
    $('case-preview').hidden=true; $('case-gaps').replaceChildren();
    $('case-product-match').textContent=''; pending=false;
    message('本地案例已清除；已填入的问答内容可在主输入框编辑。');
  }
  $('assistant-case-open').addEventListener('click', () => {
    toggleContextDrawer(true); $('case-intake').hidden=false; $('case-raw').focus();
  });
  $('case-clear').addEventListener('click', clear);
  $('case-raw').addEventListener('input', () => {
    confirmed=null; localConfirmed=null; $('case-send-consent').checked=false;
    pending=Boolean($('case-raw').value.trim()); $('case-preview').hidden=true;
    $('case-reviewed').checked=false; $('case-anonymized').checked=false;
    message('请确认脱敏后重新提取；尚未发送任何问答。');
  });
  $('case-extract').addEventListener('click', () => {
    confirmed=null; localConfirmed=null; $('case-send-consent').checked=false;
    pending=true; $('case-preview').hidden=true; $('case-reviewed').checked=false;
    if (!$('case-anonymized').checked) { message('请先人工确认已脱敏。'); return; }
    const result=parser.parseCase($('case-raw').value, products);
    if (result.blocked) { message(result.gaps.join(' ')); return; }
    for (const [field,id] of Object.entries(fields)) $(id).value=result[field];
    $('case-product-match').textContent='原始产品字段：' + (result.product_text || '未识别') + '。匹配仅为建议，请核对下方产品选择。';
    $('case-gaps').replaceChildren(...result.gaps.map(text => {const li=document.createElement('li');li.textContent=text;return li;}));
    $('case-preview').hidden=false; message('已在浏览器本地提取。请编辑并人工核对，未调用模型。');
  });
  for (const id of Object.values(fields)) $(id).addEventListener('input', invalidate);
  $('case-send-consent').addEventListener('change',()=>{if(confirmed) invalidate();});
  $('case-reviewed').addEventListener('change',()=>{if((confirmed || localConfirmed) && !$('case-reviewed').checked) invalidate();});
  for (const [id,field] of [['assistant-question','case-fault'],['assistant-product','case-product'],['assistant-firmware','case-firmware']]) {
    for (const event of ['input','change']) $(id).addEventListener(event,()=>{
      if(!applying && (confirmed || (pending && !$('case-preview').hidden))) { $(field).value=$(id).value; invalidate(); }
      else if(!applying && localConfirmed) {
        localConfirmed=null; $(field).value=$(id).value; $('case-reviewed').checked=false;
        message('PCN 案例确认已撤销，请重新人工核对；普通问答可继续使用主输入。');
      }
    });
  }
  $('case-confirm').addEventListener('click', () => {
    const values=Object.fromEntries(Object.entries(fields).map(([field,id])=>[field,$(id).value]));
    const result=parser.reviewCase(values, products, $('case-reviewed').checked);
    if (!result.payload) { message(result.errors.join(' ')); return; }
    const review=parser.reviewCaseContext(values,$('case-send-consent').checked);
    if(review.errors.length) {message(review.errors.join(' '));return;}
    const payload=result.payload;
    applying=true;
    for (const [id,value] of [['assistant-product',payload.product_id],['assistant-firmware',payload.firmware_version],['assistant-question',payload.query]]) {
      $(id).value=value; $(id).dispatchEvent(new Event('input',{bubbles:true})); $(id).dispatchEvent(new Event('change',{bubbles:true}));
    }
    applying=false; confirmed=review.context ? {main:mainValues(),context:review.context} : null;
    localConfirmed={main:mainValues(),values:{...values}};
    pending=false; $('case-raw').value='';
    message(confirmed ? '已确认本次案例发送。请主动点击生成；修改字段后须重新确认。' : '仅填入故障描述、产品和固件，案例记录不会发送给模型；可主动进行本地 PCN 审查。');
    toggleContextDrawer(false); $('assistant-question').focus();
  });
  $('case-intake').addEventListener('keydown', event => {
    if (event.key==='Enter' && event.target.tagName==='INPUT') event.preventDefault();
  });
  function renderResult(data) {
    const target=$('assistant-case-result');target.replaceChildren();target.hidden=!(data.case_records || []).length;
    if(target.hidden)return;
    node('h3','案例已记录的信息 · 人工确认来源',target);
    node('p','以下为本次确认的案例原文，不是 PDF 证据或现场确诊。案例不能覆盖手册；计划不能当作完成。',target,'steps-intro');
    for(const record of data.case_records) {
      const card=node('article','',target,'assistant-step-card');
      const body=node('div','',card,'step-body-col');
      node('strong',record.label,body);
      node('pre',record.quote,body,'citation-pre');
      const assessment=(data.case_assessment || []).find(item=>item.record_id===record.record_id);
      node('p',assessment ? assessment.hint : '仅展示人工确认的案例记录；本次没有通过校验的模型案例核对提示。',body,'steps-intro');
    }
    node('p','下面的 PDF 中文释义与技术动作仍只依据手册。案例已记录项目应先核对实际执行情况，资料不足时不推导替代维修流程。',target,'steps-intro');
  }
  function copyText(data) {
    if(!(data.case_records || []).length)return '';
    let text='\n【案例来源：本次人工确认记录，非 PDF 证据／现场确诊】\n';
    for(const record of data.case_records) {
      text+=record.label+'：'+record.quote+'\n';
      const assessment=(data.case_assessment || []).find(item=>item.record_id===record.record_id);
      if(assessment)text+='案例核对提示：'+assessment.hint+'\n';
    }
    return text+'案例不能覆盖手册，计划操作不代表已执行或手册要求。\n\n【PDF 来源：可信证据释义】\n';
  }
  window.SKECaseContext={clear,renderResult,copyText,getContext:()=>confirmed && matches() ? {...confirmed.context} : null,
    getLocalContext:()=>{
      if(!localConfirmed || JSON.stringify(mainValues())!==JSON.stringify(localConfirmed.main))return null;
      const result=parser.reviewCaseContext(localConfirmed.values,true);
      if(result.errors.length)message(result.errors.join(' '));
      return result.context;
    },
    hasPendingReview:()=>pending || !matches(),showPending:()=>{
    $('case-intake').hidden=false; message('案例尚未确认。请核对并填入问答，或清除本地案例后再生成。'); $('case-message').focus();
  }};
  window.addEventListener('pagehide', clear);
})();
