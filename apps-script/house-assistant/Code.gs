/**
 * House assistant: until-done reminders and a house log.
 *
 * Reminders: every open task in the Google Tasks list "House" gets one
 * calendar event on its due date. With nag on, a daily event replaces it from
 * the due date until the task is checked off. Checking it off schedules the
 * next task from the completion date. Deleting a task cancels it. Deleting a
 * calendar event does not; the next run puts it back.
 *
 * Fields go in the task notes (or in [brackets] in the title), one per line:
 *   repeat: 6 months | 2 weeks | 1 year | none
 *   nag: daily | off
 *   calendar: Hoa
 *   time: 09:00
 *   snap: saturday
 *
 * House log: each open task in "House log inbox" is appended to the "House Log"
 * Doc and house-log.md in the Drive folder "House", then deleted. An optional
 * "date: 2026-09-24" line sets the entry date.
 *
 * Photos: images in House/Photos/Inbox are read by the Gemini API (script
 * property GEMINI_API_KEY; GEMINI_MODEL is picked automatically if unset),
 * renamed, moved to House/Photos/<Category>, added to the "House Items" sheet
 * and house-items.md, and noted under their topic in the House Log.
 *
 * Runs hourly on Google's servers (install() creates the trigger). Script
 * property HOUSE_FOLDER overrides the Drive folder name.
 */

var HOUSE_LIST = 'House';
var INBOX_LIST = 'House log inbox';
var DEFAULT_FOLDER = 'House';
var LOG_DOC = 'House Log';
var LOG_MD = 'house-log.md';
var LOG_INTRO = 'Things done around the house, one section per topic. New notes on a topic ' +
    'are added to its section with the date.';
var DEFAULT_CALENDAR = 'Hoa';
var DEFAULT_HOUR = 9;
var DEFAULT_MINUTE = 0;
var TAG_PREFIX = 'managedId:until-done:';
var DAYS = ['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat'];
var NOTE_PREFIX = /^\s*house\s*(note|log)\s*[:\-]\s*/i;
var REMINDER_PREFIX = /^\s*house(\s*reminder)?\s*[:\-]\s*/i;
var TASK_PREFIX = /^\s*(task\s*nh[àa]|house\s*task|google\s*tasks?)\s*[:,\-]\s*/i;
var FIELD_LINE = /^\s*(repeat|nag|calendar|time|snap|date)\s*[:=]/i;

var PHOTOS_FOLDER = 'Photos';
var PHOTO_INBOX = 'Inbox';
var PHOTO_REVIEW = 'Needs review';
var REVIEW_TOPIC = 'Photos to review';
var PHOTO_CATEGORIES = ['HVAC', 'Plumbing', 'Electrical', 'Paint', 'Appliances', 'Tools',
  'Safety', 'Outdoor', 'Supplies', 'Other'];
var ITEMS_SHEET = 'House Items';
var ITEMS_MD = 'house-items.md';
var ITEM_COLUMNS = ['Date', 'Category', 'Topic', 'Item', 'Brand', 'Model', 'Size/Dimensions',
  'Color/Finish', 'Serial', 'Price', 'Store', 'Link', 'How to use / Notes', 'Photo'];
// Free tier allows ~10 requests/minute; a run must also finish inside 6 minutes.
var MAX_PHOTOS_PER_RUN = 20;
var PHOTO_GAP_MS = 7000;
var PHOTO_TIME_BUDGET_MS = 4 * 60 * 1000;
var MAX_PHOTO_TRIES = 3;
var MAX_PHOTO_BYTES = 14 * 1024 * 1024;
var GEMINI_BASE = 'https://generativelanguage.googleapis.com/v1beta';
var MODEL_CANDIDATES = ['gemini-3.8-flash', 'gemini-3.7-flash', 'gemini-3.6-flash',
  'gemini-3.5-flash', 'gemini-3-flash', 'gemini-flash-latest', 'gemini-2.5-flash'];
var PHOTO_PROMPT = [
  'This photo is of something for my house: a product, package, label, receipt, paint can,',
  'part, appliance nameplate or instructions. Extract only what is printed or clearly visible.',
  'Leave a field as an empty string when it is not visible. Never guess.',
  'category: one of ' + PHOTO_CATEGORIES.join(', ') + '.',
  'topic: 2-4 English words naming the thing in the house it is for, e.g. "Air filter",',
  '"Toilet fill valve", "Smoke detector", "Living room paint".',
  'item: short product description. size: dimensions or capacity exactly as printed (16x25x1, 1 gal).',
  'color: color name, paint code and finish. price: with currency symbol. store: where it was bought.',
  'link: any URL or QR code text visible. notes: at most two short lines with the most useful',
  'spec or usage/install instruction (e.g. "MERV 11, replace every 3 months"). Write in English,',
  'keep brand and product names as printed.',
].join(' ');
var PHOTO_SCHEMA = {
  type: 'OBJECT',
  properties: {
    category: {type: 'STRING', enum: PHOTO_CATEGORIES},
    topic: {type: 'STRING'},
    item: {type: 'STRING'},
    brand: {type: 'STRING'},
    model: {type: 'STRING'},
    size: {type: 'STRING'},
    color: {type: 'STRING'},
    serial: {type: 'STRING'},
    price: {type: 'STRING'},
    store: {type: 'STRING'},
    link: {type: 'STRING'},
    notes: {type: 'STRING'},
  },
  required: ['category', 'topic', 'item'],
};

function install() {
  ensureTrigger_();
  migrateGarageLube_();
  return tick();
}

function tick() {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(30000)) {
    return 'another run is in progress';
  }
  try {
    var lines = [sortDefaultList_()];
    lines = lines.concat(runReminders_());
    lines.push(fileHouseLog_());
    try {
      lines.push(processPhotos_());
    } catch (err) {
      lines.push('photos: error ' + err);
    }
    var summary = lines.join('\n');
    console.log(summary);
    return summary;
  } finally {
    lock.releaseLock();
  }
}

function ensureTrigger_() {
  var triggers = ScriptApp.getProjectTriggers();
  for (var i = 0; i < triggers.length; i++) {
    if (triggers[i].getHandlerFunction() === 'tick') {
      ScriptApp.deleteTrigger(triggers[i]);
    }
  }
  ScriptApp.newTrigger('tick').timeBased().everyHours(1).create();
}

// ------------------------------------------------------------ default list

// Gemini can only write to the default list, so it prefixes titles instead:
// "House note: ..." goes to the log inbox, "House: ..." to the reminders list,
// "Task nhà: ..." / "House task: ..." to either (see classifyTitle_).
function sortDefaultList_() {
  var tasks = listTasks_('@default', false);
  var houseId = null;
  var inboxId = null;
  var moved = 0;

  for (var i = 0; i < tasks.length; i++) {
    var t = tasks[i];
    if (t.status === 'completed' || !t.title) {
      continue;
    }
    var kind = classifyTitle_(t.title, !!t.due);
    if (!kind) {
      continue;
    }
    var title = kind.title;
    var notes = t.notes || '';
    var copy = {notes: notes};
    var target;
    if (kind.note) {
      inboxId = inboxId || taskListId_(INBOX_LIST);
      target = inboxId;
      copy.title = title || 'House note';
      if (!/\bdate\s*[:=]/i.test(notes)) {
        copy.notes = (notes ? notes + '\n' : '') +
            'date: ' + ymd_(new Date(t.updated || Date.now()));
      }
    } else {
      houseId = houseId || taskListId_(HOUSE_LIST);
      target = houseId;
      copy.title = title || 'House reminder';
      if (t.due) {
        copy.due = t.due;
      }
    }
    Tasks.Tasks.insert(copy, target);
    Tasks.Tasks.remove('@default', t.id);
    moved++;
  }
  return 'default list: moved ' + moved;
}

// "Task nhà: ..." is the voice wake phrase; plain Gemini may keep it in the title.
// With a due date it is a reminder, otherwise a note.
function classifyTitle_(rawTitle, hasDue) {
  var t = String(rawTitle).normalize('NFC');
  var m = t.match(NOTE_PREFIX);
  if (m) {
    return {note: true, title: t.slice(m[0].length).trim()};
  }
  m = t.match(TASK_PREFIX);
  if (m) {
    return {note: !hasDue, title: t.slice(m[0].length).trim()};
  }
  m = t.match(REMINDER_PREFIX);
  if (m) {
    return {note: false, title: t.slice(m[0].length).trim()};
  }
  return null;
}

// ---------------------------------------------------------------- reminders

function runReminders_() {
  var listId = taskListId_(HOUSE_LIST);
  var tasks = listTasks_(listId, true);
  var state = loadState_();
  var open = {};
  var out = [];

  for (var i = 0; i < tasks.length; i++) {
    var task = tasks[i];
    if (task.status === 'completed') {
      if (state[task.id]) {
        out.push(rollOver_(listId, task, state, open));
      }
      continue;
    }
    open[task.id] = true;
    out.push(syncOpen_(listId, task, state));
  }

  var ids = Object.keys(state);
  for (var j = 0; j < ids.length; j++) {
    if (!open[ids[j]]) {
      removeEvents_(state[ids[j]]);
      delete state[ids[j]];
      out.push('cancelled ' + ids[j]);
    }
  }
  saveState_(state);
  if (!out.length) {
    out.push('reminders: none');
  }
  return out;
}

function syncOpen_(listId, task, state) {
  var cfg = parseFields_(task);
  var s = state[task.id] || {};
  if (s.calendar && s.calendar !== cfg.calendar) {
    removeEvents_(s);
    s = {};
  }
  s.calendar = cfg.calendar;
  state[task.id] = s;
  var cal = calendarByName_(cfg.calendar);

  var dueYmd = taskDueYmd_(task);
  if (!dueYmd) {
    dueYmd = ymd_(new Date());
    Tasks.Tasks.patch({due: dueYmd + 'T00:00:00.000Z'}, listId, task.id);
  }
  var todayYmd = ymd_(new Date());

  if (todayYmd < dueYmd || !cfg.nag) {
    if (s.seriesId) {
      deleteSeries_(cal, s.seriesId);
      delete s.seriesId;
    }
    var ev = s.eventId ? getEvent_(cal, s.eventId) : null;
    if (ev && !sameSlot_(ev.getStartTime(), dueYmd, cfg)) {
      ev.deleteEvent();
      ev = null;
    }
    if (!ev) {
      ev = createSingle_(cal, dueYmd, cfg, task.id);
    } else if (ev.getTitle() !== cfg.title) {
      ev.setTitle(cfg.title);
    }
    s.eventId = ev.getId();
    return cfg.title + ': due ' + dueYmd;
  }

  if (s.eventId) {
    var single = getEvent_(cal, s.eventId);
    if (single) {
      single.deleteEvent();
    }
    delete s.eventId;
  }
  var series = s.seriesId ? getSeries_(cal, s.seriesId) : null;
  if (!series) {
    series = createDaily_(cal, todayYmd, cfg, task.id);
  } else if (series.getTitle() !== cfg.title) {
    series.setTitle(cfg.title);
  }
  s.seriesId = series.getId();
  return cfg.title + ': nagging daily, due ' + dueYmd;
}

function rollOver_(listId, task, state, open) {
  var cfg = parseFields_(task);
  removeEvents_(state[task.id]);
  delete state[task.id];
  var doneYmd = ymd_(task.completed ? new Date(task.completed) : new Date());
  if (!cfg.repeat) {
    return cfg.title + ': done ' + doneYmd;
  }
  var nextYmd = addInterval_(doneYmd, cfg.repeat);
  if (cfg.snap !== null) {
    nextYmd = weekdayOnOrAfter_(nextYmd, cfg.snap);
  }
  var next = Tasks.Tasks.insert(
    {title: task.title, notes: task.notes || '', due: nextYmd + 'T00:00:00.000Z'},
    listId
  );
  open[next.id] = true;
  syncOpen_(listId, next, state);
  return cfg.title + ': done ' + doneYmd + ', next due ' + nextYmd;
}

function parseFields_(task) {
  var title = task.title || '';
  var text = title + '\n' + (task.notes || '');
  var cfg = {
    title: title.replace(/\s*\[[^\]]*\]/g, '').trim() || 'House reminder',
    repeat: parseRepeat_(text),
    nag: true,
    calendar: DEFAULT_CALENDAR,
    hour: DEFAULT_HOUR,
    minute: DEFAULT_MINUTE,
    snap: null,
    details: detailLines_(task.notes || ''),
  };

  var nag = text.match(/\bnag\s*[:=]\s*(\w+)/i);
  if (nag && /^(off|no|none|false)$/i.test(nag[1])) {
    cfg.nag = false;
  }
  var cal = text.match(/\bcalendar\s*[:=]\s*([^\n\];|]+)/i);
  if (cal) {
    cfg.calendar = cal[1].trim();
  }
  var time = text.match(/\btime\s*[:=]\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?/i);
  if (time) {
    var h = Number(time[1]) % 24;
    var ap = (time[3] || '').toLowerCase();
    if (ap === 'pm' && h < 12) h += 12;
    if (ap === 'am' && h === 12) h = 0;
    cfg.hour = h;
    cfg.minute = time[2] ? Number(time[2]) : 0;
  }
  var snap = text.match(/\bsnap\s*[:=]\s*(sun|mon|tue|wed|thu|fri|sat)/i);
  if (snap) {
    cfg.snap = DAYS.indexOf(snap[1].toLowerCase());
  }
  return cfg;
}

function parseRepeat_(text) {
  var m = text.match(/\brepeat\s*[:=]\s*([^\n\];|]+)/i);
  var raw = m ? m[1] : '';
  if (!raw) {
    var every = text.match(/\bevery\s+(\d+\s*)?(day|week|month|year)s?\b/i);
    raw = every ? every[0] : '';
  }
  raw = raw.trim().toLowerCase();
  if (!raw || /^(none|once|never|no|off)\b/.test(raw)) {
    return null;
  }
  var words = {daily: 'day', weekly: 'week', monthly: 'month', yearly: 'year', annually: 'year'};
  var w = raw.match(/^(daily|weekly|monthly|yearly|annually)/);
  if (w) {
    return {n: 1, unit: words[w[1]]};
  }
  var r = raw.match(/(\d+)?\s*(day|week|month|year)s?/);
  if (!r) {
    return null;
  }
  return {n: r[1] ? Number(r[1]) : 1, unit: r[2]};
}

function detailLines_(notes) {
  return notes
    .split('\n')
    .filter(function (line) {
      return line.trim() && !FIELD_LINE.test(line);
    })
    .map(function (line) {
      return line.trim();
    });
}

function eventDescription_(cfg, taskId) {
  var lines = cfg.details.slice();
  lines.push(
    'Check off "' + cfg.title + '" in Google Tasks (House list) when finished. ' +
      'Deleting this event does not count; deleting the task cancels it.'
  );
  lines.push(TAG_PREFIX + taskId);
  return lines.join('\n');
}

function createSingle_(cal, dueYmd, cfg, taskId) {
  var start = parseYmdAt_(dueYmd, cfg.hour, cfg.minute);
  var ev = cal.createEvent(cfg.title, start, new Date(start.getTime() + 3600000), {
    description: eventDescription_(cfg, taskId),
  });
  ev.removeAllReminders();
  ev.addPopupReminder(0);
  return ev;
}

function createDaily_(cal, startYmd, cfg, taskId) {
  var start = parseYmdAt_(startYmd, cfg.hour, cfg.minute);
  var series = cal.createEventSeries(
    cfg.title,
    start,
    new Date(start.getTime() + 3600000),
    CalendarApp.newRecurrence().addDailyRule(),
    {description: eventDescription_(cfg, taskId)}
  );
  series.removeAllReminders();
  series.addPopupReminder(0);
  return series;
}

function sameSlot_(start, dueYmd, cfg) {
  return ymd_(start) === dueYmd && start.getHours() === cfg.hour && start.getMinutes() === cfg.minute;
}

function removeEvents_(s) {
  if (!s) {
    return;
  }
  var cal;
  try {
    cal = calendarByName_(s.calendar || DEFAULT_CALENDAR);
  } catch (err) {
    console.log('skip cleanup: ' + err);
    return;
  }
  if (s.eventId) {
    var ev = getEvent_(cal, s.eventId);
    if (ev) {
      ev.deleteEvent();
    }
  }
  if (s.seriesId) {
    deleteSeries_(cal, s.seriesId);
  }
}

function getEvent_(cal, id) {
  try {
    return cal.getEventById(id);
  } catch (err) {
    return null;
  }
}

function getSeries_(cal, id) {
  try {
    return cal.getEventSeriesById(id);
  } catch (err) {
    return null;
  }
}

function deleteSeries_(cal, id) {
  var series = getSeries_(cal, id);
  if (series) {
    series.deleteEventSeries();
  }
}

// ---------------------------------------------------------------- house log

function fileHouseLog_() {
  var listId = taskListId_(INBOX_LIST);
  var tasks = listTasks_(listId, false).filter(function (t) {
    return t.status !== 'completed';
  });
  if (!tasks.length) {
    return 'house log: nothing new';
  }
  tasks.sort(function (a, b) {
    return String(a.updated).localeCompare(String(b.updated));
  });

  var folder = houseFolder_();
  var doc = DocumentApp.openById(logDocId_(folder));
  var body = doc.getBody();
  normalizeLegacySections_(body);
  var merged = 0;

  for (var i = 0; i < tasks.length; i++) {
    var t = tasks[i];
    var notes = t.notes || '';
    var date = notes.match(/\bdate\s*[:=]\s*(\d{4}-\d{2}-\d{2})/i);
    var entryYmd = date ? date[1] : ymd_(t.updated ? new Date(t.updated) : new Date());
    var topic = (t.title || 'House note').trim();
    var details = detailLines_(notes);
    if (!details.length) {
      details = [topic];
    }
    var bullets = details.map(function (d) { return entryYmd + ': ' + d; });
    if (addToSection_(body, topic, bullets)) {
      merged++;
    }
  }
  var mdText = renderMarkdown_(body);
  doc.saveAndClose();
  DriveApp.getFileById(logMdId_(folder)).setContent(mdText);

  for (var k = 0; k < tasks.length; k++) {
    Tasks.Tasks.remove(listId, tasks[k].id);
  }
  return 'house log: filed ' + tasks.length + ' (' + merged + ' into existing topics)';
}

// Sections are one HEADING2 per topic with dated bullets under it. Returns
// true when the topic already existed.
function addToSection_(body, topic, bullets) {
  var H2 = DocumentApp.ParagraphHeading.HEADING2;
  var n = body.getNumChildren();
  var start = findSection_(body, topic);
  var existed = start >= 0;
  var insertAt;
  var listRef = null;
  var seen = {};

  if (existed) {
    insertAt = n;
    for (var i = start + 1; i < n; i++) {
      var el = body.getChild(i);
      if (el.getType() === DocumentApp.ElementType.PARAGRAPH &&
          el.asParagraph().getHeading() !== DocumentApp.ParagraphHeading.NORMAL) {
        insertAt = i;
        break;
      }
      if (el.getType() === DocumentApp.ElementType.LIST_ITEM) {
        listRef = el.asListItem();
        seen[detailKey_(listRef.getText())] = true;
      }
    }
    bullets = bullets.filter(function (b) {
      var k = detailKey_(b);
      if (seen[k]) {
        return false;
      }
      seen[k] = true;
      return true;
    });
  } else {
    body.appendParagraph(topic).setHeading(H2);
    insertAt = body.getNumChildren();
  }

  for (var j = 0; j < bullets.length; j++) {
    var item = insertAt + j >= body.getNumChildren()
      ? body.appendListItem(bullets[j])
      : body.insertListItem(insertAt + j, bullets[j]);
    item.setGlyphType(DocumentApp.GlyphType.BULLET);
    if (listRef) {
      item.setListId(listRef);
    }
    listRef = item;
  }
  return existed;
}

function findSection_(body, topic) {
  var key = topicKey_(topic);
  var loose = -1;
  for (var i = 0; i < body.getNumChildren(); i++) {
    var el = body.getChild(i);
    if (el.getType() !== DocumentApp.ElementType.PARAGRAPH ||
        el.asParagraph().getHeading() !== DocumentApp.ParagraphHeading.HEADING2) {
      continue;
    }
    var k = topicKey_(el.asParagraph().getText());
    if (k === key) {
      return i;
    }
    if (loose < 0 && k.length >= 4 && key.length >= 4 && (k.indexOf(key) >= 0 || key.indexOf(k) >= 0)) {
      loose = i;
    }
  }
  return loose;
}

// Same detail on another day still counts as a duplicate.
function detailKey_(bullet) {
  return topicKey_(String(bullet).replace(/^\d{4}-\d{2}-\d{2}:\s*/, ''));
}

function topicKey_(text) {
  return String(text).toLowerCase().replace(/[^\p{L}\p{N}]+/gu, '');
}

// Early entries used "YYYY-MM-DD - Topic" headings with undated bullets.
function normalizeLegacySections_(body) {
  var legacy = /^(\d{4}-\d{2}-\d{2}) - (.+)$/;
  var dated = /^\d{4}-\d{2}-\d{2}: /;
  var ymd = null;
  for (var i = 0; i < body.getNumChildren(); i++) {
    var el = body.getChild(i);
    if (el.getType() === DocumentApp.ElementType.PARAGRAPH &&
        el.asParagraph().getHeading() === DocumentApp.ParagraphHeading.HEADING2) {
      var m = el.asParagraph().getText().match(legacy);
      ymd = m ? m[1] : null;
      if (m) {
        el.asParagraph().setText(m[2]);
      }
    } else if (el.getType() === DocumentApp.ElementType.PARAGRAPH &&
        /^Things done around the house\. New entries are added at the bottom/.test(el.asParagraph().getText())) {
      el.asParagraph().setText(LOG_INTRO);
    } else if (ymd && el.getType() === DocumentApp.ElementType.LIST_ITEM) {
      var li = el.asListItem();
      if (!dated.test(li.getText())) {
        li.setText(ymd + ': ' + li.getText());
      }
    }
  }
}

function renderMarkdown_(body) {
  var out = ['# House Log', ''];
  for (var i = 0; i < body.getNumChildren(); i++) {
    var el = body.getChild(i);
    var type = el.getType();
    if (type === DocumentApp.ElementType.LIST_ITEM) {
      out.push('- ' + el.asListItem().getText());
    } else if (type === DocumentApp.ElementType.PARAGRAPH) {
      var p = el.asParagraph();
      var text = p.getText().trim();
      if (!text || p.getHeading() === DocumentApp.ParagraphHeading.TITLE) {
        continue;
      }
      if (p.getHeading() === DocumentApp.ParagraphHeading.HEADING2) {
        out.push('', '## ' + text, '');
      } else {
        out.push(text);
      }
    }
  }
  return out.join('\n').replace(/\n{3,}/g, '\n\n').trim() + '\n';
}

function houseFolder_() {
  var name = PropertiesService.getScriptProperties().getProperty('HOUSE_FOLDER') || DEFAULT_FOLDER;
  var it = DriveApp.getRootFolder().getFoldersByName(name);
  return it.hasNext() ? it.next() : DriveApp.getRootFolder().createFolder(name);
}

function logDocId_(folder) {
  var id = liveFileId_('LOG_DOC_ID', folder, LOG_DOC);
  if (id) {
    return id;
  }
  var doc = DocumentApp.create(LOG_DOC);
  doc.getBody().appendParagraph(LOG_INTRO);
  doc.saveAndClose();
  DriveApp.getFileById(doc.getId()).moveTo(folder);
  PropertiesService.getScriptProperties().setProperty('LOG_DOC_ID', doc.getId());
  return doc.getId();
}

function logMdId_(folder) {
  var id = liveFileId_('LOG_MD_ID', folder, LOG_MD);
  if (id) {
    return id;
  }
  var file = folder.createFile(LOG_MD, '# House log\n', MimeType.PLAIN_TEXT);
  PropertiesService.getScriptProperties().setProperty('LOG_MD_ID', file.getId());
  return file.getId();
}

function liveFileId_(prop, folder, name) {
  var props = PropertiesService.getScriptProperties();
  var id = props.getProperty(prop);
  if (id) {
    try {
      if (!DriveApp.getFileById(id).isTrashed()) {
        return id;
      }
    } catch (err) {
      // fall through and look it up by name
    }
  }
  var files = folder.getFilesByName(name);
  if (files.hasNext()) {
    id = files.next().getId();
    props.setProperty(prop, id);
    return id;
  }
  return null;
}

// ---------------------------------------------------------------- photos

// Photos shared to House/Photos/Inbox are read by the Gemini API, renamed,
// moved to House/Photos/<Category>, listed in the House Items sheet and noted
// under their topic in the House Log.
function processPhotos_() {
  var started = Date.now();
  var props = PropertiesService.getScriptProperties();
  var folder = houseFolder_();
  var photos = childFolder_(folder, PHOTOS_FOLDER);
  var inbox = childFolder_(photos, PHOTO_INBOX);

  var files = [];
  var it = inbox.getFiles();
  while (it.hasNext()) {
    var f = it.next();
    if (/^image\//.test(f.getMimeType())) {
      files.push(f);
    }
  }
  if (!files.length) {
    return 'photos: nothing new';
  }
  var key = props.getProperty('GEMINI_API_KEY');
  if (!key) {
    return 'photos: ' + files.length + ' waiting; set script property GEMINI_API_KEY';
  }
  files.sort(function (a, b) {
    return a.getDateCreated().getTime() - b.getDateCreated().getTime();
  });

  var tries = JSON.parse(props.getProperty('PHOTO_TRIES') || '{}');
  var doc = DocumentApp.openById(logDocId_(folder));
  var body = doc.getBody();
  var sheet = itemsSheet_(folder);
  var done = 0;
  var failed = 0;
  var i = 0;

  for (; i < files.length && i < MAX_PHOTOS_PER_RUN; i++) {
    if (Date.now() - started > PHOTO_TIME_BUDGET_MS) {
      break;
    }
    if (i > 0) {
      Utilities.sleep(PHOTO_GAP_MS);
    }
    var file = files[i];
    var id = file.getId();
    try {
      var info = extractPhotoInfo_(file, key);
      fileItemPhoto_(file, info, photos, sheet, body);
      delete tries[id];
      done++;
    } catch (err) {
      console.log('photo ' + file.getName() + ': ' + err);
      tries[id] = (tries[id] || 0) + 1;
      if (tries[id] >= MAX_PHOTO_TRIES) {
        file.moveTo(childFolder_(photos, PHOTO_REVIEW));
        addToSection_(body, REVIEW_TOPIC, [
          ymd_(file.getDateCreated()) + ': Could not read ' + file.getName() + ' - ' + file.getUrl(),
        ]);
        delete tries[id];
      }
      failed++;
    }
  }
  props.setProperty('PHOTO_TRIES', JSON.stringify(tries));

  var mdText = renderMarkdown_(body);
  doc.saveAndClose();
  DriveApp.getFileById(logMdId_(folder)).setContent(mdText);
  DriveApp.getFileById(itemsMdId_(folder)).setContent(renderItemsMarkdown_(sheet.getDataRange().getValues()));

  var out = 'photos: filed ' + done;
  if (failed) {
    out += ', failed ' + failed + ' (retried next run)';
  }
  if (files.length > i) {
    out += ', ' + (files.length - i) + ' left for next run';
  }
  return out;
}

function fileItemPhoto_(file, info, photosFolder, sheet, body) {
  var ymd = ymd_(file.getDateCreated());
  var category = photoCategory_(info.category);
  var topic = (info.topic || info.item || 'House item').trim();
  var ext = (file.getName().match(/\.[A-Za-z0-9]{2,5}$/) || ['.jpg'])[0].toLowerCase();

  file.setName(photoFileName_(ymd, topic, info, ext));
  file.moveTo(childFolder_(photosFolder, category));
  file.setDescription(photoDescription_(info));

  var url = file.getUrl();
  sheet.appendRow([
    ymd, category, topic, info.item || '', info.brand || '', info.model || '',
    info.size || '', info.color || '', info.serial || '', info.price || '',
    info.store || '', info.link || '', info.notes || '', url,
  ]);
  addToSection_(body, topic, [ymd + ': Photo: ' + photoSummary_(info) + ' - ' + url]);
}

function extractPhotoInfo_(file, key) {
  var blob = file.getBlob();
  var bytes = blob.getBytes();
  if (bytes.length > MAX_PHOTO_BYTES) {
    throw new Error('photo is ' + Math.round(bytes.length / 1048576) + ' MB, over the limit');
  }
  var props = PropertiesService.getScriptProperties();
  var model = geminiModel_(key);
  var payload = {
    contents: [{
      parts: [
        {text: PHOTO_PROMPT},
        {inline_data: {mime_type: blob.getContentType() || 'image/jpeg', data: Utilities.base64Encode(bytes)}},
      ],
    }],
    generationConfig: {
      temperature: 0.1,
      responseMimeType: 'application/json',
      responseSchema: PHOTO_SCHEMA,
    },
  };
  var res = UrlFetchApp.fetch(GEMINI_BASE + '/models/' + model + ':generateContent', {
    method: 'post',
    contentType: 'application/json',
    headers: {'x-goog-api-key': key},
    payload: JSON.stringify(payload),
    muteHttpExceptions: true,
  });
  var code = res.getResponseCode();
  var text = res.getContentText();
  if (code !== 200) {
    if (code === 404 || (code === 429 && /limit"?:\s*0\b/.test(text))) {
      var skip = JSON.parse(props.getProperty('GEMINI_MODEL_SKIP') || '[]');
      skip.push(model);
      props.setProperty('GEMINI_MODEL_SKIP', JSON.stringify(skip));
      props.deleteProperty('GEMINI_MODEL');
    }
    throw new Error('Gemini ' + model + ' HTTP ' + code + ': ' + text.slice(0, 300));
  }
  var json = JSON.parse(text);
  var part = json.candidates && json.candidates[0] && json.candidates[0].content &&
      json.candidates[0].content.parts && json.candidates[0].content.parts[0];
  if (!part || !part.text) {
    throw new Error('Gemini returned no content: ' + text.slice(0, 300));
  }
  return JSON.parse(part.text);
}

function geminiModel_(key) {
  var props = PropertiesService.getScriptProperties();
  var model = props.getProperty('GEMINI_MODEL');
  if (model) {
    return model;
  }
  var res = UrlFetchApp.fetch(GEMINI_BASE + '/models?pageSize=200', {
    headers: {'x-goog-api-key': key},
    muteHttpExceptions: true,
  });
  if (res.getResponseCode() !== 200) {
    throw new Error('list models HTTP ' + res.getResponseCode() + ': ' + res.getContentText().slice(0, 300));
  }
  var names = (JSON.parse(res.getContentText()).models || [])
    .filter(function (m) {
      return (m.supportedGenerationMethods || []).indexOf('generateContent') >= 0;
    })
    .map(function (m) { return m.name.replace(/^models\//, ''); });
  model = pickModel_(names, JSON.parse(props.getProperty('GEMINI_MODEL_SKIP') || '[]'));
  props.setProperty('GEMINI_MODEL', model);
  return model;
}

function pickModel_(names, skip) {
  var ok = names.filter(function (n) { return skip.indexOf(n) < 0; });
  for (var i = 0; i < MODEL_CANDIDATES.length; i++) {
    if (ok.indexOf(MODEL_CANDIDATES[i]) >= 0) {
      return MODEL_CANDIDATES[i];
    }
  }
  var flash = ok.filter(function (n) {
    return /^gemini-[\d.]+-flash$/.test(n);
  }).sort(function (a, b) {
    return parseFloat(b.slice(7)) - parseFloat(a.slice(7));
  });
  if (!flash.length) {
    throw new Error('no usable Gemini Flash model for this key');
  }
  return flash[0];
}

// Run from the editor after setting GEMINI_API_KEY.
function checkGemini() {
  var key = PropertiesService.getScriptProperties().getProperty('GEMINI_API_KEY');
  if (!key) {
    throw new Error('Set script property GEMINI_API_KEY first');
  }
  var model = geminiModel_(key);
  var res = UrlFetchApp.fetch(GEMINI_BASE + '/models/' + model + ':generateContent', {
    method: 'post',
    contentType: 'application/json',
    headers: {'x-goog-api-key': key},
    payload: JSON.stringify({contents: [{parts: [{text: 'Reply with the single word OK.'}]}]}),
    muteHttpExceptions: true,
  });
  var msg = 'model ' + model + ': HTTP ' + res.getResponseCode() + ' ' + res.getContentText().slice(0, 200);
  console.log(msg);
  return msg;
}

function photoCategory_(raw) {
  var key = String(raw || '').trim().toLowerCase();
  for (var i = 0; i < PHOTO_CATEGORIES.length; i++) {
    if (PHOTO_CATEGORIES[i].toLowerCase() === key) {
      return PHOTO_CATEGORIES[i];
    }
  }
  return 'Other';
}

function photoFileName_(ymd, topic, info, ext) {
  var bits = [info.brand, info.model, info.size].filter(function (x) { return x && String(x).trim(); });
  var name = ymd + ' ' + topic + (bits.length ? ' - ' + bits.join(' ') : '');
  name = name.replace(/[\\\/:*?"<>|\r\n]+/g, ' ').replace(/\s+/g, ' ').trim();
  return name.slice(0, 120) + ext;
}

function photoSummary_(info) {
  var parts = [];
  var main = [info.brand, info.model].filter(Boolean).join(' ');
  if (info.item && main.toLowerCase().indexOf(String(info.item).toLowerCase()) < 0) {
    parts.push(info.item);
  }
  if (main) parts.push(main);
  if (info.size) parts.push(info.size);
  if (info.color) parts.push(info.color);
  if (info.serial) parts.push('serial ' + info.serial);
  if (info.price) parts.push(info.price);
  if (info.store) parts.push(info.store);
  if (info.notes) parts.push(info.notes);
  if (info.link) parts.push(info.link);
  return parts.join(', ') || 'photo';
}

function photoDescription_(info) {
  var labels = [['Item', 'item'], ['Brand', 'brand'], ['Model', 'model'], ['Size', 'size'],
    ['Color', 'color'], ['Serial', 'serial'], ['Price', 'price'], ['Store', 'store'],
    ['Link', 'link'], ['Notes', 'notes']];
  return labels.filter(function (l) { return info[l[1]]; })
    .map(function (l) { return l[0] + ': ' + info[l[1]]; })
    .join('\n');
}

function renderItemsMarkdown_(rows) {
  var byCat = {};
  for (var i = 1; i < rows.length; i++) {
    var r = rows[i];
    if (!r[0] && !r[3]) {
      continue;
    }
    var date = r[0] instanceof Date ? ymd_(r[0]) : String(r[0]);
    var info = {item: r[3], brand: r[4], model: r[5], size: r[6], color: r[7], serial: r[8],
      price: r[9], store: r[10], link: r[11], notes: r[12]};
    var line = '- ' + date + ' ' + r[2] + ': ' + photoSummary_(info) + (r[13] ? ' ([photo](' + r[13] + '))' : '');
    (byCat[r[1] || 'Other'] = byCat[r[1] || 'Other'] || []).push(line);
  }
  var out = ['# House Items', ''];
  Object.keys(byCat).sort().forEach(function (c) {
    out.push('## ' + c, '');
    out = out.concat(byCat[c]);
    out.push('');
  });
  return out.join('\n').trim() + '\n';
}

function childFolder_(parent, name) {
  var it = parent.getFoldersByName(name);
  return it.hasNext() ? it.next() : parent.createFolder(name);
}

function itemsSheet_(folder) {
  var id = liveFileId_('ITEMS_SHEET_ID', folder, ITEMS_SHEET);
  if (id) {
    return SpreadsheetApp.openById(id).getSheets()[0];
  }
  var ss = SpreadsheetApp.create(ITEMS_SHEET);
  var sheet = ss.getSheets()[0];
  sheet.setName('Items');
  sheet.appendRow(ITEM_COLUMNS);
  sheet.getRange(1, 1, 1, ITEM_COLUMNS.length).setFontWeight('bold');
  sheet.setFrozenRows(1);
  DriveApp.getFileById(ss.getId()).moveTo(folder);
  PropertiesService.getScriptProperties().setProperty('ITEMS_SHEET_ID', ss.getId());
  return sheet;
}

function itemsMdId_(folder) {
  var id = liveFileId_('ITEMS_MD_ID', folder, ITEMS_MD);
  if (id) {
    return id;
  }
  var file = folder.createFile(ITEMS_MD, '# House Items\n', MimeType.PLAIN_TEXT);
  PropertiesService.getScriptProperties().setProperty('ITEMS_MD_ID', file.getId());
  return file.getId();
}

// ---------------------------------------------------------------- migration

function migrateGarageLube_() {
  var props = PropertiesService.getScriptProperties();
  if (props.getProperty('GARAGE_MIGRATED')) {
    return;
  }
  var due = props.getProperty('DUE') || '2027-03-06';
  var oldTaskId = props.getProperty('TASK_ID');
  if (oldTaskId) {
    try {
      Tasks.Tasks.remove('@default', oldTaskId);
    } catch (err) {
      console.log('old garage task already gone: ' + err);
    }
  }

  var oldTag = 'managedId:hoa-garage-door-lube';
  var cal = calendarByName_('Hoa');
  var now = new Date();
  var windows = [
    [parseYmdAt_(due, 0, 0), parseYmdAt_(due, 23, 59)],
    [new Date(now.getTime() - 14 * 86400000), new Date(now.getTime() + 21 * 86400000)],
  ];
  var doneSeries = {};
  for (var w = 0; w < windows.length; w++) {
    var events = cal.getEvents(windows[w][0], windows[w][1], {search: oldTag});
    for (var i = 0; i < events.length; i++) {
      var ev = events[i];
      if ((ev.getDescription() || '').indexOf(oldTag) === -1) {
        continue;
      }
      if (ev.isRecurringEvent()) {
        var sid = ev.getEventSeries().getId();
        if (!doneSeries[sid]) {
          doneSeries[sid] = true;
          ev.getEventSeries().deleteEventSeries();
        }
      } else {
        ev.deleteEvent();
      }
    }
  }

  Tasks.Tasks.insert(
    {
      title: 'Lube garage door & spring',
      notes: [
        'Use garage-door lubricant (not WD-40) on rollers, hinges, and springs.',
        'repeat: 6 months',
        'nag: daily',
        'calendar: Hoa',
        'time: 09:00',
        'snap: saturday',
      ].join('\n'),
      due: due + 'T00:00:00.000Z',
    },
    taskListId_(HOUSE_LIST)
  );

  ['DUE', 'TASK_ID', 'MANAGED_ID', 'TITLE', 'CALENDAR_NAME', 'TASK_LIST'].forEach(function (k) {
    props.deleteProperty(k);
  });
  props.setProperty('GARAGE_MIGRATED', new Date().toISOString());
}

// ---------------------------------------------------------------- helpers

function taskListId_(title) {
  var pageToken;
  do {
    var res = Tasks.Tasklists.list({maxResults: 100, pageToken: pageToken});
    var items = res.items || [];
    for (var i = 0; i < items.length; i++) {
      if (items[i].title === title) {
        return items[i].id;
      }
    }
    pageToken = res.nextPageToken;
  } while (pageToken);
  return Tasks.Tasklists.insert({title: title}).id;
}

function listTasks_(listId, includeCompleted) {
  var out = [];
  var pageToken;
  do {
    var res = Tasks.Tasks.list(listId, {
      maxResults: 100,
      showCompleted: includeCompleted,
      showHidden: includeCompleted,
      pageToken: pageToken,
    });
    out = out.concat(res.items || []);
    pageToken = res.nextPageToken;
  } while (pageToken);
  return out;
}

function taskDueYmd_(task) {
  return task.due ? String(task.due).slice(0, 10) : null;
}

var calendarCache_ = {};

function calendarByName_(name) {
  if (calendarCache_[name]) {
    return calendarCache_[name];
  }
  var cals = CalendarApp.getCalendarsByName(name);
  if (cals.length !== 1) {
    throw new Error('Expected exactly one calendar named ' + name + ', found ' + cals.length);
  }
  calendarCache_[name] = cals[0];
  return cals[0];
}

function loadState_() {
  var raw = PropertiesService.getScriptProperties().getProperty('STATE');
  return raw ? JSON.parse(raw) : {};
}

function saveState_(state) {
  PropertiesService.getScriptProperties().setProperty('STATE', JSON.stringify(state));
}

function addInterval_(ymd, repeat) {
  var p = ymd.split('-').map(Number);
  var d;
  if (repeat.unit === 'day' || repeat.unit === 'week') {
    var days = repeat.unit === 'week' ? repeat.n * 7 : repeat.n;
    d = new Date(p[0], p[1] - 1, p[2] + days, 12, 0, 0);
  } else {
    var months = repeat.unit === 'year' ? repeat.n * 12 : repeat.n;
    var first = new Date(p[0], p[1] - 1 + months, 1, 12, 0, 0);
    var lastDay = new Date(first.getFullYear(), first.getMonth() + 1, 0).getDate();
    d = new Date(first.getFullYear(), first.getMonth(), Math.min(p[2], lastDay), 12, 0, 0);
  }
  return ymd_(d);
}

function weekdayOnOrAfter_(ymd, dow) {
  var d = parseYmdAt_(ymd, 12, 0);
  d.setDate(d.getDate() + ((dow - d.getDay() + 7) % 7));
  return ymd_(d);
}

function parseYmdAt_(ymd, hour, minute) {
  var p = ymd.split('-').map(Number);
  return new Date(p[0], p[1] - 1, p[2], hour, minute, 0);
}

function ymd_(date) {
  return Utilities.formatDate(date, Session.getScriptTimeZone(), 'yyyy-MM-dd');
}
