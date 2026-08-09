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

function initDataFromLocation() {
    for (const source of [window.location.hash.slice(1), window.location.search.slice(1)]) {
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
    const initData = telegramAuthorization();
    return {
        'Authorization': initData,
        'X-Telegram-Init-Data': initData
    };
}

function showToast(message) {
    const toast = document.getElementById('toast');
    toast.textContent = message;
    toast.classList.remove('hidden');
    clearTimeout(window.__fyvessaToastTimer);
    window.__fyvessaToastTimer = setTimeout(() => toast.classList.add('hidden'), 2800);
}

function errorMessage(payload) {
    if (typeof payload?.detail === 'string') return payload.detail;
    if (Array.isArray(payload?.detail)) {
        return payload.detail.map(item => item.msg?.replace(/^Value error, /, '') || 'Проверьте данные').join('. ');
    }
    return 'Не удалось выполнить действие';
}

async function api(url, options = {}) {
    const response = await fetch(url, {
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

async function loadProtectedFragment(container) {
    try {
        const response = await fetch(container.dataset.authFragment, {
            headers: telegramHeaders()
        });
        if (!response.ok) {
            const payload = await response.json().catch(() => ({}));
            throw new Error(errorMessage(payload));
        }
        container.innerHTML = await response.text();
        if (container.dataset.authFragment === '/api/cart') scheduleCartPolling(container);
    } catch (error) {
        container.innerHTML = `<div class="rounded-[2rem] border border-dashed border-black/20 p-10 text-center"><p class="text-3xl">↗</p><h2 class="mt-3 text-xl font-black">Нужен Telegram</h2><p class="mt-2 text-black/50">${error.message}</p></div>`;
    }
}

async function loadProtectedFragments() {
    await Promise.all(
        [...document.querySelectorAll('[data-auth-fragment]')].map(loadProtectedFragment)
    );
}

function applyShopState(state) {
    const favoriteIds = new Set(state.favorite_product_ids || []);
    document.querySelectorAll('[data-favorite-product-id]').forEach((button) => {
        const isFavorite = favoriteIds.has(Number(button.dataset.favoriteProductId));
        button.textContent = isFavorite ? '♥' : '♡';
        button.setAttribute('aria-label', isFavorite ? 'Удалить из избранного' : 'Добавить в избранное');
    });
    document.querySelectorAll('[data-cart-count]').forEach((badge) => {
        const count = Number(state.cart_quantity || 0);
        badge.textContent = count;
        badge.classList.toggle('hidden', count === 0);
    });
}

async function refreshShopState() {
    try { telegramAuthorization(); } catch (_) { return; }
    try { applyShopState(await api('/api/shop-state')); } catch (_) {}
}

async function refreshCart() {
    const container = document.querySelector('[data-auth-fragment="/api/cart"]');
    if (container) await loadProtectedFragment(container);
}

function scheduleCartPolling(container) {
    clearTimeout(window.__fyvessaCartPollTimer);
    if (!container.querySelector('[data-cart-pending="true"]')) return;
    window.__fyvessaCartPollTimer = setTimeout(async () => {
        if (document.body.contains(container)) await loadProtectedFragment(container);
    }, 5000);
}

async function toggleFavorite(productId, button) {
    try {
        const result = await api(`/api/favorites/${productId}`, {method: 'POST'});
        document.querySelectorAll(`[data-favorite-product-id="${productId}"]`).forEach((item) => {
            item.textContent = result.favorite ? '♥' : '♡';
        });
        showToast(result.favorite ? 'Добавлено в избранное' : 'Удалено из избранного');
        tg?.HapticFeedback?.impactOccurred('light');
        const favorites = document.querySelector('[data-auth-fragment="/api/favorites"]');
        if (favorites && !result.favorite) {
            await loadProtectedFragment(favorites);
            await refreshShopState();
        }
    } catch (error) { showToast(error.message); }
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
    } catch (error) { showToast(error.message); }
    finally {
        if (button) {
            button.disabled = false;
            button.textContent = originalText;
        }
    }
}

async function changeCartQuantity(productId, delta, button) {
    const input = button.closest('[data-cart-item]').querySelector('[data-cart-quantity]');
    const quantity = Math.max(1, Math.min(999, Number(input.value) + delta));
    try {
        await api(`/api/cart/${productId}`, {
            method: 'PUT', body: JSON.stringify({quantity})
        });
        await refreshCart();
        await refreshShopState();
    } catch (error) { showToast(error.message); }
}

async function removeFromCart(productId) {
    try {
        await api(`/api/cart/${productId}`, {method: 'DELETE'});
        await refreshCart();
        await refreshShopState();
        showToast('Товар удалён');
    } catch (error) { showToast(error.message); }
}

async function recordProductView(productId) {
    try { telegramAuthorization(); } catch (_) { return; }
    try {
        await api(`/api/products/${productId}/view`, {method: 'POST'});
    } catch (_) {}
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
    if (await sendAvailabilityRequest(productId, quantity, button)) await refreshCart();
}

async function checkout(form) {
    const data = Object.fromEntries(new FormData(form).entries());
    data.coins_requested = data.coins_requested || '0';
    const button = form.querySelector('[type="submit"]');
    button.disabled = true;
    button.textContent = 'Создаём заказ…';
    try {
        const result = await api('/api/orders', {
            method: 'POST', body: JSON.stringify(data)
        });
        window.location.href = `/orders/${result.order_id}`;
    } catch (error) {
        showToast(error.message);
        if (error.message.startsWith('Заполните')) {
            setTimeout(() => { window.location.href = '/profile'; }, 1200);
        }
        button.disabled = false;
        button.textContent = 'Оформить заказ';
    }
}

async function reportPayment(orderId, button) {
    button.disabled = true;
    try {
        await api(`/api/orders/${orderId}/report-payment`, {method: 'POST'});
        const container = document.querySelector(`[data-auth-fragment="/api/orders/${orderId}"]`);
        if (container) await loadProtectedFragment(container);
        showToast('Передано администратору на проверку');
    } catch (error) {
        button.disabled = false;
        showToast(error.message);
    }
}

document.addEventListener('submit', async (event) => {
    if (event.target.id === 'profile-form') {
        event.preventDefault();
        const data = Object.fromEntries(new FormData(event.target).entries());
        try {
            await api('/api/profile', {method: 'POST', body: JSON.stringify(data)});
            showToast('Профиль сохранён');
            await loadProtectedFragment(event.target.closest('[data-auth-fragment]'));
        } catch (error) { showToast(error.message); }
    }
    if (event.target.id === 'checkout-form') {
        event.preventDefault();
        await checkout(event.target);
    }
    if (event.target.matches('[data-availability-form]')) {
        event.preventDefault();
        await requestAvailability(event.target.dataset.productId, event.target);
    }
});

loadProtectedFragments().then(refreshShopState);
