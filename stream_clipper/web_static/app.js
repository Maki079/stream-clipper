'use strict';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const clone = value => structuredClone(value);
const state = {config: null, schema: null, revision: null, csrf: null, dirty: false, status: null, page: 'overview', settings: 'output', busy: false, containerRestarting: false};
const titles = {overview: 'Overview', sources: 'Sources', campaigns: 'Campaigns', clips: 'Clips', jobs: 'Job queue', settings: 'Settings'};
const languageCodes = ['en','pl','sk','cs','de','fr','es','it','pt','nl','uk','ru','ar','zh','ja','ko','hi','tr','sv','no','da','fi','el','ro','hu','bg','hr','sr','bs','sl','lt','lv','et','he','fa','id','ms','vi','th','bn','ta','te','ur','sw','af','ca','cy','is','ga','mk','sq','hy','ka','az','kk','ne','si','tl','la'];
const languageNames = new Intl.DisplayNames(['en'], {type: 'language'});
state.logsFollow = true;
state.logScrollTop = 0;
const labels = {url: 'Source URL or path', enabled: 'Enabled', mode: 'Capture mode', creator_name: 'Creator mention', required_hashtags: 'Required hashtags', extra_hashtags: 'Extra hashtags', min_seconds: 'Minimum length (seconds)', max_seconds: 'Maximum length (seconds)', pre_seconds: 'Before the moment (seconds)', post_seconds: 'After the moment (seconds)', chat_delay_seconds: 'Chat reaction delay (seconds)', cpu_workers: 'CPU workers', threads_per_worker: 'Threads per worker', io_slots: 'Concurrent I/O jobs', llm: 'AI highlight judge', vod: 'Video highlights', whisper: 'Transcription', ntfy: 'ntfy', rclone: 'Google Drive (rclone)', subtitle_y: 'Subtitle position (0–1)', top_n: 'Highlights per video', type: 'Layout', bot_token: 'Bot token', youtube_api_key: 'YouTube API key', fonts_dir: 'Font directory', ollama_url: 'Ollama address'};
let fieldCounter = 0;
const label = key => labels[key] || key.replaceAll('_', ' ').replace(/^./, c => c.toUpperCase());
const node = (tag, className, text) => {const el = document.createElement(tag); if (className) el.className = className; if (text !== undefined) el.textContent = text; return el;};
const append = (parent, ...children) => {children.filter(Boolean).forEach(child => parent.append(child)); return parent;};
function button(text, action, style = '') {const el = node('button', `button ${style}`, text); el.type = 'button'; el.addEventListener('click', action); return el;}
function badge(text, kind = '') {return node('span', `badge ${kind}`, text);}
function markDirty() {state.dirty = true; $('#savebar').hidden = false;}
function toast(message, error = false) {const el = $('#toast'); el.textContent = message; el.className = error ? 'error' : ''; el.hidden = false; clearTimeout(toast.timer); toast.timer = setTimeout(() => {el.hidden = true;}, error ? 14000 : 4500);}
async function api(path, data) {
  const response = await fetch(`/api/${path}`, data === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Clipper-CSRF': state.csrf}, body: JSON.stringify(data)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `Request failed (${response.status})`);
  return result;
}
function ask(title, message, confirm = 'Continue') {return new Promise(resolve => {
  const dialog = $('#confirm'); $('#confirm-title').textContent = title; $('#confirm-message').textContent = message; $('#confirm-yes').textContent = confirm;
  $('#confirm-yes').onclick = () => dialog.close('yes'); $('#confirm-no').onclick = () => dialog.close('no');
  dialog.onclose = () => resolve(dialog.returnValue === 'yes'); dialog.returnValue = ''; dialog.showModal();
});}
function notice(text, kind = '') {return node('div', `notice ${kind}`, text);}
function empty(title, description, action) {const el = node('div', 'empty'); append(el, node('strong', '', title), node('p', '', description), action); return el;}
function heading(title, description, action) {const el = node('div', 'page-heading'); append(el, append(node('div'), node('div', 'eyebrow', 'YOUR CLIPPING WORKSPACE'), node('h1', '', title), node('p', 'description', description)), action); return el;}
function section(title, linkText, href) {const el = append(node('div', 'section-head'), node('h2', '', title)); if (linkText) {const a = node('a', '', linkText); a.href = href; el.append(a);} return el;}

function resolveSchema(schema) {
  if (schema.$ref) return resolveSchema(state.schema.$defs[schema.$ref.split('/').pop()]);
  if (schema.anyOf) return {...resolveSchema(schema.anyOf.find(s => s.type !== 'null')), nullable: schema.anyOf.some(s => s.type === 'null')};
  return schema;
}
function defaults(schema) {
  const resolved = resolveSchema(schema);
  if (schema.default !== undefined) return clone(schema.default);
  if (resolved.type === 'object') return Object.fromEntries(Object.entries(resolved.properties || {}).map(([key, prop]) => [key, defaults(prop)]));
  if (resolved.type === 'array') return [];
  if (resolved.enum) return resolved.enum[0];
  if (resolved.type === 'boolean') return false;
  if (['number', 'integer'].includes(resolved.type)) return resolved.minimum ?? 0;
  return '';
}

// Schema-backed fields keep every setting available without requiring YAML or JSON.
function field(key, prop, obj, changed, options = {}) {
  const schema = resolveSchema(prop);
  if (schema.type === 'object') {
    const details = node('details', 'nested'); const summary = node('summary', '', label(key)); details.append(summary);
    const area = node('div', 'form-grid');
    if (schema.nullable) {
      const toggle = node('label', 'toggle'); const input = document.createElement('input'); input.type = 'checkbox'; input.checked = obj[key] !== null;
      append(toggle, input, document.createTextNode('Use custom ' + label(key).toLowerCase())); details.append(toggle);
      input.addEventListener('change', () => {obj[key] = input.checked ? defaults({...schema, nullable: false}) : null; area.replaceChildren(); if (obj[key]) fillFields(area, schema, obj[key], changed); changed();});
    }
    if (obj[key] !== null) {obj[key] ??= defaults(schema); fillFields(area, schema, obj[key], changed);}
    details.append(area); return details;
  }
  const arrayChoices = schema.type === 'array' && (resolveSchema(schema.items || {}).enum || (key === 'campaigns' && options.campaigns ? Object.keys(state.config.campaigns) : null));
  const wrapper = node('div', 'field'); const caption = node(arrayChoices ? 'span' : 'label', '', label(key)); wrapper.append(caption);
  let input;
  const enumeration = schema.enum || (schema.const !== undefined ? [schema.const] : null);
  if (key === 'language' || key === 'caption_language') {
    input = document.createElement('select');
    const codes = key === 'language' ? ['auto', ...languageCodes] : [...languageCodes];
    if (obj[key] && !codes.includes(obj[key])) codes.push(obj[key]);
    for (const code of codes) {
      let name; try {name = code === 'auto' ? 'Detect automatically' : `${languageNames.of(code)} (${code})`;} catch {name = code;}
      const option = node('option', '', name); option.value = code; input.append(option);
    }
    input.value = obj[key] || (key === 'language' ? 'auto' : 'en');
    input.addEventListener('change', () => {obj[key] = input.value; changed();});
  } else if (arrayChoices) {
    input = node('div', 'checkboxes');
    for (const name of arrayChoices) {
      const option = node('label'); const check = document.createElement('input'); check.type = 'checkbox'; check.value = name; check.checked = (obj[key] || []).includes(name);
      check.addEventListener('change', () => {obj[key] = $$('input:checked', input).map(el => el.value); changed();});
      append(option, check, document.createTextNode(' ' + (key === 'campaigns' ? name : label(name)))); input.append(option);
    }
    if (!arrayChoices.length) input.append(node('small', '', 'Create a campaign first to assign clips for delivery.'));
  } else if (schema.type === 'boolean') {
    wrapper.classList.add('toggle'); input = document.createElement('input'); input.type = 'checkbox'; input.checked = Boolean(obj[key]);
    input.addEventListener('change', () => {obj[key] = input.checked; changed();});
  } else if (enumeration) {
    input = document.createElement('select');
    for (const value of enumeration) {const option = node('option', '', value === 'blur_fit' ? 'Fit with blurred background' : value === 'center_crop' ? 'Center crop' : value === 'stack' ? 'Facecam + gameplay' : label(String(value))); option.value = value; input.append(option);}
    input.value = obj[key]; input.addEventListener('change', () => {obj[key] = input.value; changed();});
  } else if (schema.type === 'array') {
    input = document.createElement('textarea'); input.value = (obj[key] || []).join('\n'); input.rows = 3;
    input.addEventListener('input', () => {obj[key] = input.value.split('\n').map(s => s.trim()).filter(Boolean); changed();});
    wrapper.append(node('small', '', 'One item per line.'));
  } else {
    input = document.createElement(key === 'notes' ? 'textarea' : 'input');
    const numeric = ['number', 'integer'].includes(schema.type);
    if (input.tagName === 'INPUT') input.type = numeric ? 'number' : /token|api_key|password/.test(key) ? 'password' : 'text';
    if (numeric) {input.step = schema.type === 'integer' ? '1' : 'any'; if (schema.minimum !== undefined) input.min = schema.minimum; if (schema.maximum !== undefined) input.max = schema.maximum;}
    input.value = obj[key] ?? ''; if (schema.nullable) input.placeholder = 'Automatic';
    if (input.type === 'password') input.autocomplete = 'new-password';
    input.addEventListener('input', () => {obj[key] = input.value === '' && schema.nullable ? null : numeric ? (input.value === '' ? null : Number(input.value)) : input.value; changed();});
  }
  if (schema.description) wrapper.append(node('small', '', schema.description));
  const id = `field-${++fieldCounter}`; input.id = id; caption.id = `${id}-label`;
  if (arrayChoices) {input.setAttribute('role', 'group'); input.setAttribute('aria-labelledby', caption.id);}
  else caption.htmlFor = id;
  const help = $$('small', wrapper); help.forEach((el, index) => {el.id = `${id}-help-${index}`;});
  if (help.length) input.setAttribute('aria-describedby', help.map(el => el.id).join(' '));
  wrapper.append(input); return wrapper;
}
function fillFields(container, schema, obj, changed, options = {}) {
  for (const [key, prop] of Object.entries(resolveSchema(schema).properties || {})) {
    if (options.only && !options.only.includes(key) || options.omit?.includes(key)) continue;
    container.append(field(key, prop, obj, changed, options));
  }
}

function runtimeBadges(name, src) {
  const activity = state.status?.sources?.[name];
  const states = {paused: 'Paused', stopped: 'Service stopped', video: 'Video source', farming: 'Online · farming', online: 'Online · no chat capture', offline: 'Offline / reconnecting', unknown: 'Checking stream'};
  const area = node('div', 'runtime-badges');
  area.append(badge(activity ? states[activity.status] : !src.enabled ? 'Paused' : state.status?.running ? 'Waiting for status' : 'Service stopped', activity?.online ? 'good' : ''));
  if (activity?.building) area.append(badge(activity.open_clip ? 'Capturing a clip' : 'Making clips', 'warn'));
  return area;
}
function runtimeDetails(name) {
  const activity = state.status?.sources?.[name]; const area = node('div', 'source-runtime');
  if (activity) {
    area.append(node('div', 'source-metrics', `${activity.clips_24h} clips captured · ${activity.delivered_24h} delivered (24h)`));
    area.append(node('div', 'source-metrics', `${activity.running_jobs} running jobs · ${activity.queued_jobs} queued`));
    if (activity.farming) area.append(node('div', 'source-metrics', `${activity.messages_in_window} messages in chat window · ${activity.spikes} spikes this session`));
  }
  return area;
}
function sourceCard(name, src, campaign = false, readOnly = false) {
  const card = node('article', 'source-card');
  if (!campaign) card.dataset.source = name;
  const icon = node('div', `platform-icon ${campaign ? '' : src.platform}`, campaign ? '⚑' : ({twitch: 'T', youtube: '▶', kick: 'K', local: '▤', gdrive: 'D'}[src.platform] || '◉'));
  const title = append(node('div'), node('h3', '', name), node('div', 'source-type', campaign ? src.creator_name : `${label(src.platform)} · ${src.mode === 'live' ? 'Live capture' : 'Video highlights'}`));
  const top = append(node('div', 'source-top'), icon, title);
  if (!campaign) card.append(runtimeBadges(name, src));
  const tags = node('div', 'tag-list'); (campaign ? src.required_hashtags : src.campaigns).forEach(tag => tags.append(node('span', 'tag', tag)));
  if (!tags.children.length) tags.append(node('span', 'muted', campaign ? 'No required hashtags' : 'No campaigns assigned'));
  const bottom = node('div', 'source-bottom');
  if (campaign) bottom.append(node('span', 'source-type', `${src.min_seconds}–${src.max_seconds}s clips`));
  else {const toggle = node('label', 'toggle'); const input = document.createElement('input'); input.type = 'checkbox'; input.checked = src.enabled; input.setAttribute('aria-label', `Enable ${name}`); input.addEventListener('change', () => {src.enabled = input.checked; markDirty(); render();}); append(toggle, input, document.createTextNode('Capture enabled')); bottom.append(toggle);}
  const actions = node('div', 'actions');
  const edit = button('Edit', () => editEntity(campaign ? 'campaigns' : 'sources', name), 'subtle');
  const remove = button('Delete', async () => {
    const kind = campaign ? 'campaign' : 'source';
    if (!await ask(`Delete ${name}?`, campaign ? 'This removes the campaign and unassigns it from all sources. Existing clips are kept.' : 'This removes the source from your settings. Existing clips are kept. Save and apply to stop its capture.', 'Delete')) return;
    delete state.config[campaign ? 'campaigns' : 'sources'][name];
    if (campaign) Object.values(state.config.sources).forEach(s => {s.campaigns = s.campaigns.filter(c => c !== name);});
    markDirty(); render();
  }, 'subtle danger');
  append(actions, edit, button('Duplicate', () => editEntity(campaign ? 'campaigns' : 'sources', name, true), 'subtle'), remove); bottom.append(actions);
  card.prepend(top);
  append(card, node('div', 'source-url', campaign ? src.notes || 'Campaign rules and delivery captions' : src.url), tags);
  if (!campaign) card.append(runtimeDetails(name));
  if (!readOnly) card.append(bottom);
  return card;
}
function showWarnings(root) {
  if (!state.exists) root.append(notice('Welcome! Add a campaign and a source, then save your settings to set up Stream Clipper.', 'info'));
  if (state.status?.restart_required) root.append(notice('Saved settings are ready. Choose “Apply & restart” to use them. Running recordings keep their current settings until then.', 'info'));
  if (state.status?.external) root.append(notice('A clipper service is running outside this interface. Stop that process before starting the service here.', 'info'));
  if (state.status?.database_error) root.append(notice('Could not read job history: ' + state.status.database_error, 'error'));
  if (state.warnings?.length) {const box = notice('A few things need your attention:'); const list = node('ul'); state.warnings.forEach(w => list.append(node('li', '', w))); box.append(list); root.append(box);}
}
function serviceButtons() {
  const actions = node('div', 'actions'); const running = state.status?.running;
  if (running) {
    actions.append(button('Stop service', () => serviceAction('stop'), 'subtle'));
    actions.append(button('Restart capture', () => serviceAction('restart'), 'subtle'));
    if (state.status.restart_required) actions.append(button('Apply & restart', () => serviceAction('restart'), 'primary'));
  } else actions.append(button('Start service', () => serviceAction('start'), 'primary'));
  if (state.status?.container_restart_available) actions.append(button('Restart container', restartContainer, 'subtle'));
  $$('button', actions).forEach(b => {b.disabled = state.busy || state.containerRestarting || state.status?.external;}); return actions;
}
async function restartContainer() {
  if (state.dirty) {toast('Save or discard your changes before restarting the container.', true); return;}
  if (!await ask('Restart the capture container?', 'This restarts the clipper container, including capture workers and the web panel. Running work gets time to finish. The panel reconnects automatically; allow up to two minutes.', 'Restart container')) return;
  state.containerRestarting = true; render();
  try {await api('container/restart', {}); toast('Container restart requested. Waiting for the panel to return…');}
  catch (error) {state.containerRestarting = false; toast(error.message, true); render();}
}
async function serviceAction(action) {
  if (state.dirty && action !== 'stop') {toast('Save your changes before starting or applying settings.', true); return;}
  if (action !== 'stop' && !Object.values(state.config.sources).some(s => s.enabled)) {toast('Add and enable at least one source before starting.', true); return;}
  if (action !== 'start' && !await ask(action === 'stop' ? 'Stop the clipping service?' : 'Apply saved settings?', 'Open clips are flushed and running jobs are given time to finish. This can take about a minute.', action === 'stop' ? 'Stop service' : 'Apply & restart')) return;
  state.busy = true; render(); toast(action === 'start' ? 'Starting the service…' : 'Finishing running work. Please wait…');
  try {await api('service', {action}); await refreshStatus(); toast(action === 'stop' ? 'Service stopped.' : 'Service started. Check status and logs as it starts up.');}
  catch (error) {toast(error.message, true);} finally {state.busy = false; render();}
}

function overview(root) {
  root.append(heading('Your next great clip starts here.', 'Manage your sources. Catch the highlights. Keep creating.', serviceButtons()));
  showWarnings(root);
  const running = state.status?.running;
  const heroText = append(node('div'), node('div', 'eyebrow', 'LESS EDITING. MORE CREATING.'), node('h2', '', running ? 'Your clipping workflow is running.' : 'Great moments deserve to be seen.'), node('p', '', running ? 'Watch your queue and recent clips below. Add new sources or fine-tune your campaigns whenever you need.' : 'Connect a stream or video, set your campaign rules, and turn the best moments into vertical clips.'), button('+ Add a source', () => editEntity('sources'), 'primary'));
  const art = append(node('div', 'hero-art'), node('div', 'film', '▶')); art.setAttribute('aria-hidden', 'true');
  root.append(append(node('section', 'hero'), heroText, art));
  const stats = node('div', 'stats');
  const active = Object.entries(state.status?.sources || {}).filter(([, activity]) => activity.online && activity.farming);
  const sum = key => active.reduce((total, [, activity]) => total + (activity[key] || 0), 0);
  const metrics = [['Actively farming', active.length, 'Online streams capturing highlights'], ['Clips captured', sum('clips_24h'), 'Active streams · last 24 hours'], ['Clips delivered', sum('delivered_24h'), 'Active streams · last 24 hours'], ['Production jobs', sum('running_jobs') + sum('queued_jobs'), `${sum('running_jobs')} running · ${sum('queued_jobs')} queued`]];
  metrics.forEach(([name, value, note]) => stats.append(append(node('div', 'stat'), node('div', 'stat-label', name), node('div', 'stat-number', String(value)), node('div', 'stat-note', note)))); root.append(stats);
  root.append(section('Streams farming clips', 'Manage sources', '#sources'));
  const grid = node('div', 'card-grid'); active.forEach(([name, activity]) => grid.append(sourceCard(name, activity.source, false, true)));
  root.append(grid.children.length ? grid : empty('No streams are farming clips right now.', 'Start the service and wait for an enabled live source to come online. All configured sources are available on the Sources page.'));
  root.append(section('Recent clips', 'View all clips', '#clips')); clipTable(root, (state.status?.clips || []).slice(0, 5));
  const logHeading = section('Service activity'); const follow = node('label', 'toggle');
  const followInput = document.createElement('input'); followInput.type = 'checkbox'; followInput.checked = state.logsFollow; followInput.id = 'follow-logs';
  append(follow, followInput, document.createTextNode('Follow latest')); logHeading.append(follow); root.append(logHeading);
  const logs = node('pre', 'logs', state.status?.logs?.join('\n') || 'Service logs will appear here after you start the service from this interface.');
  logs.id = 'service-logs'; logs.tabIndex = 0; logs.setAttribute('aria-label', 'Service activity logs');
  followInput.addEventListener('change', () => {state.logsFollow = followInput.checked; if (state.logsFollow) logs.scrollTop = logs.scrollHeight;});
  logs.addEventListener('scroll', () => {state.logScrollTop = logs.scrollTop; state.logsFollow = logs.scrollHeight - logs.scrollTop - logs.clientHeight < 24; followInput.checked = state.logsFollow;});
  root.append(logs);
  requestAnimationFrame(() => {logs.scrollTop = state.logsFollow ? logs.scrollHeight : state.logScrollTop;});
}
function entities(root, kind) {
  const campaign = kind === 'campaigns'; root.append(heading(titles[kind], campaign ? 'Set creator mentions, campaign rules, and captions in one place.' : 'Choose where your highlights come from and how they are captured.', button(campaign ? '+ Add campaign' : '+ Add source', () => editEntity(kind), 'primary')));
  if (!campaign) root.append(notice('Online status comes from fresh recorder activity. “Farming” means live footage is being recorded with chat detection enabled. Clip activity also includes queued and running work. Status refreshes automatically. Save and apply configuration changes to update capture.', 'info'));
  const search = document.createElement('input'); search.type = 'search'; search.placeholder = campaign ? 'Search campaigns…' : 'Search sources…'; search.setAttribute('aria-label', search.placeholder);
  const filters = append(node('div', 'filters'), search); const grid = node('div', 'card-grid'); root.append(filters, grid);
  function draw() {grid.replaceChildren(); Object.entries(state.config[kind]).filter(([name, value]) => `${name} ${value.url || value.creator_name}`.toLowerCase().includes(search.value.toLowerCase())).forEach(([name, value]) => grid.append(sourceCard(name, value, campaign))); if (!grid.children.length) grid.append(empty(search.value ? 'No matches found.' : campaign ? 'Create your first campaign.' : 'Connect your first source.', search.value ? 'Try another search.' : campaign ? 'Define how your clips should be captioned and delivered.' : 'Use a Twitch, YouTube, or Kick channel, or a finished video.'));}
  search.addEventListener('input', draw); draw();
}
function table(headers, rows) {const wrap = node('div', 'table-wrap'); const t = document.createElement('table'); const head = document.createElement('thead'); const hr = document.createElement('tr'); headers.forEach(h => hr.append(node('th', '', h))); head.append(hr); const body = document.createElement('tbody'); rows.forEach(cells => {const row = document.createElement('tr'); cells.forEach(cell => {const td = document.createElement('td'); td.append(cell instanceof Node ? cell : document.createTextNode(String(cell))); row.append(td);}); body.append(row);}); append(t, head, body); wrap.append(t); return wrap;}
function statusBadge(status) {return badge(label(status), ['done', 'delivered', 'rendered'].includes(status) ? 'good' : status === 'failed' ? 'bad' : status === 'running' ? 'warn' : '');}
function clipTable(root, clips) {
  if (!clips.length) {root.append(empty('The highlights are on their way.', 'Finished clips and their processing status will appear here.')); return;}
  root.append(table(['Clip / source', 'Mode', 'Length', 'Score', 'Status', 'Created', 'Download'], clips.map(c => {
    const actions = node('div', 'actions');
    if (c.download_available) {const link = node('a', 'button subtle', 'Download MP4'); link.href = `/api/clips/${encodeURIComponent(c.id)}/download`; link.download = `${c.id}.mp4`; actions.append(link);}
    if (c.drive_url) {const link = node('a', 'button subtle', 'Open Drive'); link.href = c.drive_url; link.target = '_blank'; link.rel = 'noopener noreferrer'; actions.append(link);}
    if (!actions.children.length) actions.append(node('span', 'muted', 'Not available'));
    return [append(node('div'), node('div', '', c.source), node('small', 'muted', c.id)), label(c.mode), `${Math.round(c.end_ts - c.start_ts)}s`, c.score.toFixed(1), statusBadge(c.status), new Date(c.created_at * 1000).toLocaleString(), actions];
  })));
}
function activity(root, jobs) {
  const actions = append(node('div', 'actions'), button('Refresh', async () => {await refreshStatus(); draw();}, 'subtle'));
  if (jobs) actions.append(button('Retry failed jobs', async () => {if (!await ask('Retry all failed jobs?', 'Failed jobs will return to the queue with their attempt count reset.', 'Retry jobs')) return; try {const result = await api('retry', {}); toast(`${result.retried} jobs requeued.`); await refreshStatus(); draw();} catch (error) {toast(error.message, true);}}, 'subtle'));
  root.append(heading(jobs ? 'Job queue' : 'Your clips', jobs ? 'See what’s processing and retry jobs that need another attempt.' : 'A running history of the moments your sources have captured.', actions));
  if (!jobs) root.append(notice('Download finished local videos with “Download MP4”. Google Drive deliveries open in Drive, where you can download them after signing in. Removed local files are marked unavailable.', 'info'));
  const filters = node('div', 'filters'); const search = document.createElement('input'); search.type = 'search'; search.placeholder = 'Search recent history…'; search.setAttribute('aria-label', 'Search recent history'); const select = document.createElement('select'); select.setAttribute('aria-label', 'Filter by status'); ['all', ...(jobs ? ['queued', 'running', 'done', 'failed'] : ['pending', 'cut', 'rendered', 'delivered', 'failed', 'skipped'])].forEach(value => {const option = node('option', '', value === 'all' ? 'All statuses' : label(value)); option.value = value; select.append(option);}); append(filters, search, select); root.append(filters);
  const area = node('div'); root.append(area);
  function draw() {area.replaceChildren(); const records = (state.status?.[jobs ? 'jobs' : 'clips'] || []).filter(r => (select.value === 'all' || r.status === select.value) && `${r.id} ${r.type || r.source} ${r.error || ''}`.toLowerCase().includes(search.value.toLowerCase()));
    if (!jobs) {clipTable(area, records); return;}
    if (!records.length) {area.append(empty('No jobs to show.', 'Jobs appear when the service starts processing your sources.')); return;}
    area.append(table(['Job', 'Task', 'Status', 'Attempts', 'Details', ''], records.map(j => {const error = node('span', '', j.error || '—'); const retry = j.status === 'failed' ? button('Retry', async () => {try {await api('retry', {id: j.id}); await refreshStatus(); draw(); toast(`Job #${j.id} requeued.`);} catch (err) {toast(err.message, true);}}, 'subtle') : ''; return [`#${j.id}`, label(j.type), statusBadge(j.status), `${j.attempts} / ${j.max_attempts}`, error, retry];})));
    $$('tbody tr', area).forEach(row => {row.children[4].className = 'error';});
  }
  search.addEventListener('input', draw); select.addEventListener('change', draw); draw();
  root.append(node('p', 'description', 'Showing the latest 100 records. Refresh the page to load newer history.'));
}

function settings(root) {
  root.append(heading('Make it yours.', 'Fine-tune your output, processing, delivery, and notifications.'));
  const groups = {output: ['Video & subtitles', 'Shape the look of every clip. Motion controls are available below.'], whisper: ['Transcription', 'Choose the models used for subtitles and video analysis.'], llm: ['AI highlight judge', 'Rate moments, trim clips, and create hooks with a local Ollama model.'], delivery: ['Delivery', 'Save clips locally or deliver them to Google Drive through rclone.'], notifications: ['Notifications', 'Get updates through Telegram or ntfy.'], workers: ['Processing', 'Balance CPU use, concurrent work, and retries.'], general: ['General', 'Storage paths, timezone, logging, and live buffer settings.']};
  const tabs = node('div', 'settings-tabs'); Object.entries(groups).forEach(([key, [name]]) => tabs.append(button(name, () => {state.settings = key; render();}, state.settings === key ? 'active' : 'subtle'))); root.append(tabs);
  const key = state.settings; const [name, desc] = groups[key]; const section = append(node('section', 'settings-section'), node('h2', '', name), node('p', '', desc)); const grid = node('div', 'form-grid'); const schema = state.schema.$defs.GlobalCfg;
  if (key === 'general') fillFields(grid, schema, state.config.global, markDirty, {omit: Object.keys(groups).filter(k => k !== 'general')});
  else fillFields(grid, schema.properties[key], state.config.global[key], markDirty);
  section.append(grid); root.append(section); root.append(notice('Changes are validated before saving. Apply saved settings from Overview to update the running service.', 'info'));
}

function editEntity(kind, oldName, duplicate = false) {
  const isSource = kind === 'sources'; const schema = state.schema.$defs[isSource ? 'SourceCfg' : 'CampaignCfg'];
  const data = oldName ? clone(state.config[kind][oldName]) : defaults(schema);
  let copiedName = '';
  if (duplicate) {
    copiedName = `${oldName}_copy`; let suffix = 2;
    while (state.config[kind][copiedName]) copiedName = `${oldName}_copy_${suffix++}`;
    oldName = undefined;
  }
  if (!oldName && !duplicate && isSource) {data.mode = 'live'; data.platform = 'twitch'; data.url = ''; data.campaigns = Object.keys(state.config.campaigns).slice(0, 1);}
  const nameObj = {name: copiedName || oldName || ''}; const fields = $('#editor-fields'); fields.replaceChildren();
  $('#editor-title').textContent = `${duplicate ? 'Duplicate' : oldName ? 'Edit' : 'Add'} ${isSource ? 'source' : 'campaign'}`; $('#editor-kind').textContent = isSource ? 'CONNECT YOUR CONTENT' : 'SET YOUR CAMPAIGN RULES';
  if (duplicate) fields.append(notice(isSource ? 'All source settings have been copied. Change the name, URL, language, and campaign assignment for the new streamer.' : 'Campaign rules have been copied. Change the name, creator mention, and hashtags for the new streamer. Existing sources keep their original campaign.', 'info'));
  const primary = node('div', 'form-grid'); primary.append(field('name', {type: 'string', description: 'Letters, numbers, underscores, and hyphens. Example: my_channel'}, nameObj, () => {}));
  const basics = isSource ? ['enabled', 'mode', 'platform', 'url', 'campaigns', 'language'] : ['creator_name', 'caption_language', 'required_hashtags', 'extra_hashtags', 'min_seconds', 'max_seconds', 'allowed_platforms', 'notes'];
  fillFields(primary, schema, data, () => {}, {only: basics, campaigns: true}); fields.append(primary);
  const advanced = node('details'); advanced.append(node('summary', '', isSource ? 'Capture, layout & advanced options' : 'Caption & advanced options')); const more = node('div', 'form-grid'); fillFields(more, schema, data, () => {}, {omit: basics}); advanced.append(more); fields.append(advanced);
  const errorArea = node('div'); fields.append(errorArea); const dialog = $('#editor'); dialog.showModal();
  $('#editor-form').onsubmit = async event => {
    event.preventDefault(); errorArea.replaceChildren(); const name = nameObj.name.trim();
    if (isSource && !data.url.trim() || !isSource && !data.creator_name.trim()) {errorArea.append(notice(isSource ? 'Enter a source URL or file path.' : 'Enter the creator mention required by this campaign.', 'error')); return;}
    if (!/^[A-Za-z0-9_-]+$/.test(name)) {errorArea.append(notice('Choose a name using letters, numbers, underscores, and hyphens.', 'error')); return;}
    if (name !== oldName && state.config[kind][name]) {errorArea.append(notice('That name is already used. Choose a unique name.', 'error')); return;}
    const candidate = clone(state.config); if (oldName) delete candidate[kind][oldName]; candidate[kind][name] = data;
    if (!isSource && oldName && oldName !== name) Object.values(candidate.sources).forEach(s => {s.campaigns = s.campaigns.map(c => c === oldName ? name : c);});
    const submit = $('button[type=submit]', dialog); submit.disabled = true;
    try {await api('validate', {config: candidate}); state.config = candidate; markDirty(); dialog.close(); render(); toast(`${isSource ? 'Source' : 'Campaign'} updated. Save changes when ready.`);}
    catch (error) {errorArea.append(notice(error.message, 'error')); errorArea.scrollIntoView({block: 'nearest'});} finally {submit.disabled = false;}
  };
}

function render() {
  if (!state.config) return;
  const oldLogs = $('#service-logs'); if (oldLogs) state.logScrollTop = oldLogs.scrollTop;
  const page = location.hash.slice(1); state.page = titles[page] ? page : 'overview'; $('#breadcrumb').textContent = titles[state.page];
  $$('nav a').forEach(a => {a.classList.toggle('active', a.dataset.page === state.page); if (a.dataset.page === state.page) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');});
  const root = $('#main'); root.replaceChildren();
  if (state.page === 'overview') overview(root);
  else if (['sources', 'campaigns'].includes(state.page)) entities(root, state.page);
  else if (state.page === 'settings') settings(root);
  else activity(root, state.page === 'jobs');
}
async function refreshStatus() {
  try {const status = await api('status');
    if (state.containerRestarting && state.status?.instance_id !== status.instance_id) {
      // A restarted server has a new CSRF token. Refresh it before the next edit.
      state.csrf = (await api('config')).csrf; state.containerRestarting = false; toast('Container restarted. The panel is connected again.');
    }
    state.status = status; const el = $('#connection'); const s = state.status; el.textContent = state.containerRestarting ? 'Restarting container…' : state.busy ? 'Updating service' : s.healthy ? 'Service running' : s.running ? 'Starting / checking heartbeat' : s.external ? 'External service' : s.exit_code !== null && s.exit_code !== 0 ? 'Service exited' : 'Service stopped'; el.className = `badge ${s.healthy ? 'good' : s.exit_code ? 'bad' : ''}`;
    if (state.page === 'overview' && !$('#confirm').open && !$('#editor').open) render();
    if (state.page === 'sources') {
      $$('article[data-source]').forEach(card => {
        const name = card.dataset.source; const src = state.config.sources[name];
        if (src) {card.querySelector('.runtime-badges')?.replaceWith(runtimeBadges(name, src)); card.querySelector('.source-runtime')?.replaceWith(runtimeDetails(name));}
      });
    }
  } catch (error) {$('#connection').textContent = state.containerRestarting ? 'Restarting container…' : 'Connection lost'; $('#connection').className = 'badge bad';}
}
async function load() {
  const result = await api('config'); state.config = result.config; state.schema = result.schema; state.csrf = result.csrf; state.revision = result.revision; state.warnings = result.warnings; state.exists = result.exists; state.dirty = false; $('#savebar').hidden = true; render();
}
$('#save').onclick = async () => {
  const saveButton = $('#save'); saveButton.disabled = true;
  try {const result = await api('config', {config: state.config, revision: state.revision}); state.config = result.config; state.revision = result.revision; state.warnings = result.warnings; state.exists = true; state.dirty = false; $('#savebar').hidden = true; await refreshStatus(); render(); toast('Settings saved.' + (state.status?.running ? ' Apply & restart from Overview when ready.' : ' Start the service from Overview when ready.'));}
  catch (error) {toast(error.message, true);} finally {saveButton.disabled = false;}
};
$('#discard').onclick = async () => {if (!await ask('Discard unsaved changes?', 'Your settings will return to the last saved version.', 'Discard')) return; try {await load();} catch (error) {toast(error.message, true);}};
$('#close-editor').onclick = $('#cancel-editor').onclick = () => $('#editor').close();
window.addEventListener('hashchange', render);
window.addEventListener('beforeunload', event => {if (state.dirty) {event.preventDefault(); event.returnValue = '';}});
load().then(refreshStatus).catch(error => {const root = $('#main'); root.replaceChildren(heading('Unable to open your workspace', 'Resolve the error below, then reload this page.'), notice(error.message, 'error'), button('Try again', () => location.reload(), 'primary'));});
setInterval(() => {if (!state.busy) refreshStatus();}, 6000);
