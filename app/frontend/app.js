// AI-фотоинсталляция «Ремтехника» — киоск-флоу
const state = { location: null, locationTitle: null, locationOutfit: 'workwear', outfit: 'male', variants: [], chosen: null, card: null };
let stream = null, idleTimer = null, loadingElapsed = null, qrPoller = null;
let loadingMode = 'gen';          // что показывает экран загрузки: 'gen' или 'card'

// Сообщение гостю поверх экрана. Раньше ошибки показывал системный alert: на
// iPad это серое окно с кнопкой «ОК» поверх киоска, и без касания оно висит.
let noticeTimer = null;
function notify(text) {
  const el = document.getElementById('notice');
  if (!el) return;
  el.textContent = text;
  el.classList.remove('hidden');
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => el.classList.add('hidden'), 7000);
}

const $ = (s) => document.querySelector(s);
const screens = document.querySelectorAll('.screen');

// ─── Навигация ────────────────────────────────────────────────
function show(name) {
  closeContacts();                 // смена экрана закрывает окно контактов
  screens.forEach(s => s.classList.toggle('active', s.dataset.screen === name));
  updateTopbar(name);
  resetIdle();
  if (name === 'capture') { startCamera(); showCameraMode(); } else { stopCamera(); stopQrPoller(); }
  if (name === 'welcome') resetState();
  if (name === 'loading') startLoadingTimer(); else stopLoadingTimer();
}

function resetState() {
  state.location = state.chosen = state.card = state.cardFull = null; state.variants = [];
  resetEmailForm();
}

// Форма почты привязана к конкретной карточке. Раньше статус «Письмо отправлено»
// оставался на экране, когда гость переходил к другой карточке, и выглядел так,
// будто её тоже уже отправили. Сбрасываем при каждом показе новой карточки и при
// возврате на старт; адрес тоже стираем — следующая карточка может быть чужой.
function resetEmailForm() {
  const status = document.getElementById('email-status');
  const input  = document.getElementById('email-input');
  const btn    = document.getElementById('send-email');
  if (status) { status.textContent = ''; status.className = 'email-status'; }
  if (input) input.value = '';
  if (btn) btn.disabled = false;
}

// ─── Авто-сброс по бездействию ────────────────────────────────
function resetIdle() {
  clearTimeout(idleTimer);
  const cur = document.querySelector('.screen.active')?.dataset.screen;
  // Экраны с результатом по таймеру НЕ сбрасываются: гость смотрит свои кадры
  // и карточку столько, сколько нужно, и уходит с них только сам — кнопкой
  // «Готово». Раньше через 90 с киоск возвращался к началу и стирал варианты,
  // и это читалось как «фотографии пропали».
  if (['welcome', 'loading', 'done', 'variants', 'card'].includes(cur)) return;
  idleTimer = setTimeout(() => show('welcome'), 90000);
}
['click', 'touchstart'].forEach(e => document.addEventListener(e, resetIdle));

// ─── data-go навигация ────────────────────────────────────────
document.querySelectorAll('[data-go]').forEach(b =>
  b.addEventListener('click', () => show(b.dataset.go)));

// Подписи в топбаре. Меняются под конкретное мероприятие — держим их здесь,
// а не по коду. Название события стоит на ВСЕХ экранах, включая внутренние:
// гость у стенда должен видеть, чья это инсталляция, в любой момент флоу.
const TOPBAR_EVENT = 'ExpoDrev Russia 26';
const TOPBAR_CITY  = 'Красноярск';

// ─── Топбар: переключение режима ─────────────────────────────
function updateTopbar(screenName) {
  const topbar   = document.getElementById('global-topbar');
  const title    = document.getElementById('topbar-title');
  const partners = document.querySelector('.global-partners');
  // Название набирается двумя начертаниями: событие — плотным белым,
  // город — разрядкой жёлтым. textContent тут не годится, нужна разметка.
  const lockup = (main, sub) =>
    `<span class="tb-main">${main}</span>` +
    (sub ? `<span class="tb-dot"></span><span class="tb-sub">${sub}</span>` : '');
  title.innerHTML = lockup(TOPBAR_EVENT, TOPBAR_CITY);
  if (screenName === 'welcome') {
    topbar.classList.remove('inner');
    if (partners) partners.style.display = '';   // показать нижнюю полосу
  } else {
    topbar.classList.add('inner');                // тёмная полоса + логотипы справа
    if (partners) partners.style.display = 'none'; // убрать нижнюю полосу
  }
}

// ─── Таймер загрузки ──────────────────────────────────────────
// Режимы экрана загрузки. Раньше при сборке карточки крупный заголовок оставался
// «Собираем ваш кадр…», а через секунду счётчик затирал строку про карточку
// надписью «Идёт генерация». Полоса рассчитана на реальное время: два варианта
// с переносом лица по журналам занимают до 150 с, а при 90 с полоса вставала
// на 92% и последнюю минуту выглядела зависшей.
const LOADING = {
  gen:  { eyebrow: 'ИДЁТ ГЕНЕРАЦИЯ',    title: 'Собираем ваш кадр…', seconds: 150 },
  card: { eyebrow: 'СОБИРАЕМ КАРТОЧКУ', title: 'Собираем карточку…', seconds: 8 },
};

function startLoadingTimer() {
  const mode = LOADING[loadingMode] || LOADING.gen;
  let sec = 0;
  const timerEl = document.getElementById('loading-timer');
  const titleEl = document.querySelector('.loading-title');
  const fillEl  = document.getElementById('loading-bar-fill');

  if (titleEl) titleEl.textContent = mode.title;
  if (timerEl) timerEl.textContent = mode.eyebrow;
  // Сброс прогресс-бара
  if (fillEl) { fillEl.style.transition = 'none'; fillEl.style.width = '0%'; }

  // Запускаем анимацию прогресс-бара через кадр (после сброса)
  requestAnimationFrame(() => requestAnimationFrame(() => {
    if (fillEl) {
      fillEl.style.transition = `width ${mode.seconds}s linear`;
      fillEl.style.width = '92%';
    }
  }));

  stopLoadingTimer();
  loadingElapsed = setInterval(() => {
    sec++;
    if (timerEl) timerEl.textContent = `${mode.eyebrow} · ПРОШЛО ${sec} СЕК`;
  }, 1000);
}

function stopLoadingTimer() {
  if (loadingElapsed) { clearInterval(loadingElapsed); loadingElapsed = null; }
}

// ─── Локации ──────────────────────────────────────────────────
async function loadLocations() {
  const r = await fetch('/api/locations');
  const list = await r.json();
  const box = $('#locations'); box.innerHTML = '';
  list.forEach(loc => {
    const el = document.createElement('div');
    el.className = 'loc ' + (loc.enabled ? 'on' : 'off');
    el.innerHTML = `<h3>${loc.title}</h3><p class="loc-slogan">${loc.subtitle}</p>` +
      (loc.enabled ? '' : '<div class="soon">Скоро</div>');
    if (loc.enabled) el.addEventListener('click', () => {
      state.location = loc.id; state.locationTitle = loc.title;
      $('#capture-title').textContent = `${loc.title}: сделайте фото`;
      state.locationOutfit = loc.outfit || 'workwear';
      describeOutfit(state.locationOutfit);
      show('outfit');
    });
    box.appendChild(el);
  });
}

// Выбор образа
document.querySelectorAll('.outfit').forEach(o =>
  o.addEventListener('click', () => { state.outfit = o.dataset.outfit; show('capture'); }));

// ─── Камера ───────────────────────────────────────────────────
// Причина отказа камеры, словами оператора. Раньше на любой сбой висело общее
// «Камера недоступна», и было не понять: нет устройства, не дали доступ или сайт
// открыт по IP (браузер отдаёт камеру только на localhost и по HTTPS).
function cameraProblem(err) {
  if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    return 'Камера работает только на защищённом адресе — откройте сайт через https://';
  }
  switch (err && err.name) {
    case 'NotFoundError':
    case 'OverconstrainedError':
      return 'Камера не найдена — проверьте, подключена ли она';
    case 'NotAllowedError':
      return 'Браузер не дал доступ к камере — разрешите его в адресной строке';
    case 'NotReadableError':
      return 'Камеру занял другой сервис — закройте Zoom, Skype и подобные';
    default:
      return 'Камера недоступна — загрузите фото с телефона';
  }
}

async function startCamera() {
  const box = $('#cam-error');
  box.classList.add('hidden');
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: 'user', width: { ideal: 2560 }, height: { ideal: 1920 } },
      audio: false
    });
    $('#video').srcObject = stream;
  } catch (e) {
    box.textContent = cameraProblem(e);
    box.classList.remove('hidden');
    console.warn('[camera]', e && e.name, e && e.message);
  }
}
function stopCamera() {
  if (stream) { stream.getTracks().forEach(t => t.stop()); stream = null; }
}

$('#shoot').addEventListener('click', () => {
  const v = $('#video');
  if (!v.videoWidth) return;

  // Снимаем РОВНО то, что гость видел в овале. Превью показывает видео через
  // object-fit:cover в рамке 3:4, то есть от кадра 4:3 видно только центральные
  // 56% по ширине. Раньше в обработку уходил весь широкий кадр целиком: лицо
  // оказывалось в 1.8 раза мельче, чем гость видел, и в снимок попадали края,
  // где у сверхширокой фронталки iPad искажения максимальны.
  const wrap = document.querySelector('.camera-wrap').getBoundingClientRect();
  const boxAspect = wrap.width / wrap.height;
  const videoAspect = v.videoWidth / v.videoHeight;
  let sw, sh;
  if (videoAspect > boxAspect) {          // кадр шире рамки — режем по бокам
    sh = v.videoHeight;
    sw = Math.round(sh * boxAspect);
  } else {                                // кадр уже рамки — режем сверху и снизу
    sw = v.videoWidth;
    sh = Math.round(sw / boxAspect);
  }
  const sx = Math.round((v.videoWidth - sw) / 2);
  const sy = Math.round((v.videoHeight - sh) / 2);

  const c = document.createElement('canvas');
  c.width = sw; c.height = sh;
  c.getContext('2d').drawImage(v, sx, sy, sw, sh, 0, 0, sw, sh);
  c.toBlob(b => generate(b), 'image/jpeg', 0.92);
});

$('#file').addEventListener('change', e => {
  if (e.target.files[0]) generate(e.target.files[0]);
});

// ─── Генерация ────────────────────────────────────────────────
// Сервер сразу отдаёт job_id, результат забираем короткими опросами.
// Держать один запрос открытым все ~2 минуты нельзя: сеть гостя может оборвать
// его на 60-й секунде, и готовый кадр пропадал с «Failed to fetch».
async function generate(blob) {
  loadingMode = 'gen';
  show('loading');
  const fd = new FormData();
  fd.append('location', state.location);
  fd.append('outfit', state.outfit || 'male');
  fd.append('photo', blob, 'guest.jpg');
  try {
    const r = await fetch('/api/generate', { method: 'POST', body: fd });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || 'Ошибка генерации');
    const { job_id } = await r.json();
    const data = await waitForJob(job_id);
    state.variants = data.variants;
    $('#variants-loc').textContent = data.location + (data.stub_mode ? ' · демо-режим (без API-ключа)' : '');
    renderVariants();
    show('variants');
  } catch (e) {
    show('capture');
    notify('Не получилось сгенерировать. ' + e.message);
  }
}

// Опрос результата. Одиночный сбой сети не роняет сессию: пробуем дальше,
// сдаёмся только после нескольких неудач подряд или по общему лимиту времени.
async function waitForJob(jobId, { intervalMs = 2500, timeoutMs = 420000 } = {}) {
  const until = Date.now() + timeoutMs;
  let misses = 0;
  while (Date.now() < until) {
    await new Promise(r => setTimeout(r, intervalMs));
    let res;
    try {
      res = await fetch(`/api/generate-status/${jobId}`);
    } catch (_) {
      if (++misses >= 8) throw new Error('Нет связи с сервером');
      continue;
    }
    if (res.status === 404) throw new Error('Задача не найдена — переснимите');
    if (!res.ok) { if (++misses >= 8) throw new Error('Сервер не отвечает'); continue; }
    misses = 0;
    const data = await res.json();
    if (data.status === 'done') return data;
    if (data.status === 'error') throw new Error(data.detail || 'Ошибка генерации');
  }
  throw new Error('Генерация заняла слишком долго — попробуйте ещё раз');
}

function renderVariants() {
  const box = $('#variants'); box.innerHTML = '';
  state.variants.forEach(v => {
    const img = document.createElement('img');
    img.src = v.url;
    img.addEventListener('click', () => chooseVariant(v, img));
    box.appendChild(img);
  });
}

async function chooseVariant(v, imgEl) {
  document.querySelectorAll('.variants img').forEach(i => i.classList.remove('sel'));
  imgEl.classList.add('sel');
  state.chosen = v.id;
  loadingMode = 'card';
  show('loading');
  const fd = new FormData(); fd.append('variant_id', v.id);
  if (state.location) fd.append('location', state.location);
  try {
    const r = await fetch('/api/card', { method: 'POST', body: fd });
    if (!r.ok) throw new Error('card ' + r.status);
    const data = await r.json();
    state.card = data.card_id;
    state.cardFull = data.card_url;
    resetEmailForm();
    // на экран — лёгкое превью; полная карточка нужна только для печати
    $('#card-img').src = data.card_preview_url || data.card_url;
    $('#qr-img').src = data.qr_url;
    show('card');
  } catch (e) {
    show('variants');
    notify('Не получилось собрать карточку. Выберите кадр ещё раз.');
  }
}

$('#retake').addEventListener('click', () => show('capture'));

// ─── Печать ───────────────────────────────────────────────────
// Два пути. Серверный (lpr) работает, только когда приложение крутится на том же
// компьютере, к которому подключён принтер. У нас сервер в другой стране, а принтер
// будет стоять у киоска — поэтому основной путь второй: печать из самого планшета
// через системный диалог (на iPad это AirPrint по Wi-Fi).
$('#print').addEventListener('click', async () => {
  const fd = new FormData(); fd.append('card_id', state.card);
  let data = {};
  try {
    const r = await fetch('/api/print', { method: 'POST', body: fd });
    data = await r.json();
  } catch (_) { /* сервер недоступен — печатаем с планшета */ }

  // printed — напечатал сам сервер; queued — карточка встала в очередь, её
  // заберёт программа у принтера. В обоих случаях гостю делать больше нечего.
  if (data.printed || data.queued) {
    $('#done-note').textContent = 'Заберите карточку у стенда';
    finishFlow();
    return;
  }
  printFromDevice();
});

// Печать с устройства: на печать уходит только картинка карточки — интерфейс
// скрывается правилами @media print в styles.css.
function printFromDevice() {
  const img = document.getElementById('card-img');
  if (!img || !img.getAttribute('src')) { notify('Карточка ещё не готова'); return; }
  // На экране превью; на бумагу должна уйти полная карточка. Подменяем картинку
  // и печатаем, когда она догрузится.
  if (state.cardFull && !img.src.endsWith(state.cardFull)) {
    img.addEventListener('load', () => printFromDevice(), { once: true });
    img.addEventListener('error', () => notify('Не удалось загрузить карточку для печати'), { once: true });
    img.src = state.cardFull;
    return;
  }
  let done = false;
  const finish = () => {
    if (done) return;              // afterprint и таймер не должны сработать оба
    done = true;
    window.removeEventListener('afterprint', finish);
    $('#done-note').textContent = 'Заберите карточку у стенда';
    finishFlow();
  };
  window.addEventListener('afterprint', finish);
  // Подстраховка: на iOS afterprint приходит не всегда. Задание к этому моменту
  // уже отрисовано и живёт отдельно от страницы, поэтому вернуть киоск к началу
  // безопасно, даже если гость ещё выбирает принтер.
  setTimeout(finish, 10000);
  window.print();
}
// «Готово» возвращает прямо на начальный экран, без промежуточного финала:
// гость уже всё увидел и забрал. Экран «Готово!» остаётся для печати — там
// важно сообщить, что карточку нужно взять у стенда.
$('#finish').addEventListener('click', () => show('welcome'));

function finishFlow() {
  show('done');
  let n = 8; $('#cd').textContent = n;
  const t = setInterval(() => {
    n--; $('#cd').textContent = n;
    if (n <= 0) { clearInterval(t); show('welcome'); }
  }, 1000);
}

// ─── QR-загрузка фото с телефона гостя ───────────────────────
function showCameraMode() {
  document.getElementById('camera-mode').classList.remove('hidden');
  document.getElementById('qr-mode').classList.add('hidden');
}
function showQrMode() {
  document.getElementById('camera-mode').classList.add('hidden');
  document.getElementById('qr-mode').classList.remove('hidden');
}
function stopQrPoller() {
  if (qrPoller) { clearInterval(qrPoller); qrPoller = null; }
}

document.getElementById('qr-upload-btn').addEventListener('click', async () => {
  try {
    const r = await fetch('/api/upload-session', { method: 'POST' });
    const data = await r.json();
    document.getElementById('qr-upload-img').src = data.qr_url;
    document.getElementById('qr-waiting').textContent = 'Ожидаем фото…';
    showQrMode();
    stopCamera();

    qrPoller = setInterval(async () => {
      try {
        const sr = await fetch(`/api/upload-status/${data.session_id}`);
        const s = await sr.json();
        if (s.ready) {
          stopQrPoller();
          document.getElementById('qr-waiting').textContent = '✓ Фото получено! Генерируем…';
          const ir = await fetch(`/files/${s.photo_id}`);
          const blob = await ir.blob();
          generate(blob);
        }
      } catch (_) {}
    }, 2000);
  } catch (e) {
    notify('Не удалось показать QR-код. Попробуйте ещё раз.');
  }
});

document.getElementById('notice').addEventListener('click', e => e.currentTarget.classList.add('hidden'));

// ─── Контакты ─────────────────────────────────────────────────
// Окно поверх любого экрана. Закрывается крестиком, касанием мимо карточки,
// клавишей Esc и сменой экрана — в том числе сбросом киоска по бездействию.
function openContacts() {
  const m = document.getElementById('contacts');
  if (!m) return;
  m.classList.remove('hidden');
  document.getElementById('contacts-close')?.focus({ preventScroll: true });
}
function closeContacts() {
  document.getElementById('contacts')?.classList.add('hidden');
}
document.getElementById('contacts-btn').addEventListener('click', openContacts);
document.getElementById('contacts-close').addEventListener('click', closeContacts);
document.getElementById('contacts').addEventListener('click', e => {
  if (e.target === e.currentTarget) closeContacts();
});
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeContacts(); });

document.getElementById('qr-cancel').addEventListener('click', () => {
  stopQrPoller();
  showCameraMode();
  startCamera();
});

// ─── Email-отправка карточки ──────────────────────────────────
document.getElementById('send-email').addEventListener('click', async () => {
  const input  = document.getElementById('email-input');
  const status = document.getElementById('email-status');
  const btn    = document.getElementById('send-email');
  const email  = input.value.trim();

  if (!email || !email.includes('@')) {
    status.textContent = 'Введите корректный email';
    status.className = 'email-status error';
    return;
  }
  if (!state.card) {
    status.textContent = 'Карточка ещё не готова';
    status.className = 'email-status error';
    return;
  }

  btn.disabled = true;
  status.className = 'email-status';
  status.textContent = 'Отправляем…';
  // Ответ почты может прийти, когда гость уже открыл другую карточку. Тогда
  // результат относится к старой и на новый экран его писать нельзя.
  const cardAtSend = state.card;

  try {
    const fd = new FormData();
    fd.append('card_id', cardAtSend);
    fd.append('email', email);
    const r = await fetch('/api/send-email', { method: 'POST', body: fd });
    const data = await r.json();
    if (state.card !== cardAtSend) return;
    if (data.sent) {
      status.textContent = '✓ Письмо отправлено!';
      input.value = '';
    } else {
      status.textContent = data.reason || 'Не удалось отправить';
      status.className = 'email-status error';
    }
  } catch (_) {
    if (state.card !== cardAtSend) return;
    status.textContent = 'Ошибка соединения';
    status.className = 'email-status error';
  } finally {
    btn.disabled = false;
  }
});

// Описание образа зависит от площадки: на рабочих — спецовка, на Столбах
// городской образ. Текст на экране должен совпадать с тем, что реально уйдёт
// в генерацию (набор задаётся полем outfit в locations.json).
const OUTFIT_TEXT = {
  workwear: {
    desc: 'Фирменная спецовка Ремтехники и кепка',
    emoji: { male: '👷‍♂️', female: '👷‍♀️' },
  },
  casual: {
    desc: 'Джинсы и худи с лого',
    emoji: { male: '🚶‍♂️', female: '🚶‍♀️' },
  },
};
function describeOutfit(kind) {
  const t = OUTFIT_TEXT[kind] || OUTFIT_TEXT.workwear;
  Object.entries(t.emoji).forEach(([who, ch]) => {
    const el = document.getElementById('outfit-emoji-' + who);
    if (el) el.textContent = ch;
  });
  document.querySelectorAll('.outfit-desc').forEach(el => { el.textContent = t.desc; });
}

// ─── Логотипы партнёров (все три места сразу) ─────────────────
async function loadAllLogos() {
  try {
    const r = await fetch('/api/logos');
    const list = await r.json();
    ['welcome-logos', 'topbar-logos', 'loading-logos'].forEach(id => {
      const box = document.getElementById(id);
      if (!box) return;
      box.innerHTML = '';
      list.forEach(({ url }) => {
        const img = document.createElement('img');
        img.src = url; img.alt = '';
        box.appendChild(img);
      });
    });
    // Пока логотипов партнёров нет, нижняя полоса — это пустая рамка с подписью
    // «при поддержке». Прячем её целиком, иначе на экране висит пустой блок.
    const partners = document.querySelector('.global-partners');
    if (partners) partners.classList.toggle('empty', list.length === 0);
  } catch (_) {}
}

// ─── Init ─────────────────────────────────────────────────────
loadLocations();
loadAllLogos();
