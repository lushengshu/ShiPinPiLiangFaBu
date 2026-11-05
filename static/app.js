let state = { items: [], upload_url: '', root: '', publish: {}, channels: {}, analysis: {}, analysis_batch: {} };
// 选择状态只保存在当前页面，不写入发布队列；刷新后默认不选中任何素材。
let selectedQueueIds = new Set();
// 队列卡片的编辑器使用原生 details 折叠；轮询重绘时保留用户刚刚
// 展开的区块，避免页面跳回顶部或重新折叠。
const openQueueSections = new Set();
// 编辑面板首次出现时默认展开；用户手动收起后，轮询刷新会尊重该选择。
const initializedQueueSections = new Set();
// 某些编辑操作需要在下一次重绘后主动展开指定区块。单独记录这类请求，
// 避免 render() 先读取旧 DOM 的折叠状态时把刚刚请求的展开标记覆盖掉。
const pendingOpenQueueSections = new Set();
// 文案等可编辑字段在轮询重绘期间先保存在内存草稿中。这样用户正在
// 输入时不会被 1.5 秒轮询返回的旧队列值覆盖；保存成功且服务端回读
// 同值后才会清除对应草稿。
const editorDrafts = new Map();
const editorSaveTimers = new Map();
// 输入事件和 change/blur 事件可能在同一轮触发；记录正在保存的值，避免
// 同一字段同时发出两次 PATCH，也避免导入内容和旧的自动保存发生竞态。
const editorSaveInFlight = new Map();
// 轮询会每 1.5 秒刷新一次队列。直接替换 queue.innerHTML 会销毁正在打开的
// input/select，导致输入法、光标和原生下拉菜单被打断。用户正在操作表单时，
// 先保留当前 DOM，等控件失焦后再补一次完整渲染。
let deferredRenderPending = false;
let captionImportTargetId = '';
let captionImportBusy = false;
let copyGenerateBusy = false;
let bulkVideoTypeBusy = false;
let bulkPreferencesBusy = false;
// 定时计划是一次批量写入；期间暂时锁住队列编辑，避免用户同时修改同一条记录。
let schedulePlanBusy = false;
let schedulePlanInitialized = false;
let analysisBatchResultsToken = '';
let analysisApplyMessage = '';
// 自动暂停提醒只在同一事件首次出现时展示；关闭后轮询不会再次打扰用户。
const autoPauseNoticeState = { seen: new Set(), dismissed: new Set(), activeEventId: '' };
// 常用间隔同时保留 2 分钟测试档和 10～100 分钟档；也可在“自定义”中输入
// 1～43200 分钟。这里的间隔只写入抖音预约时间，不会让本地程序 sleep。
const SCHEDULE_INTERVAL_PRESETS = ['2', '10', '20', '30', '40', '50', '60', '100'];
const MAX_SCHEDULE_INTERVAL_MINUTES = 43200;
const PANORAMA_FORMATS = [
  ['360_mono', '360°普通单目'],
  ['360_stereo', '360°立体双目'],
  ['180_mono', '180°普通单目'],
  ['180_stereo', '180°立体双目'],
];
const WORKFLOW_PROFILE_STORAGE_KEY = 'douyin-publisher-workflow-profile-v1';
// 入口类型是“新加入素材”的默认发布路线；已有队列条目仍以自己的卡片
// 配置为准。用 localStorage 记住上次选择，避免用户刷新后又从普通视频开始。
let workflowProfile = {videoType: 'normal', panoramaFormat: ''};
let workflowProfileInitialized = false;
let workflowProfileNotice = '';
const $ = (s) => document.querySelector(s);
const escapeHtml = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function normalizeWorkflowProfile(value = {}) {
  const videoType = value?.videoType === 'panorama' ? 'panorama' : 'normal';
  const allowed = new Set(PANORAMA_FORMATS.map(([key]) => key));
  const panoramaFormat = videoType === 'panorama' && allowed.has(String(value?.panoramaFormat || ''))
    ? String(value.panoramaFormat)
    : '';
  return {videoType, panoramaFormat};
}

function loadWorkflowProfile() {
  if (workflowProfileInitialized) return workflowProfile;
  workflowProfileInitialized = true;
  try {
    const stored = JSON.parse(localStorage.getItem(WORKFLOW_PROFILE_STORAGE_KEY) || '{}');
    workflowProfile = normalizeWorkflowProfile(stored);
  } catch (_) {
    workflowProfile = {videoType: 'normal', panoramaFormat: ''};
  }
  return workflowProfile;
}

function saveWorkflowProfile() {
  try {
    localStorage.setItem(WORKFLOW_PROFILE_STORAGE_KEY, JSON.stringify(workflowProfile));
  } catch (_) {
    // Private browsing or a disabled storage area should not block the queue.
  }
}

function inferWorkflowProfileFromQueue(items = []) {
  loadWorkflowProfile();
  let stored = false;
  try {
    stored = Boolean(localStorage.getItem(WORKFLOW_PROFILE_STORAGE_KEY));
  } catch (_) {
    stored = false;
  }
  if (stored) return;
  const modes = [...new Set(items.map(item => videoTypeOf(item)))];
  if (modes.length !== 1) return;
  const mode = modes[0];
  const format = mode === 'panorama'
    ? panoramaFormatOf(items.find(item => videoTypeOf(item) === 'panorama') || {})
    : '';
  workflowProfile = normalizeWorkflowProfile({videoType: mode, panoramaFormat: format});
}

function workflowProfileReady() {
  loadWorkflowProfile();
  return workflowProfile.videoType !== 'panorama' || Boolean(workflowProfile.panoramaFormat);
}

const EDITOR_DRAFT_FIELDS = new Set([
  'caption', 'hashtags', 'cover_tsp', 'note', 'official_topic_keywords', 'schedule_at', 'schedule_interval_custom',
]);

function rememberEditorDraft(id, field, value) {
  const key = String(id || '');
  if (!key || !EDITOR_DRAFT_FIELDS.has(field)) return;
  const draft = editorDrafts.get(key) || {};
  draft[field] = String(value ?? '');
  editorDrafts.set(key, draft);
}

function editorFieldValue(item, field) {
  const id = queueItemId(item);
  const draft = editorDrafts.get(id);
  if (draft && Object.prototype.hasOwnProperty.call(draft, field)) return draft[field];
  return item?.[field] ?? '';
}

function clearEditorDraft(id, field = '') {
  const key = String(id || '');
  if (!key) return;
  if (!field) {
    editorDrafts.delete(key);
    return;
  }
  const draft = editorDrafts.get(key);
  if (!draft) return;
  delete draft[field];
  if (!Object.keys(draft).length) editorDrafts.delete(key);
}

function hasCaptionDraft(id) {
  const draft = editorDrafts.get(String(id || ''));
  return Boolean(draft && (Object.prototype.hasOwnProperty.call(draft, 'caption') || Object.prototype.hasOwnProperty.call(draft, 'hashtags')));
}

function pruneEditorDrafts(items = state.items || []) {
  const valid = new Set(items.map(queueItemId).filter(Boolean));
  for (const id of editorDrafts.keys()) {
    if (!valid.has(id)) editorDrafts.delete(id);
  }
}

function queueSectionKey(id, section) {
  return `${String(id || '')}::${String(section || '')}`;
}

function captureQueueSectionState() {
  document.querySelectorAll('.item details[data-section]').forEach(details => {
    const card = details.closest('.item');
    const id = card?.dataset?.id;
    const section = details.dataset.section;
    if (!id || !section) return;
    const key = queueSectionKey(id, section);
    if (details.open) openQueueSections.add(key);
    else openQueueSections.delete(key);
  });
}

function pruneQueueSectionState(items = state.items || []) {
  const valid = new Set(items.map(queueItemId).filter(Boolean));
  for (const key of [...openQueueSections]) {
    const id = key.split('::')[0];
    if (!valid.has(id)) openQueueSections.delete(key);
  }
  for (const key of [...initializedQueueSections]) {
    const id = key.split('::')[0];
    if (!valid.has(id)) initializedQueueSections.delete(key);
  }
}

function queueSectionOpen(id, section) {
  return openQueueSections.has(queueSectionKey(id, section));
}

function requestQueueSectionOpen(id, section) {
  const key = queueSectionKey(id, section);
  openQueueSections.add(key);
  pendingOpenQueueSections.add(key);
}

function bindQueueSectionState(card, id) {
  card.querySelectorAll('details[data-section]').forEach(details => {
    details.addEventListener('toggle', () => {
      const key = queueSectionKey(id, details.dataset.section);
      if (details.open) openQueueSections.add(key);
      else openQueueSections.delete(key);
    });
  });
}

function compactText(value, maxLength = 94) {
  const text = String(value || '').replace(/\s+/g, ' ').trim();
  if (!text) return '';
  return text.length > maxLength ? `${text.slice(0, maxLength - 1)}…` : text;
}

function queueCopyState(item) {
  const id = queueItemId(item);
  const caption = String(editorFieldValue(item, 'caption') || '').trim();
  const hashtags = String(editorFieldValue(item, 'hashtags') || '').trim();
  if (!caption && !hashtags) return {label: '待填写文案', tone: 'empty'};
  if (hasCaptionDraft(id)) return {label: '有未保存草稿', tone: 'draft'};
  return {label: '文案已填写', tone: 'ready'};
}

function queueScheduleSummary(item) {
  if (scheduleModeOf(item) !== 'scheduled') return '立即发布';
  const at = String(item?.schedule_at || '').replace('T', ' ');
  return at ? `预约 ${at}` : '待设置预约时间';
}

function captureActiveEditor() {
  const active = document.activeElement;
  if (!active || !active.dataset?.editorField || typeof active.value !== 'string') return null;
  const card = active.closest?.('.item');
  if (!card || !card.dataset.id) return null;
  const id = String(card.dataset.id);
  const field = String(active.dataset.editorField);
  rememberEditorDraft(id, field, active.value);
  return {
    id,
    field,
    selectionStart: typeof active.selectionStart === 'number' ? active.selectionStart : null,
    selectionEnd: typeof active.selectionEnd === 'number' ? active.selectionEnd : null,
    scrollTop: Number(active.scrollTop || 0),
  };
}

function restoreActiveEditor(snapshot) {
  if (!snapshot) return;
  const card = [...document.querySelectorAll('.item')].find(item => item.dataset.id === snapshot.id);
  const control = card?.querySelector(`[data-editor-field="${snapshot.field}"]`);
  if (!control) return;
  control.focus({preventScroll: true});
  if (snapshot.selectionStart != null && typeof control.setSelectionRange === 'function') {
    const end = Math.min(snapshot.selectionEnd ?? snapshot.selectionStart, control.value.length);
    const start = Math.min(snapshot.selectionStart, end);
    control.setSelectionRange(start, end);
  }
  control.scrollTop = snapshot.scrollTop || 0;
}

function activeFormControl() {
  const active = document.activeElement;
  if (!active || active === document.body || active === document.documentElement) return null;
  if (active.disabled || active.readOnly) return null;
  if (!active.matches?.('input:not([type="hidden"]), textarea, select, [contenteditable="true"]')) return null;
  return active;
}

function shouldDeferRender() {
  return Boolean(activeFormControl());
}

function flushDeferredRender() {
  if (!deferredRenderPending || shouldDeferRender()) return;
  deferredRenderPending = false;
  render({force: true});
}

function editorFieldLabel(field) {
  if (field === 'caption') return '文案';
  if (field === 'hashtags') return '话题';
  if (field === 'official_topic_keywords') return '官方活动/话题匹配关键词';
  return field;
}
// “published” 在队列中表示已成功提交到抖音；平台仍可能处于审核中。
const label = { pending:'待发布', publishing:'发布中', published:'已提交', skipped:'已跳过', failed:'失败待处理' };
const resumableStatuses = new Set(['pending', 'failed', 'publishing']);
const isResumable = (item) => resumableStatuses.has(item.status);
const DOUYIN_UPLOAD_URL = 'https://creator.douyin.com/creator-micro/content/upload';
const DOUYIN_PANORAMA_UPLOAD_URL = `${DOUYIN_UPLOAD_URL}?default-tab=4`;
const queueItemId = (item) => String(item?.id || item?.path || '');
const scheduleModeOf = (item) => item?.schedule_mode === 'scheduled' ? 'scheduled' : 'immediate';
const videoTypeOf = (item) => item?.video_type === 'panorama' ? 'panorama' : 'normal';
function platformBadgesHtml(item) {
  const channels = item?.platforms?.channels || {};
  const channelsLabel = videoTypeOf(item) === 'panorama'
    ? '视频号不支持全景'
    : (channels.status === 'published' ? '视频号已提交' : '视频号待发布');
  const channelsClass = videoTypeOf(item) === 'panorama' ? 'unsupported' : String(channels.status || 'pending');
  return `<span class="platform-badge douyin">抖音</span><span class="platform-badge channels ${channelsClass}">${escapeHtml(channelsLabel)}</span>`;
}
const panoramaFormatOf = (item) => PANORAMA_FORMATS.some(([value]) => value === item?.panorama_format) ? item.panorama_format : '';
const panoramaProfileOf = (item) => item?.panorama_profile && typeof item.panorama_profile === 'object'
  ? item.panorama_profile : {status: 'unknown', label: '待页面确认', blocking: [], warnings: [], message: '尚未完成本地预检'};
function panoramaFormatOptions(value = '') {
  const normalized = panoramaFormatOf({panorama_format: value});
  return `<option value="">请选择全景格式</option>${PANORAMA_FORMATS.map(([option, label]) => `<option value="${option}" ${option === normalized ? 'selected' : ''}>${label}</option>`).join('')}`;
}
const scheduleIntervalOf = (item) => {
  const value = String(item?.schedule_interval_minutes ?? '').trim();
  return /^\d+$/.test(value) && Number(value) > 0 ? value : '';
};

function publishField(pub, ...names) {
  for (const name of names) {
    const value = pub?.[name];
    if (value !== undefined && value !== null && String(value).trim() !== '') return value;
  }
  return '';
}

function publishCurrentLabel(pub = {}) {
  const current = publishField(pub, 'current', 'current_video', 'currentVideo', 'current_name', 'current_video_name', 'current_item');
  if (current && typeof current === 'object') {
    return String(current.name || current.path || current.id || '').trim();
  }
  return String(current || '').trim();
}

function pauseSourceIsManual(source) {
  return /^(manual|user|human|user_pause|manual_pause|pause_requested)$/i.test(String(source || '').trim());
}

function pad2(value) { return String(value).padStart(2, '0'); }

function localDateTimeValue(date) {
  if (!(date instanceof Date) || Number.isNaN(date.getTime())) return '';
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}T${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
}

function normalizeDateTimeText(value) {
  return String(value || '')
    .normalize('NFKC')
    .replace(/[\u0000-\u001F\u007F-\u009F\u00AD\u061C\u180E\u200B-\u200F\u202A-\u202E\u2060-\u2064\u2066-\u2069\uFEFF]/g, '')
    .replace(/[\u00A0\u2007\u202F]/g, ' ')
    .trim();
}

function parseLocalDateTime(value) {
  const text = normalizeDateTimeText(value);
  // datetime-local 的标准值是 YYYY-MM-DDTHH:mm；部分浏览器/手输会带秒、斜杠、
  // 全角数字或冒号，NFKC 后一并接受；不存在的日期和 DST 跳变时间仍然拒绝。
  let match = /^(\d{4})[-/](\d{2})[-/](\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?$/.exec(text);
  if (!match) {
    // 异常分隔符/夹杂不可见字符时的宽松兜底；仍由日期组件校验兜底正确性。
    const loose = text.match(/(\d{4})\D+(\d{1,2})\D+(\d{1,2})\D+(\d{1,2})\D+(\d{1,2})/);
    if (loose) match = loose;
  }
  if (!match) {
    // 最后兜底：直接取数字（YYYYMMDDHHmm），即使数字之间夹了不可见字符。
    const digits = text.replace(/\D/g, '');
    if (digits.length >= 12) {
      match = [null, digits.slice(0, 4), digits.slice(4, 6), digits.slice(6, 8), digits.slice(8, 10), digits.slice(10, 12)];
    }
  }
  if (!match) return null;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const date = new Date(year, month - 1, day, hour, minute, 0, 0);
  if (Number.isNaN(date.getTime())) return null;
  if (
    date.getFullYear() !== year
    || date.getMonth() !== month - 1
    || date.getDate() !== day
    || date.getHours() !== hour
    || date.getMinutes() !== minute
  ) return null;
  return date;
}

function parseDateTimeInput(input) {
  if (!input) return null;
  const parsed = parseLocalDateTime(input.value);
  if (parsed) return parsed;
  // 兜底一：浏览器原生 Date 解析（datetime-local 不带时区时按本地时间解释）。
  const native = new Date(normalizeDateTimeText(input.value));
  if (!Number.isNaN(native.getTime())) return native;
  // 兜底二：某些浏览器/输入法会给 datetime-local 留下不可见字符，字符串解析会
  // 失败；valueAsNumber 仍能给出该控件按 UTC 解释的毫秒数，换算回本地时间。
  const numeric = Number(input.valueAsNumber);
  if (!Number.isFinite(numeric)) return null;
  const corrected = new Date(numeric + new Date(numeric).getTimezoneOffset() * 60000);
  return Number.isNaN(corrected.getTime()) ? null : corrected;
}

function formatScheduleMoment(date) {
  if (!(date instanceof Date) || Number.isNaN(date.getTime())) return '';
  return `${pad2(date.getMonth() + 1)}-${pad2(date.getDate())} ${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
}

function scheduleIntervalOptions(value = '') {
  const normalized = String(value || '').trim();
  const customValue = normalized && !SCHEDULE_INTERVAL_PRESETS.includes(normalized) ? 'custom' : normalized;
  const options = [['', '不设置']].concat(SCHEDULE_INTERVAL_PRESETS.map(item => [item, item]));
  options.push(['custom', '自定义']);
  return options.map(([option, text]) => `<option value="${escapeHtml(option)}" ${option === customValue ? 'selected' : ''}>${escapeHtml(text)}</option>`).join('');
}

function itemDisplayLabel(item) {
  if (item?.status === 'published' && scheduleModeOf(item) === 'scheduled') return '已预约';
  return label[item?.status] || item?.status || '';
}

function panoramaProfileText(item) {
  const profile = panoramaProfileOf(item);
  const details = [];
  if (profile.resolution) details.push(profile.resolution);
  if (profile.duration) details.push(profile.duration);
  if (profile.size) details.push(profile.size);
  const warning = Array.isArray(profile.blocking) && profile.blocking.length
    ? profile.blocking[0]
    : Array.isArray(profile.warnings) && profile.warnings.length ? profile.warnings[0] : '';
  return `${profile.label || '待页面确认'}${details.length ? ` · ${details.join(' · ')}` : ''}${warning ? ` · ${warning}` : ''}`;
}

function panoramaFieldsHtml(item) {
  const panorama = videoTypeOf(item) === 'panorama';
  const profile = panoramaProfileOf(item);
  const statusClass = `panorama-${String(profile.status || 'unknown').replace(/[^a-z-]/gi, '')}`;
  const officialAuto = item?.official_topic_auto !== false;
  const picoEnabled = item?.pico_enabled === true;
  return `<label>发布类型<select class="video-type"><option value="normal" ${!panorama ? 'selected' : ''}>普通视频</option><option value="panorama" ${panorama ? 'selected' : ''}>全景视频</option></select></label><label class="panorama-format-field" ${panorama ? '' : 'hidden'}>全景格式<select class="panorama-format">${panoramaFormatOptions(item.panorama_format)}</select></label><span class="panorama-profile ${statusClass}" title="${escapeHtml(profile.message || '')}">${escapeHtml(panorama ? panoramaProfileText(item) : '普通视频 · 不走全景入口')}</span><label class="panorama-topic-field" ${panorama ? '' : 'hidden'}>官方活动/话题匹配关键词（可选）<input class="official-topic-keywords" data-editor-field="official_topic_keywords" value="${escapeHtml(editorFieldValue(item, 'official_topic_keywords'))}" placeholder="留空自动匹配；多个关键词用逗号分隔" maxlength="500"></label><label class="check-label panorama-official-auto" title="只选择页面实际显示且与当前内容明确匹配的官方活动/话题" ${panorama ? '' : 'hidden'}><input class="official-topic-auto" type="checkbox" ${officialAuto ? 'checked' : ''}> 自动匹配官方活动/话题</label><label class="check-label panorama-pico-field" title="这是抖音平台授权项，只有勾选后工具才会在发布页开启" ${panorama ? '' : 'hidden'}><input class="pico-enabled" type="checkbox" ${picoEnabled ? 'checked' : ''}> 授权 PICO 播放</label>`;
}

function schedulePlanTargets(items = state.items || []) {
  return items.filter(isResumable);
}

function defaultScheduleStart(items = state.items || []) {
  // 优先沿用队列里仍然有效的定时时间；已经过去的时间不能再作为默认值，
  // 否则用户刷新页面后第一眼就看到一个点不动的“过去时间”。
  const existing = items.find(item => {
    if (scheduleModeOf(item) !== 'scheduled') return false;
    const parsed = parseLocalDateTime(item.schedule_at);
    return Boolean(parsed && parsed.getTime() > Date.now());
  });
  if (existing) return localDateTimeValue(parseLocalDateTime(existing.schedule_at));
  const start = new Date(Date.now() + 10 * 60 * 1000);
  start.setSeconds(0, 0);
  return localDateTimeValue(start);
}

function ensureSchedulePlanDefaults(items = state.items || []) {
  const start = $('#scheduleStartAt');
  const interval = $('#scheduleDefaultInterval');
  const custom = $('#scheduleCustomInterval');
  if (!start || !interval || !custom) return;
  if (!schedulePlanInitialized) {
    start.value = defaultScheduleStart(items);
    const existingInterval = items.map(scheduleIntervalOf).find(Boolean) || '10';
    if (SCHEDULE_INTERVAL_PRESETS.includes(existingInterval)) {
      interval.value = existingInterval;
      custom.value = '';
    } else {
      interval.value = 'custom';
      custom.value = existingInterval;
    }
    schedulePlanInitialized = true;
  }
  custom.hidden = interval.value !== 'custom';
}

function pruneSelectedQueueIds(items = state.items || []) {
  const valid = new Set(items.map(queueItemId).filter(Boolean));
  selectedQueueIds = new Set([...selectedQueueIds].filter(id => valid.has(id)));
}

function visibleQueueItems(items = state.items || []) {
  const filter = $('#filter')?.value || 'all';
  return filter === 'all'
    ? items
    : items.filter(item => filter === 'pending'
      ? (item.status === 'pending' || item.status === 'publishing')
      : filter === 'scheduled'
        ? scheduleModeOf(item) === 'scheduled'
      : item.status === filter);
}

function updateQueueSelectionUi(visible = [], locked = queueUiLocked()) {
  const bar = $('#queueSelectionBar');
  const selectAll = $('#selectAllQueue');
  const summary = $('#selectionSummary');
  const deleteButton = $('#deleteSelectedBtn');
  if (!bar || !selectAll || !summary || !deleteButton) return;
  const visibleIds = visible.map(queueItemId).filter(Boolean);
  const selectedVisible = visibleIds.filter(id => selectedQueueIds.has(id));
  const selectedCount = selectedQueueIds.size;
  bar.hidden = !visible.length;
  selectAll.disabled = locked || !visible.length;
  selectAll.checked = visible.length > 0 && selectedVisible.length === visible.length;
  selectAll.indeterminate = selectedVisible.length > 0 && selectedVisible.length < visible.length;
  summary.textContent = `已选择 ${selectedCount} 条`;
  deleteButton.textContent = selectedCount ? `删除所选（${selectedCount}）` : '删除所选';
  deleteButton.disabled = locked || selectedCount === 0;
  const lockTitle = locked ? '自动发布正在运行、暂停或终止处理中，暂时不能修改队列' : '';
  selectAll.title = lockTitle || '选择当前筛选列表中的全部视频';
  deleteButton.title = lockTitle || (selectedCount ? '只移除选中的队列记录，不删除源视频文件' : '请先勾选要移除的视频');
}

function updateBulkVideoTypeUi(locked = queueUiLocked()) {
  const type = $('#bulkVideoType');
  const formatWrap = $('#bulkPanoramaFormatWrap');
  const format = $('#bulkPanoramaFormat');
  const button = $('#applyBulkVideoType');
  if (!type || !formatWrap || !format || !button) return;
  formatWrap.hidden = type.value !== 'panorama';
  const selected = [...selectedQueueIds].filter(Boolean).length;
  for (const id of ['bulkDownload', 'applyBulkDownload', 'bulkOfficialTopic', 'applyBulkOfficialTopic']) {
    const control = document.getElementById(id);
    if (control) control.disabled = locked || !selected || bulkPreferencesBusy;
  }
  type.disabled = locked || bulkVideoTypeBusy || !selected;
  format.disabled = locked || bulkVideoTypeBusy || !selected || type.value !== 'panorama';
  button.disabled = locked || bulkVideoTypeBusy || !selected || (type.value === 'panorama' && !format.value);
  button.textContent = bulkVideoTypeBusy ? '应用中…' : '应用到勾选';
  button.title = selected
    ? (type.value === 'panorama' ? '将所勾选视频设为全景并使用所选格式' : '将所勾选视频改为普通视频')
    : '先勾选一个或多个队列视频';
}

function updateWorkflowTypeUi(items = state.items || [], locked = queueUiLocked()) {
  const type = $('#workflowVideoType');
  const formatWrap = $('#workflowPanoramaFormatWrap');
  const format = $('#workflowPanoramaFormat');
  const hint = $('#workflowTypeHint');
  const mediaPickerBtn = $('#mediaPickerBtn');
  const folderBtn = $('#folderBtn');
  const filesBtn = $('#filesBtn');
  if (!type || !formatWrap || !format || !hint) return;
  loadWorkflowProfile();
  type.value = workflowProfile.videoType;
  format.value = workflowProfile.panoramaFormat;
  const panorama = workflowProfile.videoType === 'panorama';
  const ready = workflowProfileReady();
  formatWrap.hidden = !panorama;
  type.disabled = locked;
  format.disabled = locked || !panorama;
  const existingModes = [...new Set(items.map(item => videoTypeOf(item)))];
  const existingHint = existingModes.length > 1
    ? '当前队列含普通/全景混合条目，卡片设置优先。'
    : '';
  if (workflowProfileNotice) {
    hint.textContent = workflowProfileNotice;
  } else if (panorama && !workflowProfile.panoramaFormat) {
    hint.textContent = '先选全景格式，再选择视频；新增素材将进入全景上传页。';
  } else if (panorama) {
    hint.textContent = `全景视频 · ${PANORAMA_FORMATS.find(([key]) => key === workflowProfile.panoramaFormat)?.[1] || '已选格式'}；新增素材走全景发布方式。${existingHint ? ` ${existingHint}` : ''}`;
  } else {
    hint.textContent = `普通视频；新增素材走普通发布方式。${existingHint ? ` ${existingHint}` : ''}`;
  }
  const blocked = locked || !ready;
  [mediaPickerBtn, folderBtn, filesBtn].forEach(control => {
    if (control) control.disabled = blocked;
  });
  if (mediaPickerBtn) {
    mediaPickerBtn.title = locked
      ? '自动发布或识别任务进行中，暂不能选择视频'
      : !ready
        ? '请先选择全景视频格式'
        : panorama
          ? '按已选全景格式选择视频，新增条目会进入全景上传方式'
          : '按普通视频方式选择文件夹或视频文件';
  }
}

function workflowSelectionPayload() {
  loadWorkflowProfile();
  if (!workflowProfileReady()) {
    throw new Error('请先在第 1 步选择全景视频格式');
  }
  return {
    video_type: workflowProfile.videoType,
    panorama_format: workflowProfile.videoType === 'panorama' ? workflowProfile.panoramaFormat : '',
  };
}

function publishFlags(pub = {}) {
  const message = String(pub.message || '');
  const running = pub.running === true;
  const pauseRequested = pub.pause_requested === true || pub.pauseRequested === true;
  // 终止请求可能在不同版本的服务端使用不同字段；兼容这些字段和
  // 状态文本，确保请求期间即使 running 已先落盘为 false，也不会误解锁队列。
  const terminateRequested = pub.terminate_requested === true || pub.terminateRequested === true ||
    pub.stop_requested === true || pub.stopRequested === true || pub.stopping === true ||
    pub.terminating === true || pub.status === 'stopping' || pub.state === 'stopping' ||
    /正在终止|终止中|停止中/.test(message);
  // 服务端可能继续返回旧的能力字段，但当前终止接口由服务端校验 PID
  // 后强制停止发布器，页面不再用该字段把终止按钮挡住。
  const terminated = pub.terminated === true || pub.stop_completed === true ||
    /已终止自动发布/.test(message);
  // 兼容新状态字段，也兼容旧版本只写 message 的状态文件。
  const paused = !terminated && (pub.paused === true || pub.status === 'paused' || pub.state === 'paused' ||
    (!running && /已暂停|暂停中/.test(message)));
  const actionRequired = pub.action_required === true || pub.actionRequired === true;
  const autoPaused = pub.auto_paused === true || pub.autoPaused === true;
  const pauseSource = String(publishField(pub, 'pause_source', 'pauseSource') || '').trim();
  const pauseReason = String(publishField(pub, 'pause_reason', 'pauseReason') || '').trim();
  const pauseEventId = String(publishField(pub, 'pause_event_id', 'pauseEventId', 'attention_id', 'attentionId') || '').trim();
  const pausedAt = String(publishField(pub, 'paused_at', 'pausedAt') || '').trim();
  const completed = Number(pub.completed || 0);
  const total = Number(pub.total || 0);
  const done = !running && !paused && !terminateRequested && !terminated && !actionRequired &&
    ((total > 0 && completed >= total) || /全部待发布视频已提交|等待平台审核|发布完成/.test(message));
  return { running, pauseRequested, paused, terminateRequested, terminated, actionRequired, autoPaused, pauseSource, pauseReason, pauseEventId, pausedAt, done, completed, total, message };
}

// “继续发布”所对应的中断任务也允许直接终止。自动暂停/需要人工处理时
// 发布器可能已经退出，但队列现场和当前视频仍然保留，终止按钮不能因为
// running=false 就消失；真正空闲或已完成的状态则不显示该按钮。
function publishCanTerminate(pub = {}, flags = publishFlags(pub)) {
  if (flags.terminateRequested || flags.terminated) return false;
  if (flags.running) return true;
  const hasCurrent = Boolean(publishCurrentLabel(pub));
  return hasCurrent && (flags.pauseRequested || flags.paused || flags.autoPaused || flags.actionRequired);
}

// 仅认定真正结束运行且由系统/异常触发的暂停。普通“暂停发布”没有
// auto_paused/action_required，不会进入这里，也不会弹窗。
function autoPauseEvent(pub = {}, flags = publishFlags(pub)) {
  if (flags.running || flags.terminateRequested || flags.terminated) return null;
  const explicitManual = pub.manual_pause === true || pub.manualPaused === true || pauseSourceIsManual(flags.pauseSource);
  if (explicitManual || (flags.paused && flags.pauseRequested)) return null;
  const eventMarker = flags.pauseEventId && (flags.paused || flags.actionRequired || flags.pauseReason);
  if (flags.autoPaused) {
    const reason = flags.pauseReason || flags.message || '自动发布因异常已暂停，等待人工处理。';
    const current = publishCurrentLabel(pub);
    const id = flags.pauseEventId || `auto-pause:${flags.pausedAt || flags.pauseSource || reason}|${current}`;
    return {id, reason, current, source: flags.pauseSource};
  }
  // 部分中间版本先写入 pause_source/attention_id，再补齐 action_required；
  // 只要仍处于暂停/人工处理终态，也按非手动来源提示一次。
  if (!explicitManual && (eventMarker || (flags.pauseSource && (flags.paused || flags.actionRequired)))) {
    const reason = flags.pauseReason || flags.message || '自动发布因异常已暂停，等待人工处理。';
    const current = publishCurrentLabel(pub);
    const id = flags.pauseEventId || `auto-pause:${flags.pausedAt || flags.pauseSource || reason}|${current}`;
    return {id, reason, current, source: flags.pauseSource};
  }
  // 兼容旧服务端：只有“需要人工处理”且已停止轮询时才视为自动暂停。
  // 用户点击暂停期间通常带 pause_requested，不将其误报成异常暂停。
  if (!flags.actionRequired) return null;
  const reason = flags.pauseReason || flags.message || '自动发布因异常已暂停，等待人工处理。';
  const current = publishCurrentLabel(pub);
  const id = flags.pauseEventId || `auto-pause:${flags.pausedAt || flags.pauseSource || reason}|${current}`;
  return {id, reason, current, source: flags.pauseSource};
}

function renderAutoPauseNotice(pub = {}, flags = publishFlags(pub)) {
  const notice = $('#autoPauseNotice');
  if (!notice) return;
  const event = autoPauseEvent(pub, flags);
  if (!event) {
    notice.hidden = true;
    notice.innerHTML = '';
    autoPauseNoticeState.activeEventId = '';
    return;
  }
  // 当前事件仍在展示中时保留用户可见的关闭状态，避免 1.5 秒轮询重置它。
  if (autoPauseNoticeState.activeEventId === event.id) return;
  autoPauseNoticeState.activeEventId = event.id;
  if (autoPauseNoticeState.seen.has(event.id) || autoPauseNoticeState.dismissed.has(event.id)) {
    notice.hidden = true;
    notice.innerHTML = '';
    return;
  }
  autoPauseNoticeState.seen.add(event.id);
  const current = event.current
    ? `<span class="auto-pause-current">当前视频：${escapeHtml(event.current)}</span>`
    : '<span class="auto-pause-current">当前视频：后端未提供视频名称</span>';
  notice.hidden = false;
  notice.innerHTML = `<div><strong>自动发布已暂停，需要人工处理</strong><p>${escapeHtml(event.reason)}</p></div><button class="auto-pause-dismiss" type="button" data-dismiss-auto-pause aria-label="关闭自动暂停提醒">关闭</button>${current}<span class="auto-pause-guidance">浏览器页面会保持打开；完成处理后请手动点击“继续发布”，任务不会自动恢复。</span>`;
  const dismiss = notice.querySelector('[data-dismiss-auto-pause]');
  if (dismiss) dismiss.onclick = () => {
    autoPauseNoticeState.dismissed.add(event.id);
    autoPauseNoticeState.activeEventId = event.id;
    notice.hidden = true;
    notice.innerHTML = '';
  };
}

// 选择/清空会改动本地发布队列。自动发布（包括已请求暂停或已暂停在
// 安全检查点）期间锁定这些操作，避免把正在处理的队列换掉。
function queueActionsLocked(pub = state.publish || {}) {
  const flags = publishFlags(pub);
  if (flags.terminated && !flags.running && !flags.terminateRequested) return false;
  return flags.running || flags.pauseRequested || flags.paused || flags.terminateRequested;
}

function channelsPublishRunning() {
  return Boolean(state.channels && state.channels.running);
}

function queueUiLocked(pub = state.publish || {}) {
  return queueActionsLocked(pub) || channelsPublishRunning() || schedulePlanBusy || bulkPreferencesBusy;
}

function openUploadPage() {
  // 当前页面保留在本地工具；抖音上传页只在新标签打开。
  const route = preferredUploadType();
  const url = route === 'panorama' ? DOUYIN_PANORAMA_UPLOAD_URL : DOUYIN_UPLOAD_URL;
  window.open(url, '_blank', 'noopener,noreferrer');
}

function preferredUploadType() {
  const current = (state.items || []).find(isResumable) || (state.items || [])[0];
  return current ? videoTypeOf(current) : (loadWorkflowProfile().videoType === 'panorama' ? 'panorama' : 'normal');
}

function openUploadPageFor(item) {
  const url = videoTypeOf(item) === 'panorama' ? DOUYIN_PANORAMA_UPLOAD_URL : DOUYIN_UPLOAD_URL;
  window.open(url, '_blank', 'noopener,noreferrer');
}

// 文件夹选择和多文件选择共用一个可见入口。菜单只负责选择模式，实际
// 选择仍调用各自原生接口，避免把路径或大视频经过浏览器传输。
function closeMediaPicker() {
  const menu = $('#mediaPickerMenu');
  const trigger = $('#mediaPickerBtn');
  if (!menu || !trigger) return;
  menu.hidden = true;
  trigger.setAttribute('aria-expanded', 'false');
}

function toggleMediaPicker() {
  const menu = $('#mediaPickerMenu');
  const trigger = $('#mediaPickerBtn');
  if (!menu || !trigger || trigger.disabled) return;
  const opening = menu.hidden;
  menu.hidden = !opening;
  trigger.setAttribute('aria-expanded', String(opening));
}

async function request(url, options={}) {
  let res;
  try {
    res = await fetch(url, options);
  } catch (err) {
    // 本地服务停掉时 fetch 只会抛 "Failed to fetch"，用户看不出该做什么。
    // 这里统一换成可执行的提示；请求未发出，重试不会造成重复入队。
    const offline = new Error(
      `无法连接本地服务（${location.host}）。服务可能已经退出：请重新双击「视频发布工具.app」，等页面自动打开后再试一次。`
    );
    offline.offline = true;
    offline.cause = err;
    throw offline;
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || '操作失败');
  return data;
}

function analysisBatchFlags(batch = state.analysis_batch || {}) {
  const status = String(batch.status || 'idle');
  return {
    running: batch.running === true || status === 'running' || status === 'cancelling',
    cancelling: status === 'cancelling',
    completed: Number(batch.completed || 0),
    total: Number(batch.total || 0),
    status,
    message: String(batch.message || ''),
  };
}

function analysisBatchRunning(batch = state.analysis_batch || {}) {
  return analysisBatchFlags(batch).running;
}

function mergeBatchAnalysis(batch = state.analysis_batch || {}) {
  const results = batch && typeof batch.results === 'object' ? batch.results : {};
  state.analysis = state.analysis || {};
  Object.entries(results).forEach(([id, result]) => {
    if (!result || result.status !== 'completed' || !result.analysis) return;
    // Batch results are read-only snapshots until the user presses “采用”.
    const previous = state.analysis[id];
    const resultTime = Date.parse(String(result.analyzed_at || result.analysis.analyzed_at || '')) || 0;
    if (previous && resultTime && Number(previous.analyzedAt || 0) > resultTime) return;
    const normalized = normalizeAnalysis({analysis: result.analysis});
    normalized.adopted = previous?.adopted || '';
    normalized.analyzedAt = resultTime || normalized.analyzedAt;
    state.analysis[id] = normalized;
  });
}

async function refreshAnalysisBatchStatus(includeResults = false) {
  const batch = await request(`/api/analyze-status${includeResults ? '?include_results=1' : ''}`);
  state.analysis_batch = batch || {};
  if (includeResults && batch?.batch_token) analysisBatchResultsToken = String(batch.batch_token);
  mergeBatchAnalysis(state.analysis_batch);
  return state.analysis_batch;
}

function analysisBatchTargets() {
  // If the user has checked cards, use that subset; otherwise analyze all
  // non-skipped entries in one click.  This keeps the common “全部识别” path
  // simple while allowing a large queue to be processed in smaller groups.
  const selected = (state.items || []).filter(item =>
    selectedQueueIds.has(queueItemId(item)) && item.status !== 'skipped'
  );
  if (selected.length) return selected;
  return (state.items || []).filter(item => item.status !== 'skipped');
}

async function startBatchAnalysis() {
  if (queueUiLocked()) return;
  const targets = analysisBatchTargets();
  if (!targets.length) {
    alert('当前没有可识别的视频。');
    return;
  }
  const button = $('#batchAnalyzeBtn');
  if (button) {
    button.disabled = true;
    button.textContent = `准备识别（${targets.length} 条）…`;
  }
  try {
    const payload = await request('/api/analyze-batch', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ids: targets.map(queueItemId)}),
    });
    state.analysis_batch = payload.state || payload;
    analysisBatchResultsToken = '';
    analysisApplyMessage = '';
    // Do not show a previous run's recommendation while the new batch is
    // still processing the same item.  Completed results will be merged back
    // as each batch snapshot becomes available.
    state.analysis = state.analysis || {};
    targets.forEach(item => { delete state.analysis[queueItemId(item)]; });
    mergeBatchAnalysis(state.analysis_batch);
    render();
  } catch (err) {
    alert(`批量识别启动失败：${err.message}`);
    await refreshAnalysisBatchStatus().catch(() => {});
    render();
  }
}

async function cancelBatchAnalysis() {
  const flags = analysisBatchFlags();
  if (!flags.running || flags.cancelling) return;
  const button = $('#cancelBatchAnalyzeBtn');
  if (button) {
    button.disabled = true;
    button.textContent = '正在停止…';
  }
  try {
    const payload = await request('/api/analyze-cancel', {method: 'POST'});
    state.analysis_batch = payload.state || payload;
    render();
  } catch (err) {
    alert(`停止批量识别失败：${err.message}`);
    await refreshAnalysisBatchStatus().catch(() => {});
    render();
  }
}

function batchAnalysisApplyTargets() {
  const results = state.analysis_batch?.results && typeof state.analysis_batch.results === 'object'
    ? state.analysis_batch.results
    : {};
  const itemsById = new Map((state.items || []).map(item => [queueItemId(item), item]));
  const completed = Object.entries(results)
    .filter(([id, result]) => {
      const item = itemsById.get(String(id));
      return Boolean(
        item
        && !hasCaptionDraft(id)
        && copyIsUntouchedForBatch(item)
        && result
        && result.status === 'completed'
        && result.analysis?.recommended,
      );
    })
    .map(([id]) => String(id));
  if (selectedQueueIds.size) return completed.filter(id => selectedQueueIds.has(id));
  return completed;
}

function defaultCaptionForBatch(item) {
  const rawName = String(item?.name || item?.path || '视频');
  const name = rawName.split(/[\\/]/).pop() || '视频';
  const stem = name.replace(/\.[^.]+$/, '').replace(/[_-]+/g, ' ').trim() || '视频';
  return `${stem}｜演唱会现场 #演唱会 #现场`;
}

function copyIsUntouchedForBatch(item) {
  const caption = String(item?.caption || '').trim();
  const hashtags = normalizeHashtags(item?.hashtags || '');
  return (!caption || caption === defaultCaptionForBatch(item)) && (!hashtags || hashtags === '演唱会,现场');
}

async function applyBatchRecommendations() {
  if (queueUiLocked()) return;
  const targets = batchAnalysisApplyTargets();
  if (!targets.length) {
    revealBatchRecommendations();
    return;
  }
  const selectedHint = selectedQueueIds.size ? '已勾选的视频' : '所有已完成识别的视频';
  if (!confirm(`将为${selectedHint}采用推荐文案和话题。已经手动编辑过的条目会跳过，不会被覆盖。是否继续？`)) return;
  const button = $('#batchAdoptBtn');
  if (button) {
    button.disabled = true;
    button.textContent = '采用中…';
  }
  try {
    const payload = await request('/api/analyze-apply', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ids: targets}),
    });
    (payload.applied || []).forEach(id => {
      const result = state.analysis?.[id];
      if (result?.recommended) result.adopted = candidateKey(result.recommended);
    });
    analysisApplyMessage = payload.message || `已采用 ${payload.applied_count || 0} 条推荐`;
    await load();
    render();
  } catch (err) {
    alert(`批量采用推荐失败：${err.message}`);
    await load().catch(() => {});
  } finally {
    const current = $('#batchAdoptBtn');
    if (current) current.disabled = false;
  }
}

function revealBatchRecommendations() {
  const results = state.analysis_batch?.results && typeof state.analysis_batch.results === 'object'
    ? state.analysis_batch.results
    : {};
  const ids = Object.entries(results)
    .filter(([, result]) => result && result.status === 'completed' && result.analysis?.recommended)
    .map(([id]) => String(id));
  if (!ids.length) return;
  ids.forEach(id => requestQueueSectionOpen(id, 'analysis'));
  render();
  const first = [...document.querySelectorAll('.item')].find(card => card.dataset.id === ids[0]);
  first?.scrollIntoView({behavior: 'smooth', block: 'center'});
}

function normalizeHashtags(value) {
  if (Array.isArray(value)) value = value.join(',');
  return String(value ?? '')
    .replace(/#/g, '')
    .split(/[\s,，、;；]+/)
    .map(x => x.trim())
    .filter(Boolean)
    .join(',');
}

function importObjectValue(object, aliases) {
  if (!object || typeof object !== 'object' || Array.isArray(object)) return {found: false, value: ''};
  const wanted = new Set(aliases.map(alias => String(alias).trim().toLowerCase()));
  for (const [key, value] of Object.entries(object)) {
    if (wanted.has(String(key).trim().toLowerCase())) return {found: true, value};
  }
  return {found: false, value: ''};
}

function normalizeCaptionImportRow(raw, fallbackKey = '') {
  if (typeof raw === 'string' || typeof raw === 'number') {
    return {id: '', name: String(fallbackKey || ''), path: '', title: '', caption: String(raw).trim(), hashtags: '', hasCaption: true, hasHashtags: false};
  }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const idValue = importObjectValue(raw, ['id', 'video_id', 'item_id', '视频id']);
  const nameValue = importObjectValue(raw, ['name', 'filename', 'file_name', 'video', '视频', '文件名']);
  const pathValue = importObjectValue(raw, ['path', 'file', 'file_path', 'video_path', '视频路径', '文件路径']);
  const titleValue = importObjectValue(raw, ['title', '标题', '主题', '作品标题']);
  const captionValue = importObjectValue(raw, ['caption', 'text', 'description', 'desc', 'content', '文案', '作品描述']);
  const hashtagsValue = importObjectValue(raw, ['hashtags', 'hashtag', 'tags', 'topics', '话题', '标签']);
  const id = String(idValue.found ? idValue.value ?? '' : '').trim();
  const name = String(nameValue.found ? nameValue.value ?? '' : fallbackKey || '').trim();
  const path = String(pathValue.found ? pathValue.value ?? '' : '').trim();
  const title = String(titleValue.found ? titleValue.value ?? '' : '').trim();
  let caption = '';
  if (captionValue.found) {
    caption = Array.isArray(captionValue.value)
      ? captionValue.value.join('、').trim()
      : String(captionValue.value ?? '').trim();
  }
  const hashtags = normalizeHashtags(hashtagsValue.found ? hashtagsValue.value : '');
  return {id, name, path, title, caption, hashtags, hasCaption: captionValue.found, hasHashtags: hashtagsValue.found};
}

function parseCaptionCsv(text) {
  const source = String(text || '').replace(/^\uFEFF/, '');
  const rows = [];
  let row = [];
  let cell = '';
  let quoted = false;
  for (let index = 0; index < source.length; index += 1) {
    const char = source[index];
    if (quoted) {
      if (char === '"' && source[index + 1] === '"') {
        cell += '"';
        index += 1;
      } else if (char === '"') {
        quoted = false;
      } else {
        cell += char;
      }
      continue;
    }
    if (char === '"') {
      quoted = true;
    } else if (char === ',') {
      row.push(cell);
      cell = '';
    } else if (char === '\n') {
      row.push(cell);
      rows.push(row);
      row = [];
      cell = '';
    } else if (char !== '\r') {
      cell += char;
    }
  }
  if (cell || row.length) {
    row.push(cell);
    rows.push(row);
  }
  const nonEmpty = rows.filter(entry => entry.some(value => String(value).trim() !== ''));
  if (!nonEmpty.length) return [];
  const headers = nonEmpty.shift().map((value, index) => String(value || `column_${index + 1}`).trim());
  return nonEmpty.map(values => {
    const record = {};
    headers.forEach((header, index) => { record[header] = values[index] ?? ''; });
    return record;
  });
}

function parseCaptionImportText(text, filename = '') {
  const extension = String(filename).toLowerCase().split('.').pop();
  const source = String(text || '').replace(/^\uFEFF/, '');
  if (extension === 'csv') {
    return parseCaptionCsv(source).map(row => normalizeCaptionImportRow(row)).filter(Boolean);
  }
  if (extension !== 'json') {
    const caption = source.trim();
    return caption ? [{id: '', name: '', path: '', caption, hashtags: '', hasCaption: true, hasHashtags: false}] : [];
  }
  let parsed;
  try {
    parsed = JSON.parse(source);
  } catch (error) {
    throw new Error(`JSON 文案文件格式错误：${error.message}`);
  }
  let rawRows = parsed;
  if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
    const listValue = importObjectValue(parsed, ['items', 'videos', 'records', 'rows', 'data', '文案']);
    if (listValue.found && Array.isArray(listValue.value)) rawRows = listValue.value;
    else if (importObjectValue(parsed, ['caption', 'text', 'description', '文案', '作品描述']).found) rawRows = [parsed];
    else rawRows = Object.entries(parsed).map(([key, value]) => {
      if (value && typeof value === 'object' && !Array.isArray(value)) return {...value, name: value.name || key};
      return {name: key, caption: value};
    });
  }
  if (!Array.isArray(rawRows)) rawRows = [rawRows];
  return rawRows.map((row, index) => normalizeCaptionImportRow(row, `第${index + 1}条`)).filter(Boolean);
}

function importComparable(value) {
  return String(value || '').trim().replaceAll('\\', '/').replace(/^\.\//, '').toLowerCase();
}

function importBasename(value) {
  const normalized = importComparable(value).replace(/\/+$/, '');
  return normalized.split('/').pop() || '';
}

function captionImportMatches(item, row) {
  // Explicit IDs and paths are authoritative: never fall back to a basename
  // when a supplied identity points at a different file.
  if (String(row.id || '').trim()) return importComparable(row.id) === importComparable(queueItemId(item));
  if (String(row.path || '').trim()) return importComparable(row.path) === importComparable(item?.path);
  const name = importComparable(row.name);
  if (!name) return false;
  if (name.includes('/')) return name === importComparable(item?.path);
  return name === importComparable(item?.name) || name === importBasename(item?.path);
}

function captionImportRowHasIdentity(row) {
  const name = String(row?.name || '').trim();
  // JSON 字符串数组会得到“第1条”这类内部占位名，它不是匹配键，
  // 允许在已勾选队列中按顺序应用；真正的文件名仍按 name 匹配。
  const syntheticName = /^第\d+条$/.test(name);
  return Boolean(row && (String(row.id || '').trim() || String(row.path || '').trim() || (name && !syntheticName)));
}

function captionImportTargets(rows) {
  const items = state.items || [];
  const available = items.filter(item => item.status !== 'skipped');
  const availableNames = available.map(item => item.name || item.path || '');
  let targets = available;
  let allowSequential = false;
  if (captionImportTargetId) {
    const item = available.find(entry => queueItemId(entry) === captionImportTargetId);
    if (!item) throw new Error('目标视频已不在当前队列中或已跳过，请重新选择。');
    targets = [item];
    allowSequential = true;
  } else {
    const selected = available.filter(item => selectedQueueIds.has(queueItemId(item)));
    if (selected.length) {
      targets = selected;
      allowSequential = true;
    }
  }
  if (!rows.length) throw new Error('导入文件没有可用的文案内容。');

  // A single plain-text record is intentionally reusable across selected cards.
  if (allowSequential && rows.length === 1 && !captionImportRowHasIdentity(rows[0])) {
    return {matched: targets.map(item => ({id: queueItemId(item), row: rows[0]})), unmatched: [], availableNames};
  }
  const assignments = [];
  const unmatched = [];
  const generic = [];
  for (const row of rows) {
    if (!captionImportRowHasIdentity(row)) {
      generic.push(row);
      continue;
    }
    // Count all candidates before reserving targets so duplicate names cannot
    // become artificially unambiguous after a previous row was assigned.
    const candidates = targets.filter(item => captionImportMatches(item, row));
    if (candidates.length === 1) assignments.push({id: queueItemId(candidates[0]), row});
    else unmatched.push({...row, reason: candidates.length ? '文件名对应多个视频，请使用唯一ID、完整路径或单独选择目标视频' : '未找到匹配的目标视频'});
  }
  const counts = new Map();
  assignments.forEach(entry => counts.set(entry.id, (counts.get(entry.id) || 0) + 1));
  const matched = assignments.filter(entry => counts.get(entry.id) === 1);
  assignments.filter(entry => counts.get(entry.id) > 1).forEach(entry => {
    unmatched.push({...entry.row, reason: '多条记录指向同一视频，请去重后重新导入'});
  });
  const reserved = new Set(assignments.map(entry => entry.id));
  const remaining = targets.filter(item => !reserved.has(queueItemId(item)));
  generic.forEach((row, index) => {
    if (allowSequential && remaining[index]) matched.push({id: queueItemId(remaining[index]), row});
    else unmatched.push({...row, reason: allowSequential ? '无对应的剩余已选视频' : '缺少匹配身份，请填写文件名或先勾选目标视频'});
  });
  return {matched, unmatched, availableNames};
}

async function exportUnmatchedCaptionRows(unmatched, availableNames) {
  const payload = {unmatched, available_names: availableNames};
  const response = await request('/api/export-unmatched', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
  // The response here is actually handled by fetch blob; but request() parses JSON.
  // We'll handle this differently - use raw fetch for binary.
  throw new Error('internal: should use raw fetch');
}

async function applyCaptionImport(result, overwrite = true, confirmed = false) {
  const {matched, unmatched, availableNames} = result;
  const valid = matched.filter(entry => entry?.id && (entry.row?.hasCaption || entry.row?.hasHashtags || entry.row?.title));
  if (!valid.length && !unmatched.length) throw new Error('导入内容没有文案、话题或标题字段。');

  // Write matched first
  let writtenCount = 0;
  if (valid.length) {
    const scope = valid.length === 1 ? '1 条视频' : `${valid.length} 条视频`;
    if (overwrite && !confirmed && !confirm(`将导入 ${scope}的文案/话题/标题；这会替换对应字段的现有文案，但不会发布视频。是否继续？`)) return false;
    for (const assignment of valid) {
      const current = (state.items || []).find(item => queueItemId(item) === String(assignment.id)) || {};
      const body = {};
      if (assignment.row.title && (overwrite || !String(current.work_title || '').trim())) body.work_title = assignment.row.title;
      if (assignment.row.hasCaption && (overwrite || !String(current.caption || '').trim())) body.caption = assignment.row.caption;
      if (assignment.row.hasHashtags && (overwrite || !String(current.hashtags || '').trim())) body.hashtags = assignment.row.hashtags;
      if (!Object.keys(body).length) continue;
      const pendingSaves = ['caption', 'hashtags']
        .map(field => editorSaveInFlight.get(`${assignment.id}:${field}`)?.promise)
        .filter(Boolean);
      if (pendingSaves.length) await Promise.allSettled(pendingSaves);
      cancelEditorSave(assignment.id, 'caption');
      cancelEditorSave(assignment.id, 'hashtags');
      clearEditorDraft(assignment.id, 'caption');
      clearEditorDraft(assignment.id, 'hashtags');
      await request('/api/items/' + encodeURIComponent(assignment.id), {
        method: 'PATCH',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
      });
      writtenCount++;
    }
    await load();
  }

  // Handle unmatched
  if (unmatched.length) {
    const unmatchedNames = unmatched.map(r => r.name || r.id || r.path || '（无名）').join('、');
    const exportIt = confirm(`已导入 ${writtenCount} 条视频的文案/话题/标题。\n\n有 ${unmatched.length} 条文案未匹配到队列视频：\n${unmatchedNames}\n\n是否导出未匹配记录（Excel），方便排查？\n\n点击「确定」导出排查文件，点击「取消」跳过。`);
    if (exportIt) {
      try {
        const response = await fetch('/api/export-unmatched', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({unmatched, available_names: availableNames}),
        });
        if (!response.ok) {
          const data = await response.json().catch(() => ({}));
          throw new Error(data.error || `导出失败 (${response.status})`);
        }
        const blob = await response.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = '未匹配记录.xlsx';
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
        alert(`已导出 ${unmatched.length} 条未匹配记录，请检查文件名是否与队列一致。`);
      } catch (err) {
        alert(`导出未匹配记录失败：${err.message}`);
      }
    }
  } else if (writtenCount > 0) {
    alert(`已导入 ${writtenCount} 条视频的文案/话题/标题。`);
  }
  return true;
}

async function handleCaptionImport(event) {
  const input = event?.target;
  const file = input?.files?.[0];
  if (!file || captionImportBusy) return;
  if (queueUiLocked()) {
    alert('自动发布正在运行、暂停或终止处理中，暂时不能导入文案。');
    input.value = '';
    return;
  }
  captionImportBusy = true;
  try {
    let rows;
    const extension = String(file.name).toLowerCase().split('.').pop();
    if (extension === 'xlsx') {
      const formData = new FormData();
      formData.append('file', file);
      const response = await request('/api/import-captions', {
        method: 'POST',
        body: formData,
      });
      if (!response.rows || !Array.isArray(response.rows)) {
        throw new Error(response.error || '后端未返回有效的 Excel 数据');
      }
      rows = response.rows.map(row => normalizeCaptionImportRow(row)).filter(Boolean);
    } else {
      rows = parseCaptionImportText(await file.text(), file.name);
    }
    if (!rows.length) throw new Error('导入文件为空，或没有可用的文案字段。');
    await applyCaptionImport(captionImportTargets(rows), captionImportOverwrite, captionImportConfirmed);
  } catch (error) {
    alert(`导入文案失败：${error.message}`);
  } finally {
    captionImportBusy = false;
    captionImportTargetId = '';
    captionImportConfirmed = false;
    input.value = '';
    render();
  }
}

function normalizeCandidate(raw = {}) {
  const candidate = raw && typeof raw === 'object' ? raw : {};
  const confidence = candidate.confidence == null ? '' : String(candidate.confidence);
  return {
    title: String(candidate.title ?? candidate.label ?? '').trim(),
    caption: String(candidate.caption ?? candidate.text ?? '').trim(),
    hashtags: normalizeHashtags(candidate.hashtags ?? candidate.tags ?? candidate.topics),
    reason: String(candidate.reason ?? '').trim(),
    confidence,
  };
}
function normalizeEvidence(value) {
  if (Array.isArray(value)) {
    return value.map((entry, index) => {
      if (entry && typeof entry === 'object') {
        return {
          label: String(entry.title ?? entry.label ?? entry.id ?? `依据${index + 1}`),
          value: String(entry.value ?? ''),
          detail: String(entry.detail ?? ''),
          confidence: String(entry.confidence ?? ''),
        };
      }
      return {label: `依据${index + 1}`, value: String(entry ?? ''), detail: '', confidence: ''};
    }).filter(entry => entry.value || entry.detail);
  }
  if (value && typeof value === 'object') {
    return Object.entries(value).map(([label, entry]) => ({
      label: String(label),
      value: Array.isArray(entry) ? entry.join('、') : String(entry ?? ''),
      detail: '',
      confidence: '',
    })).filter(entry => entry.value);
  }
  return [];
}
function normalizeAnalysis(payload = {}) {
  const source = payload.analysis && typeof payload.analysis === 'object' ? payload.analysis : payload;
  const candidates = Array.isArray(source.candidates || payload.candidates)
    ? (source.candidates || payload.candidates).map(normalizeCandidate).filter(x => x.caption || x.hashtags)
    : [];
  let recommended = normalizeCandidate(source.recommended || payload.recommended || {});
  if ((!recommended.caption && !recommended.hashtags) && candidates.length) {
    recommended = {...candidates[0], reason: '优先使用最保守的现场记录表达。'};
  }
  const recommendedTags = normalizeHashtags(source.recommended_tags || payload.recommended_tags || recommended.hashtags);
  if (!recommended.hashtags && recommendedTags) recommended.hashtags = recommendedTags;
  return {
    candidates,
    recommended,
    recommendedTags,
    tagRecommendations: source.tag_recommendations || payload.tag_recommendations || [],
    evidence: normalizeEvidence(source.evidence ?? payload.evidence),
    mode: String(source.mode || payload.mode || ''),
    capabilities: source.capabilities || payload.capabilities || {},
    visualSignals: Array.isArray(source.visual_signals || payload.visual_signals) ? (source.visual_signals || payload.visual_signals) : [],
    audioEvidence: normalizeEvidence(source.audio_evidence ?? payload.audio_evidence),
    frames: Array.isArray(source.frames || payload.frames) ? (source.frames || payload.frames) : [],
    limitations: Array.isArray(source.limitations || payload.limitations) ? (source.limitations || payload.limitations).map(String) : [],
    degraded: source.degraded === true || payload.degraded === true,
    degradedReason: String(source.degraded_reason || payload.degraded_reason || ''),
    adopted: '',
    analyzedAt: Date.now(),
  };
}
function candidateKey(candidate) {
  return JSON.stringify([candidate.caption, candidate.hashtags]);
}
function formatHashtags(value) {
  const normalized = normalizeHashtags(value);
  return normalized ? normalized.split(',').map(tag => `#${tag}`).join(' ') : '未提供话题';
}
async function load() {
  const analysis = state.analysis || {};
  const previousBatch = state.analysis_batch || {};
  const previousResults = previousBatch.results && typeof previousBatch.results === 'object'
    ? previousBatch.results
    : {};
  const hasFullResults = Object.values(previousResults).some(result => result && result.analysis);
  state = await request('/api/state');
  state.analysis = analysis;
  state.analysis_batch = state.analysis_batch || {};
  const summary = state.analysis_batch;
  const batchToken = String(summary.batch_token || '');
  if (!analysisBatchRunning(summary) && batchToken && (!hasFullResults || analysisBatchResultsToken !== batchToken)) {
    await refreshAnalysisBatchStatus(true).catch(() => {});
  } else if (hasFullResults && batchToken && batchToken === analysisBatchResultsToken) {
    // /api/state intentionally omits the large frame/audio payload after the
    // first load.  Preserve the already-loaded candidates across ordinary
    // actions such as saving an editor field; otherwise one PATCH would make
    // the batch-adoption control disappear until a full page reload.
    state.analysis_batch = {...summary, results: previousResults};
  }
  mergeBatchAnalysis(state.analysis_batch);
  render();
}
async function scan() {
  const latest = await request('/api/scan', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(workflowSelectionPayload()),
  });
  state.items = latest.items || [];
  state.root = latest.root || '';
  state.publish = await request('/api/publish-status');
  state.analysis_batch = await request('/api/analyze-status').catch(() => state.analysis_batch || {});
  analysisBatchResultsToken = '';
  render();
}
async function chooseFolder() {
  const latest = await request('/api/select-folder', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(workflowSelectionPayload()),
  });
  state.items = latest.items || [];
  state.root = latest.root || '';
  state.publish = await request('/api/publish-status');
  state.analysis = {};
  state.analysis_batch = {};
  analysisBatchResultsToken = '';
  render();
}
async function chooseFiles() {
  // 文件选择由本地服务打开 macOS 原生多选对话框；不把大视频复制到浏览器或
  // 网页进程，只把选中的本机路径加入队列。
  const latest = await request('/api/select-files', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(workflowSelectionPayload()),
  });
  if (latest.cancelled) return;
  state.items = latest.items || [];
  state.root = latest.root || state.root;
  state.publish = await request('/api/publish-status');
  state.analysis = {};
  state.analysis_batch = {};
  analysisBatchResultsToken = '';
  render();
  const added = Number(latest.added || 0);
  const skipped = Array.isArray(latest.skipped) ? latest.skipped.length : 0;
  const invalid = Array.isArray(latest.invalid) ? latest.invalid.length : 0;
  const summary = [`已加入 ${added} 条`];
  if (skipped) summary.push(`跳过重复 ${skipped} 条`);
  if (invalid) summary.push(`忽略非视频或无效文件 ${invalid} 个`);
  $('#publishHint').textContent = summary.join('，') + '。';
}

function schedulePlanIntervalValue() {
  const select = $('#scheduleDefaultInterval');
  const custom = $('#scheduleCustomInterval');
  if (!select) return null;
  const raw = select.value === 'custom' ? String(custom?.value || '').trim() : select.value;
  if (!/^\d+$/.test(raw) || Number(raw) < 1 || Number(raw) > MAX_SCHEDULE_INTERVAL_MINUTES) return null;
  return String(Number(raw));
}

function scheduleIntervalBody(select, custom) {
  const body = {};
  if (!select) return body;
  if (select.value !== 'custom') {
    body.schedule_interval_minutes = select.value;
    return body;
  }
  const raw = String(custom?.value || '').trim();
  if (/^\d+$/.test(raw) && Number(raw) >= 1 && Number(raw) <= MAX_SCHEDULE_INTERVAL_MINUTES) {
    body.schedule_interval_minutes = String(Number(raw));
  }
  // 自定义框为空或无效时不发送 interval PATCH；用户补全后再保存。
  return body;
}

function buildScheduleIntervalCascade(item, intervalValue) {
  const updates = [{
    id: queueItemId(item),
    body: {schedule_interval_minutes: intervalValue},
  }];
  const result = {updates, affected: 0, stoppedReason: ''};
  // 空间隔只保存本条设置，不会猜测后续视频的发布时间。
  if (!intervalValue) return result;
  const items = state.items || [];
  const currentIndex = items.findIndex(candidate => queueItemId(candidate) === queueItemId(item));
  const currentAt = parseLocalDateTime(item.schedule_at);
  if (currentIndex < 0 || !currentAt) {
    result.stoppedReason = '当前视频没有有效的定时发布时间';
    return result;
  }
  // 已提交/已跳过的条目不可被批量重排；当前条目仍可保存自己的间隔。
  if (!isResumable(item)) {
    result.stoppedReason = '当前视频已提交或已跳过';
    return result;
  }
  let cursor = currentAt;
  let gap = Number(intervalValue);
  for (let index = currentIndex + 1; index < items.length; index += 1) {
    const next = items[index];
    // 立即发布的条目不占用定时间隔，继续寻找下一条定时视频。
    if (scheduleModeOf(next) !== 'scheduled') continue;
    if (!isResumable(next)) {
      result.stoppedReason = `遇到不可编辑条目“${next.name || next.path || ''}”`;
      break;
    }
    // 后续条目必须已有可解析时间，避免把一个半配置条目静默改成定时。
    if (!parseLocalDateTime(next.schedule_at)) {
      result.stoppedReason = `后续条目“${next.name || next.path || ''}”缺少有效发布时间`;
      break;
    }
    cursor = new Date(cursor.getTime() + gap * 60 * 1000);
    updates.push({
      id: queueItemId(next),
      body: {schedule_at: localDateTimeValue(cursor)},
    });
    result.affected += 1;
    const nextGap = scheduleIntervalOf(next);
    if (!nextGap) break;
    gap = Number(nextGap);
  }
  return result;
}

async function saveScheduleIntervalWithCascade(item, id, intervalValue, select, custom) {
  const cascade = buildScheduleIntervalCascade(item, intervalValue);
  if (cascade.affected > 0) {
    const names = cascade.updates.slice(1).map(update => {
      const target = (state.items || []).find(candidate => queueItemId(candidate) === update.id);
      return target?.name || target?.path || update.id;
    });
    const preview = names.length > 3 ? `${names.slice(0, 3).join('、')} 等 ${names.length} 条` : names.join('、');
    if (!confirm(`修改本条间隔会同步调整后续 ${names.length} 条定时视频的预约时间：\n${preview}\n\n是否继续？`)) {
      const previousInterval = scheduleIntervalOf(item);
      select.value = SCHEDULE_INTERVAL_PRESETS.includes(previousInterval) ? previousInterval : (previousInterval ? 'custom' : '');
      custom.value = SCHEDULE_INTERVAL_PRESETS.includes(previousInterval) ? '' : previousInterval;
      custom.hidden = select.value !== 'custom';
      return;
    }
  }
  const suffix = cascade.affected
    ? `已保存本条间隔，并同步调整后续 ${cascade.affected} 条预约时间。`
    : cascade.stoppedReason
      ? `已保存本条间隔；未继续调整后续时间（${cascade.stoppedReason}）。`
      : '已保存本条间隔。';
  try {
    await applyScheduleUpdates(cascade.updates, suffix);
  } catch (err) {
    // Card-level changes should report the same way as the other editable
    // fields; do not leave an unhandled promise that makes the control look
    // saved when the server rejected the atomic update.
    alert(`保存定时设置失败：${err.message}`);
    await load().catch(() => {});
  }
}

function setSchedulePlanHint(message, kind = '') {
  const hint = $('#schedulePlanHint');
  if (!hint) return;
  hint.textContent = message || '';
  hint.className = `schedule-plan-hint${kind ? ` ${kind}` : ''}`;
}

async function applyScheduleUpdates(updates, successMessage) {
  if (!updates.length) {
    setSchedulePlanHint('当前没有待发布视频可设置。', 'warning');
    return;
  }
  if (queueActionsLocked() || schedulePlanBusy) {
    setSchedulePlanHint('发布任务正在运行或正在处理计划，请稍后再修改。', 'warning');
    return;
  }
  schedulePlanBusy = true;
  setSchedulePlanHint(`正在保存 ${updates.length} 条设置…`);
  render();
  let saved = 0;
  try {
    await request('/api/schedule-plan', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({updates: updates.map(update => ({id: update.id, ...update.body}))}),
    });
    saved = updates.length;
    await load();
    setSchedulePlanHint(successMessage || `已保存 ${saved} 条设置。`, 'success');
  } catch (err) {
    await load().catch(() => {});
    setSchedulePlanHint(`已保存 ${saved} 条，剩余设置失败：${err.message}`, 'warning');
  } finally {
    schedulePlanBusy = false;
    render();
  }
}

async function generateSchedulePlan() {
  const targets = schedulePlanTargets(state.items || []);
  const mode = $('#schedulePlanMode')?.value || 'scheduled';
  if (mode === 'immediate') {
    await setAllImmediate(false);
    return;
  }
  if (queueUiLocked()) return;
  const startInput = $('#scheduleStartAt');
  let rawStart = String(startInput?.value || '').trim();
  // 输入框为空时不直接报错：填入默认的首条时间并继续，避免“明明有默认时间却
  // 提示请选择”的死路。用户仍可在生成前改成本次想要的时间。
  if (!rawStart) {
    rawStart = defaultScheduleStart(state.items || []);
    if (startInput) startInput.value = rawStart;
  }
  const normalizedStart = normalizeDateTimeText(rawStart);
  if (normalizedStart !== rawStart && startInput) {
    // 全角数字/冒号或不可见字符：先把输入框改成标准写法，再做解析。
    rawStart = normalizedStart;
    startInput.value = rawStart;
  }
  const start = parseDateTimeInput(startInput) || parseLocalDateTime(rawStart);
  if (!start) {
    const length = [...rawStart].length;
    const numeric = Number(startInput?.valueAsNumber);
    const controlState = Number.isFinite(numeric) ? '有效' : '空/无效';
    console.warn('schedule start parse failed', {
      value: rawStart,
      length,
      codes: [...rawStart].map(char => char.codePointAt(0).toString(16)),
      valueAsNumber: startInput?.valueAsNumber,
    });
    setSchedulePlanHint(`无法读取首条定时发布时间（当前值：${rawStart || '空'}；长度 ${length}；控件值：${controlState}），请重新选择。`, 'warning');
    updateSchedulePlanPreview();
    startInput?.focus();
    return;
  }
  if (start.getTime() <= Date.now()) {
    setSchedulePlanHint('首条定时发布时间必须晚于当前时间；这不是软件等待时间。', 'warning');
    updateSchedulePlanPreview();
    startInput?.focus();
    return;
  }
  const interval = schedulePlanIntervalValue();
  if (!interval) {
    setSchedulePlanHint('请输入 1～43200 分钟的有效间隔。', 'warning');
    $('#scheduleCustomInterval')?.focus();
    return;
  }
  const updates = [];
  let cursor = new Date(start.getTime());
  targets.forEach((item, index) => {
    const isLast = index === targets.length - 1;
    updates.push({
      id: queueItemId(item),
      body: {
        schedule_mode: 'scheduled',
        schedule_at: localDateTimeValue(cursor),
        // 该字段表示本条到下一条定时视频的间隔；最后一条没有下一条。
        schedule_interval_minutes: isLast ? '' : interval,
      },
    });
    cursor = new Date(cursor.getTime() + Number(interval) * 60 * 1000);
  });
  await applyScheduleUpdates(updates, `已为 ${updates.length} 条待发布视频生成定时计划（首条 ${formatScheduleMoment(start)}，间隔 ${interval} 分钟）。`);
}

async function setAllImmediate(confirmBefore = true) {
  if (queueUiLocked()) return;
  const targets = schedulePlanTargets(state.items || []);
  if (!targets.length) {
    setSchedulePlanHint('当前没有待发布视频可设置。', 'warning');
    return;
  }
  if (confirmBefore && !confirm(`确定将 ${targets.length} 条待发布视频改为立即发布吗？\n\n这会清除它们的定时发布时间，但不会删除视频或文案。`)) return;
  const updates = targets.map(item => ({
    id: queueItemId(item),
    body: {schedule_mode: 'immediate', schedule_at: '', schedule_interval_minutes: ''},
  }));
  await applyScheduleUpdates(updates, `已将 ${updates.length} 条待发布视频设为立即发布。`);
}

function clearSchedulePlanHint() {
  const hint = $('#schedulePlanHint');
  if (!hint || (!hint.classList.contains('warning') && !hint.classList.contains('success'))) return;
  hint.textContent = '';
  hint.className = 'schedule-plan-hint';
}

function updateSchedulePlanPreview() {
  const preview = $('#schedulePlanPreview');
  if (!preview) return;
  const targets = schedulePlanTargets(state.items || []);
  if (!targets.length) {
    preview.textContent = '';
    return;
  }
  const mode = $('#schedulePlanMode')?.value || 'scheduled';
  if (mode !== 'scheduled') {
    preview.textContent = `将把 ${targets.length} 条待发布视频设为立即发布。`;
    return;
  }
  const start = parseDateTimeInput($('#scheduleStartAt'));
  const interval = schedulePlanIntervalValue();
  if (!start || !interval) {
    preview.textContent = '';
    return;
  }
  const last = new Date(start.getTime() + Number(interval) * 60 * 1000 * Math.max(0, targets.length - 1));
  const past = start.getTime() <= Date.now() ? '（首条时间已过去）' : '';
  preview.textContent = `首条 ${formatScheduleMoment(start)} · 间隔 ${interval} 分钟 · 最后一条 ${formatScheduleMoment(last)} · 共 ${targets.length} 条${past}`;
}

function updateSchedulePlanUi(items = state.items || [], pub = state.publish || {}) {
  ensureSchedulePlanDefaults(items);
  const targets = schedulePlanTargets(items);
  const scheduled = targets.filter(item => scheduleModeOf(item) === 'scheduled').length;
  const summary = $('#schedulePlanSummary');
  if (summary) summary.textContent = targets.length ? `待发布 ${targets.length} 条 · 已配置定时 ${scheduled} 条` : '暂无待发布视频';
  const locked = queueUiLocked(pub) || !targets.length;
  const planMode = $('#schedulePlanMode');
  const planIsScheduled = !planMode || planMode.value === 'scheduled';
  ['schedulePlanMode', 'scheduleStartAt', 'scheduleDefaultInterval', 'scheduleCustomInterval', 'generateScheduleBtn', 'allImmediateBtn', 'defaultScheduleBtn']
    .map(id => $('#' + id)).filter(Boolean).forEach(control => { control.disabled = locked || (!planIsScheduled && ['scheduleStartAt', 'scheduleDefaultInterval', 'scheduleCustomInterval'].includes(control.id)); });
  const custom = $('#scheduleCustomInterval');
  const select = $('#scheduleDefaultInterval');
  if (custom && select) custom.hidden = select.value !== 'custom';
  const generate = $('#generateScheduleBtn');
  if (generate) generate.textContent = schedulePlanBusy ? '保存中…' : (planIsScheduled ? '生成定时计划' : '按模式设为立即发布');
  updateSchedulePlanPreview();
}

async function clearQueue() {
  if (queueUiLocked()) {
    alert('自动发布正在运行、暂停或终止处理中，暂时不能清空队列；请等待任务停止后再试。');
    return;
  }
  if (!(state.items || []).length) return;
  const confirmed = confirm('确定清空当前发布队列吗？\n\n这里只会移除本地工具中的队列记录，不会删除源视频文件。');
  if (!confirmed) return;
  const latest = await request('/api/clear-queue', {method:'POST'});
  state.items = latest.items || [];
  state.root = latest.root || state.root;
  state.analysis = {};
  state.publish = await request('/api/publish-status');
  selectedQueueIds.clear();
  render();
}

async function deleteQueueItems(ids, {confirmBefore = true} = {}) {
  const uniqueIds = [...new Set((ids || []).map(String).filter(Boolean))];
  if (!uniqueIds.length) return;
  if (queueUiLocked()) {
    alert('自动发布正在运行、暂停或终止处理中，暂时不能删除队列视频；请等待任务停止后再试。');
    return;
  }
  if (confirmBefore) {
    const wording = uniqueIds.length === 1 ? '这条视频' : `选中的 ${uniqueIds.length} 条视频`;
    if (!confirm(`确定从发布队列中删除${wording}吗？\n\n这里只会移除本地队列记录，不会删除原视频文件。`)) return;
  }
  const button = $('#deleteSelectedBtn');
  if (button) button.disabled = true;
  try {
    const latest = await request('/api/items/delete', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ids: uniqueIds}),
    });
    selectedQueueIds = new Set([...selectedQueueIds].filter(id => !uniqueIds.includes(id)));
    state.items = latest.items || [];
    state.root = latest.root || state.root;
    state.analysis = {};
    state.publish = await request('/api/publish-status');
    render();
  } catch (err) {
    alert(`删除队列视频失败：${err.message}`);
    render();
  }
}

async function stopPublish() {
  const flags = publishFlags(state.publish || {});
  if (!publishCanTerminate(state.publish || {}, flags)) return;
  // 终止是明确的即时控制动作，不再弹二次确认；后端负责停止发布进程，
  // 保留浏览器页面、草稿和本地队列，便于用户检查现场。
  const button = $('#stopPublishBtn');
  button.disabled = true;
  button.textContent = '正在终止…';
  try {
    await request('/api/publish-stop', {method:'POST'});
    await load();
  } catch (err) {
    alert(`终止自动发布失败：${err.message}`);
    await load().catch(() => {});
  }
}

// 开始、暂停、继续共用一个按钮。按钮文案只反映当前发布状态，避免
// 同一时刻出现多个互相矛盾的控制入口。
async function togglePublish() {
  // 批量计划保存必须先完成，避免发布器读到半更新的时间表。
  if (schedulePlanBusy) return;
  const flags = publishFlags(state.publish || {});
  const workItems = (state.items || []).filter(isResumable);
  let endpoint = '';
  let buttonText = '';

  // 暂停状态下暂停标记通常仍会保留，直到用户点击继续；因此 paused
  // 必须优先于 pauseRequested 判断，否则按钮会一直停在“正在暂停”。
  if (flags.terminateRequested || (flags.pauseRequested && !flags.paused)) return;
  if (flags.running && flags.paused) {
    endpoint = '/api/publish-resume';
    buttonText = '正在继续…';
  } else if (flags.running) {
    endpoint = '/api/publish-pause';
    buttonText = '正在暂停…';
  } else {
    if (!workItems.length) return;
    // 任务已暂停但发布器进程已退出、或任务被终止后重新开始时，
    // /api/publish 会按当前队列状态恢复，而不是重置已完成条目。
    endpoint = '/api/publish';
    buttonText = '正在开始…';
  }

  const button = $('#autoPublishBtn');
  button.disabled = true;
  const original = button.textContent;
  button.textContent = buttonText;
  try {
    await request(endpoint, {method:'POST'});
    await load();
  } catch (err) {
    alert(`发布控制失败：${err.message}`);
    button.textContent = original;
    await load().catch(() => {});
  }
}

async function controlChannels(action) {
  const endpoint = `/api/channels-publish/${action}`;
  try {
    await request(endpoint, {method:'POST'});
    await load();
  } catch (err) {
    alert(`视频号控制失败：${err.message}`);
    await load().catch(() => {});
  }
}

async function publishChannelsQueue() {
  if (channelsPublishRunning() || queueUiLocked()) return;
  const items = state.items || [];
  const selected = selectedQueueIds.size
    ? items.filter(item => selectedQueueIds.has(queueItemId(item)))
    : items.filter(item => item.status !== 'skipped');
  const ids = selected
    .filter(item => videoTypeOf(item) === 'normal')
    .filter(item => ['pending', 'failed', 'publishing'].includes(String(item?.platforms?.channels?.status || 'pending')))
    .map(queueItemId);
  if (!ids.length) {
    $('#publishHint').textContent = '当前选择中没有可发布到视频号的普通视频。全景视频会自动跳过。';
    return;
  }
  const button = $('#channelsPublishBtn');
  if (button) { button.disabled = true; button.textContent = '正在启动视频号…'; }
  try {
    const result = await request('/api/channels-publish-queue', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ids}),
    });
    const skipped = Array.isArray(result.skipped) ? result.skipped.length : 0;
    $('#publishHint').textContent = `已启动视频号发布 ${result.count || ids.length} 条${skipped ? `，跳过全景视频 ${skipped} 条` : ''}。`;
    await load();
  } catch (err) {
    if (button) { button.disabled = false; button.textContent = '发布视频号'; }
    alert(`视频号发布启动失败：${err.message}`);
    await load().catch(() => {});
  }
}

async function patch(id, body) {
  await request('/api/items/' + encodeURIComponent(id), {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  await load();
}

function scheduleEditorSave(id, field, value) {
  rememberEditorDraft(id, field, value);
  const key = `${String(id)}:${field}`;
  const previous = editorSaveTimers.get(key);
  if (previous) clearTimeout(previous);
  const timer = setTimeout(() => {
    editorSaveTimers.delete(key);
    saveEditorField(id, field, value).catch(() => {});
  }, 800);
  editorSaveTimers.set(key, timer);
}

function cancelEditorSave(id, field) {
  const key = `${String(id)}:${field}`;
  const timer = editorSaveTimers.get(key);
  if (timer) clearTimeout(timer);
  editorSaveTimers.delete(key);
}

function saveEditorField(id, field, value) {
  const key = `${String(id)}:${field}`;
  const timer = editorSaveTimers.get(key);
  if (timer) clearTimeout(timer);
  editorSaveTimers.delete(key);
  const normalized = String(value ?? '');
  const previous = editorSaveInFlight.get(key);
  if (previous && previous.value === normalized) return previous.promise;
  rememberEditorDraft(id, field, normalized);
  const promise = (async () => {
    try {
      await request('/api/items/' + encodeURIComponent(id), {
        method: 'PATCH',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({[field]: normalized}),
      });
      // 如果用户在请求返回前又输入了新内容，保留新草稿，不能误清除。
      if (editorDrafts.get(String(id))?.[field] === normalized) clearEditorDraft(id, field);
      await load();
    } catch (error) {
      // 保留草稿，下一次 change/input 仍可重试；错误只提示一次，不丢失用户输入。
      alert(`保存${editorFieldLabel(field)}失败：${error.message}`);
      throw error;
    }
  })();
  editorSaveInFlight.set(key, {value: normalized, promise});
  // 使用双分支 then 清理，不产生未处理的 rejected finally promise。
  promise.then(
    () => { if (editorSaveInFlight.get(key)?.promise === promise) editorSaveInFlight.delete(key); },
    () => { if (editorSaveInFlight.get(key)?.promise === promise) editorSaveInFlight.delete(key); },
  );
  return promise;
}

function analysisOptionHtml(candidate, kind, adoptedKey) {
  if (!candidate.caption && !candidate.hashtags) return '';
  const key = candidateKey(candidate);
  const adopted = adoptedKey && key === adoptedKey;
  const title = kind === 'recommended' ? '推荐组合' : (candidate.title || candidate.label || '候选组合');
  const confidence = candidate.confidence ? ` · 置信度 ${escapeHtml(candidate.confidence)}` : '';
  const reason = candidate.reason ? `<p class="analysis-reason">${escapeHtml(candidate.reason)}${confidence}</p>` : (confidence ? `<p class="analysis-reason">${confidence.slice(3)}</p>` : '');
  return `<article class="analysis-option ${kind}">
    <div class="analysis-option-top"><strong>${title}</strong><button class="adopt-analysis" type="button" data-analysis-key="${escapeHtml(key)}" ${adopted ? 'disabled' : ''}>${adopted ? '已采用' : '采用'}</button></div>
    <p class="analysis-caption">${escapeHtml(candidate.caption || '（无文案，仅提供话题）')}</p>
    <p class="analysis-hashtags">${escapeHtml(formatHashtags(candidate.hashtags))}</p>
    ${reason}
  </article>`;
}

function analysisPanelHtml(item) {
  const result = (state.analysis || {})[queueItemId(item)];
  if (!result) return '<section class="analysis-panel" hidden></section>';
  const modeLabel = result.mode === 'vision_frames_sound'
    ? '关键帧视觉 + 声音场景'
    : result.mode === 'vision_frames'
      ? '关键帧视觉'
      : result.mode === 'filename_metadata'
        ? '文件/元数据降级'
        : (result.mode || '本地分析');
  const options = [];
  const recommended = analysisOptionHtml(result.recommended, 'recommended', result.adopted);
  if (recommended) options.push(recommended);
  const recommendedKey = result.recommended ? candidateKey(result.recommended) : '';
  (result.candidates || []).filter(candidate => candidateKey(candidate) !== recommendedKey).forEach(candidate => {
    const html = analysisOptionHtml(candidate, 'candidate', result.adopted);
    if (html) options.push(html);
  });
  const evidenceEntries = Array.isArray(result.evidence) ? result.evidence : [];
  const evidence = evidenceEntries.length
    ? `<details class="analysis-evidence"><summary>查看识别依据</summary><dl>${evidenceEntries.map(entry => `<dt>${escapeHtml(entry.label)}</dt><dd>${escapeHtml(entry.value)}${entry.detail ? `<small>${escapeHtml(entry.detail)}</small>` : ''}${entry.confidence ? `<small>置信度：${escapeHtml(entry.confidence)}</small>` : ''}</dd>`).join('')}</dl></details>`
    : '';
  const limitations = Array.isArray(result.limitations) ? result.limitations : [];
  const frameCount = Array.isArray(result.frames) ? result.frames.length : 0;
  const audioCount = Array.isArray(result.audioEvidence) ? result.audioEvidence.length : 0;
  const evidenceSummary = frameCount || audioCount
    ? `已采样 ${frameCount} 个关键帧${audioCount ? `，分析 ${audioCount} 个声音片段` : ''}。`
    : '';
  const isVisionMode = result.mode === 'vision_frames' || result.mode === 'vision_frames_sound';
  const reason = String(result.degradedReason || '').trim();
  const reasonText = isVisionMode && reason.startsWith('已完成本机')
    ? reason
    : `${isVisionMode ? '已完成本机关键帧视觉/声音场景分析；' : '当前是基础识别：'}${reason || '未启用音频转写或画面理解'}`;
  const analysisNote = result.degraded
    ? `<p class="analysis-warning">${escapeHtml(reasonText)} ${escapeHtml(evidenceSummary)}请先核对候选内容，再点击“采用”。</p>`
    : '';
  const limitationNote = limitations.length
    ? `<details class="analysis-limitations"><summary>识别范围与限制</summary><ul>${limitations.map(item => `<li>${escapeHtml(item)}</li>`).join('')}</ul></details>`
    : '';
  return `<section class="analysis-panel">
    <div class="analysis-head"><strong>识别候选</strong><span>识别方式：${escapeHtml(modeLabel)} · 仅供预览，点击“采用”后才会写入队列</span></div>
    ${analysisNote}
    ${options.length ? `<div class="analysis-options">${options.join('')}</div>` : '<p class="analysis-empty">暂未生成可用候选，请点击“重新识别”。</p>'}
    ${evidence}
    ${limitationNote}
  </section>`;
}

async function analyzeItem(id, card) {
  const button = card.querySelector('.analyze-item');
  const panel = card.querySelector('.analysis-panel');
  button.disabled = true;
  button.textContent = '识别中…';
  panel.hidden = false;
  panel.innerHTML = '<p class="analysis-loading">正在分析视频信息，请稍候；不会自动修改文案或发布状态。</p>';
  try {
    const payload = await request('/api/items/' + encodeURIComponent(id) + '/analyze', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({}),
    });
    state.analysis = state.analysis || {};
    state.analysis[id] = normalizeAnalysis(payload);
    requestQueueSectionOpen(id, 'analysis');
    render();
  } catch (err) {
    panel.innerHTML = `<p class="analysis-error">识别失败：${escapeHtml(err.message)}。可以点击“重新识别”再试。</p>`;
    button.disabled = false;
    button.textContent = '重新识别';
  }
}

async function adoptAnalysis(id, candidate, button) {
  if (!candidate.caption && !candidate.hashtags) return;
  if (hasCaptionDraft(id)) {
    alert('这条视频还有未保存的手动文案或话题，请先等待自动保存完成，再采用识别结果。');
    return;
  }
  const original = button.textContent;
  button.disabled = true;
  button.textContent = '采用中…';
  try {
    // 只有用户明确点击“采用”才写回文案/话题；不触碰 status 等发布字段。
    cancelEditorSave(id, 'caption');
    cancelEditorSave(id, 'hashtags');
    clearEditorDraft(id, 'caption');
    clearEditorDraft(id, 'hashtags');
    await patch(id, {caption: candidate.caption, hashtags: candidate.hashtags});
    state.analysis = state.analysis || {};
    if (state.analysis[id]) state.analysis[id].adopted = candidateKey(candidate);
    requestQueueSectionOpen(id, 'editor');
    render();
  } catch (err) {
    button.disabled = false;
    button.textContent = original;
    alert(`采用识别结果失败：${err.message}`);
  }
}

function renderAnalysisBatchStatus() {
  const statusBox = $('#analysisBatchStatus');
  const startButton = $('#batchAnalyzeBtn');
  const cancelButton = $('#cancelBatchAnalyzeBtn');
  const adoptButton = $('#batchAdoptBtn');
  if (!statusBox || !startButton || !cancelButton) return;
  const batch = state.analysis_batch || {};
  // 队列被清空或尚未加载时，不展示上一批视频遗留的校验结果；
  // 如果后台仍在校验，则保留停止按钮，避免失去控制入口。
  if (!(state.items || []).length && !analysisBatchRunning(batch)) {
    statusBox.hidden = true;
    if (adoptButton) adoptButton.hidden = true;
    startButton.disabled = true;
    startButton.textContent = '识别视频内容';
    cancelButton.hidden = true;
    return;
  }
  const flags = analysisBatchFlags(batch);
  const hasMessage = Boolean(flags.message && flags.status !== 'idle');
  statusBox.hidden = !flags.running && !hasMessage;
  statusBox.className = 'analysis-batch-status';
  if (flags.running) statusBox.classList.add('running');
  else if (flags.status === 'completed_with_errors' || flags.status === 'failed') statusBox.classList.add('warning');
  else if (flags.status === 'completed') statusBox.classList.add('complete');
  const progress = flags.total > 0 ? `（${flags.completed}/${flags.total}）` : '';
  const current = batch.current ? `<span class="analysis-batch-current">当前：${escapeHtml(batch.current)}</span>` : '';
  const errors = Array.isArray(batch.errors) ? batch.errors : [];
  const errorDetails = errors.length
    ? `<details class="analysis-batch-errors"><summary>${errors.length} 条视频识别失败，可单独重试</summary><ul>${errors.slice(0, 8).map(error => `<li>${escapeHtml(error.name || error.id || '视频')}：${escapeHtml(error.error || '未知错误')}</li>`).join('')}${errors.length > 8 ? `<li>其余 ${errors.length - 8} 条请查看本地状态或逐条重试。</li>` : ''}</ul></details>`
    : '';
  const applyTargets = batchAnalysisApplyTargets();
  const applyMessage = analysisApplyMessage ? ` ${escapeHtml(analysisApplyMessage)}` : '';
  const completedResultCount = Object.values(batch.results && typeof batch.results === 'object' ? batch.results : {})
    .filter(result => result && result.status === 'completed' && result.analysis?.recommended).length;
  const protectedCount = Math.max(0, completedResultCount - applyTargets.length);
  const protectedMessage = !flags.running && protectedCount
    ? ` ${protectedCount} 条已有文案，批量匹配不会覆盖；请展开对应视频后逐条核对并采用。`
    : '';
  statusBox.innerHTML = `<strong>${flags.running ? (flags.cancelling ? '正在停止视频校验' : '视频校验中') : '视频校验状态'}</strong>${escapeHtml(flags.message)}${progress}${current}${protectedMessage}${applyMessage}${errorDetails}`;
  startButton.disabled = flags.running || queueActionsLocked() || schedulePlanBusy || !analysisBatchTargets().length;
  startButton.textContent = flags.running ? `校验中（${flags.completed}/${flags.total}）` : '识别视频内容';
  startButton.title = flags.running
    ? '正在逐条分析视频，请等待完成或点击停止识别'
    : (selectedQueueIds.size ? '分析已勾选且未跳过的视频；结果只读展示，需人工采用' : '一次分析队列中的所有非跳过视频，结果只读展示，需人工采用');
  cancelButton.hidden = !flags.running;
  cancelButton.disabled = flags.cancelling;
  cancelButton.textContent = flags.cancelling ? '正在停止…' : '停止校验';
  if (adoptButton) {
    const hasCompletedResults = completedResultCount > 0;
    const revealOnly = hasCompletedResults && applyTargets.length === 0;
    adoptButton.hidden = flags.running || !hasCompletedResults;
    adoptButton.disabled = queueUiLocked();
    adoptButton.dataset.mode = revealOnly ? 'reveal' : 'adopt';
    adoptButton.textContent = revealOnly
      ? `查看推荐文案（${completedResultCount}）`
      : `匹配推荐内容（${applyTargets.length}）`;
    adoptButton.title = revealOnly
      ? '已有手工文案，批量匹配不会覆盖；点击后展开每条候选，再逐条采用'
      : (selectedQueueIds.size
        ? '只处理已勾选且仍为默认文案的视频；手动编辑过的条目会跳过'
        : '处理仍为默认文案且已完成识别的视频；手动编辑过的条目会跳过');
  }
}

/* 动线感知：根据真实队列状态点亮顶部 5 步导航，并高亮队列工具区对应的当前阶段。 */
const WORKFLOW_STEP_STAGE = { 1: 'type', 2: 'import', 3: 'verify', 4: 'match' };

function renderWorkflowSteps(items, flags = {}) {
  const nav = document.getElementById('workflowSteps');
  if (!nav) return;
  const resumableItems = items.filter(isResumable);
  const copyReadyCount = resumableItems.filter(item => queueCopyState(item).tone === 'ready').length;
  const ready = workflowProfileReady();
  // 本轮批次已无待处理视频时，校验/匹配步骤视为完成，避免全部提交后步骤条停在中间态。
  const batchFinished = items.length > 0 && resumableItems.length === 0;
  const done = {
    1: ready,
    2: items.length > 0,
    // 内容识别入口已移出主流程；队列存在即完成本地视频校验阶段，
    // 真正的发布环境检查仍由“检查发布环境”按钮负责。
    3: batchFinished || items.length > 0,
    4: batchFinished || (resumableItems.length > 0 && copyReadyCount === resumableItems.length),
    5: batchFinished,
  };
  const stepOrder = [1, 2, 3, 4, 5];
  const publishing = Boolean(flags.running && !flags.paused);
  // 全部完成时不点亮任何“当前步”，整排绿色对勾即收尾状态。
  let activeStep = stepOrder.find(step => !done[step]) || 0;
  if (publishing) activeStep = 5;
  const hints = {
    1: ready ? '已完成' : '普通或全景方式',
    2: items.length ? `队列 ${items.length} 个` : '加入发布队列',
    3: resumableItems.length ? '待检查发布环境' : (batchFinished ? '已完成' : '内容与发布环境'),
    4: resumableItems.length ? `已就绪 ${copyReadyCount}/${resumableItems.length}` : (batchFinished ? '已完成' : '文案、话题和设置'),
    5: publishing ? '正在提交…' : (done[5] ? '本轮已全部提交' : '打开上传页后提交'),
  };
  nav.querySelectorAll('.workflow-step').forEach(el => {
    const step = Number(el.dataset.step || 0);
    el.classList.toggle('done', Boolean(done[step]));
    el.classList.toggle('active', step === activeStep);
    const small = el.querySelector('small');
    if (small && hints[step]) small.textContent = hints[step];
  });
  document.querySelectorAll('.queue-stage').forEach(el => {
    el.classList.toggle('is-current', el.dataset.stage === WORKFLOW_STEP_STAGE[activeStep]);
  });
}

function queueCardHtml(item, items) {
  const id = queueItemId(item);
  const copyState = queueCopyState(item);
  const caption = String(editorFieldValue(item, 'caption') || '').trim();
  const hashtags = String(editorFieldValue(item, 'hashtags') || '').trim();
  const coverName = String(item.cover_image_name || '').trim();
  const coverLandscapeName = String(item.cover_image_landscape_name || '').trim();
  const coverPortraitName = String(item.cover_image_portrait_name || '').trim();
  const hasCustomCover = Boolean(coverName || coverLandscapeName || coverPortraitName);
  const editorKey = queueSectionKey(id, 'editor');
  const editorOpen = initializedQueueSections.has(editorKey) ? queueSectionOpen(id, 'editor') : true;
  initializedQueueSections.add(editorKey);
  const itemIndex = items.indexOf(item) + 1;
  const queueTone = `queue-tone-${Math.max(0, (itemIndex - 1) % 10)}`;
  const captionSummary = compactText(caption, 118) || '尚未填写，点击“编辑文案与发布设置”或导入文案';
  const hashtagSummary = compactText(formatHashtags(hashtags), 54);
  const copyClass = `copy-state ${copyState.tone}`;
  const statusClass = `${item.status} ${queueTone} ${scheduleModeOf(item) === 'scheduled' ? 'item-scheduled' : ''} ${videoTypeOf(item) === 'panorama' ? 'item-panorama' : ''}`;
  return `<article class="item ${statusClass}" data-id="${escapeHtml(id)}">
    <div class="item-top">
      <label class="item-select"><input class="item-checkbox" type="checkbox" ${selectedQueueIds.has(id) ? 'checked' : ''} aria-label="选择 ${escapeHtml(item.name)}"></label>
      <div class="num">${itemIndex}</div>
      <div class="item-heading">
        <div class="item-title-line"><div class="name" title="${escapeHtml(item.path || item.name)}">${escapeHtml(item.name)}</div>${videoTypeOf(item) === 'panorama' ? '<span class="mode-badge">全景</span>' : ''}${platformBadgesHtml(item)}<span class="badge ${item.status} ${scheduleModeOf(item) === 'scheduled' ? 'scheduled' : ''}">${escapeHtml(itemDisplayLabel(item))}</span></div>
        <div class="path" title="${escapeHtml(item.path || '')}">${escapeHtml(item.path || '')}</div>
      </div>
    </div>
    <div class="item-summary">
      <div class="summary-main"><span class="summary-label">作品描述</span><span class="summary-copy">${escapeHtml(captionSummary)}</span></div>
      <div class="summary-meta-row"><span class="${copyClass}">${escapeHtml(copyState.label)}</span><span class="summary-meta" title="${escapeHtml(hashtagSummary)}">${escapeHtml(hashtagSummary)}</span><span class="summary-meta">${escapeHtml(queueScheduleSummary(item))}</span></div>
    </div>
    <div class="item-actions item-actions-primary">
      <button class="ghost import-caption" type="button">导入文案</button>
      <button class="ghost open-upload" type="button">${videoTypeOf(item) === 'panorama' ? '打开全景上传页' : '打开上传页'}</button>
      <button class="ghost channels-upload" type="button" ${videoTypeOf(item) === 'panorama' ? 'disabled title="视频号暂不支持全景视频"' : ''}>${videoTypeOf(item) === 'panorama' ? '视频号不支持全景' : '发布视频号'}</button>
      <button class="secondary open-video" type="button">打开视频</button>
      <button class="secondary copy-caption" type="button">复制文案</button>
      <button class="danger skip-item" type="button">跳过</button>
      <button class="danger delete-item" type="button">删除</button>
      <button class="publish" type="button">${item.status === 'published' ? '重新提交（可能重复）' : '标记为已提交'}</button>
    </div>
    <details class="item-editor-details" data-section="editor" ${editorOpen ? 'open' : ''}>
      <summary><span>编辑文案与发布设置</span><span class="details-hint">${escapeHtml(copyState.label)} · ${escapeHtml(hashtagSummary)}</span></summary>
      <div class="item-editor-body">
        <label>作品标题（30字以内，可选，填写到抖音标题栏）<input class="work-title" data-editor-field="work_title" value="${escapeHtml(editorFieldValue(item, 'work_title'))}" placeholder="例如：演唱会现场精选" maxlength="30"></label>
        <textarea class="caption" data-editor-field="caption" aria-label="${escapeHtml(item.name)} 的发布文案" title="可直接修改；输入停顿后自动保存，也可以使用导入文案" placeholder="输入或导入作品描述">${escapeHtml(editorFieldValue(item, 'caption'))}</textarea>
        <div class="field-grid">${panoramaFieldsHtml(item)}<label>话题（逗号分隔）<input class="hashtags" data-editor-field="hashtags" value="${escapeHtml(editorFieldValue(item, 'hashtags'))}" title="可直接修改；输入停顿后自动保存" placeholder="演唱会,现场"></label><label>封面时间（秒）<input class="cover-tsp" data-editor-field="cover_tsp" type="text" value="${escapeHtml(editorFieldValue(item, 'cover_tsp'))}" placeholder="留空自动选中段，如 18 或 00:18"></label><label>可见范围<select class="visibility"><option ${item.visibility === '全部可见' ? 'selected' : ''}>全部可见</option><option ${item.visibility === '仅自己可见' ? 'selected' : ''}>仅自己可见</option><option ${item.visibility === '好友可见' ? 'selected' : ''}>好友可见</option></select></label><label class="check-label"><input class="allow-download" type="checkbox" ${item.allow_download !== false ? 'checked' : ''}> 允许下载</label></div>
        <div class="cover-image-row"><span class="cover-image-label">自定义封面图片</span><span class="cover-image-value ${hasCustomCover ? 'has-cover' : ''}" title="${escapeHtml([coverLandscapeName, coverPortraitName, coverName].filter(Boolean).join('；'))}">${hasCustomCover ? `横版：${escapeHtml(coverLandscapeName || '未设置')}　竖版：${escapeHtml(coverPortraitName || '未设置')}${coverName ? `　未分类：${escapeHtml(coverName)}` : ''}` : '未设置；可在上方按文件名批量导入'}</span>${hasCustomCover ? '<button class="ghost clear-cover" type="button">清除封面</button>' : ''}</div>
        <div class="schedule-fields"><label>发布方式<select class="schedule-mode"><option value="immediate" ${scheduleModeOf(item) === 'immediate' ? 'selected' : ''}>立即发布</option><option value="scheduled" ${scheduleModeOf(item) === 'scheduled' ? 'selected' : ''}>定时发布</option></select></label><label>定时发布时间<input class="schedule-at" data-editor-field="schedule_at" type="datetime-local" step="60" value="${escapeHtml(editorFieldValue(item, 'schedule_at'))}" ${scheduleModeOf(item) === 'immediate' ? 'disabled' : ''}></label><label>与下一条定时视频间隔（分钟）<select class="schedule-interval" ${scheduleModeOf(item) === 'immediate' ? 'disabled' : ''}>${scheduleIntervalOptions(scheduleIntervalOf(item))}</select><input class="schedule-interval-custom" data-editor-field="schedule_interval_custom" type="number" min="1" max="43200" step="1" value="${escapeHtml(SCHEDULE_INTERVAL_PRESETS.includes(scheduleIntervalOf(item)) ? '' : editorFieldValue(item, 'schedule_interval_custom') || scheduleIntervalOf(item))}" placeholder="自定义分钟" ${scheduleModeOf(item) === 'immediate' || SCHEDULE_INTERVAL_PRESETS.includes(scheduleIntervalOf(item)) || !scheduleIntervalOf(item) ? 'hidden' : ''} ${scheduleModeOf(item) === 'immediate' ? 'disabled' : ''}></label><p class="schedule-field-help">定时模式会在抖音后台预约；最后一条可不设置间隔。修改间隔时会提示并同步后续可编辑定时视频；也可以直接改具体时间。</p></div>
        <input class="note" data-editor-field="note" placeholder="备注（可选，例如：已检查封面）" value="${escapeHtml(editorFieldValue(item, 'note'))}">
      </div>
    </details>
  </article>`;
}

function render(options = {}) {
  if (!options.force && shouldDeferRender()) {
    deferredRenderPending = true;
    return;
  }
  deferredRenderPending = false;
  captureQueueSectionState();
  // DOM 状态优先用于保留用户手动的折叠/展开；程序明确请求展开的区块
  // 在本轮重绘中覆盖旧 DOM 状态。
  for (const key of pendingOpenQueueSections) openQueueSections.add(key);
  pendingOpenQueueSections.clear();
  const activeEditor = captureActiveEditor();
  const items = state.items || [];
  inferWorkflowProfileFromQueue(items);
  pruneSelectedQueueIds(items);
  pruneEditorDrafts(items);
  pruneQueueSectionState(items);
  ensureSchedulePlanDefaults(items);
  const filter = $('#filter').value;
  const visible = visibleQueueItems(items);
  const counts = {
    pending:items.filter(x=>x.status==='pending' || x.status==='publishing').length,
    published:items.filter(x=>x.status==='published').length,
    failed:items.filter(x=>x.status==='failed').length,
    scheduled:items.filter(x=>x.status !== 'skipped' && scheduleModeOf(x)==='scheduled').length
  };
  $('#stats').innerHTML = [['待发布', counts.pending], ['已提交', counts.published], ['失败待处理', counts.failed], ['已配置定时', counts.scheduled], ['总视频', items.length]].map(x => `<div class="stat"><b>${x[1]}</b><span>${x[0]}</span></div>`).join('');

  const pub = state.publish || {};
  const flags = publishFlags(pub);
  renderWorkflowSteps(items, flags);
  renderAutoPauseNotice(pub, flags);
  const hasScheduledPlan = items.some(item => item.status !== 'skipped' && scheduleModeOf(item) === 'scheduled');
  const statusBox = $('#publishStatus');
  statusBox.hidden = !flags.running && !flags.pauseRequested && !flags.paused && !flags.terminateRequested && !flags.terminated && !flags.autoPaused && !flags.actionRequired && !flags.message;
  let statusClass = 'publish-status';
  if (flags.terminateRequested) statusClass += ' stopping';
  else if (flags.terminated) statusClass += ' stopped';
  else if (flags.paused) statusClass += ' paused';
  else if (flags.autoPaused) statusClass += ' attention';
  else if (flags.running && flags.pauseRequested) statusClass += ' pausing';
  else if (flags.running) statusClass += ' running';
  else if (flags.actionRequired) statusClass += ' attention';
  else if (flags.done) statusClass += ' complete';
  statusBox.className = statusClass;
  const progress = flags.total > 0 ? `（${flags.completed}/${flags.total}）` : '';
  const current = pub.current ? `<span class="publish-current">当前：${escapeHtml(pub.current)}</span>` : '';
  let statusTitle = '自动发布已停止';
  if (flags.terminateRequested) statusTitle = '正在终止自动发布';
  else if (flags.terminated) statusTitle = '自动发布已终止';
  else if (flags.paused) statusTitle = '自动发布已暂停';
  else if (flags.autoPaused) statusTitle = '自动发布已自动暂停';
  else if (flags.running && flags.pauseRequested) statusTitle = '正在暂停自动发布';
  else if (flags.running) statusTitle = flags.actionRequired ? '需要人工处理' : '自动发布中';
  else if (flags.actionRequired) statusTitle = '需要人工处理';
  else if (flags.done) statusTitle = '自动发布已提交';
  const guidance = flags.terminateRequested
    ? '<span class="status-guidance">正在停止后续自动操作，浏览器页面会保持打开；完成后可以清空本地队列。</span>'
    : flags.terminated
    ? '<span class="status-guidance">后续自动操作已停止，浏览器页面保持打开；当前条目未标记为已提交。现在可以清空队列，或点击“重新开始发布”重新上传。</span>'
    : flags.autoPaused
    ? `<span class="status-guidance">${escapeHtml(flags.pauseReason || flags.message || '系统因异常暂停')}；浏览器页面保持打开，处理完成后点击“继续发布”。</span>`
    : flags.actionRequired
    ? `<span class="status-guidance">请在自动打开的浏览器中完成登录、验证码或封面操作，${flags.running ? '页面会继续轮询，请保持浏览器打开。' : '完成后点击“继续发布”。'}</span>`
    : flags.paused
      ? '<span class="status-guidance">浏览器页面保持打开，点击“继续发布”即可恢复。</span>'
      : '';
  statusBox.innerHTML = `<strong>${statusTitle}</strong>${escapeHtml(flags.message || '')}${progress}${current}${guidance}`;

  const resumable = items.some(isResumable);
  const panoramaNeedsFormat = items.some(item => isResumable(item) && videoTypeOf(item) === 'panorama' && !panoramaFormatOf(item));
  const queueLocked = queueUiLocked(pub);
  const mediaPickerBtn = $('#mediaPickerBtn');
  const folderBtn = $('#folderBtn');
  const filesBtn = $('#filesBtn');
  const clearQueueBtn = $('#clearQueueBtn');
  const importCopyBtn = $('#importCopyBtn');
  const coverImportBtn = $('#coverImportBtn');
  const uploadBtn = $('#uploadBtn');
  if (uploadBtn) {
    const uploadType = preferredUploadType();
    uploadBtn.textContent = uploadType === 'panorama' ? '打开全景上传页' : '打开普通上传页';
    uploadBtn.title = uploadType === 'panorama'
      ? '按当前待发布视频打开抖音全景上传页'
      : '按当前待发布视频打开抖音普通上传页';
  }
  if (queueLocked) closeMediaPicker();
  clearQueueBtn.disabled = queueLocked || !items.length;
  if (importCopyBtn) {
    importCopyBtn.disabled = queueLocked || captionImportBusy || !items.length;
    importCopyBtn.textContent = captionImportBusy ? '导入中…' : '导入文案';
    importCopyBtn.title = queueLocked
      ? '自动发布正在运行、暂停或终止处理中，暂不能导入文案'
      : (items.length ? '支持 txt / md / JSON / CSV / Excel；Excel 按文件名一一对应' : '当前队列为空');
  }
  const generateCopyBtn = $('#generateCopyBtn');
  const copyOverwriteInput = $('#copyOverwriteInput');
  if (generateCopyBtn) {
    generateCopyBtn.disabled = queueLocked || copyGenerateBusy || !items.length;
    generateCopyBtn.textContent = copyGenerateBusy ? '生成中…' : '一键生成文案';
    generateCopyBtn.title = queueLocked
      ? '自动发布正在运行、暂停或终止处理中，暂不能生成文案'
      : '按文件名歌名匹配本地文案库；勾选视频时只处理勾选项，手写文案默认保留';
  }
  if (copyOverwriteInput) {
    copyOverwriteInput.disabled = queueLocked || copyGenerateBusy || !items.length;
  }
  if (coverImportBtn) {
    coverImportBtn.disabled = queueLocked || !items.length;
    coverImportBtn.title = queueLocked
      ? '自动发布正在运行、暂停或终止处理中，暂不能导入封面'
      : '选择一张或多张图片，按图片文件名匹配队列视频文件名';
  }
  const queueLockTitle = queueLocked
    ? (flags.terminateRequested
      ? '当前任务正在终止；请等待浏览器页面保留完成后再修改或清空队列'
      : flags.paused
      ? '当前发布任务已暂停；请先继续并完成任务，才能修改或清空队列'
      : '自动发布正在运行，请先完成当前任务')
    : '';
  updateWorkflowTypeUi(items, queueLocked);
  const workflowReady = workflowProfileReady();
  const workflowTitle = !workflowReady ? '请先在第 1 步选择全景视频格式' : '';
  mediaPickerBtn.title = queueLockTitle || workflowTitle || mediaPickerBtn.title || '选择视频文件夹，或添加一个/多个视频文件';
  folderBtn.title = queueLockTitle || workflowTitle || '选择包含视频的本地文件夹';
  filesBtn.title = queueLockTitle || workflowTitle || '从本机选择一个或多个视频文件加入队列';
  clearQueueBtn.title = queueLockTitle || (items.length ? '清除队列记录，不删除源视频文件' : '当前队列为空');
  updateQueueSelectionUi(visible, queueLocked);
  updateBulkVideoTypeUi(queueLocked);
  updateSchedulePlanUi(items, pub);
  const autoPublishBtn = $('#autoPublishBtn');
  // 开始、暂停、继续统一为一个状态按钮：
  //   空闲 -> 开始发布；已终止 -> 重新开始发布（重新上传未完成条目）
  //   运行中 -> 暂停发布
  //   已暂停 -> 继续发布
  //   请求暂停/终止 -> 显示进行中状态并暂时锁定
  let publishButtonText = '开始发布';
  let publishButtonTitle = resumable ? '开始处理待发布视频' : '当前没有待发布视频';
  if (flags.terminateRequested) {
    publishButtonText = '正在终止…';
    publishButtonTitle = '正在停止当前任务，请保持浏览器页面打开';
  } else if (flags.running && flags.paused) {
    publishButtonText = '继续发布';
    publishButtonTitle = '从当前进度继续发布';
  } else if (flags.pauseRequested) {
    publishButtonText = '正在暂停…';
    publishButtonTitle = '正在等待当前步骤到达安全暂停点';
  } else if (flags.running) {
    publishButtonText = '暂停发布';
    publishButtonTitle = '在当前视频步骤完成后暂停，不会关闭浏览器';
  } else if (flags.terminated) {
    publishButtonText = '重新开始发布';
    publishButtonTitle = resumable ? '放弃上次终止的草稿，重新上传未完成视频' : '当前没有待发布视频';
  } else if (flags.autoPaused || flags.actionRequired || flags.paused) {
    publishButtonText = '继续发布';
    publishButtonTitle = '按当前进度继续处理待发布视频';
  }
  autoPublishBtn.textContent = publishButtonText;
  autoPublishBtn.title = publishButtonTitle;
  const toggleUnavailable = !resumable && !flags.running && !flags.paused && !flags.autoPaused && !flags.actionRequired && !flags.terminated;
  autoPublishBtn.disabled = flags.terminateRequested || (flags.pauseRequested && !flags.paused) || toggleUnavailable;
  const channelsPublishBtn = $('#channelsPublishBtn');
  const channelState = state.channels || {};
  const selectedChannelItems = selectedQueueIds.size
    ? items.filter(item => selectedQueueIds.has(queueItemId(item)))
    : items.filter(item => item.status !== 'skipped');
  const channelTargets = selectedChannelItems.filter(item => videoTypeOf(item) === 'normal' && ['pending', 'failed', 'publishing'].includes(String(item?.platforms?.channels?.status || 'pending')));
  if (channelsPublishBtn) {
    const progress = channelState.total ? `（${channelState.completed || 0}/${channelState.total}）` : '';
    channelsPublishBtn.textContent = channelState.running ? `视频号发布中${progress}` : '发布视频号';
    channelsPublishBtn.title = channelState.running
      ? '视频号发布任务正在运行，请保持浏览器页面打开'
      : (channelTargets.length ? `使用当前抖音队列发布 ${channelTargets.length} 条普通视频到视频号` : '当前没有可发布到视频号的普通视频');
    channelsPublishBtn.disabled = queueLocked || channelState.running || channelState.paused || !channelTargets.length;
  }
  const channelsPauseBtn = $('#channelsPauseBtn');
  const channelsStopBtn = $('#channelsStopBtn');
  const channelsActive = Boolean(channelState.running);
  const channelsBusy = channelsActive || Boolean(channelState.paused);
  if (channelsPauseBtn) {
    channelsPauseBtn.hidden = !channelsActive;
    channelsPauseBtn.disabled = Boolean(channelState.stop_requested || channelState.terminated);
    channelsPauseBtn.textContent = channelState.paused
      ? '继续视频号'
      : (channelState.pause_requested ? '正在暂停…' : '暂停视频号');
    channelsPauseBtn.title = channelState.paused
      ? '继续处理视频号待发布条目'
      : '当前视频完成后暂停，浏览器页面保持打开';
  }
  if (channelsStopBtn) {
    channelsStopBtn.hidden = !channelsBusy;
    channelsStopBtn.disabled = Boolean(channelState.stop_requested);
    channelsStopBtn.textContent = channelState.stop_requested ? '正在终止…' : '终止视频号';
    channelsStopBtn.title = '停止视频号后续发布；当前条目标记为失败，浏览器页面关闭';
  }
  const stopBtn = $('#stopPublishBtn');
  const stopAllowed = publishCanTerminate(pub, flags);
  const stopVisible = stopAllowed || flags.terminateRequested;
  stopBtn.hidden = !stopVisible;
  stopBtn.disabled = flags.terminateRequested || !stopAllowed;
  stopBtn.textContent = flags.terminateRequested ? '正在终止…' : '终止当前任务';
  stopBtn.title = flags.terminateRequested
    ? '正在停止后续自动操作，请保持浏览器页面打开'
    : '停止后续自动操作；保留浏览器页面和本地队列，不删除视频文件';

  const publishHint = $('#publishHint');
  if (flags.terminateRequested) {
    publishHint.textContent = '正在终止当前任务；浏览器页面会保持打开，完成后可以清空本地队列。';
  } else if (flags.terminated) {
    publishHint.textContent = '自动发布已终止；浏览器页面保持打开，当前条目未标记为已提交。现在可以清空队列，或点击“重新开始发布”重新上传。';
  } else if (flags.paused) {
    publishHint.textContent = `自动发布已暂停；点击“继续发布”从当前进度恢复。选择、扫描和清空队列按钮会暂时锁定。${hasScheduledPlan ? '已写入抖音的预约时间不会自动顺延，继续前请检查预约时间。' : ''}`;
  } else if (flags.running && flags.pauseRequested) {
    publishHint.textContent = '已请求暂停，正在等待当前步骤完成；浏览器不会关闭。暂停完成后可点击“继续发布”。';
  } else if (flags.running && flags.actionRequired) {
    publishHint.textContent = '请在浏览器中完成必要操作，页面会保持打开。';
  } else if (flags.running) {
    publishHint.textContent = '自动发布正在运行，请等待当前视频上传完成。需要时可点击“暂停发布”。';
  } else if (flags.autoPaused || flags.actionRequired) {
    publishHint.textContent = '需要人工处理；完成浏览器操作后点击“继续发布”。';
  } else if (flags.done) {
    publishHint.textContent = '队列中的视频已提交，平台审核状态请在抖音内容管理中确认。';
  } else if (!items.length) {
    publishHint.textContent = workflowProfileReady()
      ? '已选发布方式，请点击“选择视频”添加文件夹或视频文件。'
      : '请先在第 1 步选择全景视频格式，再选择视频。';
  } else if (panoramaNeedsFormat && !flags.running && !flags.paused && !flags.autoPaused && !flags.actionRequired && !flags.terminated) {
    publishHint.textContent = '队列中有全景视频尚未选择格式；请在卡片或批量设置中选择 360/180 的单目或双目格式。';
  } else if (!resumable) {
    publishHint.textContent = '当前没有待发布视频；请选择新文件夹，或在具体条目上点击“重新提交（可能重复）”。';
  } else {
    publishHint.textContent = `有 ${items.filter(isResumable).length} 条视频待处理。`;
  }
  $('#rootHint').textContent = state.root ? `当前文件夹：${state.root}` : '等待选择视频文件夹';
  $('#empty').hidden = visible.length !== 0;
  if (!visible.length) {
    $('#empty').textContent = !items.length
      ? '还没有发现视频。先在第 1 步选择普通/全景发布方式，再选择视频文件夹或一个/多个视频文件。'
      : filter !== 'all'
        ? '当前筛选没有匹配的视频。可以切换筛选条件查看其他队列状态。'
        : '当前队列没有可显示的视频。请重新选择视频文件夹或添加视频。';
  }
  $('#queue').innerHTML = visible.map(item => queueCardHtml(item, items)).join('');
  document.querySelectorAll('.item').forEach(card => {
    const id = card.dataset.id, item = items.find(x => queueItemId(x) === id);
    bindQueueSectionState(card, id);
    const channelsButton = card.querySelector('.channels-upload');
    channelsButton?.addEventListener('click', async () => {
      if (queueUiLocked() || videoTypeOf(item) === 'panorama') return;
      channelsButton.disabled = true;
      channelsButton.textContent = '正在打开…';
      try {
        await request('/api/channels-publish', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({item_id:id})});
        channelsButton.textContent = '视频号发布中';
        await load();
      } catch (err) {
        channelsButton.disabled = false;
        channelsButton.textContent = '发布视频号';
        alert(`视频号发布启动失败：${err.message}`);
      }
    });
    const itemCheckbox = card.querySelector('.item-checkbox');
    itemCheckbox.disabled = queueLocked;
    itemCheckbox.onchange = (event) => {
      if (event.target.checked) selectedQueueIds.add(id);
      else selectedQueueIds.delete(id);
      updateQueueSelectionUi(visible, queueUiLocked());
      updateBulkVideoTypeUi(queueUiLocked());
    };
    const editableControls = card.querySelectorAll('.caption, .work-title, .hashtags, .cover-tsp, .visibility, .allow-download, .note, .video-type, .panorama-format, .official-topic-keywords, .official-topic-auto, .pico-enabled, .schedule-mode, .schedule-at, .schedule-interval, .schedule-interval-custom');
    editableControls.forEach(control => {
      control.disabled = queueLocked;
      const editorTitle = control.dataset.editorField === 'caption'
        ? '可直接修改；输入停顿后自动保存，也可以使用导入文案'
        : control.dataset.editorField === 'hashtags'
          ? '可直接修改；输入停顿后自动保存'
          : control.dataset.editorField === 'official_topic_keywords'
            ? '留空时按当前文案和话题自动匹配抖音官方活动/话题'
          : '';
      control.title = queueLocked ? '自动发布进行中、暂停或终止处理中，暂不能修改队列' : editorTitle;
    });
    card.querySelectorAll('[data-editor-field]').forEach(control => {
      control.addEventListener('input', event => {
        const field = event.target.dataset.editorField;
        rememberEditorDraft(id, field, event.target.value);
        if (field === 'caption' || field === 'hashtags') scheduleEditorSave(id, field, event.target.value);
      });
    });
    card.querySelector('.caption').addEventListener('change', e => saveEditorField(id, 'caption', e.target.value));
    card.querySelector('.hashtags').addEventListener('change', e => saveEditorField(id, 'hashtags', e.target.value));
    const workTitleControl = card.querySelector('.work-title');
    workTitleControl?.addEventListener('change', e => saveEditorField(id, 'work_title', e.target.value));
    card.querySelector('.cover-tsp').addEventListener('change', e => saveEditorField(id, 'cover_tsp', e.target.value));
    card.querySelector('.clear-cover')?.addEventListener('click', async () => {
      if (queueUiLocked()) return;
      try {
        await patch(id, {
          cover_image: '', cover_image_name: '',
          cover_image_landscape: '', cover_image_landscape_name: '',
          cover_image_portrait: '', cover_image_portrait_name: '',
        });
      } catch (err) {
        alert(`清除自定义封面失败：${err.message}`);
      }
    });
    card.querySelector('.visibility').addEventListener('change', e => patch(id, {visibility:e.target.value}));
    card.querySelector('.allow-download').addEventListener('change', e => patch(id, {allow_download:e.target.checked}));
    card.querySelector('.note').addEventListener('change', e => saveEditorField(id, 'note', e.target.value));
    const officialTopicKeywordsControl = card.querySelector('.official-topic-keywords');
    officialTopicKeywordsControl?.addEventListener('input', event => {
      rememberEditorDraft(id, 'official_topic_keywords', event.target.value);
      scheduleEditorSave(id, 'official_topic_keywords', event.target.value);
    });
    officialTopicKeywordsControl?.addEventListener('change', event => saveEditorField(id, 'official_topic_keywords', event.target.value));
    const officialTopicAutoControl = card.querySelector('.official-topic-auto');
    officialTopicAutoControl?.addEventListener('change', event => patch(id, {official_topic_auto: event.target.checked}));
    const picoControl = card.querySelector('.pico-enabled');
    picoControl?.addEventListener('change', event => patch(id, {pico_enabled: event.target.checked}));
    const videoTypeControl = card.querySelector('.video-type');
    const panoramaFormatControl = card.querySelector('.panorama-format');
    const panoramaFormatField = card.querySelector('.panorama-format-field');
    const panoramaTopicField = card.querySelector('.panorama-topic-field');
    const panoramaOfficialAutoField = card.querySelector('.panorama-official-auto');
    const panoramaPicoField = card.querySelector('.panorama-pico-field');
    const refreshPanoramaControls = () => {
      const panorama = videoTypeControl.value === 'panorama';
      panoramaFormatField.hidden = !panorama;
      panoramaFormatControl.disabled = queueLocked || !panorama;
      if (!panorama) panoramaFormatControl.value = '';
      if (panoramaTopicField) panoramaTopicField.hidden = !panorama;
      if (panoramaOfficialAutoField) panoramaOfficialAutoField.hidden = !panorama;
      if (panoramaPicoField) panoramaPicoField.hidden = !panorama;
      if (officialTopicKeywordsControl) officialTopicKeywordsControl.disabled = queueLocked || !panorama;
      if (officialTopicAutoControl) officialTopicAutoControl.disabled = queueLocked || !panorama;
      if (picoControl) picoControl.disabled = queueLocked || !panorama;
    };
    refreshPanoramaControls();
    videoTypeControl.onchange = async () => {
      if (videoTypeControl.value === 'panorama') {
        refreshPanoramaControls();
        if (!panoramaFormatControl.value) {
          await patch(id, {video_type: 'panorama', panorama_format: ''});
          panoramaFormatControl.focus();
          setSchedulePlanHint('已切换为全景视频，请选择 360/180 的单目或双目格式。', 'warning');
          return;
        }
        await patch(id, {video_type: 'panorama', panorama_format: panoramaFormatControl.value});
      } else {
        await patch(id, {video_type: 'normal', panorama_format: ''});
      }
    };
    panoramaFormatControl.onchange = () => {
      if (videoTypeControl.value !== 'panorama') return;
      if (!panoramaFormatControl.value) {
        setSchedulePlanHint('全景视频必须选择格式后才能开始发布。', 'warning');
        return;
      }
      patch(id, {video_type: 'panorama', panorama_format: panoramaFormatControl.value});
    };
    const importButton = card.querySelector('.import-caption');
    importButton.disabled = queueLocked || captionImportBusy;
    importButton.title = queueLocked
      ? '自动发布进行中、暂停或终止处理中，暂不能导入文案'
      : '为当前视频导入文案；支持 txt、md、JSON、CSV、Excel';
    importButton.onclick = () => {
      if (queueUiLocked() || captionImportBusy) return;
      captionImportTargetId = id;
      captionImportOverwrite = Boolean($('#copyOverwriteInput')?.checked);
      captionImportConfirmed = false;
      if (captionImportOverwrite && hasExistingCopy()) {
        if (!confirm('导入文案将替换现有文案，是否继续？')) return;
        captionImportConfirmed = true;
      }
      const input = $('#captionImportInput');
      if (!input) return;
      input.value = '';
      input.click();
    };
    const scheduleModeControl = card.querySelector('.schedule-mode');
    const scheduleAtControl = card.querySelector('.schedule-at');
    const scheduleIntervalControl = card.querySelector('.schedule-interval');
    const scheduleCustomControl = card.querySelector('.schedule-interval-custom');
    const refreshScheduleControls = () => {
      const scheduled = scheduleModeControl.value === 'scheduled';
      scheduleAtControl.disabled = queueLocked || !scheduled;
      scheduleIntervalControl.disabled = queueLocked || !scheduled;
      scheduleCustomControl.disabled = queueLocked || !scheduled;
      scheduleCustomControl.hidden = !scheduled || scheduleIntervalControl.value !== 'custom';
    };
    refreshScheduleControls();
    const saveSchedule = async (body) => {
      try {
        await patch(id, body);
      } catch (err) {
        alert(`保存定时设置失败：${err.message}`);
        await load().catch(() => {});
      }
    };
    scheduleModeControl.onchange = () => {
      // 先更新本卡片的可编辑状态，再异步保存；轮询重绘会在控件失焦后
      // 才执行，因此用户选择“定时发布”后日期/间隔输入应立即可用。
      refreshScheduleControls();
      if (scheduleModeControl.value === 'immediate') {
        saveSchedule({schedule_mode: 'immediate', schedule_at: '', schedule_interval_minutes: ''});
        return;
      }
      // 若用户刚切换到定时模式，优先沿用卡片时间；没有时使用面板首条时间，
      // 让后端的 scheduled 必填校验不会留下半配置状态。
      const fallback = scheduleAtControl.value || $('#scheduleStartAt')?.value || '';
      if (fallback) scheduleAtControl.value = fallback;
      saveSchedule({schedule_mode: 'scheduled', schedule_at: fallback, ...scheduleIntervalBody(scheduleIntervalControl, scheduleCustomControl)});
    };
    scheduleAtControl.onchange = () => {
      const value = scheduleAtControl.value;
      if (!value) {
        setSchedulePlanHint('定时发布需要填写具体发布时间。', 'warning');
        return;
      }
      if (scheduleModeControl.value !== 'scheduled') scheduleModeControl.value = 'scheduled';
      saveSchedule({schedule_mode: 'scheduled', schedule_at: value, ...scheduleIntervalBody(scheduleIntervalControl, scheduleCustomControl)});
    };
    scheduleIntervalControl.onchange = () => {
      refreshScheduleControls();
      if (scheduleIntervalControl.value === 'custom') {
        scheduleCustomControl.focus();
        return;
      }
      if (scheduleModeControl.value === 'scheduled') {
        saveScheduleIntervalWithCascade(
          item,
          id,
          scheduleIntervalControl.value,
          scheduleIntervalControl,
          scheduleCustomControl,
        );
      }
    };
    scheduleCustomControl.onchange = () => {
      const value = String(scheduleCustomControl.value || '').trim();
      if (!/^\d+$/.test(value) || Number(value) < 1 || Number(value) > MAX_SCHEDULE_INTERVAL_MINUTES) {
        setSchedulePlanHint('间隔必须是 1～43200 分钟的整数。', 'warning');
        return;
      }
      if (scheduleModeControl.value === 'scheduled') {
        saveScheduleIntervalWithCascade(
          item,
          id,
          String(Number(value)),
          scheduleIntervalControl,
          scheduleCustomControl,
        );
      }
    };
    card.querySelector('.open-video').onclick = () => request('/api/open?path=' + encodeURIComponent(item.path));
    card.querySelector('.open-upload').onclick = () => openUploadPageFor(item);
    card.querySelector('.copy-caption').onclick = async (e) => { await navigator.clipboard.writeText(editorFieldValue(item, 'caption')); e.target.textContent='已复制'; setTimeout(()=>e.target.textContent='复制文案',1000); };
    const publishButton = card.querySelector('.publish');
    publishButton.disabled = queueLocked;
    publishButton.title = queueLocked ? '自动发布进行中、暂停或终止处理中，暂不能修改队列状态' : '';
    publishButton.onclick = () => {
      if (item.status === 'published') {
        if (!confirm('这条视频已经提交到抖音，重新提交可能产生重复作品。仍要重新加入待发布队列吗？')) return;
        patch(id, {status:'pending'});
        return;
      }
      patch(id, {status:'published'});
    };
    const skipButton = card.querySelector('.skip-item');
    skipButton.disabled = queueLocked;
    skipButton.title = queueLocked ? '自动发布进行中、暂停或终止处理中，暂不能修改队列状态' : '';
    skipButton.onclick = () => patch(id, {status:'skipped'});
    const deleteButton = card.querySelector('.delete-item');
    deleteButton.disabled = queueLocked;
    deleteButton.title = queueLocked ? '自动发布进行中，暂时不能删除' : '从本地发布队列移除，不删除原视频文件';
    deleteButton.onclick = () => deleteQueueItems([id]);
  });
  restoreActiveEditor(activeEditor);
}

$('#workflowVideoType').onchange = () => {
  const type = $('#workflowVideoType')?.value === 'panorama' ? 'panorama' : 'normal';
  workflowProfile = normalizeWorkflowProfile({
    videoType: type,
    panoramaFormat: type === 'panorama' ? $('#workflowPanoramaFormat')?.value : '',
  });
  saveWorkflowProfile();
  workflowProfileNotice = type === 'panorama'
    ? (workflowProfile.panoramaFormat
      ? '已进入全景发布方式；本次新增视频会按全景格式加入，已有队列不自动改动。'
      : '已选择全景视频，请先选择全景格式；已有队列不自动改动。')
    : '已进入普通发布方式；本次新增视频会按普通视频加入，已有队列不自动改动。';
  render({force: true});
  if (type === 'panorama' && !workflowProfile.panoramaFormat) $('#workflowPanoramaFormat')?.focus();
};
$('#workflowPanoramaFormat').onchange = () => {
  workflowProfile = normalizeWorkflowProfile({
    videoType: 'panorama',
    panoramaFormat: $('#workflowPanoramaFormat')?.value || '',
  });
  saveWorkflowProfile();
  workflowProfileNotice = workflowProfile.panoramaFormat
    ? '全景格式已准备好；现在可以选择视频，新增条目会走全景上传方式。已有队列不自动改动。'
    : '请选择全景格式后再选择视频。';
  render({force: true});
};
$('#mediaPickerBtn').onclick = (event) => {
  event.stopPropagation();
  toggleMediaPicker();
};
$('#folderBtn').onclick = async () => {
  if (queueUiLocked()) return;
  closeMediaPicker();
  const button = $('#folderBtn');
  button.disabled = true;
  try { await chooseFolder(); } catch (err) { alert(err.message); } finally {
    button.disabled = queueUiLocked();
  }
};
$('#filesBtn').onclick = async () => {
  if (queueUiLocked()) {
    alert('自动发布正在运行、暂停或终止处理中，暂时不能添加视频；请等待任务停止后再试。');
    return;
  }
  closeMediaPicker();
  const button = $('#filesBtn');
  button.disabled = true;
  const original = button.textContent;
  button.textContent = '请选择视频…';
  try {
    await chooseFiles();
  } catch (err) {
    alert(err.message);
  } finally {
    button.textContent = original;
    // render() normally restores this state; this also covers a cancelled dialog.
    button.disabled = queueUiLocked();
  }
};
document.addEventListener('click', (event) => {
  const picker = $('#mediaPicker');
  if (picker && !picker.contains(event.target)) closeMediaPicker();
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') closeMediaPicker();
});
document.addEventListener('focusout', () => {
  // 等浏览器完成 focus 转移后再判断，避免从一个输入框切到另一个输入框
  // 时中间闪一次重绘；离开最后一个控件后立即补上轮询期间的更新。
  // 使用一个 macrotask，让同一轮的 click/change 事件先完成，避免用户
  // 从输入框点击按钮时，失焦刷新先替换按钮导致点击丢失。
  setTimeout(flushDeferredRender, 0);
});
document.addEventListener('visibilitychange', () => {
  if (document.hidden) flushDeferredRender();
});
$('#selectAllQueue').onchange = (event) => {
  if (queueUiLocked()) {
    event.target.checked = false;
    return;
  }
  const visible = visibleQueueItems(state.items || []);
  visible.forEach(item => {
    const id = queueItemId(item);
    if (event.target.checked) selectedQueueIds.add(id);
    else selectedQueueIds.delete(id);
  });
  updateQueueSelectionUi(visible, queueUiLocked());
  updateBulkVideoTypeUi(queueUiLocked());
  document.querySelectorAll('.item-checkbox').forEach(input => { input.checked = event.target.checked; });
};
$('#deleteSelectedBtn').onclick = () => deleteQueueItems([...selectedQueueIds]);
async function applyBulkPreferences(field, controlId) {
  if (queueUiLocked()) return;
  const ids = [...selectedQueueIds];
  if (!ids.length) return;
  const value = document.getElementById(controlId).value === 'true';
  bulkPreferencesBusy = true;
  render();
  try {
    const result = await request('/api/items/bulk-preferences', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ids, field, value}),
    });
    await load();
    $('#bulkPreferencesHint').textContent = `已更新 ${result.updated} 条${result.skipped ? `，跳过 ${result.skipped} 条普通视频` : ''}。`;
  } catch (err) {
    $('#bulkPreferencesHint').textContent = `批量设置失败：${err.message}`;
  } finally {
    bulkPreferencesBusy = false;
    render();
  }
}
$('#applyBulkDownload').onclick = () => applyBulkPreferences('allow_download', 'bulkDownload');
$('#applyBulkOfficialTopic').onclick = () => applyBulkPreferences('official_topic_auto', 'bulkOfficialTopic');
$('#clearQueueBtn').onclick = async () => {
  const button = $('#clearQueueBtn');
  if (button.disabled) return;
  button.disabled = true;
  try {
    await clearQueue();
  } catch (err) {
    alert(`清空队列失败：${err.message}`);
  } finally {
    // confirm 取消、状态刚好变化或队列为空时也要立即恢复按钮状态，
    // 不必等待下一次状态轮询。
    render();
  }
};
$('#autoPublishBtn').onclick = togglePublish;
$('#stopPublishBtn').onclick = stopPublish;
$('#scheduleDefaultInterval').onchange = () => {
  const select = $('#scheduleDefaultInterval');
  const custom = $('#scheduleCustomInterval');
  if (!select || !custom) return;
  custom.hidden = select.value !== 'custom';
  if (select.value === 'custom') custom.focus();
  clearSchedulePlanHint();
  updateSchedulePlanPreview();
};
$('#schedulePlanMode').onchange = () => {
  clearSchedulePlanHint();
  updateSchedulePlanUi(state.items || [], state.publish || {});
};
['scheduleStartAt', 'scheduleDefaultInterval', 'scheduleCustomInterval'].forEach(id => {
  const control = $('#' + id);
  if (!control) return;
  control.addEventListener('input', () => {
    clearSchedulePlanHint();
    updateSchedulePlanPreview();
  });
  control.addEventListener('change', () => {
    if (id === 'scheduleStartAt') {
      // 浏览器或手输可能带秒/斜杠；失焦时归一化，避免显示值和解析值不一致。
      const parsed = parseLocalDateTime(control.value);
      if (parsed) control.value = localDateTimeValue(parsed);
    }
    clearSchedulePlanHint();
    updateSchedulePlanPreview();
  });
});
$('#generateScheduleBtn').onclick = generateSchedulePlan;
$('#allImmediateBtn').onclick = () => setAllImmediate(true);
$('#defaultScheduleBtn').onclick = () => {
  if (queueUiLocked()) return;
  const input = $('#scheduleStartAt');
  if (!input) return;
  input.value = defaultScheduleStart(state.items || []);
  clearSchedulePlanHint();
  updateSchedulePlanPreview();
  const parsed = parseDateTimeInput(input);
  setSchedulePlanHint(
    parsed ? `已填入默认时间 ${formatScheduleMoment(parsed)}，可直接生成。` : '已填入默认时间。',
    parsed ? 'success' : 'warning',
  );
  input.focus();
};
$('#bulkVideoType').onchange = () => updateBulkVideoTypeUi(queueUiLocked());
$('#bulkPanoramaFormat').onchange = () => updateBulkVideoTypeUi(queueUiLocked());
$('#applyBulkVideoType').onclick = async () => {
  if (queueUiLocked() || bulkVideoTypeBusy) return;
  const ids = [...selectedQueueIds];
  const type = $('#bulkVideoType')?.value || 'normal';
  const format = $('#bulkPanoramaFormat')?.value || '';
  if (!ids.length) {
    alert('请先勾选要设置的视频。');
    return;
  }
  if (type === 'panorama' && !format) {
    alert('请选择全景视频格式。');
    return;
  }
  bulkVideoTypeBusy = true;
  render();
  try {
    for (const id of ids) {
      await request('/api/items/' + encodeURIComponent(id), {
        method: 'PATCH',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({video_type: type, panorama_format: type === 'panorama' ? format : ''}),
      });
    }
    await load();
    setSchedulePlanHint(`已将 ${ids.length} 条视频设置为${type === 'panorama' ? '全景视频' : '普通视频'}。`, 'success');
  } catch (err) {
    alert(`批量设置发布类型失败：${err.message}`);
    await load().catch(() => {});
  } finally {
    bulkVideoTypeBusy = false;
    render();
  }
};
function setGenerateCopyHint(message, isError = false) {
  const hint = $('#generateCopyHint');
  if (!hint) return;
  hint.textContent = message || '';
  hint.classList.toggle('is-error', Boolean(isError));
}

async function generateQueueCopyFromLibrary() {
  if (queueUiLocked() || copyGenerateBusy) return;
  const items = state.items || [];
  const eligible = item => item.status !== 'skipped' && item.status !== 'published';
  const targets = selectedQueueIds.size
    ? items.filter(item => selectedQueueIds.has(queueItemId(item)) && eligible(item))
    : items.filter(eligible);
  if (!targets.length) {
    setGenerateCopyHint('当前没有可生成文案的视频', true);
    return;
  }
  if (targets.some(item => hasCaptionDraft(queueItemId(item)))) {
    setGenerateCopyHint('有文案正在编辑，请等待自动保存后再生成', true);
    return;
  }
  const overwrite = Boolean($('#copyOverwriteInput')?.checked);
  if (overwrite && !confirm(`将为 ${targets.length} 条视频生成文案，并替换现有文案。生成前会备份 queue.json，是否继续？`)) {
    return;
  }
  copyGenerateBusy = true;
  render();
  setGenerateCopyHint(`正在为 ${targets.length} 条视频匹配文案…`);
  try {
    const payload = await request('/api/copy-generate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ids: targets.map(queueItemId), overwrite}),
    });
    state.items = payload.items || state.items;
    await load();
    const parts = [payload.message || `已生成 ${(payload.generated || []).length} 条文案`];
    const unmatched = Array.isArray(payload.unmatched) ? payload.unmatched : [];
    if (unmatched.length) {
      const names = unmatched.slice(0, 3).map(row => row.name).join('、');
      parts.push(`未匹配：${names}${unmatched.length > 3 ? ' 等' : ''}`);
    }
    if ((payload.skipped || []).length && !overwrite) {
      parts.push('勾选“替换现有文案”可重写现有文案');
    }
    setGenerateCopyHint(parts.join('；'), !(payload.generated || []).length && unmatched.length > 0);
  } catch (err) {
    setGenerateCopyHint(`生成失败：${err.message}`, true);
  } finally {
    copyGenerateBusy = false;
    render();
  }
}

function toggleCopyMenu(open) {
  const menu = $('#copyMenu');
  const button = $('#copyMenuBtn');
  if (!menu || !button) return;
  const next = open === undefined ? menu.hidden : open;
  menu.hidden = !next;
  button.setAttribute('aria-expanded', String(next));
}
$('#copyMenuBtn').onclick = event => {
  event.stopPropagation();
  toggleCopyMenu();
};
document.addEventListener('click', event => {
  if (!event.target.closest('#copyMenuPicker')) toggleCopyMenu(false);
});
document.addEventListener('keydown', event => {
  if (event.key === 'Escape') toggleCopyMenu(false);
});

$('#generateCopyBtn').onclick = () => {
  toggleCopyMenu(false);
  generateQueueCopyFromLibrary();
};

let captionImportOverwrite = true;
let captionImportConfirmed = false;
function hasExistingCopy() {
  return (state.items || []).some(item => String(item.work_title || '').trim() || String(item.caption || '').trim() || String(item.hashtags || '').trim());
}
$('#importCopyBtn').onclick = () => {
  toggleCopyMenu(false);
  if (queueUiLocked() || captionImportBusy || !(state.items || []).length) return;
  captionImportTargetId = '';
  captionImportOverwrite = Boolean($('#copyOverwriteInput')?.checked);
  captionImportConfirmed = false;
  if (captionImportOverwrite && hasExistingCopy()) {
    if (!confirm('导入文案将替换现有文案，是否继续？')) return;
    captionImportConfirmed = true;
  }
  const input = $('#captionImportInput');
  if (!input) return;
  input.value = '';
  input.click();
};
async function importCoverImages(files) {
  if (queueUiLocked() || !files.length) return;
  const formData = new FormData();
  files.forEach(file => formData.append('files', file, file.name));
  const button = $('#coverImportBtn');
  const hint = $('#coverImportHint');
  const count = files.length;
  if (button) {
    button.disabled = true;
    button.textContent = '匹配中…';
  }
  if (hint) {
    hint.classList.remove('is-error');
    hint.textContent = `正在读取 ${count} 张封面，按文件名匹配横版/竖版…`;
    hint.title = '';
  }
  try {
    const result = await request('/api/covers/import', {method: 'POST', body: formData});
    state.items = result.items || state.items;
    if (hint) {
      const unmatched = Array.isArray(result.unmatched) ? result.unmatched : [];
      const details = [];
      const matched = Array.isArray(result.matched) ? result.matched : [];
      if (matched.length) details.push(...matched.map(row => `${row.video} ← ${row.cover}（${row.action === 'replaced' ? '替换' : '新增'}）`));
      if (unmatched.length) details.push(...unmatched.map(row => `${row.filename}：${row.reason}`));
      hint.textContent = result.message || `本轮导入完成：匹配 ${matched.length} 张`;
      hint.title = details.join('\n');
      hint.classList.toggle('is-error', !matched.length);
    }
    render({force: true});
  } catch (err) {
    if (hint) {
      hint.textContent = `封面导入失败：${err.message}`;
      hint.title = err.message;
      hint.classList.add('is-error');
    }
  } finally {
    if (button) {
      button.disabled = queueUiLocked();
      button.textContent = '导入自定义封面';
    }
  }
}
$('#coverImportBtn').onclick = () => {
  if (queueUiLocked()) return;
  $('#coverImportInput')?.click();
};
$('#coverImportInput').onchange = event => {
  const files = [...(event.target.files || [])];
  importCoverImages(files).finally(() => { event.target.value = ''; });
};
$('#downloadTemplateBtn').onclick = async () => {
  try {
    const items = (state.items || []).map(item => ({
      name: item.name || '',
      work_title: item.work_title || '',
      caption: item.caption || '',
      hashtags: item.hashtags || '',
    }));
    const response = await fetch('/api/caption-template', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({items}),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || `下载失败 (${response.status})`);
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = '文案导入模板.xlsx';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (err) {
    alert(`下载模板失败：${err.message}`);
  }
};
$('#captionImportInput').onchange = handleCaptionImport;
// 用户在原生文件选择器中点取消时，不能把上一次卡片目标带到下一次导入。
$('#captionImportInput').oncancel = () => {
  captionImportTargetId = '';
  $('#captionImportInput').value = '';
};
$('#checkBtn').onclick = async () => {
  try { await request('/api/publish-check', {method:'POST'}); await load(); } catch (err) { alert(err.message); }
};
$('#uploadBtn').onclick = openUploadPage;
$('#channelsPublishBtn').onclick = publishChannelsQueue;
$('#channelsPauseBtn').onclick = () => controlChannels(state.channels?.paused ? 'resume' : 'pause');
$('#channelsStopBtn').onclick = () => {
  if (!confirm('终止视频号发布？当前视频会标记为失败，浏览器将关闭。')) return;
  controlChannels('terminate');
};
$('#filter').onchange = () => render({force: true});
function showModeChooser() {
  const chooser = $('#modeChooser');
  const shell = $('#douyinShell');
  if (chooser) chooser.hidden = false;
  if (shell) shell.hidden = true;
}

function enterDouyinMode() {
  const chooser = $('#modeChooser');
  const shell = $('#douyinShell');
  if (chooser) chooser.hidden = true;
  if (shell) shell.hidden = false;
  load().catch(() => {});
}

function openDouyinWindow() {
  const hint = $('#modeHint');
  const win = window.open('/?platform=douyin', '_blank');
  if (!win) {
    if (hint) hint.textContent = '浏览器拦截了新窗口，请允许弹出窗口后重试。';
    return;
  }
  if (hint) hint.textContent = '已在新窗口打开抖音发布页。';
}

async function enterChannelsMode() {
  const hint = $('#modeHint');
  const win = window.open('about:blank', '_blank');
  if (!win) {
    if (hint) hint.textContent = '浏览器拦截了新窗口，请允许弹出窗口后重试。';
    return;
  }
  if (hint) hint.textContent = '正在新窗口打开视频号发布页…';
  try {
    const result = await request('/api/open-channels-ui', {method: 'POST'});
    if (!result || !result.url) throw new Error((result && result.error) || '视频号页面启动失败');
    win.location.href = result.url;
    if (hint) hint.textContent = '已在新窗口打开视频号发布页。';
  } catch (err) {
    try { win.close(); } catch (_) {}
    if (hint) hint.textContent = `视频号页面打开失败：${err.message}`;
  }
}

document.querySelectorAll('[data-mode]').forEach(button => {
  button.addEventListener('click', () => {
    if (button.dataset.mode === 'channels') enterChannelsMode();
    else openDouyinWindow();
  });
});
const modeSwitchBtn = $('#modeSwitchBtn');
if (modeSwitchBtn) modeSwitchBtn.addEventListener('click', showModeChooser);

if (navigator.webdriver || new URLSearchParams(location.search).get('platform') === 'douyin') {
  enterDouyinMode();
} else {
  showModeChooser();
}

load().then(() => setInterval(async () => {
  try {
    const pub = await request('/api/publish-status');
    const latest = await request('/api/state');
    state.publish = pub;
    state.channels = latest.channels || {};
    state.items = latest.items;
    state.root = latest.root;
    // Poll the independent batch-analysis control plane; completed results
    // are merged into the existing per-card review panels without touching
    // caption/status fields in the queue.
    const previousBatch = state.analysis_batch || {};
    const batchSummary = latest.analysis_batch || await request('/api/analyze-status');
    const batchToken = String(batchSummary.batch_token || '');
    const previousResults = previousBatch.results && typeof previousBatch.results === 'object'
      ? previousBatch.results
      : {};
    const hasFullResults = Object.values(previousResults).some(result => result && result.analysis);
    // /api/state intentionally returns a compact summary on every poll.  Do
    // not replace an already-loaded full result set with that summary: doing
    // so made “匹配推荐内容” disappear after the first 1.5s refresh even
    // though the per-card candidates were still visible.
    state.analysis_batch = (hasFullResults && batchToken && batchToken === analysisBatchResultsToken)
      ? {...batchSummary, results: previousResults}
      : batchSummary;
    // /api/state carries a compact summary so large frame/audio evidence is
    // not downloaded every 1.5 seconds.  Fetch the full result payload once
    // per terminal batch token for the per-card review panels.
    if (!analysisBatchRunning(batchSummary) && batchToken && (!hasFullResults || analysisBatchResultsToken !== batchToken)) {
      state.analysis_batch = await request('/api/analyze-status?include_results=1');
      analysisBatchResultsToken = String(state.analysis_batch.batch_token || batchToken);
    }
    mergeBatchAnalysis(state.analysis_batch);
    render();
  } catch (_) {}
}, 1500)).catch(err => { $('#queue').innerHTML = `<p class="empty">${escapeHtml(err.message)}</p>`; });
