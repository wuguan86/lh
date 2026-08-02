const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || '';
const runButton = document.querySelector('#run-now');
const toast = document.querySelector('#toast');
const selectedStrategy = runButton?.dataset.strategy || 'equal_decline';

function showToast(message) {
  if (!toast) return;
  toast.textContent = message;
  toast.classList.add('is-visible');
  window.setTimeout(() => toast.classList.remove('is-visible'), 3200);
}

async function startRun() {
  if (!runButton) return;
  runButton.disabled = true;
  runButton.querySelector('.run-label').textContent = '提交中';
  try {
    const response = await fetch('/api/runs', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': csrfToken,
      },
      body: JSON.stringify({ strategy: selectedStrategy }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || '任务提交失败');
    runButton.querySelector('.run-label').textContent = '执行中';
    showToast('筛选任务已提交');
    pollRun(payload.run_id);
  } catch (error) {
    runButton.disabled = false;
    runButton.querySelector('.run-label').textContent = '立即执行';
    showToast(error.message);
  }
}

function pollRun(runId) {
  const timer = window.setInterval(async () => {
    try {
      const response = await fetch(`/api/runs/${runId}`);
      if (!response.ok) return;
      const current = await response.json();
      if (current && ['succeeded', 'succeeded_with_warnings', 'failed'].includes(current.status)) {
        window.clearInterval(timer);
        window.location.assign(`/?run_id=${runId}`);
      }
    } catch (_) {
      // 短暂网络失败时保持轮询，任务状态由服务端持久化。
    }
  }, 3000);
}

if (runButton?.dataset.activeRunId) {
  pollRun(Number(runButton.dataset.activeRunId));
}

runButton?.addEventListener('click', startRun);

document.querySelector('#run-history')?.addEventListener('change', (event) => {
  if (event.target.value) {
    window.location.assign(`/?strategy=${selectedStrategy}&run_id=${event.target.value}`);
  }
});

let selectedBoardType = '';
let selectedTimeframe = '';
const searchInput = document.querySelector('#board-search');
const categoryFilter = document.querySelector('#category-filter');
const rows = [...document.querySelectorAll('.result-row')];

function filterRows() {
  const query = (searchInput?.value || '').trim().toLowerCase();
  const selectedCategory = categoryFilter?.value || '';
  let visibleCount = 0;
  rows.forEach((row) => {
    const matchesType = !selectedBoardType || row.dataset.boardType === selectedBoardType;
    const matchesSearch = !query || row.dataset.boardName.toLowerCase().includes(query);
    const matchesTimeframe = !selectedTimeframe || row.dataset.timeframe === selectedTimeframe;
    const matchesCategory = !selectedCategory || (row.dataset.category || '').includes(selectedCategory);
    const visible = matchesType && matchesSearch && matchesTimeframe && matchesCategory;
    row.classList.toggle('is-hidden', !visible);
    if (visible) visibleCount += 1;
  });
  const counter = document.querySelector('#visible-count');
  if (counter) counter.textContent = `${visibleCount} 条`;
}

document.querySelectorAll('#board-type-filter button').forEach((button) => {
  button.addEventListener('click', () => {
    selectedBoardType = button.dataset.boardType;
    document.querySelectorAll('#board-type-filter button').forEach((item) => {
      item.setAttribute('aria-pressed', String(item === button));
    });
    filterRows();
  });
});

searchInput?.addEventListener('input', filterRows);
categoryFilter?.addEventListener('change', filterRows);

document.querySelectorAll('#timeframe-filter button').forEach((button) => {
  button.addEventListener('click', () => {
    selectedTimeframe = button.dataset.timeframe;
    document.querySelectorAll('#timeframe-filter button').forEach((item) => {
      item.setAttribute('aria-pressed', String(item === button));
    });
    filterRows();
  });
});

const sortDirections = new Map();
document.querySelectorAll('.sort-button').forEach((button) => {
  button.addEventListener('click', () => {
    const key = button.dataset.sortKey;
    const direction = sortDirections.get(key) === 1 ? -1 : 1;
    sortDirections.clear();
    sortDirections.set(key, direction);
    document.querySelectorAll('.sort-button').forEach((item) => item.classList.toggle('is-active', item === button));
    rows.sort((leftRow, rightRow) => {
      const leftValue = Number.parseFloat(leftRow.dataset[key]);
      const rightValue = Number.parseFloat(rightRow.dataset[key]);
      if (Number.isNaN(leftValue)) return 1;
      if (Number.isNaN(rightValue)) return -1;
      return (leftValue - rightValue) * direction;
    });
    const body = document.querySelector('#results-body');
    rows.forEach((row) => body.append(row));
  });
});

const detailDialog = document.querySelector('#result-detail');
const detailTitle = document.querySelector('#detail-title');
const detailContent = document.querySelector('#detail-content');
const equalDeclineDetailFields = [
  '板块类型', '最新交易日', '当前价格', '1:1等距目标价', '1.272扩展目标价',
  '1.618扩展目标价', '目标偏离率', '最大跌幅',
  '首次跌破目标日期', '跌破目标后最低价', '最低价日期', '支撑位', '最高点价格',
  '上涨幅度', '跌破日期', '统计天数', '下跌天数占比', '反弹天数', '20日乖离率',
  '最高点日期', '起涨点日期', '关联ETF代码', '关联ETF名称',
];
const divergenceDetailFields = [
  '板块类型', '周期', '背离分类', '背离次数', '最新交易日', '当前价格',
  '第一低点日期', '第一低点价格', '第二低点日期', '第二低点价格',
  '价格创新低幅度', '价格反弹幅度', '第一DIF低点日期', '第一DIF低点',
  '第二DIF低点日期', '第二DIF低点', 'DIF抬高幅度', 'DIF回升幅度',
  '第一段绿柱面积', '第二段绿柱面积', '绿柱面积缩小率',
  '当前MACD柱', '当前柱状态', '低点链', '关联ETF代码', '关联ETF名称',
];
const detailFieldLabels = {
  '目标偏离率': '1:1目标偏离率',
  '最大跌幅': '跌破1:1后最大跌幅',
  '首次跌破目标日期': '首次跌破1:1日期',
  '跌破目标后最低价': '跌破1:1后最低价',
};

function openDetail(row) {
  const result = JSON.parse(row.dataset.details);
  if (!result['1:1等距目标价'] && result['目标位价格']) {
    result['1:1等距目标价'] = result['目标位价格'];
  }
  detailTitle.textContent = result['周期']
    ? `${result['板块名称']} · ${result['周期']}`
    : result['板块名称'];
  detailContent.replaceChildren();
  const grid = document.createElement('div');
  grid.className = 'detail-grid';
  const detailFields = result['周期'] ? divergenceDetailFields : equalDeclineDetailFields;
  detailFields.forEach((field) => {
    const item = document.createElement('div');
    item.className = 'detail-item';
    const label = document.createElement('span');
    const value = document.createElement('strong');
    label.textContent = detailFieldLabels[field] || field;
    const rawValue = result[field];
    value.textContent = rawValue === null || rawValue === undefined || rawValue === ''
      ? (field === '最大跌幅' ? '未跌破' : '—')
      : String(rawValue);
    item.append(label, value);
    grid.append(item);
  });
  detailContent.append(grid);
  detailDialog.showModal();
}

rows.forEach((row) => {
  row.addEventListener('click', () => openDetail(row));
  row.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      openDetail(row);
    }
  });
});

document.querySelector('#detail-close')?.addEventListener('click', () => detailDialog.close());
detailDialog?.addEventListener('click', (event) => {
  if (event.target === detailDialog) detailDialog.close();
});

document.querySelector('#logout-button')?.addEventListener('click', async () => {
  const response = await fetch('/logout', {
    method: 'POST',
    headers: { 'X-CSRF-Token': csrfToken },
  });
  if (response.redirected) window.location.assign(response.url);
});
