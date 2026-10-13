'use strict';

const form = document.getElementById('assistant-form');
const statusText = document.getElementById('assistant-status');
const statusDetail = document.getElementById('assistant-status-detail');
const statusBox = document.getElementById('assistant-status-box');
const statusIcon = document.getElementById('assistant-status-icon');
const resultPanel = document.getElementById('assistant-result');
const button = document.getElementById('assistant-submit');
const stepsContainer = document.getElementById('assistant-steps-container');
const stepsList = document.getElementById('assistant-steps');
const factsList = document.getElementById('assistant-facts');
const cautionsList = document.getElementById('assistant-cautions');
const missingList = document.getElementById('assistant-missing');
const coverageText = document.getElementById('assistant-coverage');
const noActionsText = document.getElementById('assistant-no-actions');
const itemLabels = {fact: '已知故障信息', action: '建议核查事项', caution: '适用条件和注意事项'};
const warningsList = document.getElementById('assistant-warnings');
const citationsContainer = document.getElementById('assistant-citations-container');
const citationsList = document.getElementById('assistant-citations');
const citationCountBadge = document.getElementById('assistant-citation-count');
const skeleton = document.getElementById('assistant-loading-skeleton');
const resultActions = document.getElementById('assistant-result-actions');
const copyButton = document.getElementById('assistant-copy-btn');
const questionInput = document.getElementById('assistant-question');
const charCountSpan = document.getElementById('assistant-char-count');
const productSelect = document.getElementById('assistant-product');
const firmwareInput = document.getElementById('assistant-firmware');

let currentQueryResult = null;

function node(tag, text, parent, className) {
  const element = document.createElement(tag);
  if (text !== undefined && text !== null && text !== '') {
    element.textContent = text;
  }
  if (className) {
    element.className = className;
  }
  if (parent) {
    parent.appendChild(element);
  }
  return element;
}

// 字符数计数器
if (questionInput && charCountSpan) {
  questionInput.addEventListener('input', () => {
    const len = questionInput.value.length;
    charCountSpan.textContent = len + ' / 1000 字';
  });
}

// 快捷测试示例填入
document.querySelectorAll('.example-chip').forEach((chip) => {
  chip.addEventListener('click', () => {
    const productId = chip.dataset.product;
    const query = chip.dataset.query;
    if (productId && productSelect) {
      productSelect.value = productId;
    }
    if (query && questionInput) {
      questionInput.value = query;
      questionInput.dispatchEvent(new Event('input'));
      questionInput.focus();
    }
  });
});

// 状态横幅模式切换
function setStatusBanner(state, titleText, detailText, iconText) {
  if (!statusBox) return;
  statusBox.className = 'status-banner status-banner-' + state;
  if (statusIcon) statusIcon.textContent = iconText;
  if (statusText) statusText.textContent = titleText;
  if (statusDetail) statusDetail.textContent = detailText;
}

// 一键复制排查内容（严格绑定本次已完成请求的输入快照，防止读取未提交的新修改）
if (copyButton) {
  copyButton.addEventListener('click', async () => {
    if (!currentQueryResult || !currentQueryResult.requestSnapshot) return;
    const req = currentQueryResult.requestSnapshot;

    let text = '【AI 售后排查建议】\n';
    if (req.productName) text += '适用产品：' + req.productName + '\n';
    if (req.firmware) text += '固件版本：' + req.firmware + '\n';
    if (req.query) text += '故障现象：' + req.query + '\n';
    text += '判定状态：' + (currentQueryResult.decision || '未知') + '\n\n';

    text += '中文内容为 AI 释义，不是手册原文或现场确诊。\n';
    text += '资料覆盖度（模型评估）：' + (currentQueryResult.coverage || 'none') + '\n\n';
    const items = currentQueryResult.items || [];
    items.forEach((step, idx) => {
      const cit = (currentQueryResult.citations || []).find((c) => c.evidence_id === step.evidence_id);
      const ref = cit ? '（参考：' + cit.filename + ' 第 ' + cit.page_number + ' 页）' : '';
      text += '【' + itemLabels[step.type] + '】' + step.text + ' ' + ref + '\n';
      text += '对应手册原文：' + step.quote + '\n';
    });
    if (!(currentQueryResult.steps || []).length) text += '\n现有回答没有可确认的具体操作步骤。\n';
    text += '\n尚需补充的信息：\n' + (currentQueryResult.missing_information || []).join('\n') + '\n';

    if (currentQueryResult.citations && currentQueryResult.citations.length > 0) {
      text += '\n证据来源文档：\n';
      currentQueryResult.citations.forEach((c, idx) => {
        text += '[' + (idx + 1) + '] ' + c.filename + '（第 ' + c.page_number + ' 页）\n';
        text += 'PDF：' + new URL(c.pdf_url, window.location.origin).href + '\nEvidence ID：' + c.evidence_id + '\n';
      });
    }

    text += '\n说明：来源和引用匹配校验不能证明中文语义完全正确，请核对原文条件和限制。';

    try {
      await navigator.clipboard.writeText(text);
      const oldText = copyButton.textContent;
      copyButton.textContent = '✅ 已复制到剪贴板！';
      copyButton.classList.add('copy-success');
      setTimeout(() => {
        copyButton.textContent = oldText;
        copyButton.classList.remove('copy-success');
      }, 2000);
    } catch {
      alert('复制失败，请手动选择复制。');
    }
  });
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  button.disabled = true;
  resultPanel.setAttribute('aria-busy', 'true');
  currentQueryResult = null;

  // 捕获本次提交时的请求输入快照
  const requestSnapshot = {
    productName: productSelect && productSelect.selectedIndex >= 0
      ? productSelect.options[productSelect.selectedIndex].text
      : '',
    firmware: firmwareInput ? firmwareInput.value.trim() : '',
    query: questionInput ? questionInput.value.trim() : '',
  };

  // 切换为检索生成中状态
  setStatusBanner(
    'loading',
    '正在检索和核验证据；通过后调用模型，请稍候…',
    '系统正在执行 G4 本地知识检索与 G3 证据可信判定，如核验通过将调取大模型生成排查要点。',
    '⏳'
  );

  if (skeleton) skeleton.style.display = 'block';
  if (stepsContainer) stepsContainer.style.display = 'none';
  if (citationsContainer) citationsContainer.style.display = 'none';
  if (resultActions) resultActions.style.display = 'none';

  stepsList.replaceChildren();
  factsList.replaceChildren();
  cautionsList.replaceChildren();
  missingList.replaceChildren();
  warningsList.replaceChildren();
  citationsList.replaceChildren();

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 45000);

  try {
    const fields = new FormData(form);
    const response = await fetch(form.dataset.url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Assistant-CSRF': form.dataset.csrf,
      },
      body: JSON.stringify(Object.fromEntries(fields)),
      signal: controller.signal,
    });

    const data = await response.json();
    // 将返回结果与本次请求输入快照强绑定
    currentQueryResult = {
      ...data,
      requestSnapshot: requestSnapshot,
    };

    // 根据不同状态设置状态栏
    const decisionSuffix = data.decision ? '（证据判定：' + data.decision + '）' : '';
    const mainTitle = (data.message || '处理完成') + decisionSuffix;

    if (data.status === 'answered' && data.decision === 'supported') {
      const citCount = data.citations ? data.citations.length : 0;
      setStatusBanner(
        'success',
        mainTitle,
        '已基于 ' + citCount + ' 项 PDF 证据生成中文解释，来源匹配已核验；中文语义请与原文对照。',
        '✅'
      );
    } else if (data.decision === 'abstain' || data.status === 'abstain') {
      setStatusBanner(
        'warning',
        mainTitle,
        '在已导入技术资料中未能检索到与当前产品或故障完全匹配的有效证据，已触发安全拒答。',
        '⚠️'
      );
    } else if (data.decision === 'conflict' || data.status === 'conflict') {
      setStatusBanner(
        'conflict',
        mainTitle,
        '检索到的历史或不同版本手册之间存在结论冲突，无法确定唯一依据，请核实有效版本。',
        '⚡'
      );
    } else {
      // 错误或未配置等状态
      setStatusBanner(
        'error',
        mainTitle,
        '未能生成有效排查建议。若为模型配置或超时问题，请检查模型服务状态。',
        '❌'
      );
    }

    // 结构化渲染 Warnings 与说明
    for (const warning of data.warnings || []) {
      const alertDiv = node('div', '', warningsList, 'alert-card');
      if (warning.startsWith('已从问题提取故障码')) {
        alertDiv.classList.add('alert-info');
        const header = node('div', '', alertDiv, 'alert-card-header');
        node('span', '🔍 检索查询解析', header, 'alert-card-tag');
        node('p', warning, alertDiv, 'alert-card-text');
      } else if (warning.includes('AI 基于资料的释义') || warning.includes('这不是现场故障确诊')) {
        alertDiv.classList.add('alert-governance');
        const header = node('div', '', alertDiv, 'alert-card-header');
        node('span', '🛡️ 治理与免责准则', header, 'alert-card-tag');
        node('p', warning, alertDiv, 'alert-card-text');
      } else {
        alertDiv.classList.add('alert-warning');
        const header = node('div', '', alertDiv, 'alert-card-header');
        node('span', '⚠️ 注意事项', header, 'alert-card-tag');
        node('p', warning, alertDiv, 'alert-card-text');
      }
    }

    // 渲染排查步骤 (Steps)
    const items = data.items || [];
    const missing = data.missing_information || [];
    if (items.length > 0 || missing.length > 0) {
      if (stepsContainer) stepsContainer.style.display = 'block';
      if (resultActions) resultActions.style.display = 'block';

      coverageText.textContent = ({partial: '资料覆盖度：部分覆盖，尚缺处理条件或具体流程。',
        sufficient: '资料覆盖度：模型认为可支持所问核查事项，仍不代表确诊或完整维修方案。',
        none: '资料覆盖度：没有可用解释。'})[data.coverage || 'none'];
      noActionsText.style.display = (data.steps || []).length ? 'none' : 'block';
      missing.forEach((info) => node('li', info, missingList));
      if (!missing.length) node('li', '模型未列出其他基础信息缺口；如需实际维修，仍须核对完整适用流程。', missingList);
      let actionIndex = 0;
      items.forEach((step) => {
        const target = step.type === 'action' ? stepsList : step.type === 'caution' ? cautionsList : factsList;
        const li = node(step.type === 'action' ? 'li' : 'article', '', target, 'assistant-step-card');
        const badgeCol = node('div', '', li, 'step-badge-col');
        const numText = step.type === 'action' ? String(++actionIndex).padStart(2, '0') : step.type === 'fact' ? '知' : '注';
        node('span', numText, badgeCol, 'step-number-badge');

        const bodyCol = node('div', '', li, 'step-body-col');
        const tag = node('div', '', bodyCol, 'step-tag-row');
        node('span', 'AI 中文释义 · ' + itemLabels[step.type], tag, 'step-tag');

        node('p', step.text, bodyCol, 'step-text');
        const comparison = node('details', '', bodyCol, 'citation-details');
        node('summary', '对照本项手册原文（引用匹配已核验）', comparison);
        node('pre', step.quote, comparison, 'citation-pre');

        // 查找对应 Citation
        const citIndex = (data.citations || []).findIndex((c) => c.evidence_id === step.evidence_id);
        if (citIndex >= 0) {
          const cit = data.citations[citIndex];
          const metaRow = node('div', '', bodyCol, 'step-meta-row');
          const link = node('a', '📖 查看引用 [' + (citIndex + 1) + '] · ' + cit.filename + '（第 ' + cit.page_number + ' 页）', metaRow, 'step-citation-anchor');
          link.href = '#assistant-citation-' + citIndex;
          link.title = '跳转至证据卡片';
        }
      });
      if (!factsList.children.length) node('p', '本次回答未提供故障含义说明，请补充适用资料并对照原文。', factsList, 'steps-intro');
      if (!cautionsList.children.length) node('p', '本次回答未列出注意事项；仍需核对原文完整条件。', cautionsList, 'steps-intro');
    } else {
      if (stepsContainer) stepsContainer.style.display = 'none';
      if (resultActions) resultActions.style.display = 'none';
    }

    // 渲染引用 (Citations)
    const citations = data.citations || [];
    if (citations.length > 0) {
      if (citationsContainer) citationsContainer.style.display = 'block';
      if (citationCountBadge) citationCountBadge.textContent = citations.length + ' 项证据引用';

      citations.forEach((citation, index) => {
        const article = node('article', '', citationsList, 'citation-card');
        article.id = 'assistant-citation-' + index;

        // 卡片头部：文件名与页码
        const topBar = node('div', '', article, 'citation-header');
        const titleArea = node('div', '', topBar, 'citation-title-area');
        node('span', '[' + (index + 1) + ']', titleArea, 'citation-index-tag');
        node('h4', citation.filename, titleArea, 'citation-filename');

        const pageBadge = node('span', '📄 第 ' + citation.page_number + ' 页 (P.' + citation.page_number + ')', topBar, 'citation-page-badge');

        // 操作按钮行：打开 PDF
        const actionRow = node('div', '', article, 'citation-actions');
        const pdfLink = node('a', '在浏览器打开 PDF 对应页 ↗', actionRow, 'button-primary button-small pdf-open-btn');
        pdfLink.href = citation.pdf_url;
        pdfLink.target = '_blank';
        pdfLink.rel = 'noopener noreferrer';

        if (citation.document_url) {
          const docLink = node('a', '查看文档元数据', actionRow, 'button-secondary button-small');
          docLink.href = citation.document_url;
          docLink.target = '_blank';
          docLink.rel = 'noopener noreferrer';
        }

        // 技术溯源凭据折叠面板 (Evidence ID)
        const techDetails = node('details', '', article, 'citation-details citation-tech-details');
        node('summary', '查看技术追溯编号与 SHA-256 (Evidence ID)', techDetails);
        const techGrid = node('div', '', techDetails, 'tech-meta-box');
        node('div', '文档内部 ID：' + citation.document_id, techGrid, 'tech-meta-item');
        node('div', 'Evidence ID：' + citation.evidence_id, techGrid, 'tech-meta-item hash-item');
        if (citation.pdf_sha256) {
          node('div', 'PDF 文件 SHA-256：' + citation.pdf_sha256, techGrid, 'tech-meta-item hash-item');
        }

        // 本页提取原文折叠面板
        const originalDetails = node('details', '', article, 'citation-details citation-original-details');
        node('summary', '查看本页提取原文片段 (PDF Extract)', originalDetails);
        const pre = node('pre', citation.original_text, originalDetails, 'citation-pre');
      });
    } else {
      if (citationsContainer) citationsContainer.style.display = 'none';
    }

  } catch (error) {
    const errorMsg = error.name === 'AbortError'
      ? '等待服务超时（45秒），未收到可验证回答。请检查模型或稍后重试。'
      : '网络或本地服务请求失败。请确认本地 Web 服务正常运行后重试。';
    setStatusBanner('error', '请求未完成', errorMsg, '❌');
  } finally {
    clearTimeout(timeout);
    button.disabled = false;
    resultPanel.setAttribute('aria-busy', 'false');
    if (skeleton) skeleton.style.display = 'none';
  }
});
