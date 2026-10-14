/* No network, storage, URL transport, or implicit model submission. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const parser=window.SKECaseParser, products=JSON.parse($('assistant-product-catalog').textContent);
  const fields={product_id:'case-product', category:'case-category', fault_desc:'case-fault',
    detection_records:'case-detection', completed_actions:'case-done', pending_actions:'case-pending', firmware_version:'case-firmware'};
  let pending=false;
  const message=text => { $('case-message').textContent=text; };
  function clear() {
    $('case-raw').value='';
    for (const id of Object.values(fields)) $(id).value='';
    $('case-anonymized').checked=false; $('case-reviewed').checked=false;
    $('case-preview').hidden=true; $('case-gaps').replaceChildren();
    $('case-product-match').textContent=''; pending=false;
    message('本地案例已清除；已填入的问答内容可在主输入框编辑。');
  }
  $('assistant-case-open').addEventListener('click', () => {
    toggleContextDrawer(true); $('case-intake').hidden=false; $('case-raw').focus();
  });
  $('case-clear').addEventListener('click', clear);
  $('case-raw').addEventListener('input', () => {
    pending=Boolean($('case-raw').value.trim()); $('case-preview').hidden=true;
    $('case-reviewed').checked=false; $('case-anonymized').checked=false;
    message('请确认脱敏后重新提取；尚未发送任何问答。');
  });
  $('case-extract').addEventListener('click', () => {
    pending=true; $('case-preview').hidden=true; $('case-reviewed').checked=false;
    if (!$('case-anonymized').checked) { message('请先人工确认已脱敏。'); return; }
    const result=parser.parseCase($('case-raw').value, products);
    if (result.blocked) { message(result.gaps.join(' ')); return; }
    for (const [field,id] of Object.entries(fields)) $(id).value=result[field];
    $('case-product-match').textContent='原始产品字段：' + (result.product_text || '未识别') + '。匹配仅为建议，请核对下方产品选择。';
    $('case-gaps').replaceChildren(...result.gaps.map(text => {const li=document.createElement('li');li.textContent=text;return li;}));
    $('case-preview').hidden=false; message('已在浏览器本地提取。请编辑并人工核对，未调用模型。');
  });
  for (const id of Object.values(fields)) $(id).addEventListener('input', () => {pending=true; $('case-reviewed').checked=false;});
  $('case-confirm').addEventListener('click', () => {
    const values=Object.fromEntries(Object.entries(fields).map(([field,id])=>[field,$(id).value]));
    const result=parser.reviewCase(values, products, $('case-reviewed').checked);
    if (!result.payload) { message(result.errors.join(' ')); return; }
    const payload=result.payload;
    for (const [id,value] of [['assistant-product',payload.product_id],['assistant-firmware',payload.firmware_version],['assistant-question',payload.query]]) {
      $(id).value=value; $(id).dispatchEvent(new Event('input',{bubbles:true})); $(id).dispatchEvent(new Event('change',{bubbles:true}));
    }
    pending=false; $('case-raw').value='';
    message('已填入确认后的故障描述、产品和固件；检测／处理未进入问答。请主动点击生成排查建议。');
    toggleContextDrawer(false); $('assistant-question').focus();
  });
  $('case-intake').addEventListener('keydown', event => {
    if (event.key==='Enter' && event.target.tagName==='INPUT') event.preventDefault();
  });
  window.SKECaseContext={clear,hasPendingReview:()=>pending,showPending:()=>{
    $('case-intake').hidden=false; message('案例尚未确认。请核对并填入问答，或清除本地案例后再生成。'); $('case-message').focus();
  }};
  window.addEventListener('pagehide', clear);
})();
