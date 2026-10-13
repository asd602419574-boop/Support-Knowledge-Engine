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

// 一键复制排查内容
if (copyButton) {
  copyButton.addEventListener('click', async () => {
    if (!currentQueryResult) return;
    const productName = productSelect && productSelect.selectedIndex >= 0
      ? productSelect.options[productSelect.selectedIndex].text
      : '';
    const query = questionInput ? questionInput.value.trim() : '';

    let text = '【AI 售后排查建议】\n';
    if (productName) text += '适用产品：' + productName + '\n';
    if (query) text += '故障现象：' + query + '\n';
    text += '判定状态：' + (currentQueryResult.decision || '未知') + '\n\n';

    text += '官方依据核查项：\n';
    const steps = currentQueryResult.steps || [];
    steps.forEach((step, idx) => {
      const cit = (currentQueryResult.citations || []).find((c) => c.evidence_id === step.evidence_id);
      const ref = cit ? '（参考：' + cit.filename + ' 第 ' + cit.page_number + ' 页）' : '';
      text += (idx + 1) + '. ' + step.text + ' ' + ref + '\n';
    });

    if (currentQueryResult.citations && currentQueryResult.citations.length > 0) {
      text += '\n证据来源文档：\n';
      currentQueryResult.citations.forEach((c, idx) => {
        text += '[' + (idx + 1) + '] ' + c.filename + '（第 ' + c.page_number + ' 页）\n';
      });
    }

    text += '\n说明：本建议选自已核验官方技术手册，请结合实机测试条件按需核查。';

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
    currentQueryResult = data;

    // 根据不同状态设置状态栏
    const decisionSuffix = data.decision ? '（证据判定：' + data.decision + '）' : '';
    const mainTitle = (data.message || '处理完成') + decisionSuffix;

    if (data.status === 'answered' && data.decision === 'supported') {
      const citCount = data.citations ? data.citations.length : 0;
      setStatusBanner(
        'success',
        mainTitle,
        '已基于 ' + citCount + ' 份官方核验 PDF 技术手册生成可溯源排查依据。',
        '✅'
      );
    } else if (data.decision === 'abstain' || data.status === 'abstain') {
      setStatusBanner(
        'warning',
        mainTitle,
        '官方知识库中未能检索到与当前产品或故障完全对应的可信证据，已触发安全拒答。',
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
      } else if (warning.includes('首版使用原文排查清单') || warning.includes('这不是现场故障确诊')) {
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
    const steps = data.steps || [];
    if (steps.length > 0) {
      if (stepsContainer) stepsContainer.style.display = 'block';
      if (resultActions) resultActions.style.display = 'block';

      steps.forEach((step, idx) => {
        const li = node('li', '', stepsList, 'assistant-step-card');
        const badgeCol = node('div', '', li, 'step-badge-col');
        const numText = (idx + 1) < 10 ? '0' + (idx + 1) : '' + (idx + 1);
        node('span', numText, badgeCol, 'step-number-badge');

        const bodyCol = node('div', '', li, 'step-body-col');
        const tag = node('div', '', bodyCol, 'step-tag-row');
        node('span', '官方依据核查项', tag, 'step-tag');

        node('p', step.text, bodyCol, 'step-text');

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
    } else {
      if (stepsContainer) stepsContainer.style.display = 'none';
      if (resultActions) resultActions.style.display = 'none';
    }

    // 渲染引用 (Citations)
    const citations = data.citations || [];
    if (citations.length > 0) {
      if (citationsContainer) citationsContainer.style.display = 'block';
      if (citationCountBadge) citationCountBadge.textContent = citations.length + ' 份官方手册引用';

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
