'use strict';
const form = document.getElementById('assistant-form');
const statusText = document.getElementById('assistant-status');
const resultPanel = document.getElementById('assistant-result');
const button = document.getElementById('assistant-submit');
const stepsList = document.getElementById('assistant-steps');
const warningsList = document.getElementById('assistant-warnings');
const citationsList = document.getElementById('assistant-citations');

function node(tag, text, parent) {
  const element = document.createElement(tag);
  element.textContent = text;
  parent.appendChild(element);
  return element;
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  button.disabled = true;
  resultPanel.setAttribute('aria-busy', 'true');
  statusText.textContent = '正在检索和核验证据；通过后调用模型，请稍候…';
  stepsList.replaceChildren();
  warningsList.replaceChildren();
  citationsList.replaceChildren();
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 45000);
  try {
    const fields = new FormData(form);
    const response = await fetch(form.dataset.url, {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-Assistant-CSRF': form.dataset.csrf},
      body: JSON.stringify(Object.fromEntries(fields)), signal: controller.signal,
    });
    const data = await response.json();
    statusText.textContent = data.message + (data.decision ? '（证据判定：' + data.decision + '）' : '');
    for (const warning of data.warnings || []) node('p', warning, warningsList);
    for (const step of data.steps || []) {
      const li = node('li', '依据资料核查：' + step.text, stepsList);
      const index = data.citations.findIndex(c => c.evidence_id === step.evidence_id);
      const link = node('a', ' 查看引用 [' + (index + 1) + ']', li);
      link.href = '#assistant-citation-' + index;
    }
    for (const [index, citation] of (data.citations || []).entries()) {
      const article = node('article', '', citationsList);
      article.id = 'assistant-citation-' + index;
      node('h3', '[' + (index + 1) + '] ' + citation.filename + ' · 第 ' + citation.page_number + ' 页', article);
      node('p', '文档 ID：' + citation.document_id + ' · Evidence ID：' + citation.evidence_id, article);
      const link = node('a', '打开 PDF 对应页面', article);
      link.href = citation.pdf_url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      const details = node('details', '', article);
      node('summary', '查看本页原文', details);
      node('pre', citation.original_text, details);
    }
  } catch (error) {
    statusText.textContent = error.name === 'AbortError' ? '等待服务超时，未收到可验证回答，请稍后重试。' : '请求失败，未收到可验证回答。请检查本地服务后重试。';
  } finally {
    clearTimeout(timeout);
    button.disabled = false;
    resultPanel.setAttribute('aria-busy', 'false');
  }
});
