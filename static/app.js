const tg = window.Telegram?.WebApp;
if (tg) {
    for (const [method, args] of [
        ['ready', []],
        ['expand', []],
        ['setHeaderColor', ['#f5f1e8']],
        ['setBackgroundColor', ['#f5f1e8']]
    ]) {
        try { tg[method]?.(...args); } catch (_) {}
    }
}

const INIT_DATA_STORAGE_KEY = 'fyvessa.telegramInitData';
const PROFILE_DRAFT_KEY = 'fyvessa.profileDraft';
const CHECKOUT_DRAFT_KEY = 'fyvessa.checkoutDraft';
const THEME_KEY = 'fyvessa.theme';
const fragmentRequests = new WeakMap();
const cartUpdates = new Map();
let navigationSequence = 0;
if ('scrollRestoration' in history) history.scrollRestoration = 'manual';

function applyTheme(theme) {
    const nextTheme = theme === 'dark' ? 'dark' : 'light';
    document.documentElement.dataset.theme = nextTheme;
    const color = nextTheme === 'dark' ? '#121210' : '#f5f1e8';
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', color);
    document.querySelectorAll('[data-theme-toggle]').forEach((button) => {
        button.textContent = nextTheme === 'dark' ? '☀' : '◐';
        button.setAttribute('aria-label', nextTheme === 'dark' ? 'Включить светлую тему' : 'Включить тёмную тему');
    });
    try {
        tg?.setHeaderColor?.(color);
        tg?.setBackgroundColor?.(color);
    } catch (_) {}
}

function toggleTheme() {
    const theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    try { localStorage.setItem(THEME_KEY, theme); } catch (_) {}
    applyTheme(theme);
}

function initAutoSliders(root = document) {
    if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    root.querySelectorAll('[data-auto-slider]').forEach((slider) => {
        if (slider.__fyvessaSliderTimer) return;
        const advance = () => {
            if (!slider.isConnected) {
                clearInterval(slider.__fyvessaSliderTimer);
                return;
            }
            const firstCard = slider.firstElementChild;
            if (!firstCard || slider.scrollWidth <= slider.clientWidth) return;
            const step = firstCard.getBoundingClientRect().width + 12;
            const atEnd = slider.scrollLeft + slider.clientWidth >= slider.scrollWidth - step / 2;
            slider.scrollTo({left: atEnd ? 0 : slider.scrollLeft + step, behavior: 'smooth'});
        };
        const resume = () => {
            clearInterval(slider.__fyvessaSliderTimer);
            slider.__fyvessaSliderTimer = setInterval(advance, 3800);
        };
        slider.addEventListener('pointerdown', () => clearInterval(slider.__fyvessaSliderTimer));
        slider.addEventListener('pointerup', resume);
        slider.addEventListener('pointercancel', resume);
        resume();
    });

    root.querySelectorAll('[data-auto-feed]').forEach((feed) => {
        if (feed.__fyvessaFeedTimer) return;
        const advance = () => {
            if (!feed.isConnected) {
                clearInterval(feed.__fyvessaFeedTimer);
                return;
            }
            if (feed.scrollHeight <= feed.clientHeight) return;
            const firstCard = feed.firstElementChild;
            const gap = parseFloat(getComputedStyle(feed).rowGap) || 32;
            const step = (firstCard?.getBoundingClientRect().height || 260) + gap;
            const atEnd = feed.scrollTop + feed.clientHeight >= feed.scrollHeight - step / 2;
            if (atEnd) {
                feed.style.scrollBehavior = 'auto';
                feed.scrollTop = 0;
                requestAnimationFrame(() => { feed.style.scrollBehavior = ''; });
            } else {
                feed.scrollTo({top: feed.scrollTop + step, behavior: 'smooth'});
            }
        };
        const resume = () => {
            clearInterval(feed.__fyvessaFeedTimer);
            feed.__fyvessaFeedTimer = setInterval(advance, 3800);
        };
        feed.addEventListener('pointerdown', () => clearInterval(feed.__fyvessaFeedTimer));
        feed.addEventListener('pointerup', resume);
        feed.addEventListener('pointercancel', resume);
        resume();
    });
}

function initDataFromLocation() {
    for (const source of [location.hash.slice(1), location.search.slice(1)]) {
        if (!source) continue;
        const value = new URLSearchParams(source).get('tgWebAppData');
        if (value) return value;
    }
    return '';
}

function telegramAuthorization() {
    let initData = tg?.initData || initDataFromLocation();
    try {
        if (initData) sessionStorage.setItem(INIT_DATA_STORAGE_KEY, initData);
        else initData = sessionStorage.getItem(INIT_DATA_STORAGE_KEY) || '';
    } catch (_) {}
    if (!initData) throw new Error('Откройте магазин кнопкой бота внутри Telegram');
    return initData;
}

function telegramHeaders() {
    return {'Authorization': telegramAuthorization()};
}

function showToast(message) {
    const toast = document.getElementById('toast');
    if (!toast) return;
    toast.textContent = message;
    toast.classList.add('is-visible');
    clearTimeout(window.__fyvessaToastTimer);
    window.__fyvessaToastTimer = setTimeout(
        () => toast.classList.remove('is-visible'),
        2800
    );
}

function errorMessage(payload) {
    if (typeof payload?.detail === 'string') return payload.detail;
    if (Array.isArray(payload?.detail)) {
        return payload.detail
            .map(item => item.msg?.replace(/^Value error, /, '') || 'Проверьте данные')
            .join('. ');
    }
    return 'Не удалось выполнить действие';
}

async function fetchWithTimeout(url, options = {}, timeoutMs = 12000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
        return await fetch(url, {...options, signal: controller.signal});
    } catch (error) {
        if (error.name === 'AbortError') {
            throw new Error('Сервер не ответил вовремя. Попробуйте ещё раз');
        }
        throw error;
    } finally {
        clearTimeout(timer);
    }
}

async function api(url, options = {}) {
    const response = await fetchWithTimeout(url, {
        ...options,
        headers: {
            'Content-Type': 'application/json',
            ...telegramHeaders(),
            ...(options.headers || {})
        }
    });
    if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(errorMessage(payload));
    }
    return response.json();
}

function showFragmentLoading(container) {
    container.innerHTML = `
        <div class="fragment-skeleton space-y-3" aria-label="Загрузка">
            <div class="h-28 rounded-[1.5rem] bg-white/70"></div>
            <div class="h-24 rounded-[1.5rem] bg-white/50"></div>
        </div>`;
}

function showFragmentError(container, error) {
    container.innerHTML = `
        <div class="rounded-[2rem] border border-coral/30 bg-white p-8 text-center shadow-soft">
            <h2 class="mt-3 text-xl font-black">Не удалось загрузить раздел</h2>
            <p data-fragment-error class="mt-2 text-black/55"></p>
            <button type="button" data-fragment-retry class="mt-5 rounded-full bg-ink px-5 py-3 font-bold text-white">Повторить</button>
        </div>`;

    container.querySelector('[data-fragment-error]').textContent = error.message;
    container.querySelector('[data-fragment-retry]').addEventListener(
        'click',
        () => loadProtectedFragment(container),
        {once: true}
    );
}

function captureFragmentState(container) {
    const controls = new Map();
    const occurrences = new Map();
    let focusKey = null;

    container.querySelectorAll('input[name], select[name], textarea[name]').forEach((control) => {
        const occurrence = occurrences.get(control.name) || 0;
        occurrences.set(control.name, occurrence + 1);
        const key = `${control.name}:${occurrence}`;
        controls.set(key, {
            value: control.value,
            checked: control.checked,
            selectionStart: control.selectionStart,
            selectionEnd: control.selectionEnd
        });
        if (control === document.activeElement) focusKey = key;
    });

    return {controls, focusKey};
}

function restoreFragmentState(container, snapshot) {
    const occurrences = new Map();
    container.querySelectorAll('input[name], select[name], textarea[name]').forEach((control) => {
        const occurrence = occurrences.get(control.name) || 0;
        occurrences.set(control.name, occurrence + 1);
        const key = `${control.name}:${occurrence}`;
        const saved = snapshot.controls.get(key);
        if (!saved) return;

        if (control.type === 'checkbox' || control.type === 'radio') {
            control.checked = saved.checked;
        } else {
            control.value = saved.value;
        }

        if (key === snapshot.focusKey) {
            control.focus({preventScroll: true});
            if (saved.selectionStart !== null && control.setSelectionRange) {
                control.setSelectionRange(saved.selectionStart, saved.selectionEnd);
            }
        }
    });
}

function replaceFragment(container, html) {
    if (container.__fyvessaFragmentHTML === html) return false;

    const snapshot = captureFragmentState(container);
    const oldHeight = container.offsetHeight;
    const scrollY = window.scrollY;
    if (oldHeight) container.style.minHeight = `${oldHeight}px`;
    container.innerHTML = html;
    container.__fyvessaFragmentHTML = html;
    container.dataset.fragmentReady = 'true';
    restoreFragmentState(container, snapshot);
    container.animate?.(
        [{opacity: .5, transform: 'translateY(3px)'}, {opacity: 1, transform: 'none'}],
        {duration: 220, easing: 'cubic-bezier(.22,1,.36,1)'}
    );
    requestAnimationFrame(() => requestAnimationFrame(() => {
        container.style.minHeight = '';
        window.scrollTo({top: scrollY, behavior: 'instant'});
    }));
    return true;
}

async function loadProtectedFragment(container, {quiet = false} = {}) {
    if (!container?.isConnected) return;

    const requestId = Symbol('fragment');
    const hasContent = container.dataset.fragmentReady === 'true';
    fragmentRequests.set(container, requestId);
    if (hasContent) container.classList.add('is-refreshing');
    else showFragmentLoading(container);

    try {
        const response = await fetchWithTimeout(container.dataset.authFragment, {
            headers: telegramHeaders()
        });
        if (!response.ok) {
            const payload = await response.json().catch(() => ({}));
            throw new Error(errorMessage(payload));
        }
        const html = await response.text();
        if (fragmentRequests.get(container) !== requestId || !container.isConnected) return;
        replaceFragment(container, html);
    } catch (error) {
        if (fragmentRequests.get(container) !== requestId) return;
        if (!hasContent) showFragmentError(container, error);
        else if (!quiet) showToast(error.message);
    } finally {
        if (fragmentRequests.get(container) === requestId) {
            container.classList.remove('is-refreshing');
            if (container.dataset.authFragment === '/api/cart') scheduleCartPolling(container);
        }
    }
}

async function loadProtectedFragments(root = document) {
    const containers = [...root.querySelectorAll('[data-auth-fragment]')];
    await Promise.all(containers.map(container => loadProtectedFragment(container)));
}

function applyShopState(state) {
    const favoriteIds = new Set(state.favorite_product_ids || []);
    document.querySelectorAll('[data-favorite-product-id]').forEach((button) => {
        const isFavorite = favoriteIds.has(Number(button.dataset.favoriteProductId));
        button.textContent = isFavorite ? '♥' : '♡';
        button.setAttribute(
            'aria-label',
            isFavorite ? 'Удалить из избранного' : 'Добавить в избранное'
        );
    });
    document.querySelectorAll('[data-cart-count]').forEach((badge) => {
        const count = Number(state.cart_quantity || 0);
        badge.textContent = count;
        badge.classList.toggle('hidden', count === 0);
    });
}

function adjustCartBadge(delta) {
    document.querySelectorAll('[data-cart-count]').forEach((badge) => {
        const count = Math.max(0, Number(badge.textContent || 0) + delta);
        badge.textContent = count;
        badge.classList.toggle('hidden', count === 0);
    });
}

async function refreshShopState() {
    try { telegramAuthorization(); } catch (_) { return; }
    try { applyShopState(await api('/api/shop-state')); } catch (_) {}
}

async function refreshCart(options = {}) {
    const container = document.querySelector('[data-auth-fragment="/api/cart"]');
    if (container) await loadProtectedFragment(container, options);
}

function scheduleCartPolling(container) {
    clearTimeout(window.__fyvessaCartPollTimer);
    if (!container.querySelector('[data-cart-pending="true"]')) return;
    window.__fyvessaCartPollTimer = setTimeout(async () => {
        if (container.isConnected) await loadProtectedFragment(container, {quiet: true});
    }, 5000);
}

function animateFavorite(button, isFavorite) {
    document.querySelectorAll(`[data-favorite-product-id="${button.dataset.favoriteProductId}"]`)
        .forEach((item) => {
            item.textContent = isFavorite ? '♥' : '♡';
            item.classList.remove('is-active');
            requestAnimationFrame(() => item.classList.toggle('is-active', isFavorite));
        });
}

async function toggleFavorite(productId, button) {
    button.disabled = true;
    try {
        const result = await api(`/api/favorites/${productId}`, {method: 'POST'});
        animateFavorite(button, result.favorite);
        showToast(result.favorite ? 'Добавлено в избранное' : 'Удалено из избранного');
        tg?.HapticFeedback?.impactOccurred('light');

        const favorites = document.querySelector('[data-auth-fragment="/api/favorites"]');
        if (favorites && !result.favorite) {
            const card = button.closest('[data-product-card-id]');
            card?.classList.add('is-removing');
            await new Promise(resolve => setTimeout(resolve, 190));
            card?.remove();
            favorites.__fyvessaFragmentHTML = null;
            if (!favorites.querySelector('[data-product-card-id]')) {
                await loadProtectedFragment(favorites, {quiet: true});
            }
        }
        await refreshShopState();
    } catch (error) {
        showToast(error.message);
    } finally {
        if (button.isConnected) button.disabled = false;
    }
}

async function addToCart(productId, quantity = 1, button = null) {
    const originalText = button?.textContent;
    if (button) {
        button.disabled = true;
        button.textContent = 'Добавляем…';
    }
    try {
        const result = await api(`/api/cart/${productId}`, {
            method: 'POST', body: JSON.stringify({quantity})
        });
        showToast(`В корзине: ${result.quantity} шт.`);
        tg?.HapticFeedback?.notificationOccurred('success');
        await refreshShopState();
        if (button) button.textContent = 'Добавлено ✓';
    } catch (error) {
        showToast(error.message);
    } finally {
        if (button) setTimeout(() => {
            if (!button.isConnected) return;
            button.disabled = false;
            button.textContent = originalText;
        }, 450);
    }
}

function formatRubles(value) {
    return `${Math.round(value).toLocaleString('ru-RU')} ₽`;
}

function updateCartTotals() {
    let total = 0;
    document.querySelectorAll('[data-cart-item]').forEach((item) => {
        const quantity = Number(item.querySelector('[data-cart-quantity]')?.value || 0);
        const lineTotal = Number(item.dataset.unitPrice || 0) * quantity;
        total += lineTotal;
        const line = item.querySelector('[data-cart-line-total]');
        if (line) line.textContent = formatRubles(lineTotal);
    });
    const totalElement = document.querySelector('[data-cart-total]');
    if (totalElement) totalElement.textContent = formatRubles(total);
}

function changeCartQuantity(productId, delta, button) {
    const item = button.closest('[data-cart-item]');
    const input = item.querySelector('[data-cart-quantity]');
    const previous = Number(input.value);
    const quantity = Math.max(1, Math.min(999, previous + delta));
    if (quantity === previous) return;

    input.value = quantity;
    updateCartTotals();
    adjustCartBadge(quantity - previous);

    const current = cartUpdates.get(productId);
    if (current?.timer) clearTimeout(current.timer);
    const version = (current?.version || 0) + 1;
    const timer = setTimeout(
        () => persistCartQuantity(productId, quantity, version),
        240
    );
    cartUpdates.set(productId, {timer, version});
}

async function persistCartQuantity(productId, quantity, version) {
    try {
        await api(`/api/cart/${productId}`, {
            method: 'PUT', body: JSON.stringify({quantity})
        });
        if (cartUpdates.get(productId)?.version !== version) return;
        cartUpdates.delete(productId);
        await Promise.all([refreshCart({quiet: true}), refreshShopState()]);
    } catch (error) {
        if (cartUpdates.get(productId)?.version !== version) return;
        cartUpdates.delete(productId);
        showToast(error.message);
        await Promise.all([refreshCart({quiet: true}), refreshShopState()]);
    }
}

async function removeFromCart(productId, button) {
    const item = button.closest('[data-cart-item]');
    const quantity = Number(item?.querySelector('[data-cart-quantity]')?.value || 0);
    button.disabled = true;
    try {
        await api(`/api/cart/${productId}`, {method: 'DELETE'});
        item?.classList.add('is-removing');
        adjustCartBadge(-quantity);
        await new Promise(resolve => setTimeout(resolve, 190));
        await Promise.all([refreshCart({quiet: true}), refreshShopState()]);
        showToast('Товар удалён');
    } catch (error) {
        button.disabled = false;
        showToast(error.message);
    }
}

async function recordProductView(productId) {
    try { telegramAuthorization(); } catch (_) { return; }
    try { await api(`/api/products/${productId}/view`, {method: 'POST'}); } catch (_) {}
}

async function sendAvailabilityRequest(productId, quantity, button) {
    const originalText = button?.textContent;
    if (button) {
        button.disabled = true;
        button.textContent = 'Отправляем…';
    }
    try {
        await api(`/api/availability/${productId}`, {
            method: 'POST', body: JSON.stringify({quantity})
        });
        showToast('Запрос отправлен администратору');
        if (button) button.textContent = 'Запрос отправлен ✓';
        tg?.HapticFeedback?.notificationOccurred('success');
        return true;
    } catch (error) {
        showToast(error.message);
        if (button) {
            button.disabled = false;
            button.textContent = originalText;
        }
        return false;
    }
}

async function requestAvailability(productId, form) {
    const quantity = Number(new FormData(form).get('quantity'));
    await sendAvailabilityRequest(productId, quantity, form.querySelector('button'));
}

async function requestCartAvailability(productId, quantity, button) {
    if (await sendAvailabilityRequest(productId, quantity, button)) {
        await refreshCart({quiet: true});
    }
}

async function checkout(form) {
    const data = Object.fromEntries(new FormData(form).entries());
    data.coins_requested = data.coins_requested || '0';
    const button = form.querySelector('[type="submit"]');
    const originalText = button.textContent;
    button.disabled = true;
    button.textContent = 'Создаём заказ…';
    try {
        const result = await api('/api/orders', {
            method: 'POST', body: JSON.stringify(data)
        });
        try { sessionStorage.removeItem(CHECKOUT_DRAFT_KEY); } catch (_) {}
        await navigateTo(`/orders/${result.order_id}`);
    } catch (error) {
        showToast(error.message);
        button.disabled = false;
        button.textContent = originalText;
    }
}

async function reportPayment(orderId, button) {
    button.disabled = true;
    try {
        await api(`/api/orders/${orderId}/report-payment`, {method: 'POST'});
        const container = document.querySelector(
            `[data-auth-fragment="/api/orders/${orderId}"]`
        );
        if (container) await loadProtectedFragment(container, {quiet: true});
        showToast('Передано администратору на проверку');
    } catch (error) {
        button.disabled = false;
        showToast(error.message);
    }
}

function markProfileDirty(form) {
    const status = form.querySelector('[data-profile-status]');
    if (!status) return;
    status.textContent = 'Есть изменения';
    status.classList.add('is-dirty');
    status.classList.remove('is-saved');
}

function saveFormDraft(form, key) {
    try {
        sessionStorage.setItem(key, JSON.stringify(Object.fromEntries(new FormData(form))));
    } catch (_) {}
}

function restoreFormDraft(form, key) {
    if (!form) return false;
    try {
        const values = JSON.parse(sessionStorage.getItem(key) || 'null');
        if (!values) return false;
        for (const [name, value] of Object.entries(values)) {
            const control = [...form.elements].find(item => item.name === name);
            if (control) control.value = value;
        }
        return true;
    } catch (_) {
        return false;
    }
}

async function saveProfile(form) {
    const button = form.querySelector('[data-profile-save]');
    const status = form.querySelector('[data-profile-status]');
    const data = Object.fromEntries(new FormData(form).entries());
    const originalText = button.textContent;
    button.disabled = true;
    button.textContent = 'Сохраняем…';
    if (status) status.textContent = 'Сохранение…';

    try {
        await api('/api/profile', {method: 'POST', body: JSON.stringify(data)});
        try { sessionStorage.removeItem(PROFILE_DRAFT_KEY); } catch (_) {}
        form.querySelectorAll('input').forEach(input => { input.defaultValue = input.value; });
        const name = `${data.first_name} ${data.last_name}`.trim();
        const initials = `${data.first_name?.[0] || ''}${data.last_name?.[0] || ''}`.toUpperCase();
        const nameElement = document.querySelector('[data-profile-name]');
        const initialsElement = document.querySelector('[data-profile-initials]');
        const completion = document.querySelector('[data-profile-completion]');
        if (nameElement) nameElement.textContent = name;
        if (initialsElement) initialsElement.textContent = initials || 'F';
        if (completion) completion.textContent = '✓ Профиль готов';
        if (status) {
            status.textContent = 'Сохранено ✓';
            status.classList.remove('is-dirty');
            status.classList.add('is-saved');
        }
        button.textContent = 'Готово ✓';
        showToast('Профиль сохранён');
        tg?.HapticFeedback?.notificationOccurred('success');
    } catch (error) {
        if (status) status.textContent = 'Не сохранено';
        showToast(error.message);
    } finally {
        setTimeout(() => {
            if (!button.isConnected) return;
            button.disabled = false;
            button.textContent = originalText;
        }, 500);
    }
}

function updateActiveNavigation() {
    const path = location.pathname;
    document.querySelectorAll('[data-main-nav] a').forEach((link) => {
        const linkPath = new URL(link.href).pathname;
        const active = path === linkPath
            || (linkPath === '/catalog' && path.startsWith('/products/'))
            || (linkPath === '/profile' && path.startsWith('/orders'));
        if (active) link.setAttribute('aria-current', 'page');
        else link.removeAttribute('aria-current');
    });
    try {
        if (path === '/') tg?.BackButton?.hide();
        else tg?.BackButton?.show();
    } catch (_) {}
}

async function hydratePage(root = document) {
    clearTimeout(window.__fyvessaCartPollTimer);
    root.querySelector('main')?.classList.add('page-enter');
    updateActiveNavigation();
    await loadProtectedFragments(root);
    const profileForm = root.querySelector('#profile-form');
    if (restoreFormDraft(profileForm, PROFILE_DRAFT_KEY)) markProfileDirty(profileForm);
    restoreFormDraft(root.querySelector('#checkout-form'), CHECKOUT_DRAFT_KEY);
    initAutoSliders(root);
    await refreshShopState();
    const product = root.querySelector('[data-record-product-view]');
    if (product) recordProductView(Number(product.dataset.recordProductView));
}

async function navigateTo(target, {push = true, scrollY = 0} = {}) {
    const url = new URL(target, location.href);
    if (url.origin !== location.origin) {
        location.assign(url.href);
        return;
    }
    if (url.href === location.href && push) return;

    const sequence = ++navigationSequence;
    if (push) {
        history.replaceState({...history.state, scrollY: window.scrollY}, '');
    }
    document.body.classList.add('is-navigating');
    try {
        const response = await fetchWithTimeout(url, {
            headers: {'X-Fyvessa-Navigation': '1'}
        });
        if (!response.ok) throw new Error(`Navigation failed: ${response.status}`);
        const page = new DOMParser().parseFromString(await response.text(), 'text/html');
        const nextMain = page.querySelector('main');
        if (!nextMain || sequence !== navigationSequence) return;

        const swap = () => {
            document.querySelector('main').replaceWith(nextMain);
            document.title = page.title;
            if (push) history.pushState({
                scrollY: 0,
                navigationDepth: Number(history.state?.navigationDepth || 0) + 1
            }, '', response.url || url.href);
        };
        if (document.startViewTransition) {
            await document.startViewTransition(swap).updateCallbackDone;
        } else {
            swap();
        }
        document.body.classList.remove('is-navigating');
        await hydratePage(document);
        window.scrollTo({top: scrollY, behavior: 'instant'});
    } catch (_) {
        if (sequence !== navigationSequence) return;
        location.assign(url.href);
    } finally {
        if (sequence === navigationSequence) document.body.classList.remove('is-navigating');
    }
}

document.addEventListener('click', (event) => {
    if (event.target.closest?.('[data-theme-toggle]')) {
        toggleTheme();
        return;
    }
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest?.('a[href]');
    if (!link || link.dataset.noSoftNav !== undefined || link.target || link.download) return;
    const url = new URL(link.href, location.href);
    if (url.origin !== location.origin || url.hash && url.pathname === location.pathname) return;
    event.preventDefault();
    navigateTo(url.href);
});

function rememberFormState(event) {
    const form = event.target.closest('#profile-form');
    if (form) {
        markProfileDirty(form);
        saveFormDraft(form, PROFILE_DRAFT_KEY);
    }
    const checkoutForm = event.target.closest('#checkout-form');
    if (checkoutForm) saveFormDraft(checkoutForm, CHECKOUT_DRAFT_KEY);
}

document.addEventListener('input', rememberFormState);
document.addEventListener('change', rememberFormState);

document.addEventListener('submit', async (event) => {
    const form = event.target;
    if (form.id === 'profile-form') {
        event.preventDefault();
        await saveProfile(form);
        return;
    }
    if (form.id === 'checkout-form') {
        event.preventDefault();
        await checkout(form);
        return;
    }
    if (form.matches('[data-availability-form]')) {
        event.preventDefault();
        await requestAvailability(form.dataset.productId, form);
        return;
    }
    if ((form.method || 'get').toLowerCase() === 'get') {
        const url = new URL(form.action || location.href, location.href);
        url.search = new URLSearchParams(new FormData(form)).toString();
        event.preventDefault();
        await navigateTo(url.href);
    }
});

let scrollStateTimer;
window.addEventListener('scroll', () => {
    clearTimeout(scrollStateTimer);
    scrollStateTimer = setTimeout(() => {
        history.replaceState({...history.state, scrollY: window.scrollY}, '');
    }, 120);
}, {passive: true});
window.addEventListener('popstate', (event) => navigateTo(
    location.href,
    {push: false, scrollY: event.state?.scrollY || 0}
));
try {
    tg?.BackButton?.onClick(() => {
        if (Number(history.state?.navigationDepth || 0) > 0) history.back();
        else navigateTo('/');
    });
} catch (_) {}
history.replaceState({
    ...history.state,
    scrollY: window.scrollY,
    navigationDepth: Number(history.state?.navigationDepth || 0)
}, '');
applyTheme(document.documentElement.dataset.theme);
hydratePage();
