'use strict';

// 核心 DOM 节点引用
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
const itemLabels = { fact: '已知故障信息', action: '建议核查事项', caution: '适用条件和注意事项' };
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

// 对话工作台扩展节点
const welcomeContainer = document.getElementById('assistant-welcome');
const userBubble = document.getElementById('assistant-user-bubble');
const userQueryText = document.getElementById('assistant-user-query-text');
const userProductTag = document.getElementById('assistant-user-product-tag');
const userFirmwareTag = document.getElementById('assistant-user-firmware-tag');
const loadingBubble = document.getElementById('assistant-loading-bubble');
const contextDrawer = document.getElementById('assistant-context-drawer');
const contextToggle = document.getElementById('assistant-context-toggle');
const contextClose = document.getElementById('assistant-context-close');
const productBadge = document.getElementById('assistant-product-badge');
const contextDot = document.getElementById('assistant-context-dot');
const clearButton = document.getElementById('assistant-clear-btn');

// 右栏上下文与证据联动节点
const panelContextProduct = document.getElementById('panel-context-product');
const panelContextFirmware = document.getElementById('panel-context-firmware');
const panelCitationsContainer = document.getElementById('assistant-panel-citations');
const panelEmpty = document.getElementById('assistant-panel-empty');
const panelCitationCountBadge = document.getElementById('assistant-panel-citation-count');

let currentQueryResult = null;

// 安全 DOM 节点创建工具（避免不安全 innerHTML 拼接）
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

// 字符数计数器与输入框高度自适应
if (questionInput) {
  const autoResize = () => {
    questionInput.style.height = 'auto';
    questionInput.style.height = Math.min(140, Math.max(40, questionInput.scrollHeight)) + 'px';
  };

  questionInput.addEventListener('input', () => {
    const len = questionInput.value.length;
    if (charCountSpan) {
      charCountSpan.textContent = len + ' / 1000 字';
    }
    autoResize();
  });

  // 支持 Ctrl+Enter / Cmd+Enter 快速提交
  questionInput.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault();
      if (form) {
        form.requestSubmit();
      }
    }
  });
}

// 产品选择变更与胶囊状态及右栏上下文同步
function syncProductBadge() {
  if (!productSelect) return;
  const selectedIndex = productSelect.selectedIndex;
  if (selectedIndex > 0 && productSelect.value) {
    const text = productSelect.options[selectedIndex].text;
    if (productBadge) productBadge.textContent = '📌 产品: ' + text;
    if (contextToggle) contextToggle.classList.add('has-product');
    if (contextDot) contextDot.classList.add('active');
    if (panelContextProduct) panelContextProduct.textContent = text;
  } else {
    if (productBadge) productBadge.textContent = '⚠️ 请选择适用产品型号 *';
    if (contextToggle) contextToggle.classList.remove('has-product');
    if (contextDot) contextDot.classList.remove('active');
    if (panelContextProduct) panelContextProduct.textContent = '未指定';
  }

  if (panelContextFirmware && firmwareInput) {
    panelContextFirmware.textContent = firmwareInput.value.trim() || '未限定（保守适用）';
  }
}

if (productSelect) {
  productSelect.addEventListener('change', syncProductBadge);
}
if (firmwareInput) {
  firmwareInput.addEventListener('input', () => {
    if (panelContextFirmware) {
      panelContextFirmware.textContent = firmwareInput.value.trim() || '未限定（保守适用）';
    }
  });
}
syncProductBadge();

// 上下文抽屉展开/折叠
function toggleContextDrawer(force) {
  if (!contextDrawer) return;
  const isHidden = contextDrawer.style.display === 'none' || !contextDrawer.style.display;
  const show = force !== undefined ? force : isHidden;
  contextDrawer.style.display = show ? 'block' : 'none';
  if (contextToggle) {
    contextToggle.classList.toggle('active', show);
  }
  if (show && productSelect) {
    productSelect.focus();
  }
}

if (contextToggle) {
  contextToggle.addEventListener('click', () => toggleContextDrawer());
}
if (contextClose) {
  contextClose.addEventListener('click', () => toggleContextDrawer(false));
}

// 快捷测试示例填入
document.querySelectorAll('.example-chip').forEach((chip) => {
  chip.addEventListener('click', () => {
    const productId = chip.dataset.product;
    const query = chip.dataset.query;
    if (productId && productSelect) {
      productSelect.value = productId;
      syncProductBadge();
    }
    if (query && questionInput) {
      questionInput.value = query;
      questionInput.dispatchEvent(new Event('input'));
      questionInput.focus();
    }
    toggleContextDrawer(false);
  });
});

// 清空当前输入并返回欢迎态
if (clearButton) {
  clearButton.addEventListener('click', () => {
    if (questionInput) {
      questionInput.value = '';
      questionInput.style.height = 'auto';
      if (charCountSpan) charCountSpan.textContent = '0 / 1000 字';
    }
    if (userBubble) userBubble.style.display = 'none';
    if (loadingBubble) loadingBubble.style.display = 'none';
    if (resultPanel) {
      resultPanel.style.display = 'none';
      resultPanel.setAttribute('aria-busy', 'false');
    }
    if (welcomeContainer) welcomeContainer.style.display = 'block';
    
    // 清空右侧面板证据卡片并恢复空态
    if (panelCitationsContainer) {
      panelCitationsContainer.replaceChildren();
      panelCitationsContainer.style.display = 'none';
    }
    if (panelEmpty) panelEmpty.style.display = 'block';
    if (panelCitationCountBadge) panelCitationCountBadge.textContent = '0 项证据';

    currentQueryResult = null;
    setStatusBanner(
      'idle',
      '等待提交问题。请选择产品并输入故障描述。',
      '系统将检索本地已校验的 PDF 技术文档，并通过严格的可信证据判定后由模型整理排查依据。',
      '💡'
    );
  });
}

// 状态横幅模式切换
function setStatusBanner(state, titleText, detailText, iconText) {
  if (!statusBox) return;
  statusBox.className = 'status-banner status-banner-' + state;
  if (statusIcon) statusIcon.textContent = iconText;
  if (statusText) statusText.textContent = titleText;
  if (statusDetail) statusDetail.textContent = detailText;
}

// 渲染单个证据文档卡片 (通用渲染逻辑：支持右栏面板与中栏内联)
function renderEvidenceCard(citation, index, container, isPanel) {
  const article = node('article', '', container, isPanel ? 'wb-evidence-card' : 'citation-card');
  article.id = (isPanel ? 'panel-citation-' : 'assistant-citation-') + index;

  // 卡片头部
  const topBar = node('div', '', article, isPanel ? 'wb-evidence-header' : 'citation-header');
  const titleArea = node('div', '', topBar, isPanel ? 'wb-evidence-title-area' : 'citation-title-area');
  node('span', '[' + (index + 1) + ']', titleArea, isPanel ? 'wb-evidence-index' : 'citation-index-tag');
  const filenameEl = node('h4', citation.filename, titleArea, isPanel ? 'wb-evidence-filename' : 'citation-filename');
  filenameEl.title = citation.filename; // 确保完整名称随时悬停可查

  node('span', '📄 第 ' + citation.page_number + ' 页 (P.' + citation.page_number + ')', topBar, isPanel ? 'wb-evidence-page' : 'citation-page-badge');

  // 操作按钮行
  const actionRow = node('div', '', article, isPanel ? 'wb-evidence-actions' : 'citation-actions');
  const pdfLink = node('a', '在浏览器打开 PDF 对应页 ↗', actionRow, isPanel ? 'wb-evidence-pdf-btn' : 'button-primary button-small pdf-open-btn');
  pdfLink.href = citation.pdf_url;
  pdfLink.target = '_blank';
  pdfLink.rel = 'noopener noreferrer';

  if (citation.document_url) {
    const docLink = node('a', '查看文档元数据', actionRow, isPanel ? 'wb-evidence-meta-btn' : 'button-secondary button-small');
    docLink.href = citation.document_url;
    docLink.target = '_blank';
    docLink.rel = 'noopener noreferrer';
  }

  // 技术溯源凭据折叠面板 (Evidence ID & SHA-256，默认折叠)
  const techDetails = node('details', '', article, isPanel ? 'wb-evidence-details' : 'citation-details citation-tech-details');
  node('summary', '查看技术追溯编号与 SHA-256 (Evidence ID)', techDetails);
  const techGrid = node('div', '', techDetails, isPanel ? 'wb-evidence-tech' : 'tech-meta-box');
  node('div', '文档内部 ID：' + citation.document_id, techGrid, isPanel ? '' : 'tech-meta-item');
  node('div', 'Evidence ID：' + citation.evidence_id, techGrid, isPanel ? 'hash-item' : 'tech-meta-item hash-item');
  if (citation.pdf_sha256) {
    node('div', 'PDF 文件 SHA-256：' + citation.pdf_sha256, techGrid, isPanel ? 'hash-item' : 'tech-meta-item hash-item');
  }

  // 本页提取原文折叠面板
  const originalDetails = node('details', '', article, isPanel ? 'wb-evidence-details' : 'citation-details citation-original-details');
  node('summary', '查看本页提取原文片段 (PDF Extract)', originalDetails);
  node('pre', citation.original_text, originalDetails, isPanel ? 'wb-evidence-pre' : 'citation-pre');
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
    items.forEach((step) => {
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
      const oldText = copyButton.textContent;
      copyButton.textContent = '⚠️ 无法写入剪贴板，请手动选中文本';
      setTimeout(() => {
        copyButton.textContent = oldText;
      }, 2500);
    }
  });
}

// 表单提交与端到端交互流
form.addEventListener('submit', async (event) => {
  event.preventDefault();

  // 1. 验证产品选择 (产品为必填项：折叠时自动展开抽屉并聚焦)
  if (!productSelect || !productSelect.value) {
    toggleContextDrawer(true);
    if (productSelect) {
      productSelect.classList.add('input-error');
      productSelect.focus();
      productSelect.addEventListener('change', () => {
        productSelect.classList.remove('input-error');
      }, { once: true });
    }
    if (productBadge) {
      productBadge.textContent = '⚠️ 请先选择适用产品型号！';
      setTimeout(syncProductBadge, 3000);
    }
    return;
  }

  // 2. 验证问题内容
  const queryText = questionInput ? questionInput.value.trim() : '';
  if (queryText.length < 2) {
    if (questionInput) {
      questionInput.classList.add('input-error');
      questionInput.focus();
      questionInput.placeholder = '⚠️ 请先输入客户故障描述或故障码...';
      questionInput.addEventListener('input', () => {
        questionInput.classList.remove('input-error');
      }, { once: true });
    }
    return;
  }

  button.disabled = true;
  if (resultPanel) resultPanel.setAttribute('aria-busy', 'true');
  currentQueryResult = null;

  // 捕获本次提交时的请求输入快照
  const requestSnapshot = {
    productName: productSelect.selectedIndex >= 0
      ? productSelect.options[productSelect.selectedIndex].text
      : '',
    firmware: firmwareInput ? firmwareInput.value.trim() : '',
    query: queryText,
  };

  // 同步右栏上下文
  if (panelContextProduct) panelContextProduct.textContent = requestSnapshot.productName;
  if (panelContextFirmware) panelContextFirmware.textContent = requestSnapshot.firmware || '未限定（保守适用）';

  // 切换界面为对答流形态
  if (welcomeContainer) welcomeContainer.style.display = 'none';

  // 渲染并显示用户气泡
  if (userBubble && userQueryText) {
    if (userProductTag) userProductTag.textContent = '📌 适用产品：' + requestSnapshot.productName;
    if (userFirmwareTag) {
      if (requestSnapshot.firmware) {
        userFirmwareTag.textContent = '🏷️ 固件版本：' + requestSnapshot.firmware;
        userFirmwareTag.style.display = 'inline-flex';
      } else {
        userFirmwareTag.style.display = 'none';
      }
    }
    userQueryText.textContent = requestSnapshot.query;
    userBubble.style.display = 'flex';
  }

  // 隐藏旧结果，展示加载中气泡
  if (resultPanel) resultPanel.style.display = 'none';
  if (loadingBubble) {
    loadingBubble.style.display = 'flex';
    loadingBubble.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  // 切换状态看板为检索生成中状态
  setStatusBanner(
    'loading',
    '正在检索和核验证据；通过后调用模型，请稍候…',
    '系统正在执行本地知识检索与可信证据判定，核验通过后调取大模型生成排查要点。',
    '⏳'
  );

  if (skeleton) skeleton.style.display = 'block';
  if (stepsContainer) stepsContainer.style.display = 'none';
  if (citationsContainer) citationsContainer.style.display = 'none';
  if (resultActions) resultActions.style.display = 'none';

  // 清空之前的数据
  stepsList.replaceChildren();
  factsList.replaceChildren();
  cautionsList.replaceChildren();
  missingList.replaceChildren();
  warningsList.replaceChildren();
  citationsList.replaceChildren();
  if (panelCitationsContainer) panelCitationsContainer.replaceChildren();

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
    currentQueryResult = {
      ...data,
      requestSnapshot: requestSnapshot,
    };

    if (loadingBubble) loadingBubble.style.display = 'none';
    if (resultPanel) {
      resultPanel.style.display = 'block';
    }

    const decisionSuffix = data.decision ? '（证据判定：' + data.decision + '）' : '';
    const mainTitle = (data.message || '处理完成') + decisionSuffix;

    const isSupported = data.status === 'answered' && data.decision === 'supported';

    if (isSupported) {
      const citCount = data.citations ? data.citations.length : 0;
      setStatusBanner(
        'success',
        mainTitle,
        '已基于 ' + citCount + ' 项 PDF 证据生成中文排查释义，来源已核验；关键操作请与原文对照。',
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
      setStatusBanner(
        'error',
        mainTitle,
        '未能生成有效排查建议。若为模型配置或超时问题，请检查模型服务状态。',
        '❌'
      );
    }

    // 渲染 Warnings 与说明（成功回答时压缩技术解析与重复免责说明，其余关键禁忌与冲突完整显示）
    for (const warning of data.warnings || []) {
      if (warning.startsWith('已从问题提取故障码')) {
        const alertEl = node(isSupported ? 'details' : 'div', '', warningsList, 'alert-card alert-info' + (isSupported ? ' alert-collapsible' : ''));
        const header = node(isSupported ? 'summary' : 'div', '', alertEl, 'alert-card-header');
        node('span', '🔍 检索查询解析', header, 'alert-card-tag');
        if (isSupported) {
          node('span', '（已自动提取故障码与检索参数 · 点击查看）', header, 'alert-collapsible-hint');
        }
        node('p', warning, alertEl, 'alert-card-text');
      } else if (warning.includes('AI 基于资料的释义') || warning.includes('这不是现场故障确诊')) {
        const alertEl = node(isSupported ? 'details' : 'div', '', warningsList, 'alert-card alert-governance' + (isSupported ? ' alert-collapsible' : ''));
        const header = node(isSupported ? 'summary' : 'div', '', alertEl, 'alert-card-header');
        node('span', '🛡️ 治理与免责准则', header, 'alert-card-tag');
        if (isSupported) {
          node('span', '（AI 释义与原文核对约束 · 点击查看）', header, 'alert-collapsible-hint');
        }
        node('p', warning, alertEl, 'alert-card-text');
      } else {
        // 重要注意事项、版本冲突或条件约束：始终保持常驻展开，不得隐藏
        const alertDiv = node('div', '', warningsList, 'alert-card alert-warning');
        const header = node('div', '', alertDiv, 'alert-card-header');
        node('span', '⚠️ 重要注意事项', header, 'alert-card-tag');
        node('p', warning, alertDiv, 'alert-card-text');
      }
    }

    // 渲染排查步骤 (Steps)
    const items = data.items || [];
    const missing = data.missing_information || [];
    if (items.length > 0 || missing.length > 0) {
      if (stepsContainer) stepsContainer.style.display = 'block';
      if (resultActions) resultActions.style.display = 'block';

      coverageText.textContent = ({
        partial: '资料覆盖度：部分覆盖，尚缺处理条件或具体流程。',
        sufficient: '资料覆盖度：模型认为可支持所问核查事项，仍不代表确诊或完整维修方案。',
        none: '资料覆盖度：没有可用解释。'
      })[data.coverage || 'none'];

      noActionsText.style.display = (data.steps || []).length ? 'none' : 'block';
      missing.forEach((info) => node('li', info, missingList));
      if (!missing.length) node('li', '模型未列出其他基础信息缺口；如需实际维修，仍须核对完整适用流程。', missingList);

      let actionIndex = 0;
      items.forEach((step) => {
        const target = step.type === 'action' ? stepsList : step.type === 'caution' ? cautionsList : factsList;
        const li = node(step.type === 'action' ? 'li' : 'article', '', target, 'assistant-step-card');
        const badgeCol = node('div', '', li, 'step-badge-col');
        
        const numText = step.type === 'action' ? String(++actionIndex).padStart(2, '0') : step.type === 'fact' ? '知' : '注';
        const badgeClass = step.type === 'action' ? 'step-number-badge step-action-badge' : step.type === 'fact' ? 'step-number-badge step-fact-badge' : 'step-number-badge step-caution-badge';
        node('span', numText, badgeCol, badgeClass);

        const bodyCol = node('div', '', li, 'step-body-col');
        const tag = node('div', '', bodyCol, 'step-tag-row');
        node('span', 'AI 中文释义 · ' + itemLabels[step.type], tag, 'step-tag step-tag-' + step.type);

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
          link.href = '#panel-citation-' + citIndex;
          link.title = '定位至证据详情卡片';
          link.addEventListener('click', (e) => {
            e.preventDefault();
            const isDesktop = window.innerWidth > 1024;
            if (isDesktop) {
              const targetCit = document.getElementById('panel-citation-' + citIndex);
              if (targetCit) {
                targetCit.scrollIntoView({ behavior: 'smooth', block: 'center' });
                targetCit.classList.add('wb-evidence-card-highlight');
                setTimeout(() => targetCit.classList.remove('wb-evidence-card-highlight'), 1600);
              }
            } else {
              // 移动端：定位到内联证据卡片
              const targetCit = document.getElementById('assistant-citation-' + citIndex);
              if (targetCit) {
                targetCit.scrollIntoView({ behavior: 'smooth', block: 'center' });
                targetCit.classList.add('citation-highlight');
                setTimeout(() => targetCit.classList.remove('citation-highlight'), 1600);
              }
            }
          });
        }
      });

      if (!factsList.children.length) node('p', '本次回答未提供故障含义说明，请补充适用资料并对照原文。', factsList, 'steps-intro');
      if (!cautionsList.children.length) node('p', '本次回答未列出注意事项；仍需核对原文完整条件。', cautionsList, 'steps-intro');
    } else {
      if (stepsContainer) stepsContainer.style.display = 'none';
      if (resultActions) resultActions.style.display = 'none';
    }

    // 渲染引用 (Citations)：同时渲染右侧常驻面板与移动端内联容器
    const citations = data.citations || [];
    if (citations.length > 0) {
      if (citationsContainer) citationsContainer.style.display = 'block';
      if (citationCountBadge) citationCountBadge.textContent = citations.length + ' 项证据引用';
      if (panelCitationCountBadge) panelCitationCountBadge.textContent = citations.length + ' 项证据';
      if (panelEmpty) panelEmpty.style.display = 'none';
      if (panelCitationsContainer) panelCitationsContainer.style.display = 'flex';

      citations.forEach((citation, index) => {
        // 渲染移动端内联卡片
        renderEvidenceCard(citation, index, citationsList, false);
        // 渲染右栏面板卡片
        if (panelCitationsContainer) {
          renderEvidenceCard(citation, index, panelCitationsContainer, true);
        }
      });
    } else {
      if (citationsContainer) citationsContainer.style.display = 'none';
      if (panelEmpty) panelEmpty.style.display = 'block';
      if (panelCitationsContainer) panelCitationsContainer.style.display = 'none';
      if (panelCitationCountBadge) panelCitationCountBadge.textContent = '0 项证据';
    }

    // 顺畅定位到结果面板
    setTimeout(() => {
      if (resultPanel) {
        const firstCard = resultPanel.querySelector('.assistant-step-card');
        if (window.innerWidth <= 640 && firstCard) {
          firstCard.scrollIntoView({ behavior: 'smooth', block: 'center' });
        } else {
          resultPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
      }
    }, 60);

  } catch (error) {
    if (loadingBubble) loadingBubble.style.display = 'none';
    if (resultPanel) resultPanel.style.display = 'block';
    const errorMsg = error.name === 'AbortError'
      ? '等待服务超时（45秒），未收到可验证回答。请检查模型或稍后重试。'
      : '网络或本地服务请求失败。请确认本地 Web 服务正常运行后重试。';
    setStatusBanner('error', '请求未完成', errorMsg, '❌');
    setTimeout(() => {
      if (resultPanel) {
        resultPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    }, 60);
  } finally {
    clearTimeout(timeout);
    button.disabled = false;
    if (resultPanel) resultPanel.setAttribute('aria-busy', 'false');
    if (skeleton) skeleton.style.display = 'none';
  }
});
