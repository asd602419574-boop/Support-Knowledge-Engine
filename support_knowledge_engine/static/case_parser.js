/* Local deterministic intake. Case observations are never PDF evidence. */
(function (root) {
  'use strict';
  const MAX_CASE_CHARS = 12000;
  const normalize = value => String(value || '').replace(/\r\n?/g, '\n').trim();
  const key = value => normalize(value).normalize('NFKC').toLowerCase().replace(/\s+/g, ' ');
  function privacyRisks(value) {
    const text = normalize(value).normalize('NFKC'), risks = [];
    if (/(?:\+?86[-\s]*)?1[3-9](?:[-\s]*\d){9}(?!\d)/.test(text)) risks.push('手机号');
    if (/[\w.+-]+@[\w.-]+\.[a-z]{2,}/i.test(text)) risks.push('邮箱');
    const labelled = /(?:^|[\n,，;； ])(?:SN|序列号|工单号|案例号|案例编号|工单编号|工单标识|客户姓名|客户身份|客户|姓名|操作人|操作人员|地址|客户地址|电话|手机号|邮箱)[ \t]*(?:[:：=][ \t]*|[ \t]+)([^\n,，;；]+)/gi;
    for (const match of text.matchAll(labelled)) {
      if (!/^(?:已脱敏|已移除|未提供|无|[＊*Xx_-]+)$/.test(match[1].trim())) risks.push('身份、序列号或案例标识');
    }
    if (/\b(?:CASE|CAS|WO)[-_][A-Z0-9][A-Z0-9_-]{3,}\b/i.test(text)) risks.push('案例标识');
    return [...new Set(risks)];
  }
  const labels = {
    产品名称:'product', 产品型号:'product', 产品:'product', 设备型号:'product', 机型:'product', 型号:'product',
    故障分类:'category', 产品故障:'category', 具体故障描述:'fault', 故障描述:'fault', 故障现象:'fault',
    检测记录:'detection', 检测结果:'detection', 检测过程:'detection',
    已执行处理:'done', 已完成处理:'done', 维修记录:'treatment', 处理过程:'treatment', 案例记录:'treatment',
    后续计划:'pending', 计划处理:'pending', 建议操作:'pending', 固件版本:'firmware',
    工单说明:'ignore', 备注:'ignore', SN:'ignore', 序列号:'ignore', 工单号:'ignore', 案例号:'ignore'
  };
  const header = new RegExp('(' + Object.keys(labels).sort((a,b) => b.length-a.length).join('|') + ')\\s*[:：]', 'g');
  const planned = /计划|建议|拟|待执行|待检查|待更换|尚未|未执行|未完成|未更换|未检查|未校准|准备|将要/;
  function parseCase(value, products) {
    const text = normalize(value);
    const result = {product_text:'', product_id:'', category:'', fault_desc:'', detection_records:'', completed_actions:'', pending_actions:'', firmware_version:'', gaps:[], blocked:false};
    if (String(value || '').length > MAX_CASE_CHARS || privacyRisks(text).length) {
      result.blocked = true;
      result.gaps.push(String(value || '').length > MAX_CASE_CHARS ? '案例超过 12000 字，请精简后重新提取。' : '发现明显隐私风险，请移除手机号、邮箱、身份及案例标识后重试。');
      return result;
    }
    const sections = {product:[], category:[], fault:[], detection:[], done:[], treatment:[], pending:[], firmware:[], ignore:[]};
    let active = 'ignore';
    for (const line of text.split('\n')) {
      header.lastIndex = 0;
      const matches = [...line.matchAll(header)];
      if (matches.length) {
        for (let i=0; i<matches.length; i++) {
          active = labels[matches[i][1]];
          const fragment = line.slice(matches[i].index + matches[i][0].length, matches[i+1]?.index).trim();
          if (fragment) sections[active].push(fragment);
        }
      } else if (/^\s*[^:：]{1,20}[:：]/.test(line)) { active = 'ignore'; }
      else if (line.trim()) sections[active].push(line.trim());
    }
    result.product_text = sections.product.join(' / ');
    const candidates = sections.product.map(name => products.filter(p => [p.name, ...(p.aliases || [])].some(n => key(n) === key(name))));
    if (candidates.length && candidates.every(c => c.length === 1) && new Set(candidates.map(c => String(c[0].id))).size === 1) result.product_id = String(candidates[0][0].id);
    else result.gaps.push('产品未识别、存在歧义或字段矛盾，请人工选择现有产品。');
    result.category = sections.category.join('\n');
    if (/^(?:未知|不确定|待确认|待核对|不详|未提供)$/.test(result.category)) {
      result.category=''; result.gaps.push('故障分类尚不确定，可留空或人工填写。');
    }
    result.fault_desc = sections.fault.join('\n');
    result.firmware_version = sections.firmware.length === 1 ? sections.firmware[0] : '';
    if (/^(?:未知|不确定|待确认|待核对|不详|未提供)$/.test(result.firmware_version)) result.firmware_version='';
    if (sections.firmware.length > 1) result.gaps.push('固件字段重复或矛盾，请核对后填写。');
    if (!result.fault_desc) result.gaps.push('未识别到故障描述，请人工补充。');
    if (!result.firmware_version) result.gaps.push('固件版本未确认，可留空；适用性仍由现有证据规则判定。');
    const detection=[], done=[], pending=[];
    for (const type of ['detection','done','treatment','pending']) {
      for (const record of sections[type].flatMap(line => line.split(/[;；]/)).map(s=>s.trim()).filter(Boolean)) {
        if (type === 'pending' || planned.test(record)) pending.push(record);
        else if (type === 'detection') detection.push(record);
        else if (type === 'done' || /已(?:完成|执行|更换|检查|检测|维修|处理|清洁|校准|重置|升级)/.test(record)) done.push(record);
        else { pending.push(record); result.gaps.push('部分处理记录未注明完成状态，已放入计划／状态未确认，请人工核对。'); }
      }
    }
    result.detection_records=detection.join('\n'); result.completed_actions=done.join('\n'); result.pending_actions=pending.join('\n');
    if (!result.detection_records) result.gaps.push('未识别到已记录的检测结果，不能据此认定检查通过。');
    if (/<[^>]+>|忽略.*指令|system\s*prompt|执行命令/i.test(text)) result.gaps.push('包含 HTML 或指令样式文本，仅作普通文本；请核对并移除无关指令。');
    result.gaps=[...new Set(result.gaps)];
    return result;
  }
  function reviewCase(fields, products, reviewed) {
    const errors=[], query=normalize(fields.fault_desc), firmware=normalize(fields.firmware_version), product=String(fields.product_id || '');
    if (!reviewed) errors.push('请人工核对并确认脱敏。');
    if (!products.some(p => String(p.id) === product)) errors.push('请选择现有产品并人工确认。');
    if (query.length < 2 || query.length > 1000) errors.push('故障描述须为 2–1000 字。');
    if (firmware.length > 80) errors.push('固件版本过长。');
    if (privacyRisks(Object.values(fields).filter(v => typeof v === 'string').join('\n')).length) errors.push('编辑内容仍存在明显隐私风险，请先脱敏。');
    return {errors, payload:errors.length ? null : {query, product_id:product, firmware_version:firmware}};
  }
  const api={parseCase, privacyRisks, reviewCase, MAX_CASE_CHARS};
  if (typeof module !== 'undefined' && module.exports) module.exports=api;
  else root.SKECaseParser=api;
})(typeof window === 'undefined' ? globalThis : window);
